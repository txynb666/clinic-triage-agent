# -*- coding: utf-8 -*-
"""
tests/test_tools_constraints.py —— 工具层约束回归测试
=====================================================
不依赖 LLM，只验证「顺序约束 / 令牌防重放 / 参数一致性 / 并发不超约」四类硬约束。
运行：pytest tests/ -v
"""

import sys
import pathlib
import pytest

# 让测试能 import app 包
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.services import tools
from app.repositories import admin_db, agent_trace
from app.core.config import COMPANY_TITLE

USER = "test_user"


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    """初始化全部表（幂等）。"""
    from app.repositories import schema
    schema.init_all()


@pytest.fixture()
def slot():
    """取一个可用号源，供预约类用例使用。"""
    r = tools.list_available_slots("全科", user_id=USER)
    assert r["ok"] and r["data"]["slots"], "演示号源应存在"
    return r["data"]["slots"][0]


# ============ 1. 报销：无校验令牌必须被拒 ============
def test_create_draft_without_token_rejected():
    r = tools.create_reimbursement_draft(
        "门诊", 300, "电子发票", "INV-NOTOKEN", "2026-10-28",
        eligibility_token="", idempotency_key="idem-notoken", user_id=USER)
    assert r["ok"] is False
    assert r["code"] == 4003
    assert "校验凭证" in r["message"]


# ============ 2. 校验不通过不签发令牌 ============
def test_ineligibility_returns_no_token():
    r = tools.check_reimbursement_eligibility(
        "门诊", 300, "电子发票", "个人", "2026-10-28", user_id=USER)
    assert r["data"]["passed"] is False
    assert "eligibility_token" not in r["data"]
    assert any("抬头" in x for x in r["data"]["reasons"])


# ============ 3. 校验通过 → 建单成功 ============
def test_valid_flow_creates_draft():
    c = tools.check_reimbursement_eligibility(
        "门诊", 300, "电子发票", COMPANY_TITLE, "2026-10-28", user_id=USER)
    assert c["data"]["passed"] is True
    assert c["data"]["reimbursable_amount"] == 200.0   # (300-50)*80%
    tok = c["data"]["eligibility_token"]

    d = tools.create_reimbursement_draft(
        "门诊", 300, "电子发票", "INV-OK", "2026-10-28", tok,
        "idem-ok-1", user_id=USER)
    assert d["ok"] is True
    assert d["data"]["status"] == "draft"


# ============ 4. 令牌一次性，重放被拒 ============
def test_token_replay_rejected():
    c = tools.check_reimbursement_eligibility(
        "门诊", 260, "电子发票", COMPANY_TITLE, "2026-10-28", user_id=USER)
    tok = c["data"]["eligibility_token"]
    first = tools.create_reimbursement_draft(
        "门诊", 260, "电子发票", "INV-R1", "2026-10-28", tok, "idem-r1", user_id=USER)
    assert first["ok"] is True

    second = tools.create_reimbursement_draft(
        "门诊", 260, "电子发票", "INV-R1", "2026-10-28", tok, "idem-r2", user_id=USER)
    assert second["code"] == 4003
    assert "已被使用" in second["message"]


# ============ 5. 篡改金额被参数一致性拦截 ============
def test_tampered_amount_rejected():
    c = tools.check_reimbursement_eligibility(
        "门诊", 100, "电子发票", COMPANY_TITLE, "2026-10-28", user_id=USER)
    tok = c["data"]["eligibility_token"]
    r = tools.create_reimbursement_draft(
        "门诊", 9999, "电子发票", "INV-T", "2026-10-28", tok, "idem-t", user_id=USER)
    assert r["code"] == 4003
    assert "不一致" in r["message"]


# ============ 6. 起付线以下不通过 ============
def test_below_deductible():
    r = tools.check_reimbursement_eligibility(
        "门诊", 30, "电子发票", COMPANY_TITLE, "2026-10-28", user_id=USER)
    assert r["data"]["passed"] is False
    assert any("起付线" in x for x in r["data"]["reasons"])


# ============ 7. AI 建预约强制 pending_triage ============
def test_ai_appointment_requires_human_confirm(slot):
    r = tools.create_appointment(
        slot["slot_id"], "发烧38.9度", "全科",
        None, "idem-appt-ai", user_id=USER)
    assert r["ok"] is True
    assert r["data"]["status"] == "pending_triage"


# ============ 8. 幂等键防重复建单 ============
def test_appointment_idempotent(slot):
    a = tools.create_appointment(slot["slot_id"], "复诊", "全科", None,
                                 "idem-dup", user_id=USER)
    b = tools.create_appointment(slot["slot_id"], "复诊", "全科", None,
                                 "idem-dup", user_id=USER)
    assert a["ok"] and b["ok"]
    assert b["data"].get("duplicated") is True
    assert a["data"]["appointment_id"] == b["data"]["appointment_id"]


# ============ 9. 号源约满后不可再约 ============
def test_slot_capacity_not_exceeded():
    # 取一个剩余量小的号源，用满它
    r = tools.list_available_slots("皮肤科", user_id=USER)
    s = r["data"]["slots"][0]
    remain = s["remain"]
    for i in range(remain):
        tools.create_appointment(s["slot_id"], "test", "皮肤科",
                                 "分诊台", f"idem-cap-{i}", user_id=f"u{i}")
    after = tools.check_slot_availability(s["slot_id"], user_id=USER)
    assert after["data"]["available"] is False
