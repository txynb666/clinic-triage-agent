# API 接口文档

Base URL：`http://localhost:8000`
所有 JSON 接口统一返回：`{"code": 0, "message": "成功", "data": {...}}`，`code=0` 为成功。

---

## 一、员工端

### 1.1 POST /chat —— 对话（SSE 流式，核心接口）

**请求体**

```json
{ "session_id": "abc123", "question": "门诊报销比例是多少" }
```

`session_id` 可为空，空则新建会话并在首个 `meta` 事件返回。

**响应**：`Content-Type: text/event-stream`，事件依次为

| event | data | 说明 |
|---|---|---|
| `meta` | `{"session_id":"..."}` | 会话标识 |
| `status` | `{"stage":"working","text":"正在检索制度…"}` | 阶段提示 |
| `tool` | `{"name":"search_policy","args":{...},"result":{...}}` | 工具执行轨迹（可多次） |
| `sources` | `{"sources":[{"doc":"就诊须知.md","snippet":"..."}]}` | 引用来源 |
| `delta` | `{"content":"门诊"}` | 回答增量（多次） |
| `confirm` | `{"token":"...","tool":"...","preview":{...}}` | **中断点：等待用户确认草稿** |
| `done` | `{"run_id":"...","tools":[...]}` | 正常结束 |
| `error` | `{"code":3002,"message":"..."}` | 异常结束 |

**示例**

```bash
curl -N -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"门诊报销比例是多少"}'
```

### 1.2 POST /chat/confirm —— 确认或取消被中断的写操作

```json
{ "token": "上一步 confirm 事件返回的令牌", "action": "confirm" }
```

- `action=confirm` → 以 SSE 流返回执行结果（`status`/`tool`/`delta`/`done`）
- `action=reject` → 普通 JSON，`{"code":0,"message":"已取消，未生成报销草稿"}`

令牌 **10 分钟有效、一次性消费**，超时返回 `4003`。

### 1.3 GET /sessions/{session_id}/messages —— 历史消息

```json
{"code":0,"data":[{"role":"user","content":"...","created_at":"..."},
                  {"role":"assistant","content":"...","sources":[...]}]}
```

### 1.4 GET /health —— 健康检查

```json
{"code":0,"data":{"status":"ok","time":"2026-10-30 10:00:00"}}
```

---

## 二、管理后台（需管理员 JWT）

登录后 JWT 写入 HttpOnly Cookie `clinic_token`，也可用 `Authorization: Bearer <token>`。

### 2.1 鉴权

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/api/login` | `{username, password}` → 设置 Cookie |
| POST | `/admin/api/logout` | 清除 Cookie |
| GET | `/admin/api/me` | 当前用户信息 |

### 2.2 知识库

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/kb/documents` | 文档列表 |
| POST | `/admin/api/kb/upload` | multipart：`file`（.md/.pdf）、`title`、`category` |
| DELETE | `/admin/api/kb/documents/{id}` | 删除并重建索引 |
| POST | `/admin/api/kb/rebuild` | 全量重建索引 |

上传流程：解析 → 500 字切片 → 向量化 → 重建 FAISS 索引。**失败自动回滚元数据与文件。**

### 2.3 提示词模板

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/prompt` | 当前生效模板 + 历史版本 |
| PUT | `/admin/api/prompt` | `{content}` → 版本 +1，**立即生效** |
| POST | `/admin/api/prompt/reset` | 恢复默认模板 |
| POST | `/admin/api/prompt/rollback/{version}` | 回滚到指定版本 |

### 2.4 对话审计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/audit/conversations` | 参数：`user_id`、`start`、`end`、`flagged=1`、`page`、`size` |
| GET | `/admin/api/audit/messages/{conv_id}` | 某会话全部问答 |
| POST | `/admin/api/audit/flag` | `{message_id, flagged, reason}` 标记/取消疑似违规 |

---

## 三、错误码总表

| code | 含义 | 中文提示 |
|---|---|---|
| 0 | 成功 | 成功 |
| 1001 | 参数不合法 | 参数不合法：question 不能为空 |
| 1002 | 提问过长 | 提问过长，请控制在 500 字以内 |
| 1003 | 无权限 | 无权限访问该功能 |
| 1004 | 登录失败 | 账号或密码错误 |
| 1005 | 登录过期 | 登录已过期，请重新登录 |
| 2001 | 会话不存在 | 会话不存在或已过期，请开启新会话 |
| 2002 | 操作已处理 | 该操作已处理或不存在 |
| 3001 | LLM 超时 | 服务繁忙，请稍后重试 |
| 3002 | LLM 不可用 | 智能服务暂不可用，您可拨打行政分机 8001 咨询 |
| 3003 | 限流 | 提问过于频繁，请稍后再试 |
| 4001 | 知识库未就绪 | 知识库初始化中，请稍后再试 |
| 4003 | 前置校验未通过 | 请先完成校验 / 凭证失效 |
| 4005 | 写操作超限 | 本次请求已执行过写操作，请分开提交 |
| 4006 | 轮数超限 | 已达处理轮数上限 |
| 5001 | 文件类型不支持 | 仅支持 .md 与 .pdf 文件 |
| 5002 | 文件解析失败 | 文件解析失败，请检查内容格式 |
| 5003 | 提示词过长 | 提示词过长，请控制在 4000 字以内 |
| 5004 | 文档不存在 | 文档不存在或已删除 |
| 5005 | 版本不存在 | 该版本不存在 |
| 5006 | 消息不存在 | 消息不存在 |
| 6001~6006 | 工具参数问题 | 见具体 message |
| 9999 | 内部错误 | 系统内部错误，请稍后重试 |

---

## 四、工具清单（Function Calling）

| 工具 | 类型 | 前置条件 | 说明 |
|---|---|---|---|
| `search_policy` | 查询 | 无 | 制度检索（RAG Top3） |
| `list_available_slots` | 查询 | 无 | 查科室可用号源 |
| `get_my_appointments` | 查询 | 无 | 查本人预约 |
| `get_my_reimbursements` | 查询 | 无 | 查本人报销单 |
| `check_reimbursement_eligibility` | 校验 | 无 | 报销资格校验，通过则签发令牌 |
| `check_slot_availability` | 校验 | 无 | 号源二次确认 |
| `create_appointment` | **写入** | 先查号源 + 确认余量 | 创建预约（AI 场景为 pending_triage） |
| `create_reimbursement_draft` | **写入** | **必须有有效 eligibility_token** | 建报销草稿，需人工确认 |

**顺序约束**：
- 制度类问题必须 `search_policy` 先行
- 报销必须 `check_reimbursement_eligibility` → `create_reimbursement_draft`
- 预约必须 `list_available_slots` → `check_slot_availability` → `create_appointment`
