# -*- coding: utf-8 -*-
"""
main.py —— 门诊问答服务后端（阶段 2：员工问答入口）
====================================================
接口：
  POST /chat                          SSE 流式问答（核心）
  GET  /sessions/{sid}/messages       回显历史消息
  GET  /health                        健康自检

约束：所有接口先返回错误码 + 中文错误信息；
     会话与消息存 SQLite（沿用阶段 1 的 conversations / messages 表结构）。
"""

import json
import time
import sqlite3
import threading
import pathlib

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import rag
from app.rag import RagError
from app.api.admin.routes import router as admin_router   # 管理后台路由
from app.repositories import admin_db
from app.agents import prompts as agent                     # 系统提示词与状态文案
from app.agents import harness                              # 执行循环（5 轮 + 中断确认）
from app.repositories import agent_trace                    # 执行轨迹与挂起动作
from app.services import tools                              # 工具层
from app.core.config import (STATIC_DIR, DB_PATH as _CFG_DB, DOC_PATH as _CFG_DOC,
                             KB_UPLOAD_DIR, INDEX_DIR, ensure_dirs)

app = FastAPI(title="门诊预约与分诊协作 · 问答服务")

# ---------------- 数据库（SQLite，沿用阶段 1 表结构） ----------------
DB_PATH = str(_CFG_DB)
_db_lock = threading.Lock()   # SQLite 单写者，用线程锁保证并发写安全


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建表：全部表结构统一由 schema 模块初始化（幂等）。"""
    from app.repositories import schema
    schema.init_core_db()


from app.repositories import schema
schema.init_all()          # 核心表 + 后台表 + 工具表 + 轨迹表（幂等）


def _seed_kb():
    """首次启动把内置《就诊须知.md》登记进知识库，保证开箱可用。
    文档本身已随仓库放在 data/kb_files/ 下，这里只做"登记入库"，不再复制文件。"""
    import pathlib as _p
    doc = _p.Path(rag.DOC_PATH)
    if doc.exists() and not admin_db.list_documents():
        try:
            chunks = rag.split_chunks(rag.load_document(str(doc)))
            admin_db.add_document(doc.stem, doc.name, "md", len(chunks),
                                  "system", "就诊须知", str(doc))
        except Exception:
            pass


_seed_kb()

app.include_router(admin_router)

# 鉴权依赖抛出的 PermissionError → 统一错误码响应（而非 500）
from fastapi import Request as _Req
from fastapi.responses import JSONResponse as _JSON

from app.api.admin import routes as _admin


@app.exception_handler(PermissionError)
async def _perm_handler(request: _Req, exc: PermissionError):
    key = str(exc) or "FORBIDDEN"
    return _admin.err(key)

# ---------------- 统一响应与错误处理 ----------------
# 错误码表：0 成功；1xxx 参数；2xxx 会话；3xxx LLM；4xxx 知识库
ERRORS = {
    "INVALID_PARAM":     (1001, "参数不合法：question 不能为空"),
    "QUESTION_TOO_LONG": (1002, "提问过长，请控制在 500 字以内"),
    "SESSION_NOT_FOUND": (2001, "会话不存在或已过期，请开启新会话"),
    "LLM_TIMEOUT":       (3001, "服务繁忙，请稍后重试"),
    "LLM_UNAVAILABLE":   (3002, "智能服务暂不可用，您可拨打行政分机 8001 咨询"),
    "INDEX_NOT_READY":   (4001, "知识库初始化中，请稍后再试"),
    "INTERNAL":          (9999, "系统内部错误，请稍后重试"),
}


def fail(code_key: str, extra: str = "") -> JSONResponse:
    """构造统一错误响应 {"code": 1001, "message": "中文错误"}。"""
    code, msg = ERRORS[code_key]
    return JSONResponse({"code": code, "message": f"{msg}{('：' + extra) if extra else ''}",
                         "data": None})


class ChatRequest(BaseModel):
    """POST /chat 请求体。"""
    session_id: str | None = Field(None, description="会话ID，为空则新建")
    question: str = Field(..., min_length=1, description="员工提问")


def sse(event: str, data: dict) -> str:
    """格式化一条 SSE 事件。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------- 核心接口：POST /chat（SSE 流式） ----------------
