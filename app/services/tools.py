# -*- coding: utf-8 -*-
"""
tools.py —— 门诊助手 Agent 的工具层（Function Calling 规范实现）
================================================================
8 个工具分三组：
  A. 只读查询：search_policy / list_available_slots / get_my_appointments / get_my_reimbursements
  B. 校验（只读但必须通过）：check_reimbursement_eligibility / check_slot_availability
  C. 写入（有副作用，受顺序约束）：create_appointment / create_reimbursement_draft

顺序约束的强制手段（三重保险）：
  1. 提示词层：agent.py 中的铁律约束模型不得跳步
  2. 令牌层：check_reimbursement_eligibility 通过后签发一次性 eligibility_token，
     create_reimbursement_draft 必须携带且服务端验签（绑定金额/类型/发票号，5 分钟过期）
  3. 服务层：create_* 内部再跑一遍校验，防止模型伪造参数
"""

import json
import time
import uuid
import hashlib
import hmac
import sqlite3
import threading
import datetime

from app.repositories import admin_db
from app.core.config import DB_PATH as _CFG_DB, REIMB_POLICY as _CFG_POLICY, COMPANY_TITLE as _CFG_TITLE

DB_PATH = str(_CFG_DB)
_lock = threading.Lock()

# 令牌签名密钥（生产从环境变量注入）
TOKEN_SECRET = "clinic-eligibility-secret"
TOKEN_TTL = 300          # 校验令牌 5 分钟有效


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# 建表：号源、预约、报销单、校验令牌
# ============================================================
def init_tools_db():
    with _lock, db() as c:
        c.executescript("""
        -- 号源（管理员维护或从医院 HIS 同步）
        CREATE TABLE IF NOT EXISTS clinic_slots (
            id          TEXT PRIMARY KEY,
            dept        TEXT NOT NULL,
            doctor      TEXT,
            slot_date   TEXT NOT NULL,
            slot_time   TEXT NOT NULL,
            capacity    INTEGER NOT NULL DEFAULT 1,
            booked      INTEGER NOT NULL DEFAULT 0,
            status      INTEGER NOT NULL DEFAULT 1,   -- 1 开放 / 0 停诊
            clinic      TEXT DEFAULT '合作门诊部'
        );
        -- 预约单
        CREATE TABLE IF NOT EXISTS appointments (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           TEXT NOT NULL,
            slot_id           TEXT NOT NULL,
            dept              TEXT NOT NULL,
            symptom_summary   TEXT,
            recommended_dept  TEXT,
            triage_confirmed_by TEXT,                -- NULL = 待分诊台确认
            status            TEXT NOT NULL DEFAULT 'pending_triage',
                              -- pending_triage / confirmed / cancelled / completed
            source            TEXT DEFAULT 'ai',
            conv_id           TEXT,
            idem_key          TEXT UNIQUE,
            created_at        TEXT DEFAULT (datetime('now','localtime'))
        );
        -- 报销单草稿
        CREATE TABLE IF NOT EXISTS reimbursement_drafts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       TEXT NOT NULL,
            visit_type    TEXT NOT NULL,
            amount        REAL NOT NULL,
            invoice_type  TEXT NOT NULL,
            invoice_no    TEXT,
            visit_date    TEXT,
            status        TEXT NOT NULL DEFAULT 'draft',   -- draft / submitted / approved / rejected
            reimb_amount  REAL,
            conv_id       TEXT,
            idem_key      TEXT UNIQUE,
            created_at    TEXT DEFAULT (datetime('now','localtime'))
        );
        -- 校验令牌（一次性，用过即失效）
        CREATE TABLE IF NOT EXISTS eligibility_tokens (
            token      TEXT PRIMARY KEY,
            user_id    TEXT,
            payload    TEXT NOT NULL,     -- 校验通过的入参快照
            signature  TEXT NOT NULL,     -- HMAC 签名，防篡改
            used       INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        );
        """)
        # 无号源时插入演示数据，保证开箱可用
        if not c.execute("SELECT 1 FROM clinic_slots LIMIT 1").fetchone():
            today = datetime.date.today()
            days = [(today + datetime.timedelta(days=d)).isoformat() for d in (1, 2, 3)]
            depts = [("全科", "王医生"), ("内科", "李医生"), ("外科", "赵医生"),
                     ("皮肤科", "孙医生"), ("眼科", "周医生"), ("儿科", "吴医生")]
            for dept, doctor in depts:
                for d in days:
                    for t in ("09:00", "10:30", "14:00", "15:30"):
                        c.execute(
                            "INSERT INTO clinic_slots(id,dept,doctor,slot_date,slot_time,capacity) "
                            "VALUES (?,?,?,?,?,?)",
                            (f"{dept}-{d}-{t}", dept, doctor, d, t, 3))


