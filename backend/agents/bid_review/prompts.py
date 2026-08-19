"""投标审查 Agent 提示词（对标 EduAgent 4.3，建筑版）"""
EXTRACT_PROMPT = """请从以下投标文件文本中提取结构化信息，输出 JSON：
- project_name: 项目名称
- bidder: 投标人/企业名称
- bid_amount: 投标报价金额（数字，万元）
- technical_solution: 技术方案摘要
- qualifications: 资质信息
- bid_documents: 文件包含的章节列表

投标文件文本：
{doc_text}
"""

# 四维评审维度与权重（对标简历六维度 0.30/0.25/...）
DIMENSIONS = [
    {"dimension": "商务响应", "weight": 0.30,
     "instruction": "评审投标文件的商务部分：报价合理性、投标有效期、付款条件、履约保函等商务条款的完整性"},
    {"dimension": "技术方案", "weight": 0.35,
     "instruction": "评审技术方案：施工组织设计、工艺工法、工期安排、质量保证措施的技术先进性与可行性"},
    {"dimension": "资质业绩", "weight": 0.20,
     "instruction": "评审企业资质与业绩：资质等级是否满足要求、类似项目业绩、项目经理资格"},
    {"dimension": "合规风险", "weight": 0.15,
     "instruction": "识别投标文件的合规风险：废标条款、无效投标情形、响应性偏差、法律风险"},
]

DIMENSION_PROMPT = """你是投标文件评审专家。请针对「{dimension}」维度评审以下投标文件，输出 JSON：
{{"score": 0-100整数, "issues": ["问题1","问题2"], "suggestions": ["建议1","建议2"]}}

评审要求：{instruction}

投标文件文本：
{doc_text}
"""

SUMMARY_PROMPT = """请基于以下四维评审结果，生成投标文件的整体评审结论，输出 JSON：
{{"overall_comment": "综合评语（1-2句）", "risk_level": "high/medium/low", "recommendation": "是否建议投标（建议投标/谨慎投标/不建议投标）"}}

评审结果：
{dimension_results}
"""
