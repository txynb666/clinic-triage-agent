# -*- coding: utf-8 -*-
"""
harness.py —— 智能体执行 Harness（框架选型说明见文件末尾）
==========================================================
为什么自研轻量 Harness 而不上 LangGraph / AutoGen？
  本系统的循环语义非常简单且强约束明确：模型决策 → 执行工具 → 结果回填 → 再决策，
  且必须内建「写操作中断确认」「每步审计落库」「轮数硬上限」三件事。
  重型框架能做的编排我们只需要 200 行，反而引入状态图抽象、版本升级、调试困难等成本。
  因此自研 Harness，接口按「可替换」设计：run() 与 resume() 两个入口，
  未来若换 LangGraph，只需在 harness 内部替换实现，上层 API 不变。

核心能力：
  1. 循环：最多 from app.core.config import MAX_ROUNDS, MAX_WRITE_PER_RUN 轮
  2. 每轮决策与工具执行 → agent_trace 落库（工具名/参数/耗时/结果摘要）
  3. 写入类工具（create_*）执行前强制中断 → 推 confirm 事件 → 等用户确认后 resume
  4. 全程事件流（yield dict），上层转 SSE

对应最终架构的 app/agents/orchestrator.py。
"""

import os
import json
import time
import uuid

from openai import OpenAI

from app.services import tools
from app.repositories import agent_trace
from app.agents import prompts as agent_mod      # 复用其中的系统提示词与状态文案

# 硬上限：最多 MAX_ROUNDS 轮决策（默认 5），单次最多 MAX_WRITE_PER_RUN 个写操作
from app.core.config import MAX_ROUNDS, MAX_WRITE_PER_RUN

# 需要人工确认才能执行的写工具（中断点）
CONFIRM_REQUIRED = {"create_reimbursement_draft"}


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("LLM_API_KEY", "EMPTY"),
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    )


def _model() -> str:
    return os.getenv("LLM_MODEL", "deepseek-chat")


