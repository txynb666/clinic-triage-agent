# -*- coding: utf-8 -*-
"""
admin_db.py —— 管理后台数据访问层
=================================
承载 4 块能力，供 admin.py 调用（对应最终架构的 app/services/* + app/repositories）：
  1. 管理员账号（bcrypt 哈希，绝不存明文）
  2. 提示词模板（存库 + 版本化，问答时实时读取 → 改完立即生效）
  3. 知识库文档元数据（上传/删除/列表）
  4. 对话审计（筛选、标记可疑）
"""

import json
import sqlite3
import hashlib
import hmac
import threading
import datetime

from app.core.config import DB_PATH as _CFG_DB
DB_PATH = str(_CFG_DB)
_lock = threading.Lock()

# 默认系统提示词（可由管理员在后台覆盖，此处仅作初始化种子）
DEFAULT_PROMPT = (
    "你是门诊预约与分诊协作系统的制度手册助手。"
    "只根据提供的制度条文回答，不确定时明确说“制度中未找到”，"
    "回答末尾附引用来源，不得编造就诊规则、报销标准与分诊建议。"
)

# 初始管理员账号（首次启动建库时写入，密码经 PBKDF2 哈希）
DEFAULT_ADMIN = ("admin", "admin123", "系统管理员", "admin")
# 演示用普通员工账号（验证接口级权限拦截）
DEFAULT_EMPLOYEE = ("employee", "emp123", "张小明", "employee")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------- 密码哈希（不引入额外依赖：PBKDF2-SHA256 + 随机盐） ----------------
def hash_password(password: str, salt: str | None = None) -> str:
    """返回格式 salt$hash，salt 为 16 字节十六进制随机串。"""
    salt = salt or hashlib.sha256(str(datetime.datetime.now()).encode()).hexdigest()[:32]
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """恒定时间比较，防时序攻击。"""
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


# ---------------- 建表 ----------------
def init_admin_db():
    """在阶段 2 的基础上新增 4 张表，并给 messages 追加审计字段。"""
    with _lock, db() as c:
        c.executescript("""
        -- ① 后台账号
        CREATE TABLE IF NOT EXISTS admin_users (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            username       TEXT UNIQUE NOT NULL,
            password_hash  TEXT NOT NULL,
            name           TEXT NOT NULL,
            role           TEXT NOT NULL DEFAULT 'employee',  -- employee / admin
            is_active      INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT DEFAULT (datetime('now','localtime'))
        );
        -- ② 提示词模板（版本化，is_active 只有一条）
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            content     TEXT NOT NULL,
            version     INTEGER NOT NULL,
            is_active   INTEGER NOT NULL DEFAULT 1,
            updated_by  TEXT,
            updated_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        -- ③ 知识库文档元数据
        CREATE TABLE IF NOT EXISTS kb_documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            filename    TEXT NOT NULL,
            filetype    TEXT NOT NULL,           -- md / pdf
            category    TEXT DEFAULT '就诊须知',
            chunk_count INTEGER DEFAULT 0,
            version     INTEGER DEFAULT 1,
            status      INTEGER DEFAULT 1,       -- 1 启用 / 0 已删除（软删）
            raw_path    TEXT,                    -- 原始文件物理路径，供索引构建与删除
            uploaded_by TEXT,
            updated_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        """)
        # messages 追加审计字段（SQLite 不支持 IF NOT EXISTS 加列，先探测）
        cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)").fetchall()}
        for col, ddl in [("flagged", "INTEGER DEFAULT 0"),
                         ("flag_reason", "TEXT"),
                         ("flagged_by", "TEXT"),
                         ("flagged_at", "TEXT")]:
            if col not in cols:
                c.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")

        # 种子数据：账号 + 默认提示词
        if not c.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
            for u, p, n, r in (DEFAULT_ADMIN, DEFAULT_EMPLOYEE):
                c.execute("INSERT INTO admin_users(username,password_hash,name,role) VALUES (?,?,?,?)",
                          (u, hash_password(p), n, r))
        if not c.execute("SELECT 1 FROM prompt_templates WHERE is_active=1").fetchone():
            c.execute("INSERT INTO prompt_templates(content,version,is_active,updated_by) "
                      "VALUES (?,1,1,'system')", (DEFAULT_PROMPT,))


# ---------------- ① 账号 ----------------
def get_user_by_username(username: str):
    with db() as c:
        return c.execute("SELECT * FROM admin_users WHERE username=? AND is_active=1",
                         (username,)).fetchone()


def get_user_by_id(uid: int):
    with db() as c:
        return c.execute("SELECT * FROM admin_users WHERE id=?", (uid,)).fetchone()


# ---------------- ② 提示词模板 ----------------
def get_active_prompt() -> str:
    """问答链路每次调用本函数 → 管理员改完立即生效，无需重启。"""
    with db() as c:
        row = c.execute("SELECT content FROM prompt_templates WHERE is_active=1 "
                        "ORDER BY version DESC LIMIT 1").fetchone()
    return row["content"] if row else DEFAULT_PROMPT


def list_prompts(limit: int = 10) -> list:
    with db() as c:
        rows = c.execute("SELECT * FROM prompt_templates ORDER BY version DESC LIMIT ?",
                         (limit,)).fetchall()
    return [dict(r) for r in rows]


