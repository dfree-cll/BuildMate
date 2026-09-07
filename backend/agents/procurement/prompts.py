"""采购审批 Agent 提示词"""

# LLM 审查：合规性 + 合理性（多品类 + 行情参考）
LLM_REVIEW_PROMPT = """你是采购审核专家。请审查以下采购单（可含多个品类），输出 JSON：
{{"compliance_issues": ["合规问题1"], "reasonableness": "价格/数量合理性评价", "suggestion": "建议", "verdict": "pass/review/reject", "reason": "判断依据"}}

审核规则：
- 单价明显高于市场行情参考价（如偏离 >30%）→ review
- 数量明显超需求 → review
- 供应商资质不明或未提供 → review
- 低金额品类（单品类 <1 万元）且无明显问题 → pass（可快速通过）
- 高金额品类（>=10 万元）→ review（需人工确认）

市场行情参考：
{market_text}

采购品类明细：
{items_text}

总金额：{total_amount} 元
"""

APPROVAL_PROMPT = """请生成采购审批的最终批复文案。

AI 审核结论：{ai_conclusion}
人工审批决定：{decision}（{comment}）

输出一段简短的审批结果说明。"""
