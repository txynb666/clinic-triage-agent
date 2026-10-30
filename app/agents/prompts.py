# -*- coding: utf-8 -*-
"""
agent.py —— 智能体编排层（多轮 Function Calling 循环）
======================================================
职责：
  1. 组装系统提示词（铁律约束，存库可被管理员覆盖）
  2. 驱动 LLM 多轮工具调用：LLM 请求工具 → 执行 → 结果回灌 → 继续
  3. 流式输出最终回答，同时把工具调用轨迹推给前端（可观测）
  4. 硬限制：写工具单轮最多 1 次、总轮数上限，防死循环与批量误建单

对应最终架构的 app/agents/orchestrator.py。
"""

import os
import json

from openai import OpenAI

from app.services import tools
from app import rag

MAX_ROUNDS = 6          # 最多 6 轮工具调用，防止无限循环
MAX_WRITE_PER_TURN = 1  # 单次请求最多 1 个写操作

# ============================================================
# 系统提示词（Agent 版：铁律约束模型不得跳过校验）
# ============================================================
AGENT_SYSTEM_PROMPT = """你是企业门诊预约与分诊协作系统的智能助手，可调用工具协助员工完成制度咨询、预约挂号与报销提交。

【铁律，违反即回答作废】
1. 制度类问题（就诊规则、报销比例、发票要求等）必须先调用 search_policy，
   再依据返回条文作答；检索结果中不存在的内容，回答“制度中未找到”，严禁编造。
2. 报销流程严禁跳步：必须依次执行
   check_reimbursement_eligibility → create_reimbursement_draft。
   未获得 eligibility_token 前不得调用 create_reimbursement_draft；
   若校验返回 passed=false，只能解释原因与整改建议，不得建单、不得改参数重试绕过。
3. 预约流程严禁跳步：必须依次执行
   list_available_slots → check_slot_availability → create_appointment。
   slot_id 只能取自工具返回，不得自行构造。
4. 分诊结果仅为参考推荐，回答中必须包含“以下仅供参考，最终以分诊台医生判断为准”，
   且创建预约时 triage_confirmed_by 传 null，交由人工确认。
5. 每次写操作生成稳定的 idempotency_key（同一意图复用同一键），避免重复建单。
6. 工具返回错误时，直接向用户说明原因，不得自行变通调用其他写工具绕过限制。
7. 不得在一轮对话中连续调用多个写工具。

【回答格式】
先给结论，再列依据（引用《文件名》条款），最后给出下一步建议（如需要用户补充的信息）。
"""


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("LLM_API_KEY", "EMPTY"),
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    )


def get_system_prompt() -> str:
    """优先使用管理员在后台配置的 Agent 提示词，未配置则用内置铁律版。
    存库的提示词若无铁律段，追加铁律，防止管理员误删关键约束导致可跳过校验。"""
    try:
        from app.repositories import admin_db
        base = admin_db.get_active_prompt()
    except Exception:
        base = ""
    if not base:
        return AGENT_SYSTEM_PROMPT
    # 兜底：库里提示词缺失关键约束时自动补齐铁律
    if "严禁跳步" not in base:
        base += "\n\n" + AGENT_SYSTEM_PROMPT
    return base


# ============================================================
# 编排主循环（生成器：yield 事件，供 SSE 转发）
# ============================================================
def run_agent(question: str, history: list, user_id: str, conv_id: str = ""):
    """执行一次智能体推理，yield 事件字典：
       {"type":"status","text":...}       阶段提示
       {"type":"tool","name":...,"args":...,"result":...}  工具调用轨迹
       {"type":"delta","text":...}        回答增量
       {"type":"done"} / {"type":"error","message":...}
    """
    messages = [{"role": "system", "content": get_system_prompt()}]
    messages += [{"role": r, "content": c} for r, c in history]
    messages.append({"role": "user", "content": question})

    write_count = 0
    client = _client()
    model = os.getenv("LLM_MODEL", "deepseek-chat")

    for round_no in range(MAX_ROUNDS):
        yield {"type": "status", "text": "正在思考…" if round_no == 0 else "正在核对结果…"}

        # ---- 调 LLM：带工具定义；首轮要求它决定是否调用工具 ----
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=0.2,
                tools=tools.TOOLS_SPEC, tool_choice="auto")
        except Exception:
            yield {"type": "error", "message": "智能服务暂不可用，请稍后重试"}
            return

        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        # ---- 无工具调用：这就是最终回答，流式补一遍输出 ----
        if not tool_calls:
            final = msg.content or ""
            if final:
                # 按句切片模拟流式，保证前端打字机效果一致
                for i in range(0, len(final), 6):
                    yield {"type": "delta", "text": final[i:i + 6]}
            yield {"type": "done"}
            return

        # ---- 把 assistant 的工具调用意图写回上下文（OpenAI 协议要求） ----
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments}}
                           for tc in tool_calls],
        })

        # ---- 逐个执行工具，结果回灌 ----
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            # 写操作配额：单次请求最多 1 次，超出直接拒绝（防批量误建单）
            if name in tools.WRITE_TOOLS:
                if write_count >= MAX_WRITE_PER_TURN:
                    result = {"ok": False, "code": 4005,
                              "message": "本次请求已执行过写操作，请分开提交",
                              "data": {}}
                    yield {"type": "tool", "name": name, "args": args, "result": result}
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(result, ensure_ascii=False)})
                    continue
                write_count += 1

            # 工具执行前先给前端一个阶段提示（让“正在检索制度…”等状态更自然）
            yield {"type": "status",
                   "text": _status_text(name)}
            result = tools.execute_tool(name, args, user_id=user_id, conv_id=conv_id)
            yield {"type": "tool", "name": name, "args": args, "result": result}
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False)})

    # 超出轮数上限：兜底收尾，避免用户干等
    yield {"type": "delta", "text": "本次处理步骤较多，已为您暂停。请补充更具体的信息后重试。"}
    yield {"type": "done"}


def _status_text(tool_name: str) -> str:
    """工具名 → 面向用户的中文状态文案。"""
    return {
        "search_policy": "正在检索制度…",
        "list_available_slots": "正在查询号源…",
        "check_slot_availability": "正在确认号源余量…",
        "check_reimbursement_eligibility": "正在校验报销资格…",
        "create_appointment": "正在创建预约…",
        "create_reimbursement_draft": "正在生成报销草稿…",
        "get_my_appointments": "正在查询您的预约…",
        "get_my_reimbursements": "正在查询您的报销单…",
    }.get(tool_name, "正在处理…")
