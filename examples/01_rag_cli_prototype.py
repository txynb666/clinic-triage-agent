# -*- coding: utf-8 -*-
"""
门诊预约与分诊协作 —— 阶段 1 裸问答原型（单脚本、无 Web）
==========================================================
流程：
  1. 读取本地「门诊就诊须知/分诊规则」Markdown 文件
  2. 按 500 字切片，向量化后存入 FAISS（本地持久化）
  3. 命令行输入问题 → BM25 + 向量混合检索 top3 → 拼入提示词 → 调 LLM 回答

供应商可配置：通过环境变量切换（OpenAI / DeepSeek / 通义 / 月之暗面 等兼容
OpenAI 协议的服务均可）。示例：
    export LLM_API_KEY=sk-xxx
    export LLM_BASE_URL=https://api.deepseek.com/v1   # 可换任意兼容地址
    export LLM_MODEL=deepseek-chat
    export EMBED_BASE_URL=...                          # Embedding 服务地址（缺省同 LLM）

运行（在项目根目录执行）：
    python examples/01_rag_cli_prototype.py                # 首次自动构建索引
    python examples/01_rag_cli_prototype.py --rebuild      # 强制重建索引
    python examples/01_rag_cli_prototype.py --doc data/kb_files/就诊须知.md

说明：本文件是阶段 1 的最小原型，独立于 app/ 包，用于理解 RAG 链路。
      正式功能请使用 `make dev` 启动完整服务。

依赖安装：
    pip install faiss-cpu jieba rank-bm25 openai numpy
"""

import os
import re
import sys
import json
import argparse
import pathlib

import numpy as np
import jieba
import faiss
from rank_bm25 import BM25Okapi
from openai import OpenAI

# ============================================================
# 一、全局配置（后续阶段复用：搬入 app/core/config.py）
# ============================================================
CHUNK_SIZE = 500          # 每片约 500 字（按需求约束）
CHUNK_OVERLAP = 50        # 相邻切片重叠 50 字，防止关键句被切断
TOP_K = 3                 # 混合检索后取 top3
VECTOR_WEIGHT = 0.6       # 混合得分权重：向量 0.6 + BM25 0.4（可调）
INDEX_DIR = "data/index"    # FAISS 索引与切片缓存目录
DOC_PATH = "data/kb_files/就诊须知.md"    # 默认制度文档路径


def get_llm_client() -> OpenAI:
    """【核心复用模块 ①】LLM 调用封装 —— 供应商可配置。

    后续阶段对应 app/agents/llm_provider.py。
    任何兼容 OpenAI 协议的供应商（OpenAI/DeepSeek/通义/Kimi/本地 vLLM）
    只需改 3 个环境变量即可切换，业务代码零改动。
    """
    return OpenAI(
        api_key=os.getenv("LLM_API_KEY", "EMPTY"),
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    )


def chat(messages: list, temperature: float = 0.2) -> str:
    """统一对话入口。temperature 取低值，制度问答要严谨、少发挥。"""
    client = get_llm_client()
    resp = client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "deepseek-chat"),
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content


def embed(texts: list) -> np.ndarray:
    """【核心复用模块 ②】向量化封装 —— Embedding 供应商同样可配置。

    后续阶段对应 app/rag/embedding.py。
    返回 shape=(n, dim) 的 float32 数组，已做 L2 归一化，
    配合 FAISS 的 IP（内积）索引等价于余弦相似度。
    """
    client = OpenAI(
        api_key=os.getenv("EMBED_API_KEY", os.getenv("LLM_API_KEY", "EMPTY")),
        base_url=os.getenv("EMBED_BASE_URL", os.getenv("LLM_BASE_URL",
                 "https://api.deepseek.com/v1")),
    )
    resp = client.embeddings.create(
        model=os.getenv("EMBED_MODEL", "BAAI/bge-m3"),
        input=texts,
    )
    vecs = np.array([d.embedding for d in resp.data], dtype="float32")
    # L2 归一化：内积 = 余弦相似度
    faiss.normalize_L2(vecs)
    return vecs


# ============================================================
# 二、文档加载与切片（后续阶段复用：搬入 app/rag/splitter.py）
# ============================================================
def load_markdown(path: str) -> str:
    """读取 Markdown 并做轻度清洗：去掉图片标记、代码块保留但去符号。"""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)        # 去图片链接
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接只留文字
    return text


def split_chunks(text: str) -> list:
    """按 CHUNK_SIZE 滑动窗口切片。

    简化实现：纯按字符数切；后续阶段可升级为
    「优先按 Markdown 标题/段落边界切」语义切片，提升召回质量。
    """
    step = CHUNK_SIZE - CHUNK_OVERLAP
    chunks = []
    for i in range(0, len(text), step):
        piece = text[i:i + CHUNK_SIZE].strip()
        if len(piece) > 30:          # 过滤过短的碎片（如纯标题残留）
            chunks.append(piece)
        if i + CHUNK_SIZE >= len(text):
            break
    return chunks