# ============================================================
# 草稿预览构造：把工具入参翻译成员工能看懂的中文草稿
# ============================================================
def build_draft_preview(args: dict, user_id: str) -> dict:
    """生成报销草稿预览卡片数据。金额/比例按制度规则预估，明确标注"以财务复核为准"。"""
    visit_type = args.get("visit_type", "门诊")
    try:
        amount = float(args.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    rule = tools.REIMB_POLICY.get(visit_type, {"rate": 0, "deductible": 0})
    reimb = round(max(0.0, amount - rule["deductible"]) * rule["rate"], 2)
    return {
        "title": "报销草稿确认",
        "fields": [
            {"label": "就诊类型", "value": visit_type},
            {"label": "发票号码", "value": args.get("invoice_no", "-")},
            {"label": "发票金额", "value": f"¥{amount:.2f}"},
            {"label": "发票类型", "value": args.get("invoice_type", "-")},
            {"label": "就诊日期", "value": args.get("visit_date", "-")},
            {"label": "起付线", "value": f"¥{rule['deductible']:.2f}"},
            {"label": "报销比例", "value": f"{int(rule['rate'] * 100)}%"},
            {"label": "预估可报", "value": f"¥{reimb:.2f}"},
        ],
        "notice": "预估金额按制度规则计算，最终以财务复核为准。确认后将生成报销草稿并进入审批流程。",
    }


def _normalize_draft_args(args: dict, user_id: str) -> dict:
    """补全建草稿所需的隐式参数。
    模型往往只传业务字段（类型/金额/发票号…），不一定带上 eligibility_token 与
    idempotency_key。做法：
      - 若模型已给 eligibility_token，则尊重它（由 tools 层验签）
      - 若未给，则服务端反查本次会话最近一次校验通过且未使用的令牌，自动绑定
        （等效于"校验过就能建单"，仍受令牌一次性与参数一致性约束）
      - 缺 idempotency_key 时生成稳定键，避免重复点击造成多单
    """
    args = dict(args or {})
    if not args.get("eligibility_token"):
        token = tools.find_recent_token(user_id)
        if token:
            args["eligibility_token"] = token
    if not args.get("idempotency_key"):
        args["idempotency_key"] = f"draft-{user_id}-{int(time.time())}"
    return args


# ============================================================
# 内部：执行单个工具（含日志与耗时）
# ============================================================
def _exec_tool(run_id, round_no, name, args, user_id, conv_id) -> dict:
    """执行工具并写日志。耗时用 perf_counter 精确到毫秒。"""
    t0 = time.perf_counter()
    result = tools.execute_tool(name, args, user_id=user_id, conv_id=conv_id)
    cost = int((time.perf_counter() - t0) * 1000)
    agent_trace.log_step(run_id, round_no, "tool", conv_id, user_id,
                         tool_name=name, arguments=args, result=result, duration_ms=cost)
    return result


# ============================================================
# 主循环：run()
# ============================================================
def run(question: str, history: list, user_id: str, conv_id: str = ""):
    """执行一次智能体推理。yield 事件字典：
       status / tool / confirm / delta / done / error
    confirm 事件发出后循环挂起（return），等 /chat/confirm 调用 resume() 继续。
    """
    run_id = uuid.uuid4().hex
    messages = [{"role": "system", "content": agent_mod.get_system_prompt()}]
    messages += [{"role": r, "content": c} for r, c in history]
    messages.append({"role": "user", "content": question})

    # 记录本轮决策起点（供分析统计）
    agent_trace.log_step(run_id, 0, "decision", conv_id, user_id,
                         arguments={"question": question})

    client = _client()
    write_count = 0

    for round_no in range(1, MAX_ROUNDS + 1):
        yield {"type": "status", "text": "正在思考…" if round_no == 1 else "正在核对结果…"}

        # ---------- ① 模型决策 ----------
        try:
            resp = client.chat.completions.create(
                model=_model(), messages=messages, temperature=0.2,
                tools=tools.TOOLS_SPEC, tool_choice="auto")
        except Exception:
            agent_trace.log_step(run_id, round_no, "error", conv_id, user_id,
                                 result={"ok": False, "code": 3002, "message": "模型调用失败"})
            yield {"type": "error", "message": "智能服务暂不可用，请稍后重试"}
            return

        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        # ---------- ② 无工具调用 = 最终回答 ----------
        if not tool_calls:
            final = msg.content or ""
            agent_trace.log_step(run_id, round_no, "final", conv_id, user_id,
                                 result={"ok": True, "code": 0,
                                         "message": f"最终回答 {len(final)} 字"})
            # 按 6 字符切片推送，保持前端打字机节奏
            for i in range(0, len(final), 6):
                yield {"type": "delta", "text": final[i:i + 6]}
            yield {"type": "done", "run_id": run_id}
            return

        # 把模型的工具调用意图写回上下文（OpenAI 协议要求）
        messages.append({
            "role": "assistant", "content": msg.content or "",
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments}}
                           for tc in tool_calls],
        })

        # ---------- ③ 逐个执行工具 / 识别中断点 ----------
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            # 写操作配额：一次执行最多 1 个写动作
            if name in tools.WRITE_TOOLS:
                if write_count >= MAX_WRITE_PER_RUN:
                    res = {"ok": False, "code": 4005,
                           "message": "本次请求已执行过写操作，请分开提交", "data": {}}
                    agent_trace.log_step(run_id, round_no, "tool", conv_id, user_id,
                                         tool_name=name, arguments=args, result=res,
                                         duration_ms=0)
                    yield {"type": "tool", "name": name, "args": args, "result": res}
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(res, ensure_ascii=False)})
                    continue
                write_count += 1

            # ★ 强制中断点：建草稿前必须让员工确认
            if name in CONFIRM_REQUIRED:
                # 先补全隐式参数（校验令牌/幂等键），确保确认后能真正落库
                args = _normalize_draft_args(args, user_id)
                preview = build_draft_preview(args, user_id)
                token = agent_trace.create_pending(
                    run_id, name, args, preview, conv_id=conv_id, user_id=user_id)
                agent_trace.log_step(run_id, round_no, "interrupt", conv_id, user_id,
                                     tool_name=name, arguments=args,
                                     result={"ok": True, "code": 0,
                                             "message": "已中断，等待员工确认草稿"})
                # 中断事件：带上确认令牌与预览数据，前端渲染确认卡片
                yield {"type": "confirm", "token": token, "tool": name,
                       "preview": preview, "run_id": run_id}
                # 挂起：不执行工具，直接结束本次生成器；用户确认后走 resume()
                return

            # 普通工具：执行 + 日志 + 结果回填
            yield {"type": "status", "text": agent_mod._status_text(name)}
            result = _exec_tool(run_id, round_no, name, args, user_id, conv_id)
            yield {"type": "tool", "name": name, "args": args, "result": result}
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False)})

    # ---------- ④ 超出轮数上限：兜底收尾 ----------
    agent_trace.log_step(run_id, MAX_ROUNDS, "error", conv_id, user_id,
                         result={"ok": False, "code": 4006,
                                 "message": f"超过 {MAX_ROUNDS} 轮上限，已中止"})
    yield {"type": "delta",
           "text": f"本次处理步骤已达 {MAX_ROUNDS} 轮上限，已为您暂停。请补充更具体的信息后重试。"}
    yield {"type": "done", "run_id": run_id, "truncated": True}


# ============================================================
# 恢复执行：resume()（员工点了"确认"之后）
# ============================================================
def resume(token: str, user_id: str):
    """凭确认令牌继续执行被中断的写操作，并把结果告诉模型收尾。"""
    row, error = agent_trace.take_pending(token, user_id)
    if not row:
        yield {"type": "error", "message": error}
        return

    run_id = row["run_id"]
    name = row["action"]
    args = _normalize_draft_args(json.loads(row["arguments"]), user_id)
    conv_id = row["conv_id"] or ""

    yield {"type": "status", "text": "正在生成报销草稿…"}
    result = _exec_tool(run_id, 99, name, args, user_id, conv_id)  # 99 表示恢复执行
    yield {"type": "tool", "name": name, "args": args, "result": result}

    # 让模型用自然语言收尾（不再给它工具，避免它再做写操作）
    try:
        client = _client()
        prompt = (
            f"员工已确认执行 {name}，工具返回结果：\n"
            f"{json.dumps(result, ensure_ascii=False)}\n\n"
            "请用简洁中文告知员工处理结果与下一步（例如如何补发票、审批时限），不要调用任何工具。"
        )
        resp = client.chat.completions.create(
            model=_model(), temperature=0.2,
            messages=[{"role": "system", "content": agent_mod.get_system_prompt()},
                      {"role": "user", "content": prompt}])
        final = resp.choices[0].message.content or ""
    except Exception:
        # 模型不可用也要把工具结果告诉员工，不能让他两眼一抹黑
        final = ("草稿已生成。" if result.get("ok") else f"生成失败：{result.get('message')}")
    for i in range(0, len(final), 6):
        yield {"type": "delta", "text": final[i:i + 6]}

    agent_trace.log_step(run_id, 99, "final", conv_id, user_id,
                         result={"ok": True, "code": 0, "message": "确认后收尾完成"})
    yield {"type": "done", "run_id": run_id, "committed": True}