@app.post("/chat")
async def chat(req: ChatRequest):
    # ---- 1. 参数校验：不合法直接返回统一错误（非流式） ----
    question = (req.question or "").strip()
    if not question:
        return fail("INVALID_PARAM")
    if len(question) > 500:
        return fail("QUESTION_TOO_LONG")
    if req.session_id and len(req.session_id) > 64:
        return fail("INVALID_PARAM", "session_id 长度超限")

    async def event_stream():
        """SSE 事件生成器：meta → status → sources → delta* → done / error。"""
        # ---- 2. 会话处理：不存在则新建，首个事件告知 session_id ----
        session_id = req.session_id
        with _db_lock, db() as c:
            if session_id:
                row = c.execute("SELECT id FROM conversations WHERE id=?",
                                (session_id,)).fetchone()
                if not row:
                    yield sse("error", {"code": 2001, "message": ERRORS["SESSION_NOT_FOUND"][1]})
                    return
            else:
                import uuid
                session_id = uuid.uuid4().hex
                c.execute("INSERT INTO conversations(id) VALUES (?)", (session_id,))
        yield sse("meta", {"session_id": session_id})

        try:
            # ---- 3. 保存用户消息 + 读取最近 6 轮历史 ----
            with _db_lock, db() as c:
                c.execute("INSERT INTO messages(conversation_id, role, content) VALUES (?,?,?)",
                          (session_id, "user", question))
                rows = c.execute(
                    "SELECT role, content FROM messages WHERE conversation_id=? "
                    "ORDER BY id DESC LIMIT 12",           # 12 条 = 6 轮(user+assistant)
                    (session_id,)).fetchall()
            history = [(r["role"], r["content"]) for r in reversed(rows)][:-1]  # 去掉当前问题

            # ---- 4. 交给 Harness 执行循环：最多 5 轮，每轮落日志，写操作中断确认 ----
            full, sources, tool_log = [], [], []
            for ev in harness.run(question, history, user_id="demo_user",
                                  conv_id=session_id):
                t = ev["type"]
                if t == "status":
                    yield sse("status", {"stage": "working", "text": ev["text"]})
                elif t == "tool":
                    tool_log.append({"name": ev["name"], "args": ev["args"],
                                     "ok": ev["result"].get("ok"),
                                     "message": ev["result"].get("message")})
                    # 检索类工具顺带把引用来源推给前端
                    if ev["name"] == "search_policy" and ev["result"].get("ok"):
                        for s in ev["result"]["data"].get("snippets", []):
                            sources.append({"doc": s["doc"],
                                            "snippet": s["text"][:100]})
                        if sources:
                            yield sse("sources", {"sources": sources})
                    yield sse("tool", {"name": ev["name"], "args": ev["args"],
                                       "result": ev["result"]})
                elif t == "delta":
                    full.append(ev["text"])
                    yield sse("delta", {"content": ev["text"]})
                elif t == "confirm":
                    # 中断点：把草稿预览与确认令牌推给前端，本次流到此结束
                    yield sse("confirm", {"token": ev["token"], "tool": ev["tool"],
                                          "preview": ev["preview"],
                                          "run_id": ev["run_id"]})
                    # 保存已有的部分回答（如有），并告知前端等待确认
                    return
                elif t == "error":
                    yield sse("error", {"code": 3002, "message": ev["message"]})
                    return
                elif t == "done":
                    break

            # ---- 5. 保存助手消息（含引用来源与工具轨迹），结束 ----
            answer = "".join(full) or "（本次未生成回答，请重试）"
            with _db_lock, db() as c:
                c.execute(
                    "INSERT INTO messages(conversation_id, role, content, sources) VALUES (?,?,?,?)",
                    (session_id, "assistant", answer, json.dumps(sources, ensure_ascii=False)))
            yield sse("done", {"message_id": None, "session_id": session_id,
                               "tools": tool_log})

        except RagError as e:                       # 检索层已知异常 → 映射错误码
            key = e.code if e.code in ERRORS else "INTERNAL"
            yield sse("error", {"code": ERRORS[key][0], "message": e.message})
        except Exception:                           # 其余一律按 LLM 异常兜底
            yield sse("error", {"code": 3002, "message": ERRORS["LLM_UNAVAILABLE"][1]})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- 确认接口：员工点「确认」后恢复中断的执行 ----------------