def save_prompt(content: str, updated_by: str) -> int:
    """停用旧版本、写入新版本（版本号自增），返回新版本号。"""
    with _lock, db() as c:
        cur = c.execute("SELECT MAX(version) v FROM prompt_templates").fetchone()
        version = (cur["v"] or 0) + 1
        c.execute("UPDATE prompt_templates SET is_active=0")
        c.execute("INSERT INTO prompt_templates(content,version,is_active,updated_by) "
                  "VALUES (?,?,1,?)", (content, version, updated_by))
    return version


def rollback_prompt(version: int, updated_by: str) -> bool:
    with _lock, db() as c:
        row = c.execute("SELECT content FROM prompt_templates WHERE version=?",
                        (version,)).fetchone()
        if not row:
            return False
    save_prompt(row["content"], updated_by)
    return True


# ---------------- ③ 知识库文档 ----------------
def add_document(title, filename, filetype, chunk_count, uploaded_by,
                 category="就诊须知", raw_path=None) -> int:
    with _lock, db() as c:
        cur = c.execute(
            "INSERT INTO kb_documents(title,filename,filetype,category,chunk_count,"
            "uploaded_by,raw_path) VALUES (?,?,?,?,?,?,?)",
            (title, filename, filetype, category, chunk_count, uploaded_by, raw_path))
        return cur.lastrowid


def list_documents() -> list:
    """仅返回启用状态的文档（软删的不展示）。"""
    with db() as c:
        rows = c.execute("SELECT * FROM kb_documents WHERE status=1 "
                         "ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_document(doc_id: int):
    with db() as c:
        return c.execute("SELECT * FROM kb_documents WHERE id=? AND status=1",
                         (doc_id,)).fetchone()


def delete_document(doc_id: int) -> bool:
    """软删除：状态置 0，并物理删除对应文件，触发索引重建。"""
    with _lock, db() as c:
        row = c.execute("SELECT raw_path FROM kb_documents WHERE id=?",
                        (doc_id,)).fetchone() if _has_raw_path(c) else None
        cur = c.execute("UPDATE kb_documents SET status=0 WHERE id=? AND status=1", (doc_id,))
        if row and row["raw_path"]:
            import os
            try:
                os.remove(row["raw_path"])
            except OSError:
                pass
        return cur.rowcount > 0


def _has_raw_path(c) -> bool:
    """兼容性探测：raw_path 列可能尚未迁移。"""
    return any(r["name"] == "raw_path" for r in c.execute("PRAGMA table_info(kb_documents)"))


def active_doc_paths() -> list:
    """返回启用文档的物理路径列表，供索引构建使用。"""
    with db() as c:
        if not _has_raw_path(c):
            return []
        rows = c.execute("SELECT raw_path FROM kb_documents WHERE status=1 AND raw_path IS NOT NULL").fetchall()
    return [r["raw_path"] for r in rows]


# ---------------- ④ 对话审计 ----------------
def list_conversations(user_id=None, start=None, end=None, flagged=None, page=1, size=20):
    """按员工/时间/可疑标记筛选会话，返回 (列表, 总数)。"""
    sql_base = """
        SELECT c.id AS conv_id, c.user_id, c.created_at,
               (SELECT content FROM messages WHERE conversation_id=c.id AND role='user'
                ORDER BY id LIMIT 1) AS first_q,
               (SELECT COUNT(*) FROM messages WHERE conversation_id=c.id) AS msg_count,
               (SELECT COUNT(*) FROM messages WHERE conversation_id=c.id AND flagged=1) AS flag_count,
               (SELECT MAX(created_at) FROM messages WHERE conversation_id=c.id) AS last_at
        FROM conversations c WHERE 1=1
    """
    conds, cargs = [], []
    if user_id:
        conds.append("c.user_id LIKE ?"); cargs.append(f"%{user_id}%")
    if start:
        conds.append("date(c.created_at) >= date(?)"); cargs.append(start)
    if end:
        conds.append("date(c.created_at) <= date(?)"); cargs.append(end)
    if flagged is True:
        conds.append("EXISTS (SELECT 1 FROM messages WHERE conversation_id=c.id AND flagged=1)")
    if conds:
        sql_base += " AND " + " AND ".join(conds)
    sql_base += " ORDER BY c.created_at DESC LIMIT ? OFFSET ?"
    cargs += [size, (page - 1) * size]

    with db() as c:
        rows = c.execute(sql_base, cargs).fetchall()
        total = c.execute("SELECT COUNT(*) n FROM conversations c WHERE 1=1" +
                          (" AND " + " AND ".join(conds) if conds else ""),
                          cargs[:-2]).fetchone()["n"]
    return [dict(r) for r in rows], total


def list_messages(conv_id: str) -> list:
    with db() as c:
        rows = c.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id",
                         (conv_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("sources"):
            try:
                d["sources"] = json.loads(d["sources"])
            except Exception:
                d["sources"] = []
        out.append(d)
    return out


def flag_message(message_id: int, flagged: bool, reason: str, by: str) -> bool:
    with _lock, db() as c:
        cur = c.execute(
            "UPDATE messages SET flagged=?, flag_reason=?, flagged_by=?, "
            "flagged_at=datetime('now','localtime') WHERE id=?",
            (1 if flagged else 0, reason if flagged else None,
             by if flagged else None, message_id))
        return cur.rowcount > 0


def stats() -> dict:
    """概览看板指标。"""
    with db() as c:
        return {
            "conversations": c.execute("SELECT COUNT(*) n FROM conversations").fetchone()["n"],
            "messages": c.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"],
            "documents": c.execute("SELECT COUNT(*) n FROM kb_documents WHERE status=1").fetchone()["n"],
            "flagged": c.execute("SELECT COUNT(*) n FROM messages WHERE flagged=1").fetchone()["n"],
        }