# ============================================================
# 三、混合检索器（后续阶段复用：搬入 app/rag/retriever.py）
# ============================================================
class HybridRetriever:
    """BM25（关键词精确匹配）+ FAISS（语义向量）混合检索器。

    为什么两者都要？
      - BM25 强项：专有名词精确命中，如「三甲医院」「发票抬头」
      - 向量强项：口语化问法语义匹配，如「看病能报多少」↔「门诊报销比例」
    混合得分 = VECTOR_WEIGHT * 归一化向量分 + (1-VECTOR_WEIGHT) * 归一化 BM25 分
    """

    def __init__(self, chunks: list):
        self.chunks = chunks
        # --- BM25 侧：中文先分词 ---
        tokenized = [list(jieba.cut(c)) for c in chunks]
        self.bm25 = BM25Okapi(tokenized)
        # --- 向量侧：构建 FAISS 内积索引（内积+归一化 = 余弦） ---
        self.vecs = embed(chunks)
        self.index = faiss.IndexFlatIP(self.vecs.shape[1])
        self.index.add(self.vecs)

    def search(self, query: str, top_k: int = TOP_K) -> list:
        """返回 top_k 个 (chunk, 混合得分)，按分降序。"""
        # 1) BM25 得分并做 min-max 归一化
        bm_scores = np.array(self.bm25.get_scores(list(jieba.cut(query))))
        bm_norm = self._minmax(bm_scores)
        # 2) 向量得分（内积，已归一化）
        qv = embed([query])
        _, idx = self.index.search(qv, len(self.chunks))
        vec_norm = np.zeros(len(self.chunks))
        vec_norm[idx[0]] = self.index.reconstruct(0).size and self._vec_scores(qv)
        # 3) 加权融合
        hybrid = VECTOR_WEIGHT * vec_norm + (1 - VECTOR_WEIGHT) * bm_norm
        order = np.argsort(-hybrid)[:top_k]
        return [(self.chunks[i], float(hybrid[i])) for i in order]

    @staticmethod
    def _minmax(x: np.ndarray) -> np.ndarray:
        rng = x.max() - x.min()
        return (x - x.min()) / rng if rng > 0 else np.zeros_like(x)

    def _vec_scores(self, qv: np.ndarray) -> np.ndarray:
        """向量与全部切片的余弦相似度（内积）。"""
        return (self.vecs @ qv[0]).astype("float32")


# ============================================================
# 四、提示词模板（后续阶段复用：搬入 app/agents/prompts.py）
# ============================================================
SYSTEM_PROMPT = """你是企业门诊预约与分诊协作系统的智能助手。请严格遵守：
1. 只依据【参考资料】回答，资料中没有的信息明确说"制度文档中未提及"，禁止编造；
2. 涉及分诊推荐时，先说"以下仅供参考，最终以分诊台医生判断为准"；
3. 回答简洁、分点，引用资料时注明来源片段编号。
"""

USER_PROMPT_TEMPLATE = """【参考资料】
{context}

【员工提问】
{question}

请基于参考资料回答。"""


# ============================================================
# 五、索引持久化（避免每次启动重复调用 Embedding API 花钱）
# ============================================================
def build_or_load_index(doc_path: str, rebuild: bool = False):
    """构建或加载索引。首次调用 Embedding API 后缓存到本地磁盘。"""
    chunks_file = os.path.join(INDEX_DIR, "chunks.json")
    index_file = os.path.join(INDEX_DIR, "faiss.index")

    if not rebuild and os.path.exists(index_file) and os.path.exists(chunks_file):
        print("[加载] 检测到本地索引缓存，直接加载（--rebuild 可强制重建）")
        chunks = json.loads(pathlib.Path(chunks_file).read_text("utf-8"))
        retriever = HybridRetriever.__new__(HybridRetriever)  # 跳过重算向量
        retriever.chunks = chunks
        retriever.bm25 = BM25Okapi([list(jieba.cut(c)) for c in chunks])
        retriever.index = faiss.read_index(index_file)
        retriever.vecs = None  # 惰性：检索时用 index.reconstruct
        retriever._vec_scores = lambda qv: np.array(
            [faiss.reconstruct(retriever.index, i) @ qv[0]
             for i in range(retriever.index.ntotal)], dtype="float32")
        return retriever

    print(f"[构建] 读取文档 {doc_path} 并切片向量化……")
    text = load_markdown(doc_path)
    chunks = split_chunks(text)
    print(f"[构建] 共切出 {len(chunks)} 片，开始向量化……")
    os.makedirs(INDEX_DIR, exist_ok=True)
    retriever = HybridRetriever(chunks)
    faiss.write_index(retriever.index, index_file)
    pathlib.Path(chunks_file).write_text(json.dumps(chunks, ensure_ascii=False), "utf-8")
    print(f"[构建] 索引已缓存至 {INDEX_DIR}/")
    return retriever


# ============================================================
# 六、命令行交互主循环（后续阶段对应 api/employee/qa.py）
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="门诊制度问答原型")
    parser.add_argument("--doc", default=DOC_PATH, help="制度 Markdown 路径")
    parser.add_argument("--rebuild", action="store_true", help="强制重建索引")
    args = parser.parse_args()

    if not os.path.exists(args.doc):
        sys.exit(f"未找到制度文档：{args.doc}，请先准备一份就诊须知 Markdown。")

    retriever = build_or_load_index(args.doc, args.rebuild)
    print("\n=== 门诊预约与分诊助手（原型 v0.1，输入 q 退出）===\n")

    while True:
        question = input("员工> ").strip()
        if not question:
            continue
        if question.lower() in ("q", "quit", "exit"):
            print("再见！")
            break

        # 1) 混合检索 top3
        hits = retriever.search(question)
        context = "\n\n".join(
            f"[片段{i+1}]（相关度 {s:.2f}）\n{c}" for i, (c, s) in enumerate(hits)
        )
        print(f"\n[检索] 命中 top{len(hits)}，最高相关度 {hits[0][1]:.2f}\n")

        # 2) 拼提示词 → 调 LLM
        answer = chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                context=context, question=question)},
        ])
        print(f"助手> {answer}\n" + "-" * 50 + "\n")


if __name__ == "__main__":
    main()