class ConfirmRequest(BaseModel):
    """POST /chat/confirm 请求体。"""
    token: str = Field(..., min_length=1, description="confirm 事件返回的令牌")
    action: str = Field("confirm", description="confirm 确认 / reject 取消")


@app.post("/chat/confirm")
async def chat_confirm(req: ConfirmRequest):
    """两种行为：
       action=confirm → 执行被中断的写操作，并把结果流式回给前端
       action=reject  → 丢弃挂起动作，草稿不落库
    """
    token = (req.token or "").strip()
    if not token:
        return fail("INVALID_PARAM", "缺少确认令牌")

    # ---- 用户取消 ----
    if req.action == "reject":
        ok = agent_trace.reject_pending(token, "demo_user")
        if not ok:
            return JSONResponse({"code": 2002, "message": "该操作已处理或不存在", "data": None})
        return {"code": 0, "message": "已取消，未生成报销草稿", "data": None}

    async def resume_stream():
        """恢复执行的 SSE 流：status → tool → delta* → done。"""
        try:
            full, tool_log = [], []
            for ev in harness.resume(token, user_id="demo_user"):
                t = ev["type"]
                if t == "status":
                    yield sse("status", {"stage": "working", "text": ev["text"]})
                elif t == "tool":
                    tool_log.append({"name": ev["name"], "ok": ev["result"].get("ok"),
                                     "message": ev["result"].get("message")})
                    yield sse("tool", {"name": ev["name"], "args": ev["args"],
                                       "result": ev["result"]})
                elif t == "delta":
                    full.append(ev["text"])
                    yield sse("delta", {"content": ev["text"]})
                elif t == "error":
                    # 令牌失效等场景，直接以错误事件收尾
                    yield sse("error", {"code": 4003, "message": ev["message"]})
                    return
                elif t == "done":
                    break
            # 把确认后的结果也记入会话，保证审计页能看到完整对话
            answer = "".join(full) or "草稿已生成"
            with _db_lock, db() as c:
                conv = c.execute("SELECT id FROM conversations ORDER BY created_at DESC "
                                 "LIMIT 1").fetchone()
                if conv:
                    c.execute("INSERT INTO messages(conversation_id, role, content, sources) "
                              "VALUES (?,?,?,?)",
                              (conv["id"], "assistant", answer, json.dumps([], ensure_ascii=False)))
            yield sse("done", {"committed": True, "tools": tool_log})
        except Exception:
            yield sse("error", {"code": 9999, "message": "系统内部错误，请稍后重试"})

    return StreamingResponse(resume_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- 历史消息回显 ----------------
@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: str):
    with db() as c:
        conv = c.execute("SELECT id FROM conversations WHERE id=?", (session_id,)).fetchone()
        if not conv:
            return fail("SESSION_NOT_FOUND")
        rows = c.execute(
            "SELECT role, content, sources, created_at FROM messages "
            "WHERE conversation_id=? ORDER BY id", (session_id,)).fetchall()
    data = []
    for r in rows:
        item = {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        if r["sources"]:
            item["sources"] = json.loads(r["sources"])
        data.append(item)
    return {"code": 0, "message": "成功", "data": data}


# ---------------- 健康自检 ----------------
@app.get("/health")
def health():
    return {"code": 0, "message": "成功",
            "data": {"status": "ok", "time": time.strftime("%Y-%m-%d %H:%M:%S")}}


# ---------------- 静态页面（员工聊天界面） ----------------
# 注意必须挂在最后，避免遮蔽 /chat 等接口路由
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
