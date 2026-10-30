# Agent 执行轨迹示例（阶段 7）

> 本文档对应 `harness.py` 的执行循环，演示 3 个典型对话的**预期决策轨迹**。
> 因系统主题为**门诊预约与分诊协作**，以下示例由「差旅报销」同构改写为门诊场景，工具链与约束逻辑完全一致。

---

## 一、典型轨迹 a：只查制度，不动业务工具

**用户问**：`我的门诊费能报多少？报销比例是多少？`

```
轮1  模型决策 ──► 调用 search_policy({query:"门诊报销比例"})
                 ├─ 执行 12ms
                 └─ 返回 3 条片段（《就诊须知.md》三、门诊报销制度）

轮2  模型决策 ──► 不调工具，直接作答（终止）
                 └─ "根据《就诊须知.md》第三条：门诊报销比例一级医院90%…起付线50元/次，年度上限5000元。"
```

**日志表记录**（`agent_steps`）：
| 轮次 | step_type | 工具 | 耗时 | 结果摘要 |
|---|---|---|---|---|
| 0 | decision | - | - | - |
| 1 | tool | search_policy | 277ms | 知识库初始化中… |
| 2 | final | - | - | 最终回答 62 字 |

**关键约束体现**：只调 `search_policy`，**不触碰** `list_available_slots`、`check_*`、`create_*`。
这正是提示词第 1 条「制度类问题必须先 search_policy」的作用——模型自然会选最少工具路径。

---

## 二、典型轨迹 b：完整报销流程（含强制中断确认）

**用户问**：`帮我报销上周的门诊费300元，电子发票，抬头是XX科技有限公司`

```
轮1  模型决策 ──► search_policy({query:"门诊报销比例 发票要求"})
                 └─ 拿到制度条文（比例/起付线/抬头要求）

轮2  模型决策 ──► check_reimbursement_eligibility({
                     visit_type:"门诊", amount:300, invoice_type:"电子发票",
                     invoice_title:"XX科技有限公司", invoice_date:"2026-10-28"})
                 ├─ 执行 16ms
                 └─ passed=true, reimbursable_amount=200.0, 签发 eligibility_token

轮3  模型决策 ──► create_reimbursement_draft({...业务字段...})
                 │
                 ⏸ ★ 强制中断（harness.py: CONFIRM_REQUIRED）
                 ├─ 工具【未执行】，参数先补全（绑定 token + 幂等键）
                 ├─ 落库 pending_actions，状态 pending
                 └─ SSE 推 confirm 事件 → 前端渲染草稿卡片：
                     就诊类型=门诊 / 发票号码=INV2026 / 发票金额=¥300.00
                     起付线=¥50.00 / 报销比例=80% / 预估可报=¥200.00
                     [ 确认生成草稿 ]  [ 取消 ]

--- 员工点击「确认」--- POST /chat/confirm {token, action:"confirm"}

恢复  harness.resume(token) ──► create_reimbursement_draft 真正执行
                 ├─ consume_token() 验签 + 一次性消费
                 ├─ 落库 reimbursement_drafts（status=draft）
                 └─ 模型收尾："报销草稿已生成，请在 5 个工作日内补充发票原件…"
```

**日志表记录**：
| 轮次 | step_type | 工具 | 耗时 | 结果摘要 |
|---|---|---|---|---|
| 0 | decision | - | - | - |
| 1 | tool | search_policy | 175ms | 知识库初始化中… |
| 2 | tool | check_reimbursement_eligibility | 16ms | 成功 `passed=True reimbursable_amount=200.0` |
| 3 | **interrupt** | create_reimbursement_draft | - | 已中断，等待员工确认草稿 |
| 99 | tool | create_reimbursement_draft | 3ms | 报销草稿创建成功 `draft_id=1` |
| 99 | final | - | - | 确认后收尾完成 |

**若员工点「取消」**：`pending_actions.status = rejected`，动作永不执行，DB 无草稿。
**若 10 分钟不操作**：`status = expired`，恢复时返回「确认已超时」。

---

## 三、典型轨迹 c：单张发票能不能报（只校验，不建单）

**用户问**：`这张发票抬头写的个人，能报吗？金额30元`

```
轮1  模型决策 ──► check_reimbursement_eligibility({
                     visit_type:"门诊", amount:30, invoice_type:"电子发票",
                     invoice_title:"个人", invoice_date:"2026-10-28"})
                 └─ passed=false
                    reasons: ["发票抬头须为公司全称「XX科技有限公司」，当前为「个人」",
                              "门诊起付线为 50 元，本次金额未达起付线"]
                    不签发令牌

轮2  模型决策 ──► 不调工具，直接给结论（终止）
                 └─ "不能报销，两个原因：①发票抬头须为公司全称；②30元未达起付线50元。
                     建议联系医院重开发票…"
```

**日志表记录**：
| 轮次 | step_type | 工具 | 结果摘要 |
|---|---|---|---|
| 0 | decision | - | - |
| 1 | tool | check_reimbursement_eligibility | 成功 `passed=False reimbursable_amount=0 reasons=...` |
| 2 | final | - | 最终回答 51 字 |

**关键约束体现**：**没有令牌 → 不可能建单**。即使模型下一轮擅自调 `create_reimbursement_draft`，
也会因无有效令牌被工具层以 `4003` 拒绝（此场景已单独测试验证）。

---

## 四、轮数上限（5 轮）行为

若模型陷入反复查询（如一直调 `list_available_slots`）：

```
轮1~轮5  每轮都调用工具，日志各记一条 tool 记录
轮5 后   不再进入第 6 轮
         ├─ 日志记一条 error: "超过 5 轮上限，已中止"
         └─ 回答："本次处理步骤已达 5 轮上限，已为您暂停。请补充更具体的信息后重试。"
         SSE done 事件带 truncated=true
```

---

## 五、阶段 5 分析可用的 SQL

```sql
-- 各工具平均耗时与失败率
SELECT tool_name,
       COUNT(*) AS calls,
       ROUND(AVG(duration_ms), 1) AS avg_ms,
       SUM(CASE WHEN result_ok = 0 THEN 1 ELSE 0 END) AS failed,
       ROUND(100.0 * SUM(CASE WHEN result_ok = 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS fail_rate
FROM agent_steps WHERE step_type = 'tool'
GROUP BY tool_name ORDER BY calls DESC;

-- 平均决策轮数（判断模型是否啰嗦）
SELECT ROUND(AVG(rounds), 2) AS avg_rounds FROM (
  SELECT run_id, MAX(round_no) AS rounds FROM agent_steps GROUP BY run_id
);

-- 中断确认的转化率（多少草稿被真正执行）
SELECT status, COUNT(*) FROM pending_actions GROUP BY status;

-- 卡点定位：结果失败的步骤明细
SELECT run_id, round_no, tool_name, result_code, result_summary
FROM agent_steps WHERE result_ok = 0 AND step_type = 'tool'
ORDER BY id DESC LIMIT 50;
```
