"""drawing2bim 最终输出节点

产出：
- final_baseline：最终黄金基准（构件标注审查痕迹 review_status）
- compliance_report：最终合规报告（附人工决策）
- content / structured_output：给用户的摘要与结构化输出
"""
from backend.agents.drawing2bim.state import Drawing2BimState
from backend.core.logger import get_logger

logger = get_logger(__name__)


async def finalize_node(state: Drawing2BimState) -> dict:
    """收尾节点：组装最终基准与合规报告"""
    merged = state.get("merged_report", {})
    hitl_decision = state.get("hitl_decision", "approved")
    baseline = state.get("golden_baseline", [])

    # 审查痕迹写回基准：被硬轨点名的构件标记 needs_fix，其余 auto_passed
    hard_element_ids = {v.get("element_id") for v in state.get("hard_violations", [])}
    final_baseline = []
    for elem in baseline:
        elem = dict(elem)
        if elem.get("element_id") in hard_element_ids:
            elem["review_status"] = "needs_fix"
        else:
            elem["review_status"] = "auto_passed"
        final_baseline.append(elem)

    verdict = merged.get("verdict", "pass")
    # 人工决策覆盖机器判定（对标采购审批：approved/rejected 以人工为准，
    # 违规清单仍完整保留在报告中供修正参考）
    if state.get("hitl_required"):
        verdict = "approved" if hitl_decision == "approved" else "rejected"

    compliance_report = {
        "verdict": verdict,
        "risk_level": merged.get("risk_level", "low"),
        "hard_violations": state.get("hard_violations", []),
        "soft_violations": state.get("soft_violations", []),
        "human_decision": hitl_decision,
        "human_comment": merged.get("human_comment", ""),
        "baseline_version": state.get("baseline_version", 0),
    }

    needs_fix = sum(1 for e in final_baseline if e["review_status"] == "needs_fix")
    content = (
        f"合规审查完成：基准构件 {len(final_baseline)} 个，"
        f"硬轨违规 {merged.get('hard_count', 0)} 条、"
        f"软轨疑似 {merged.get('soft_count', 0)} 条，"
        f"需修正构件 {needs_fix} 个。"
        f"综合判定：{verdict}（风险等级 {compliance_report['risk_level']}）。"
    )
    if hitl_decision == "rejected":
        content += " 人工审查已驳回本次基准，请修正后重新提交。"

    logger.info("finalize.done", verdict=verdict, elements=len(final_baseline))
    return {
        "final_baseline": final_baseline,
        "compliance_report": compliance_report,
        "content": content,
        "fallback_used": False,
        "structured_output": {
            "verdict": verdict,
            "baseline_version": state.get("baseline_version", 0),
            "elements_total": len(final_baseline),
            "elements_needs_fix": needs_fix,
            "hard_violations": len(state.get("hard_violations", [])),
            "soft_violations": len(state.get("soft_violations", [])),
        },
    }
