# 门诊预约与分诊协作系统（Clinic Triage Agent）

企业员工门诊预约与分诊协作平台：**对话式制度问答 + 自主决策智能体 + 预约/报销工具链 + 管理后台**。

员工在 App/网页里问「我发烧 38.9 度该挂什么科」「门诊费能报多少」，助手会自主决定检索制度、查号源、校验报销资格；**任何写操作前强制中断，等员工确认后才落库**。

<p>
<img alt="Python" src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white">
<img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white">
<img alt="License" src="https://img.shields.io/badge/License-MIT-green">
<img alt="Tests" src="https://img.shields.io/badge/Tests-9%20passed-brightgreen">
</p>

---

## 目录

- [核心能力](#核心能力)
- [快速开始](#快速开始)
- [演示账号](#演示账号)
- [项目结构](#项目结构)
- [关键设计](#关键设计)
- [开发命令](#开发命令)
- [测试与评测](#测试与评测)
- [部署](#部署)
- [文档索引](#文档索引)
- [技术栈](#技术栈)

---

## 核心能力

| 能力 | 说明 |
|---|---|
| **制度问答 RAG** | BM25 + 向量混合检索（Top3），回答必须引用《文件名》条款；查不到就答“制度中未找到”，禁止编造 |
| **自主决策 Agent** | 模型决策 → 执行工具 → 结果回填 → 再决策，**最多 5 轮**，超限自动中止 |
| **8 个 Function Calling 工具** | 制度检索、号源查询、预约创建、报销校验、报销建单等，分查询/校验/写入三组 |
| **顺序约束三重保险** | 提示词铁律 + 校验令牌（一次性/HMAC 签名/参数绑定）+ 服务层二次校验 |
| **写操作中断确认** | 建报销单前挂起并推 `confirm` 事件，员工确认后才落库；10 分钟超时自动失效 |
| **管理后台** | JWT 鉴权、知识库上传（md/pdf）、系统提示词热更新、对话审计与违规标记 |
| **可观测** | 每轮决策落 `agent_steps` 表（工具名/参数/耗时/结果摘要），支持效率与卡点分析 |
| **降级与兜底** | LLM 不可用时给出友好中文提示，核心预约链路不受影响 |

---

## 快速开始

### 方式一：本地运行（推荐开发使用）

```bash
# 一键初始化：虚拟环境 + 依赖 + .env 模板
bash scripts/init.sh

# 填入你的模型 API Key
vim .env        # LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

# 启动
source .venv/bin/activate
make dev
```

访问：

- 员工端 <http://localhost:8000>
- 管理后台 <http://localhost:8000/admin/login>

### 方式二：Docker

```bash
cp .env.example .env && vim .env    # 填入 LLM_API_KEY
docker compose up -d
```

### 手动安装

```bash
pip install -r requirements.txt
export LLM_API_KEY=sk-xxxx
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_MODEL=deepseek-chat
uvicorn app.main:app --reload --port 8000
```

> 首次启动会自动建表、把 `data/kb_files/就诊须知.md` 登记进知识库。
> 第一次提问会调用 Embedding 构建索引（约几秒），之后走 `data/index/` 缓存。

### 模型供应商可替换

任何兼容 OpenAI 协议的服务都可用，只改环境变量、不动代码：

| 供应商 | `LLM_BASE_URL` 示例 | `LLM_MODEL` 示例 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 阿里通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 月之暗面 | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 vLLM | `http://127.0.0.1:8000/v1` | 按部署模型填写 |

---

## 演示账号

| 账号 | 密码 | 角色 | 用途 |
|---|---|---|---|
| `admin` | `admin123` | 管理员 | 完整后台功能 |
| `employee` | `emp123` | 员工 | 验证越权拦截（调管理接口返回 `code=1003`） |

> 生产环境务必修改初始密码，并更换 `.env` 中的 `JWT_SECRET` 与 `ELIGIBILITY_SECRET`。

---

## 项目结构

```
clinic-triage-agent/
├── app/                                  # 应用主包
│   ├── main.py                           # FastAPI 入口：/chat、/chat/confirm、/health、静态页
│   ├── core/
│   │   └── config.py                     # ★ 全局配置：路径/模型/密钥/限流/报销规则
│   ├── api/
│   │   ├── admin/
│   │   │   └── routes.py                 # ★ 管理后台：JWT 鉴权 + 知识库 + 提示词 + 审计
│   │   └── employee/                     # 员工端接口（问答入口在 main.py，此处预留扩展）
│   ├── agents/
│   │   ├── harness.py                    # ★ 执行循环：5 轮上限 / 日志 / 中断确认 / 恢复
│   │   └── prompts.py                    # ★ 系统提示词（铁律）与状态文案
│   ├── rag/
│   │   └── __init__.py                   # ★ 文档解析(md/pdf)、切片、混合检索、索引构建
│   ├── services/
│   │   └── tools.py                      # ★ 8 个工具实现 + Function Calling Schema
│   ├── repositories/
│   │   ├── schema.py                     # ★ 统一建表入口 schema.init_all()
│   │   ├── admin_db.py                   # 账号/提示词/文档元数据/审计数据访问
│   │   └── agent_trace.py                # agent_steps 轨迹表 + pending_actions 挂起表
│   └── models/                           # 数据模型（当前 sqlite3 直连，预留 ORM 层）
├── static/
│   └── index.html                        # 员工端聊天界面（原生 JS + SSE 解析 + 打字机）
├── templates/                            # 管理后台 SSR 页面（Jinja2）
│   ├── base.html                         # 布局与公共样式
│   ├── login.html                        # 登录页
│   ├── dashboard.html                    # 概览看板
│   ├── kb.html                           # 制度知识库
│   ├── prompts.html                      # 提示词模板
│   └── audit.html                        # 对话审计
├── data/
│   ├── kb_files/就诊须知.md               # 制度文档（可上传更多 md/pdf）
│   └── index/                            # FAISS 索引与切片缓存（运行时生成，不入库）
├── tests/
│   ├── test_tools_constraints.py         # 9 项工具约束回归测试
│   └── eval_cases.json                   # 30 条评测用例
├── examples/
│   ├── README.md
│   └── 01_rag_cli_prototype.py           # 阶段 1 最小 RAG 原型（独立可运行）
├── docs/
│   ├── 需求清单.md                        # 一页纸需求与 10 项架构决策
│   ├── 架构与部署拓扑.html                 # 可视化架构图（浏览器打开）
│   ├── API.md                            # 完整接口与错误码文档
│   ├── AGENT_轨迹示例.md                  # 3 个典型对话轨迹 + 分析 SQL
│   └── 上线检查清单.md                    # 5 维度 × 5 项，含阻断红线
├── scripts/
│   └── init.sh                           # 一键初始化脚本
├── .github/workflows/ci.yml              # CI：测试 + 评测用例校验 + 镜像构建
├── Makefile                              # 常用命令（make help 查看）
├── Dockerfile  ·  docker-compose.yml
├── requirements.txt  ·  .env.example  ·  .gitignore
├── CONTRIBUTING.md  ·  LICENSE  ·  README.md
```

---

## 关键设计

### 1. 为什么“必须校验才能建单”不是只靠提示词

| 层次 | 手段 | 效果 |
|---|---|---|
| **提示词** | 7 条铁律；管理员改的提示词若缺「严禁跳步」会自动补齐 | 模型倾向遵守 |
| **令牌** | `check_reimbursement_eligibility` 通过才签发 `eligibility_token`：HMAC 签名、5 分钟有效、一次性消费、绑定金额/类型/发票类型 | **没校验过根本拿不到令牌** |
| **服务层** | `create_*` 内部再跑一次校验并验签，参数不一致直接拒绝 | 模型伪造参数也拦得住 |

### 2. 中断确认（Human-in-the-loop）

```
模型想建单
  → harness 识别 CONFIRM_REQUIRED，工具不执行
  → 参数补全（绑定令牌 + 幂等键）后写入 pending_actions（10 分钟 TTL）
  → SSE 推 confirm 事件 → 前端渲染草稿确认卡片
  → 员工点「确认」→ POST /chat/confirm → resume() 真正执行并流式收尾
  → 点「取消」→ 标记 rejected，永不落库
```

### 3. 工具调用顺序

```
制度问答：  search_policy → 作答
预约流程：  list_available_slots → check_slot_availability → create_appointment
报销流程：  check_reimbursement_eligibility → [人工确认] → create_reimbursement_draft
```

单次请求最多 5 轮决策、1 个写操作；超限返回 `4005` / `4006`。

### 4. 可观测与可分析

每次执行生成 `run_id`，每轮写一条 `agent_steps`（决策/工具/中断/最终/异常），
附带工具名、参数 JSON、耗时毫秒、结果摘要。`docs/AGENT_轨迹示例.md` 提供 4 条现成分析 SQL。

---

## 开发命令

```bash
make help          # 查看所有命令
make install       # 安装依赖
make dev           # 热重载启动
make run           # 生产模式启动
make test          # 跑约束回归测试
make lint          # 语法检查
make clean         # 清理运行数据（会删库与索引，慎用）
make docker-build  # 构建镜像
make docker-up     # 启动容器
```

---

## 测试与评测

### 约束回归测试（不依赖真实 LLM）

```bash
make test
# 9 passed
```

覆盖：无令牌建单必拒、校验不通过不签发令牌、令牌重放拒绝、篡改金额拒绝、
起付线判断、AI 建预约强制 `pending_triage`、幂等键防重单、号源约满保护。

### 30 条评测用例

`tests/eval_cases.json`，字段：`id / category / question / expected_tools / expected_behavior / pass_criteria`

| 类别 | 条数 | 重点 |
|---|---|---|
| 制度类 | 10（含 **3 条陷阱题**） | 陷阱题期望答“制度中未找到”且不含编造数字 |
| 工具调用 | 10 | 顺序约束、参数边界、幂等、工具失败、拒绝越权 |
| 违规意图 | 5 | 补发票、改抬头、虚增金额、跳流程、**提示词注入** |
| 多轮上下文 | 5 | 代词指代、追问补全、状态延续、否定修正、跨轮复用 |

CI 会自动校验用例数量与字段完整性。

---

## 部署

### Docker

```bash
docker compose up -d
docker compose logs -f
```

镜像内置健康检查（`/health`）与非 root 运行用户。数据通过 volume 挂载持久化。

### 生产前必读

上线前请逐项核对 [`docs/上线检查清单.md`](docs/上线检查清单.md)（5 维度 × 25 项），其中 **2 条为阻断性红线**：

- 🔴 虚增金额（RISK-03）或提示词注入（RISK-05）任一项穿透 → 禁止上线
- 🔴 号源并发出现超约 → 禁止上线

### 已知限制（原型阶段）

- 数据库为 SQLite，适合单机与演示；**万人级抢号场景需迁移 PostgreSQL + Redis**
- 用户身份固定为 `demo_user`，尚未接入 SSO
- 报销规则为内置字典，生产应改为配置表 + 管理界面
- 医院 HIS 对接为预留适配器，尚未实现实际同步

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/需求清单.md`](docs/需求清单.md) | 一页纸需求、10 项关键决策与架构影响 |
| [`docs/架构与部署拓扑.html`](docs/架构与部署拓扑.html) | 五层架构图 + 私有化部署拓扑（浏览器打开） |
| [`docs/API.md`](docs/API.md) | 全部接口、SSE 事件协议、错误码总表、工具清单 |
| [`docs/AGENT_轨迹示例.md`](docs/AGENT_轨迹示例.md) | 3 个典型对话轨迹 + 阶段分析 SQL |
| [`docs/上线检查清单.md`](docs/上线检查清单.md) | 数据安全/并发/兜底/内容安全/回滚 五维度检查 |
| [`examples/README.md`](examples/README.md) | 阶段 1 最小 RAG 原型说明 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 开发约定与 PR 检查清单 |

---

## 技术栈

**后端** FastAPI · Uvicorn · Python 3.11
**智能体** OpenAI 兼容 API（供应商可替换）· 自研 Harness 编排
**检索** FAISS · rank-bm25 · jieba · pgvector（生产建议）
**存储** SQLite（原型）· 文件系统向量缓存
**前端** 原生 JS + SSE · Jinja2 服务端渲染
**运维** Docker · Docker Compose · GitHub Actions

---

## License

[MIT](LICENSE)
