# -*- coding: utf-8 -*-
"""
agent_trace.py —— Agent 执行轨迹与中断挂起的数据层
===================================================
两张表：
  1. agent_steps      每一轮「模型决策 → 工具执行」的明细（工具名、参数、耗时、结果摘要）
                      → 供阶段 5 做效率分析、成功率分析、卡点定位
  2. pending_actions  中断挂起的待确认动作（草稿确认场景）
                      → SSE 推 confirm 事件后，前端确认，凭 token 恢复执行
"""

import json
import time
import uuid
import sqlite3
import threading

from app.core.config import DB_PATH as _CFG_DB, CONFIRM_TTL
DB_PATH = str(_CFG_DB)
_lock = threading.Lock()

# 结果摘要截断长度（日志表不存全量，避免膨胀；全量在 messages 里）
SUMMARY_MAX = 300


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_trace_db():
    """建表。agent_steps 按 run_id 聚合一次完整的智能体执行。"""
    with _lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS agent_steps (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT NOT NULL,        -- 一次执行的唯一标识
            conv_id       TEXT,                 -- 所属会话
            user_id       TEXT,
            round_no      INTEGER NOT NULL,     -- 第几轮决策（从 1 开始）
            step_type     TEXT NOT NULL,        -- decision / tool / interrupt / final / error
            tool_name     TEXT,                 -- 工具名（step_type=tool 时）
            arguments     TEXT,                 -- 工具入参 JSON
            result_ok     INTEGER,              -- 1 成功 / 0 失败 / NULL 未执行
            result_code   INTEGER,              -- 业务错误码
            result_summary TEXT,                -- 结果摘要（截断）
            duration_ms   INTEGER,              -- 工具执行耗时（毫秒）
            created_at    TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(run_id);
        CREATE INDEX IF NOT EXISTS idx_steps_conv ON agent_steps(conv_id);

        CREATE TABLE IF NOT EXISTS pending_actions (
            token       TEXT PRIMARY KEY,       -- 确认令牌（前端回传）
            run_id      TEXT NOT NULL,
            conv_id     TEXT,
            user_id     TEXT,
            action      TEXT NOT NULL,          -- 动作类型：create_reimbursement_draft
            arguments   TEXT NOT NULL,          -- 已校验过的完整入参（含 eligibility_token）
            preview     TEXT NOT NULL,          -- 展示给员工的草稿预览
            status      TEXT DEFAULT 'pending', -- pending / confirmed / rejected / expired
            created_at  REAL NOT NULL,
            resolved_at REAL
        );
        """)


# ---------------- 日志写入 ----------------
def log_step(run_id: str, round_no: int, step_type: str, conv_id: str = "",
             user_id: str = "", tool_name: str = None, arguments: dict = None,
             result: dict = None, duration_ms: int = None):
    """记录一条执行轨迹。result 为工具返回的统一结构 {ok, code, message, data}。"""
    ok = code = summary = None
    if result is not None:
        ok = 1 if result.get("ok") else 0
        code = result.get("code")
        # 摘要优先用 message，再拼 data 的关键信息，最后截断
        summary = _summarize(result)
    with _lock, db() as c:
        c.execute(
            "INSERT INTO agent_steps(run_id,conv_id,user_id,round_no,step_type,tool_name,"
            "arguments,result_ok,result_code,result_summary,duration_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, conv_id, user_id, round_no, step_type, tool_name,
             json.dumps(arguments, ensure_ascii=False) if arguments else None,
             ok, code, summary, duration_ms))


def _summarize(result: dict) -> str:
    """把工具返回压成一行可读摘要，便于阶段 5 做 SQL 分析。"""
    msg = result.get("message", "")
    data = result.get("data") or {}
    extra = ""
    if isinstance(data, dict):
        # 针对不同工具抽关键字段，保证摘要有信息量
        for k in ("passed", "reimbursable_amount", "appointment_id", "draft_id",
                  "available", "chunk_count", "count"):
            if k in data:
                extra += f" {k}={data[k]}"
        if data.get("reasons"):
            extra += " reasons=" + ";".join(map(str, data["reasons"]))[:120]
    text = f"{msg}{extra}".strip() or "无返回"
    return text[:SUMMARY_MAX]


# ---------------- 挂起动作（中断确认） ----------------
def create_pending(run_id: str, action: str, arguments: dict, preview: dict,
                   conv_id: str = "", user_id: str = "") -> str:
    """中断点：把待确认动作落库，返回确认令牌给前端。
    注意：此时动作尚未执行，必须等用户在界面上点「确认」。
    """
    token = uuid.uuid4().hex
    with _lock, db() as c:
        c.execute(
            "INSERT INTO pending_actions(token,run_id,conv_id,user_id,action,arguments,"
            "preview,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (token, run_id, conv_id, user_id, action,
             json.dumps(arguments, ensure_ascii=False),
             json.dumps(preview, ensure_ascii=False), time.time()))
    return token


def take_pending(token: str, user_id: str) -> tuple:
    """取出并消费挂起动作：必须 pending 且在 10 分钟内有效。
    返回 (行dict 或 None, 错误原因)。消费后状态改为 confirmed，防重复点击。
    """
    with _lock, db() as c:
        row = c.execute("SELECT * FROM pending_actions WHERE token=?", (token,)).fetchone()
        if not row:
            return None, "确认令牌不存在，请重新发起"
        if row["status"] != "pending":
            return None, "该操作已处理过，请勿重复提交"
        if time.time() - row["created_at"] > CONFIRM_TTL:
            c.execute("UPDATE pending_actions SET status='expired' WHERE token=?", (token,))
            return None, "确认已超时（超过 10 分钟），请重新发起"
        if row["user_id"] != user_id:
            return None, "确认令牌与当前用户不匹配"
        c.execute("UPDATE pending_actions SET status='confirmed', resolved_at=? WHERE token=?",
                  (time.time(), token))
        d = dict(row)
    return d, ""


def reject_pending(token: str, user_id: str) -> bool:
    """用户取消：标记 rejected，动作永不执行。"""
    with _lock, db() as c:
        cur = c.execute(
            "UPDATE pending_actions SET status='rejected', resolved_at=? "
            "WHERE token=? AND user_id=? AND status='pending'",
            (time.time(), token, user_id))
        return cur.rowcount > 0


# ---------------- 阶段 5 分析用的聚合查询 ----------------
def run_summary(run_id: str) -> dict:
    """单次执行的概览：轮数、工具数、总耗时、失败数。"""
    with db() as c:
        rows = c.execute("SELECT * FROM agent_steps WHERE run_id=? ORDER BY id",
                         (run_id,)).fetchall()
    steps = [dict(r) for r in rows]
    tools = [s for s in steps if s["step_type"] == "tool"]
    return {
        "run_id": run_id,
        "rounds": max([s["round_no"] for s in steps], default=0),
        "tool_calls": len(tools),
        "failed_calls": len([t for t in tools if t["result_ok"] == 0]),
        "total_duration_ms": sum(t["duration_ms"] or 0 for t in tools),
        "tools": [t["tool_name"] for t in tools],
    }
