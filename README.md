# 门诊预约与分诊协作系统（Clinic Triage Agent）

企业员工门诊预约与分诊协作平台：**对话式制度问答 + 自主决策智能体 + 预约/报销工具链 + 管理后台**。

> 员工在 App/网页里问「我发烧38.9度该挂什么科」「门诊费能报多少」，
> 助手自主决定检索制度、查号源、校验报销资格，写操作前强制中断等员工确认。

---

## 核心能力

| 能力 | 说明 |
|---|---|
| 制度问答 RAG | BM25 + 向量混合检索（Top3），回答必须引用《文件名》条款，查不到就说“制度中未找到” |
| 自主决策 Agent | 模型决策 → 执行工具 → 结果回填 → 再决策，**最多 5 轮** |
| 8 个 Function Calling 工具 | 制度检索、号源查询、预约创建、报销校验、报销建单等（查询/校验/写入三组） |
| 顺序约束三重保险 | 提示词铁律 + 校验令牌（一次性、HMAC 签名、参数绑定）+ 服务层二次校验 |
| 写操作中断确认 | 建报销单前挂起，SSE 推 `confirm` 事件，员工确认后才落库 |
| 管理后台 | JWT 鉴权、知识库上传（md/pdf）、提示词热更新、对话审计与违规标记 |
| 可观测 | 每轮决策落 `agent_steps` 表（工具名/参数/耗时/结果摘要），供分析 |

---

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置模型供应商（任何兼容 OpenAI 协议的服务）
export LLM_API_KEY=sk-xxxx
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_MODEL=deepseek-chat
export EMBED_MODEL=BAAI/bge-m3

# 3. 启动（首次启动自动建表、导入种子知识库）
uvicorn app.main:app --reload --port 8000
```

- 员工端：<http://localhost:8000>
- 管理后台：<http://localhost:8000/admin/login>

**演示账号**：`admin / admin123`（管理员）、`employee / emp123`（员工，用于验证越权拦截）

---

## 项目结构

```
clinic-triage-agent/
├── app/
│   ├── main.py                     # FastAPI 入口：/chat、/chat/confirm、/health
│   ├── core/
│   │   └── config.py               # 全局配置（路径/模型/密钥/限流/报销规则）
│   ├── api/
│   │   ├── employee/               # 员工端接口（问答入口在 main.py，此处预留）
│   │   └── admin/
│   │       └── routes.py           # 管理后台：鉴权 + 知识库 + 提示词 + 审计
│   ├── agents/
│   │   ├── harness.py              # ★ 执行循环：5 轮上限 / 日志 / 中断确认 / 恢复
│   │   └── prompts.py              # ★ 系统提示词（铁律约束）与状态文案
│   ├── rag/
│   │   └── __init__.py             # ★ 文档解析(md/pdf)、切片、混合检索、索引构建
│   ├── services/
│   │   └── tools.py                # ★ 8 个工具实现 + Function Calling Schema
│   ├── repositories/
│   │   ├── admin_db.py             # 账号/提示词/文档元数据/审计数据访问
│   │   └── agent_trace.py          # agent_steps 轨迹表 + pending_actions 挂起表
│   └── models/                     # 数据模型（当前用 sqlite3 直连，预留 ORM 层）
├── static/
│   └── index.html                  # 员工端聊天界面（原生 JS + SSE 解析 + 打字机）
├── templates/                      # 管理后台 SSR 页面（Jinja2）
│   ├── base.html  login.html  dashboard.html
│   └── kb.html    prompts.html  audit.html
├── data/
│   ├── kb_files/                   # 制度文档（md/pdf 原件）
│   └── index/                      # FAISS 索引与切片缓存（运行时生成，不入库）
├── tests/
│   ├── eval_cases.json             # 30 条评测用例
│   └── test_tools_constraints.py   # 工具顺序约束/令牌防重放/并发不超约
├── docs/
│   ├── 需求清单.md  ·  架构与部署拓扑.html
│   ├── AGENT_轨迹示例.md
│   ├── 上线检查清单.md
│   └── 提示词模板.md
├── requirements.txt
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 关键设计

### 1. 为什么“必须校验才能建单”不是只靠提示词

| 层次 | 手段 | 效果 |
|---|---|---|
| 提示词 | 7 条铁律，管理员改的提示词若缺「严禁跳步」会自动补齐 | 模型倾向遵守 |
| 令牌 | `check_reimbursement_eligibility` 通过才签发 `eligibility_token`（HMAC 签名、5 分钟、一次性、绑定金额/类型） | **没校验过根本拿不到令牌** |
| 服务 | `create_*` 内部再校验 + 验签 | 模型伪造参数也会被拒 |

### 2. 中断确认（Human-in-the-loop）

```
模型想建单 → harness 识别 CONFIRM_REQUIRED → 工具不执行
          → 参数补全后落 pending_actions（10 分钟 TTL）
          → SSE 推 confirm 事件 → 前端渲染草稿卡片
          → 用户点确认 → POST /chat/confirm → resume() 真正执行
```

### 3. 模型可替换

`app/core/config.py` 里 `LLM_*` / `EMBED_*` 全部走环境变量，切换供应商只改配置不改代码。

---

## 评测与上线

```bash
# 工具约束回归测试（不依赖 LLM）
pytest tests/ -v
```

- `tests/eval_cases.json`：30 条评测用例（制度 10 条含 3 条陷阱题、工具 10 条、违规意图 5 条、多轮上下文 5 条）
- `docs/上线检查清单.md`：5 维度 × 5 项，含负责人、验证方法、通过标准与 2 条阻断性红线

---

## 技术栈

FastAPI 3.11 · SQLite（原型，生产建议 PostgreSQL + pgvector）· FAISS · BM25(rank_bm25) · jieba · Jinja2 · OpenAI 兼容 API · 原生 JS
