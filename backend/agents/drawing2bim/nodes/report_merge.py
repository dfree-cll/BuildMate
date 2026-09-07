"""drawing2bim 报告融合器节点

融合硬轨（确定性违规）+ 软轨（LLM 疑似问题）：
- 硬轨 critical 强制置顶（LLM 不得吞掉确定性缺陷，对标 bim_review 规则）
- 软轨低置信度条目（< 阈值）标记为需人工确认
- 生成 merged_report：统一清单 + 风险定级 + verdict
"""
from backend.agents.drawing2bim.state import Drawing2BimState
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 软轨置信度低于该阈值 → 进入 HITL 待确认
LOW_CONFIDENCE_THRESHOLD = 0.6


def merge_reports(hard_violations: list[dict], soft_violations: list[dict]) -> dict:
    """纯函数：融合双轨结果（便于单测）"""
    # 硬轨按 severity 排序：critical > warning > info
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    hard_sorted = sorted(hard_violations, key=lambda v: severity_order.get(v.get("severity"), 9))
    soft_sorted = sorted(soft_violations, key=lambda v: severity_order.get(v.get("severity"), 9))

    hard_critical = [v for v in hard_sorted if v.get("severity") == "critical"]
    low_confidence = [v for v in soft_sorted if v.get("confidence", 1.0) < LOW_CONFIDENCE_THRESHOLD]

    # 风险定级：硬轨 critical 一票定 high；否则按两轨最高 severity
    if hard_critical:
        risk_level = "high"
    elif any(v.get("severity") == "warning" for v in hard_sorted + soft_sorted):
        risk_level = "medium"
    else:
        risk_level = "low"

    # verdict：有硬轨违规 → review；仅软轨 → 低置信度条目存在时 review，否则 pass
    if hard_sorted:
        verdict = "review"
    elif low_confidence:
        verdict = "review"
    else:
        verdict = "pass"

    return {
        "violations": hard_sorted + soft_sorted,
        "hard_count": len(hard_sorted),
        "soft_count": len(soft_sorted),
        "hard_critical_count": len(hard_critical),
        "low_confidence_count": len(low_confidence),
        "low_confidence_items": low_confidence,
        "risk_level": risk_level,
        "verdict": verdict,
    }


async def report_merge_node(state: Drawing2BimState) -> dict:
    """报告融合节点"""
    hard = state.get("hard_violations", [])
    soft = state.get("soft_violations", [])
    merged = merge_reports(hard, soft)
    logger.info("report_merge.done",
                hard=merged["hard_count"], soft=merged["soft_count"],
                risk=merged["risk_level"], verdict=merged["verdict"])
    return {"merged_report": merged}
