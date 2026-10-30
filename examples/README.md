# 示例

按开发阶段递进，用于理解系统演进路径。**正式运行请用项目根目录的 `make dev`。**

| 文件 | 阶段 | 说明 |
|---|---|---|
| `01_rag_cli_prototype.py` | 阶段 1 | 最小 RAG 原型：单脚本、无 Web。读 Markdown → 500 字切片 → FAISS → BM25+向量混合检索 Top3 → LLM 回答。**不依赖 app/ 包**，可单独运行。 |

## 运行阶段 1 原型

```bash
pip install faiss-cpu rank-bm25 jieba openai numpy
export LLM_API_KEY=sk-xxx
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_MODEL=deepseek-chat

python examples/01_rag_cli_prototype.py
```

> 首次运行会调用 Embedding 构建索引（几秒），之后走本地缓存。
> 索引缓存在 `data/index/`，与主项目共用同一目录，但文件互相独立。