# ============================================================
# 统一返回结构：{ok, code, message, data}
# ============================================================
def _ok(data, message="成功"):
    return {"ok": True, "code": 0, "message": message, "data": data}


def _fail(code, message, data=None):
    return {"ok": False, "code": code, "message": message, "data": data or {}}


# ============================================================
# 校验令牌：签发与验签
# ============================================================
def _sign(payload: dict) -> str:
    """HMAC-SHA256 签名，绑定校验通过的入参快照，防模型篡改。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hmac.new(TOKEN_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def issue_token(user_id: str, payload: dict) -> str:
    """校验通过后签发一次性令牌。"""
    token = uuid.uuid4().hex
    sig = _sign(payload)
    with _lock, db() as c:
        c.execute("INSERT INTO eligibility_tokens(token,user_id,payload,signature,created_at) "
                  "VALUES (?,?,?,?,?)",
                  (token, user_id, json.dumps(payload, ensure_ascii=False), sig, time.time()))
    return token


def consume_token(token: str, user_id: str, current: dict) -> tuple:
    """消费令牌：必须存在、未过期、未用过、签名一致、入参一致。
    返回 (是否通过, 原因)。令牌一经校验即标记 used，杜绝重放。"""
    with _lock, db() as c:
        row = c.execute("SELECT * FROM eligibility_tokens WHERE token=?", (token,)).fetchone()
        if not row:
            return False, "未找到校验凭证，请先完成报销资格校验"
        if row["used"]:
            return False, "该校验凭证已被使用，请重新校验"
        if time.time() - row["created_at"] > TOKEN_TTL:
            return False, "校验凭证已过期（超过 5 分钟），请重新校验"
        if row["user_id"] != user_id:
            return False, "校验凭证与当前用户不匹配"
        stored = json.loads(row["payload"])
        if _sign(stored) != row["signature"]:
            return False, "校验凭证签名异常"
        # 入参关键字段一致性：防止用"低金额校验"通过后提交"高金额单据"
        for key in ("amount", "visit_type", "invoice_type"):
            if key in current and stored.get(key) != current.get(key):
                return False, f"提交参数与校验时不一致（{key}），请重新校验"
        c.execute("UPDATE eligibility_tokens SET used=1 WHERE token=?", (token,))
    return True, ""


def find_recent_token(user_id: str):
    """反查最近一次有效（未使用、未过期）的校验令牌。
    用途：模型调用 create_reimbursement_draft 时常常只传业务字段，
    不一定把 eligibility_token 原样带上；由服务端绑定最近一次校验结果，
    既不违反"必须校验过才能建单"，也避免因模型漏参导致正常流程失败。
    """
    with db() as c:
        row = c.execute(
            "SELECT token FROM eligibility_tokens WHERE user_id=? AND used=0 "
            "AND created_at > ? ORDER BY created_at DESC LIMIT 1",
            (user_id, time.time() - TOKEN_TTL)).fetchone()
    return row["token"] if row else ""


# ============================================================
# A. 只读查询类
# ============================================================
def search_policy(query: str, category: str = "", user_id: str = "", **kw):
    """调用 RAG 混合检索（复用 rag.py）。"""
    from app import rag
    try:
        retriever = rag.get_retriever()
        hits = retriever.search(query, top_k=3)
    except rag.RagError as e:
        return _fail(int(e.code == "INDEX_NOT_READY") and 4001, e.message)
    snippets = [{"doc": doc, "text": chunk[:300]} for chunk, _score, doc in hits]
    return _ok({"query": query, "snippets": snippets,
                "answer_hint": "请仅依据上述片段回答，片段中无内容则回答“制度中未找到”"})


def list_available_slots(dept: str, date_from: str = "", date_to: str = "",
                         user_id: str = "", **kw):
    """查询某科室可预约号源（只返回还有余量的）。"""
    if not dept:
        return _fail(6001, "请提供科室名称")
    if not date_from:
        date_from = datetime.date.today().isoformat()
    if not date_to:
        date_to = (datetime.date.fromisoformat(date_from) + datetime.timedelta(days=7)).isoformat()
    with db() as c:
        rows = c.execute(
            "SELECT * FROM clinic_slots WHERE dept=? AND status=1 "
            "AND slot_date BETWEEN ? AND ? AND booked < capacity "
            "ORDER BY slot_date, slot_time LIMIT 12",
            (dept, date_from, date_to)).fetchall()
    slots = [{"slot_id": r["id"], "dept": r["dept"], "doctor": r["doctor"],
              "date": r["slot_date"], "time": r["slot_time"],
              "remain": r["capacity"] - r["booked"]} for r in rows]
    if not slots:
        return _ok({"dept": dept, "slots": [],
                    "hint": f"{dept} 近期暂无余号，可推荐其他科室或更改日期"})
    return _ok({"dept": dept, "slots": slots})


def get_my_appointments(status: str = "all", user_id: str = "", **kw):
    sql = "SELECT * FROM appointments WHERE user_id=?"
    args = [user_id]
    if status == "upcoming":
        sql += " AND status IN ('pending_triage','confirmed')"; 
    elif status == "history":
        sql += " AND status IN ('completed','cancelled')"
    sql += " ORDER BY id DESC LIMIT 10"
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    return _ok({"items": [dict(r) for r in rows], "count": len(rows)})


def get_my_reimbursements(status: str = "all", user_id: str = "", **kw):
    sql = "SELECT * FROM reimbursement_drafts WHERE user_id=?"
    args = [user_id]
    if status != "all":
        sql += " AND status=?"; args.append(status)
    sql += " ORDER BY id DESC LIMIT 10"
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    return _ok({"items": [dict(r) for r in rows], "count": len(rows)})


# ============================================================
# B. 校验类（只读，但必须通过才能进入写入）
# ============================================================
# 简化版报销规则引擎（对应最终架构的规则配置表；此处内置演示规则）
REIMB_POLICY = _CFG_POLICY
COMPANY_TITLE = _CFG_TITLE
ANNUAL_LIMIT_TOTAL = 8000


def check_reimbursement_eligibility(visit_type: str, amount: float, invoice_type: str,
                                    invoice_title: str, invoice_date: str,
                                    has_prescription: bool = None,
                                    user_id: str = "", **kw):
    """报销资格校验：制度规则逐条核对，通过则签发一次性令牌。"""
    reasons, suggestions = [], []
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return _fail(6002, "金额格式不正确")

    rule = REIMB_POLICY.get(visit_type)
    if not rule:
        return _fail(6003, f"不支持的就诊类型：{visit_type}")

    # ① 发票抬头
    if invoice_title.strip() != COMPANY_TITLE:
        reasons.append(f"发票抬头须为公司全称「{COMPANY_TITLE}」，当前为「{invoice_title}」")
        suggestions.append("请联系医院收费处重开发票，抬头填写公司全称")
    # ② 发票类型
    if invoice_type not in ("电子发票", "纸质发票", "卷式发票"):
        reasons.append(f"不支持的发票类型：{invoice_type}")
    # ③ 发票日期（30 天内提交）
    try:
        inv_date = datetime.date.fromisoformat(invoice_date)
        if (datetime.date.today() - inv_date).days > 30:
            reasons.append("超过就诊后 30 日的报销时限")
            suggestions.append("制度规定须在就诊后 30 日内提交，本次可能无法报销")
    except ValueError:
        reasons.append("发票日期格式应为 YYYY-MM-DD")
    # ④ 金额与起付线
    if amount <= rule["deductible"]:
        reasons.append(f"{visit_type}起付线为 {rule['deductible']} 元，本次金额未达起付线")
    # ⑤ 药品费需处方（简化：金额大于 0 且带 has_prescription=False 时提示）
    if has_prescription is False and visit_type == "门诊":
        reasons.append("药品费用须提供处方，缺少处方不予报销")
    # ⑥ 年度额度（累计已报销金额）
    with db() as c:
        used = c.execute(
            "SELECT COALESCE(SUM(reimb_amount),0) s FROM reimbursement_drafts "
            "WHERE user_id=? AND status IN ('submitted','approved')", (user_id,)).fetchone()["s"]
    reimb = round(max(0.0, (amount - rule["deductible"])) * rule["rate"], 2)
    if used + reimb > min(rule["annual_cap"], ANNUAL_LIMIT_TOTAL):
        reasons.append(f"超出年度报销额度（已用 {used} 元，上限 {rule['annual_cap']} 元）")

    if reasons:
        return _ok({"passed": False, "reasons": reasons, "suggestions": suggestions,
                    "reimbursable_amount": 0,
                    "policy_refs": ["《就诊须知.md》三、门诊报销制度"]})

    payload = {"visit_type": visit_type, "amount": amount,
               "invoice_type": invoice_type, "invoice_title": invoice_title,
               "invoice_date": invoice_date}
    token = issue_token(user_id, payload)
    return _ok({"passed": True, "eligibility_token": token, "expires_in": TOKEN_TTL,
                "reimbursable_amount": reimb,
                "detail": f"{visit_type}报销比例 {int(rule['rate']*100)}%，"
                          f"起付线 {rule['deductible']} 元，本次预估可报 {reimb} 元",
                "policy_refs": ["《就诊须知.md》三、门诊报销制度"]})


def check_slot_availability(slot_id: str, user_id: str = "", **kw):
    """号源二次确认：放号并发场景下创建前必须确认余量。"""
    if not slot_id:
        return _fail(6004, "缺少号源 ID")
    with db() as c:
        row = c.execute("SELECT * FROM clinic_slots WHERE id=?", (slot_id,)).fetchone()
    if not row:
        return _fail(6005, "号源不存在，请重新查询可用时段")
    if row["status"] != 1:
        return _ok({"available": False, "reason": "该时段已停诊，请选择其他时段"})
    remain = row["capacity"] - row["booked"]
    if remain <= 0:
        return _ok({"available": False, "reason": "该时段号源已约满，请选择其他时段"})
    return _ok({"available": True, "slot_id": slot_id, "dept": row["dept"],
                "doctor": row["doctor"], "date": row["slot_date"],
                "time": row["slot_time"], "remain": remain})


# ============================================================
# C. 写入类（受顺序约束）
# ============================================================
def create_appointment(slot_id: str, symptom_summary: str, recommended_dept: str,
                       triage_confirmed_by: str = None, idempotency_key: str = "",
                       user_id: str = "", conv_id: str = "", **kw):
    """创建预约单。AI 场景下 triage_confirmed_by 必须为 None → 待分诊台确认。"""
    if not slot_id or not idempotency_key:
        return _fail(6006, "缺少必要参数（slot_id / idempotency_key）")

    # 服务层二次校验：不信任模型传入的 slot_id
    check = check_slot_availability(slot_id, user_id)
    if not check["ok"]:
        return check
    if not check["data"]["available"]:
        return _fail(4003, check["data"]["reason"])

    # 原子扣减号源：UPDATE ... WHERE booked < capacity 保证不超约
    with _lock, db() as c:
        # 幂等：同一 idem_key 直接返回既有单
        exist = c.execute("SELECT * FROM appointments WHERE idem_key=?",
                          (idempotency_key,)).fetchone()
        if exist:
            return _ok({"appointment_id": exist["id"], "status": exist["status"],
                        "duplicated": True}, "该预约已创建，未重复提交")
        cur = c.execute(
            "UPDATE clinic_slots SET booked = booked + 1 "
            "WHERE id=? AND status=1 AND booked < capacity", (slot_id,))
        if cur.rowcount == 0:
            return _fail(4003, "号源已被约满，请重新选择时段")
        slot = c.execute("SELECT * FROM clinic_slots WHERE id=?", (slot_id,)).fetchone()
        # AI 推荐场景：强制 pending_triage，不允许模型自行确认为 confirmed
        status = "confirmed" if triage_confirmed_by else "pending_triage"
        cur = c.execute(
            "INSERT INTO appointments(user_id,slot_id,dept,symptom_summary,recommended_dept,"
            "triage_confirmed_by,status,conv_id,idem_key) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, slot_id, slot["dept"], symptom_summary, recommended_dept,
             triage_confirmed_by, status, conv_id, idempotency_key))
        aid = cur.lastrowid
    return _ok({"appointment_id": aid, "status": status,
                "dept": slot["dept"], "doctor": slot["doctor"],
                "date": slot["slot_date"], "time": slot["slot_time"],
                "notice": "预约已提交，等待分诊台人工确认后生效" if status == "pending_triage"
                          else "预约已确认"},
               "预约创建成功")


def create_reimbursement_draft(visit_type: str, amount: float, invoice_type: str,
                               invoice_no: str, visit_date: str,
                               eligibility_token: str, idempotency_key: str = "",
                               user_id: str = "", conv_id: str = "", **kw):
    """创建报销草稿。必须携带有效的 eligibility_token，否则拒绝。"""
    if not eligibility_token:
        return _fail(4003, "缺少校验凭证，请先调用 check_reimbursement_eligibility 完成校验")

    current = {"visit_type": visit_type, "amount": float(amount) if amount else 0,
               "invoice_type": invoice_type}
    passed, reason = consume_token(eligibility_token, user_id, current)
    if not passed:
        return _fail(4003, reason)

    # 服务层再跑一遍规则校验（防止令牌快照与提交内容在非关键字段上不一致）
    if visit_type not in REIMB_POLICY:
        return _fail(4003, "就诊类型不合法")
    rule = REIMB_POLICY[visit_type]
    reimb = round(max(0.0, (float(amount) - rule["deductible"])) * rule["rate"], 2)

    with _lock, db() as c:
        exist = c.execute("SELECT * FROM reimbursement_drafts WHERE idem_key=?",
                          (idempotency_key,)).fetchone() if idempotency_key else None
        if exist:
            return _ok({"draft_id": exist["id"], "duplicated": True}, "该报销单已创建")
        cur = c.execute(
            "INSERT INTO reimbursement_drafts(user_id,visit_type,amount,invoice_type,"
            "invoice_no,visit_date,status,reimb_amount,conv_id,idem_key) "
            "VALUES (?,?,?,?,?,?,'draft',?,?,?)",
            (user_id, visit_type, float(amount), invoice_type, invoice_no, visit_date,
             reimb, conv_id, idempotency_key or None))
        did = cur.lastrowid
    return _ok({"draft_id": did, "status": "draft", "reimbursable_amount": reimb,
                "notice": "报销草稿已生成，可在系统中补充发票原件后提交审批"}, "报销草稿创建成功")


# ============================================================
# 工具注册表：OpenAI Function Calling 规范
# ============================================================
TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "search_policy",
        "description": "检索门诊就诊须知、分诊规则、报销制度等制度文档。回答任何制度类问题前必须先调用本工具。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索关键词或员工原话"},
            "category": {"type": "string", "enum": ["就诊须知", "分诊规则", "报销制度", "预约规则"],
                         "description": "可选，限定文档分类"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "list_available_slots",
        "description": "查询指定科室的可预约号源。创建预约前必须先调用本工具获取真实 slot_id。",
        "parameters": {"type": "object", "properties": {
            "dept": {"type": "string", "description": "科室名：全科/内科/外科/皮肤科/眼科/儿科"},
            "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD"}},
            "required": ["dept"]}}},
    {"type": "function", "function": {
        "name": "get_my_appointments",
        "description": "查询当前员工的预约记录。",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["upcoming", "history", "all"]}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_my_reimbursements",
        "description": "查询当前员工的报销单记录。",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["all", "draft", "submitted", "approved", "rejected"]}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "check_reimbursement_eligibility",
        "description": "报销前必调。校验就诊类型、金额、发票类型与抬头、起付线、年度额度是否满足制度。passed=true 才返回 eligibility_token，凭此才能创建报销单。",
        "parameters": {"type": "object", "properties": {
            "visit_type": {"type": "string", "enum": ["门诊", "体检", "住院", "急诊"]},
            "amount": {"type": "number", "description": "发票金额（元）"},
            "invoice_type": {"type": "string", "enum": ["电子发票", "纸质发票", "卷式发票"]},
            "invoice_title": {"type": "string", "description": "发票抬头"},
            "invoice_date": {"type": "string", "description": "开票日期 YYYY-MM-DD"},
            "has_prescription": {"type": "boolean", "description": "是否有处方（药品费必填）"}},
            "required": ["visit_type", "amount", "invoice_type", "invoice_title", "invoice_date"]}}},
    {"type": "function", "function": {
        "name": "check_slot_availability",
        "description": "号源二次确认。创建预约前必须调用，确认该时段仍有号。",
        "parameters": {"type": "object", "properties": {
            "slot_id": {"type": "string", "description": "list_available_slots 返回的 slot_id"}},
            "required": ["slot_id"]}}},
    {"type": "function", "function": {
        "name": "create_appointment",
        "description": "创建预约单。必须已完成 list_available_slots 与 check_slot_availability。AI 推荐场景 triage_confirmed_by 传 null。",
        "parameters": {"type": "object", "properties": {
            "slot_id": {"type": "string"},
            "symptom_summary": {"type": "string", "description": "员工自述症状原文"},
            "recommended_dept": {"type": "string"},
            "triage_confirmed_by": {"type": "string", "description": "分诊确认人，AI 场景固定 null"},
            "idempotency_key": {"type": "string", "description": "幂等键，同一意图复用同一值"}},
            "required": ["slot_id", "symptom_summary", "recommended_dept", "idempotency_key"]}}},
    {"type": "function", "function": {
        "name": "create_reimbursement_draft",
        "description": "创建报销草稿。必须携带 check_reimbursement_eligibility 返回的 eligibility_token，否则会被拒绝。",
        "parameters": {"type": "object", "properties": {
            "visit_type": {"type": "string", "enum": ["门诊", "体检", "住院", "急诊"]},
            "amount": {"type": "number"},
            "invoice_type": {"type": "string", "enum": ["电子发票", "纸质发票", "卷式发票"]},
            "invoice_no": {"type": "string"},
            "visit_date": {"type": "string"},
            "eligibility_token": {"type": "string", "description": "必填，校验通过后的一次性令牌"},
            "idempotency_key": {"type": "string"}},
            "required": ["visit_type", "amount", "invoice_type", "invoice_no",
                         "visit_date", "eligibility_token", "idempotency_key"]}}},
]

# 工具名 → 执行函数
TOOL_FUNCS = {
    "search_policy": search_policy,
    "list_available_slots": list_available_slots,
    "get_my_appointments": get_my_appointments,
    "get_my_reimbursements": get_my_reimbursements,
    "check_reimbursement_eligibility": check_reimbursement_eligibility,
    "check_slot_availability": check_slot_availability,
    "create_appointment": create_appointment,
    "create_reimbursement_draft": create_reimbursement_draft,
}

# 写操作工具（用于编排层统计与限制）
WRITE_TOOLS = {"create_appointment", "create_reimbursement_draft"}


def execute_tool(name: str, arguments: dict, user_id: str, conv_id: str = "") -> dict:
    """统一调度入口：注入用户上下文，捕获异常转成结构化错误。"""
    func = TOOL_FUNCS.get(name)
    if not func:
        return _fail(6900, f"未知工具：{name}")
    try:
        return func(user_id=user_id, conv_id=conv_id, **(arguments or {}))
    except TypeError as e:
        return _fail(6901, f"工具参数不正确：{e}")
    except Exception as e:
        return _fail(6999, f"工具执行异常：{type(e).__name__}")
