# -*- coding: utf-8 -*-
"""
rag.py —— 检索与 LLM 模块（阶段 2 版本，阶段 3 扩展多文档与动态提示词）
======================================================================
对应最终架构中的 app/rag/*（检索）与 app/agents/llm_provider.py（LLM 抽象）。
main.py / admin.py 只调用对外接口，不关心内部实现。

阶段 3 变更：
  - 支持多份知识库文档（md + pdf），每篇文档独立切片，统一进一个索引
  - 系统提示词从数据库实时读取（管理员改完立即生效），不再写死在代码里
  - 提供 rebuild() 供后台上传/删除文档后重建索引
"""

import os
import re
import json
import pathlib
import threading

import numpy as np
import jieba
import faiss
from rank_bm25 import BM25Okapi
from openai import OpenAI

# ---------------- 配置 ----------------
CHUNK_SIZE = 500        # 每片约 500 字
CHUNK_OVERLAP = 50      # 相邻切片重叠，防止关键句被切断
TOP_K = 3               # 混合检索取 top3
VECTOR_WEIGHT = 0.6     # 混合得分权重：向量 0.6 + BM25 0.4
from app.core.config import (INDEX_DIR, KB_UPLOAD_DIR as UPLOAD_DIR,
                             DOC_PATH as _DOC_PATH)
DOC_PATH = str(_DOC_PATH)        # 内置种子文档（首次启动自动入库）


class RagError(Exception):
    """检索层业务异常，路由层捕获后转成统一错误码。"""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------- LLM 与 Embedding 抽象（供应商可配置） ----------------
def _llm_client() -> OpenAI:
    """任何兼容 OpenAI 协议的供应商都可通过环境变量切换。"""
    return OpenAI(
        api_key=os.getenv("LLM_API_KEY", "EMPTY"),
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    )


def _embed_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("EMBED_API_KEY", os.getenv("LLM_API_KEY", "EMPTY")),
        base_url=os.getenv("EMBED_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")),
    )


def chat_stream(messages: list, temperature: float = 0.2):
    """流式对话：逐段 yield 文本增量，供 SSE 的 delta 事件使用。"""
    stream = _llm_client().chat.completions.create(
        model=os.getenv("LLM_MODEL", "deepseek-chat"),
        messages=messages, temperature=temperature, stream=True)
    for chunk in stream:
        piece = chunk.choices[0].delta.content or ""
        if piece:
            yield piece


def embed(texts: list) -> np.ndarray:
    """向量化：返回 (n, dim) float32，已 L2 归一化（内积=余弦）。"""
    # 分批调用，避免单次请求过大被服务端拒绝
    out = []
    for i in range(0, len(texts), 32):
        resp = _embed_client().embeddings.create(
            model=os.getenv("EMBED_MODEL", "BAAI/bge-m3"), input=texts[i:i + 32])
        out.extend([d.embedding for d in resp.data])
    vecs = np.array(out, dtype="float32")
    faiss.normalize_L2(vecs)
    return vecs


# ---------------- 文档解析（md / pdf） ----------------
def load_markdown(path: str) -> str:
    """轻度清洗：去图片链接、链接只保留文字。"""
    text = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return text


def load_pdf(path: str) -> str:
    """PDF 提取：优先 pdfplumber（保留表格），缺失则降级 pypdf。"""
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                # 表格内容按行拼接，避免报销标准表格丢失
                for table in page.extract_tables() or []:
                    for row in table:
                        cells = [str(x) for x in row if x]
                        if cells:
                            parts.append(" | ".join(cells))
                if t:
                    parts.append(t)
        return "\n".join(parts)
    except ImportError:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)


def load_document(path: str) -> str:
    """按扩展名分发解析器。"""
    if path.lower().endswith(".pdf"):
        return load_pdf(path)
    return load_markdown(path)


def split_chunks(text: str) -> list:
    """滑动窗口切片（500 字，重叠 50 字）。"""
    step = CHUNK_SIZE - CHUNK_OVERLAP
    chunks, n = [], len(text)
    for i in range(0, n, step):
        piece = text[i:i + CHUNK_SIZE].strip()
        if len(piece) > 30:                 # 过滤过短碎片
            chunks.append(piece)
        if i + CHUNK_SIZE >= n:
            break
    return chunks


