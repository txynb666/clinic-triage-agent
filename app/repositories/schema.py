# -*- coding: utf-8 -*-
"""
app/repositories/schema.py —— 基础表结构（会话与消息）
======================================================
把建表 SQL 从 main.py 抽出，任何入口（服务、测试、脚本）都能独立初始化，
避免 "先 import main 才能建表" 的隐性依赖。
"""

import sqlite3
import threading

from app.core.config import DB_PATH

_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_core_db():
    """会话表 + 消息表（阶段 1 结构，审计字段由 admin_db.init_admin_db 追加）。"""
    with _lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL DEFAULT 'demo_user',
            agent_type  TEXT NOT NULL DEFAULT 'qa',
            status      TEXT NOT NULL DEFAULT 'active',
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS messages (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id  TEXT NOT NULL REFERENCES conversations(id),
            role             TEXT NOT NULL,
            content          TEXT NOT NULL,
            sources          TEXT,
            created_at       TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id);
        """)


def init_all():
    """一次初始化所有表：核心表 → 后台表 → 业务工具表 → 轨迹表。
    顺序不能变：后台表要给 messages 追加审计列，必须核心表先存在。"""
    from app.repositories import admin_db, agent_trace
    from app.services import tools

    init_core_db()
    admin_db.init_admin_db()
    tools.init_tools_db()
    agent_trace.init_trace_db()
