# -*- coding: utf-8 -*-
"""
app/core/config.py —— 全局配置集中入口
=====================================
所有路径、模型供应商、密钥、限流参数都从这里取。
支持环境变量覆盖，避免同一个配置散落在多个文件里。

用法：
    from app.core.config import settings
    settings.INDEX_DIR
"""

import os
import pathlib

# 项目根目录（app 的上一级），所有相对路径都基于它解析
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent

# ---------------- 目录与文件 ----------------
DATA_DIR = BASE_DIR / "data"
KB_UPLOAD_DIR = DATA_DIR / "kb_files"        # 上传的原始制度文档
INDEX_DIR = DATA_DIR / "index"               # FAISS 索引与切片缓存
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
DOC_PATH = KB_UPLOAD_DIR / "就诊须知.md"      # 内置种子文档
DB_PATH = BASE_DIR / "clinic.db"

# ---------------- 检索参数 ----------------
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
TOP_K = 3
VECTOR_WEIGHT = 0.6

# ---------------- 智能体 ----------------
MAX_ROUNDS = int(os.getenv("AGENT_MAX_ROUNDS", "5"))       # 最多 5 轮决策
MAX_WRITE_PER_RUN = 1                                       # 单次请求最多 1 个写操作
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "true").lower() == "true"  # 降级开关
CONFIRM_TTL = 600                                           # 确认令牌有效期（秒）

# ---------------- LLM 供应商（可替换） ----------------
LLM_API_KEY = os.getenv("LLM_API_KEY", "EMPTY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
EMBED_API_KEY = os.getenv("EMBED_API_KEY", LLM_API_KEY)
EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", LLM_BASE_URL)
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")

# ---------------- 安全 ----------------
JWT_SECRET = os.getenv("JWT_SECRET", "clinic-admin-dev-secret-change-me")
JWT_ALG = "HS256"
JWT_EXPIRE_HOURS = 2
TOKEN_SECRET = os.getenv("ELIGIBILITY_SECRET", "clinic-eligibility-secret")
COOKIE_NAME = "clinic_token"

# ---------------- 限流（阶段 9 上线要求） ----------------
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))   # 单用户每分钟提问上限
GLOBAL_QPS = int(os.getenv("GLOBAL_QPS", "20"))                   # 全局 QPS 上限

# ---------------- 业务规则（简化版，生产应进配置表） ----------------
REIMB_POLICY = {
    "门诊": {"rate": 0.8, "deductible": 50, "annual_cap": 5000},
    "急诊": {"rate": 0.85, "deductible": 50, "annual_cap": 5000},
    "住院": {"rate": 0.9, "deductible": 0, "annual_cap": 50000},
    "体检": {"rate": 0.5, "deductible": 0, "annual_cap": 1000},
}
COMPANY_TITLE = os.getenv("COMPANY_TITLE", "XX科技有限公司")
ANNUAL_LIMIT_TOTAL = 8000


def ensure_dirs():
    """启动时确保运行所需目录存在。"""
    for d in (DATA_DIR, KB_UPLOAD_DIR, INDEX_DIR):
        pathlib.Path(d).mkdir(parents=True, exist_ok=True)