# ---------------- 混合检索器 ----------------
class HybridRetriever:
    """BM25 管专有名词精确命中，向量管口语化语义匹配，加权融合。"""

    def __init__(self, chunks: list, vecs: np.ndarray, meta: list = None):
        self.chunks = chunks
        self.vecs = vecs
        self.meta = meta or [{"doc": DOC_PATH} for _ in chunks]   # 每片来源文档名
        self.bm25 = BM25Okapi([list(jieba.cut(c)) for c in chunks])
        self.index = faiss.IndexFlatIP(vecs.shape[1])
        self.index.add(vecs)

    @staticmethod
    def _minmax(x: np.ndarray) -> np.ndarray:
        rng = x.max() - x.min()
        return (x - x.min()) / rng if rng > 0 else np.zeros_like(x)

    def search(self, query: str, top_k: int = TOP_K) -> list:
        """返回 top_k 个 (chunk, 得分, 来源文档名)。"""
        bm_norm = self._minmax(np.array(self.bm25.get_scores(list(jieba.cut(query)))))
        qv = embed([query])
        vec_norm = (self.vecs @ qv[0]).astype("float32")
        hybrid = VECTOR_WEIGHT * vec_norm + (1 - VECTOR_WEIGHT) * bm_norm
        order = np.argsort(-hybrid)[:top_k]
        return [(self.chunks[i], float(hybrid[i]), self.meta[i]["doc"]) for i in order]


# ---------------- 索引构建 / 加载（全局单例 + 线程锁） ----------------
_retriever: HybridRetriever | None = None
_build_lock = threading.Lock()


def _collect_sources() -> list:
    """汇总所有启用文档：优先取数据库记录，失败则退化为内置单文档。"""
    try:
        from app.repositories import admin_db
        docs = [(d["raw_path"], d["title"]) for d in admin_db.list_documents()
                if d.get("raw_path") and pathlib.Path(d["raw_path"]).exists()]
        if docs:
            return docs
    except Exception:
        pass
    if pathlib.Path(DOC_PATH).exists():
        return [(DOC_PATH, DOC_PATH)]
    return []


def _build() -> HybridRetriever:
    """全量构建：解析所有文档 → 切片 → 向量化 → 写缓存。"""
    sources = _collect_sources()
    if not sources:
        raise RagError("INDEX_NOT_READY", "知识库暂无可用文档，请管理员先上传制度文件")

    chunks, metas = [], []
    for path, title in sources:
        try:
            text = load_document(path)
        except Exception as e:
            raise RagError("UPLOAD_PARSE_FAILED", f"文档《{title}》解析失败：{e}")
        for piece in split_chunks(text):
            chunks.append(piece)
            metas.append({"doc": title})
    if not chunks:
        raise RagError("INDEX_NOT_READY", "文档内容为空，请检查文件内容")

    vecs = embed(chunks)
    pathlib.Path(INDEX_DIR).mkdir(exist_ok=True)
    (pathlib.Path(INDEX_DIR) / "chunks.json").write_text(
        json.dumps({"chunks": chunks, "meta": metas}, ensure_ascii=False), "utf-8")
    np.save(pathlib.Path(INDEX_DIR) / "vecs.npy", vecs)
    return HybridRetriever(chunks, vecs, metas)


def get_retriever() -> HybridRetriever:
    """懒加载：优先读缓存，无缓存则构建。"""
    global _retriever
    if _retriever is not None:
        return _retriever
    with _build_lock:
        if _retriever is not None:
            return _retriever
        chunks_file = pathlib.Path(INDEX_DIR) / "chunks.json"
        vecs_file = pathlib.Path(INDEX_DIR) / "vecs.npy"
        try:
            if chunks_file.exists() and vecs_file.exists():
                data = json.loads(chunks_file.read_text("utf-8"))
                _retriever = HybridRetriever(data["chunks"], np.load(vecs_file), data["meta"])
            else:
                _retriever = _build()
        except RagError:
            raise
        except Exception:
            raise RagError("INDEX_NOT_READY", "知识库初始化中，请稍后再试")
    return _retriever


def rebuild() -> int:
    """强制重建索引（上传/删除文档后调用），返回切片总数。"""
    global _retriever
    with _build_lock:
        _retriever = _build()
        return len(_retriever.chunks)


# ---------------- 提示词（从数据库实时读取） ----------------
FALLBACK_PROMPT = (
    "你是门诊预约与分诊协作系统的制度手册助手。只根据提供的制度条文回答，"
    "不确定时明确说“制度中未找到”，回答末尾附引用来源，不得编造就诊规则与报销标准。"
)


def get_system_prompt() -> str:
    """每次问答都读库 → 管理员修改后立即生效，无需重启服务。"""
    try:
        from app.repositories import admin_db
        return admin_db.get_active_prompt() or FALLBACK_PROMPT
    except Exception:
        return FALLBACK_PROMPT


def build_messages(history: list, context: str, question: str) -> list:
    """系统提示（动态） + 最近 6 轮历史 + 检索片段 + 当前问题。"""
    msgs = [{"role": "system", "content": get_system_prompt()}]
    msgs += [{"role": r, "content": c} for r, c in history]
    msgs.append({"role": "user",
                 "content": f"【参考资料】\n{context}\n\n【员工提问】\n{question}"})
    return msgs
