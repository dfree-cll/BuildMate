"""drawing2bim HITL 卡点节点（对标 procurement 的 interrupt/resume 范式）

触发条件（对应架构图 O 节点"关键字段/低置信度 人工确认?"）：
- verdict == pass → 直接放行，不打断
- 存在硬轨违规 或 低置信度软轨条目 → interrupt 冻结等待人工确认

人工决策格式（Command(resume=...)）：
{"decision": "approved" | "rejected", "comment": "...", "accepted_item_ids": [...]}
"""
from collections import OrderedDict

from langgraph.types import interrupt

from backend.agents.drawing2bim.state import Drawing2BimState
from backend.agents.drawing2bim.nodes.report_merge import LOW_CONFIDENCE_THRESHOLD
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 聚合条目示例上限（同规则违规不再逐条刷屏，只展示代表性构件）
AGG_EXAMPLE_LIMIT = 5


def aggregate_violations(violations: list[dict]) -> list[dict]:
    """纯函数：按 (rule_id, description) 聚合违规 → 可读条目

    每类违规输出一条：{rule_id, severity, track, description, affected(影响数),
    examples(前 N 个构件 GlobalId)}——替代旧的逐构件刷屏（1900 面墙报 1900 条）。
    """
    agg: OrderedDict = OrderedDict()
    for v in violations:
        key = (v.get("rule_id"), v.get("description"))
        if key not in agg:
            agg[key] = {
                "rule_id": v.get("rule_id"),
                "severity": v.get("severity"),
                "track": v.get("track"),
                "description": v.get("description"),
                "affected": 0,
                "examples": [],
            }
        agg[key]["affected"] += 1
        eid = v.get("element_id", "")
        if eid and len(agg[key]["examples"]) < AGG_EXAMPLE_LIMIT:
            agg[key]["examples"].append(eid)
    return list(agg.values())


async def hitl_review_node(state: Drawing2BimState) -> dict:
    """人工确认卡点：低风险直接通过；否则 interrupt 等待人工决策

    待确认条目按规则聚合（避免上千条同规则违规逐条展示）。
    """
    merged = state.get("merged_report", {})
    verdict = merged.get("verdict", "pass")
    gate_report = state.get("geometry_report") or {}

    # ★ 质量闸门冻结: 闸门失败(轴网不完整/提取异常) → 必须人工确认, 不能直接放行
    gate_blocked = state.get("gate_blocked") or gate_report.get("gate_blocked")
    gate_checks = [c for c in (gate_report.get("checks") or [])
                   if c.get("severity") in ("critical", "warning")]

    # 无违规无待确认且闸门通过 → 直接放行（零人工介入路径）
    if verdict == "pass" and not gate_blocked and not gate_checks:
        logger.info("hitl.auto_passed")
        return {"hitl_required": False, "hitl_decision": "approved",
                "hitl_items": []}

    # 组装待确认条目：硬轨全部 + 软轨低置信度条目 + 闸门异常, 按规则聚合
    raw_items = []
    for v in merged.get("violations", []):
        need_confirm = (v.get("track") == "hard" or
                        v.get("confidence", 1.0) < LOW_CONFIDENCE_THRESHOLD)
        if need_confirm:
            raw_items.append(v)
    for gc in gate_checks:
        raw_items.append({
            "rule_id": gc.get("rule_id", "GQ-000"),
            "severity": gc.get("severity", "warning"),
            "track": "hard",
            "description": "[质量闸门] " + gc.get("description", ""),
            "confidence": 1.0,
        })
    hitl_items = aggregate_violations(raw_items)

    display_data = {
        "baseline_version": state.get("baseline_version", 0),
        "risk_level": merged.get("risk_level"),
        "hard_count": merged.get("hard_count", 0),
        "low_confidence_count": merged.get("low_confidence_count", 0),
        "gate_blocked": bool(gate_blocked),
        "items": hitl_items,
    }

    # ← 冻结在此，等待 Command(resume=decision_dict)
    decision = interrupt(display_data)

    final_decision = (decision or {}).get("decision", "rejected")
    logger.info("hitl.resumed", decision=final_decision,
                items=len(raw_items), agg_items=len(hitl_items))
    return {
        "hitl_required": True,
        "hitl_decision": final_decision,
        "hitl_items": hitl_items,
        "merged_report": {**merged, "human_comment": (decision or {}).get("comment", "")},
    }
