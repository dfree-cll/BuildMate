"""ReviewEngine：审查引擎（HR-001~007 硬规则 + LLM 软审查）

薄封装 drawing2bim/compliance 的规则检查与 LLM 审查节点。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ReviewEngine:
    """审查引擎——统一入口：硬规则（HR-001~007）+ LLM 软审查"""

    @staticmethod
    def hard_review(elements: list[dict], source: str = "drawing") -> list[dict]:
        """硬规则审查（HR-001~007——确定性规则）——返回违规列表"""
        from backend.agents.drawing2bim.nodes.compliance import hard_track_review
        viols = hard_track_review(elements, source=source)
        return [v.to_dict() if hasattr(v, "to_dict") else vars(v) for v in viols]

    @staticmethod
    async def soft_review(elements: list[dict], regulation_context: str = "") -> list[dict]:
        """LLM 软审查（规则解释/分类——自适应）"""
        from backend.agents.drawing2bim.nodes.compliance import soft_track_review
        viols = await soft_track_review(elements, regulation_context=regulation_context)
        return [v.to_dict() if hasattr(v, "to_dict") else vars(v) for v in viols]

    @staticmethod
    def inject_review(data: dict, violations: list[dict]) -> dict:
        """审查注入（Review Injection）：违规按 ifc_guid/name 匹配——加 review 字段

        violations: [{"element_id"/"name", "rule_id", "severity", "description"}]
        返回注入后的 data（model_elements 带 review 字段——"带违规标注的模型"）
        """
        if not violations:
            return data
        # 建立匹配索引（guid -> violation 列表, name -> violation 列表）
        by_guid: dict[str, list[dict]] = {}
        by_name: dict[str, list[dict]] = {}
        for v in violations:
            eid = str(v.get("element_id") or "")
            name = str(v.get("name") or "")
            item = {
                "rule": v.get("rule_id", "llm_soft"),
                "severity": v.get("severity", "warning"),
                "desc": v.get("description", ""),
                "track": v.get("track", "hard"),
            }
            if eid:
                by_guid.setdefault(eid, []).append(item)
            if name:
                by_name.setdefault(name, []).append(item)
        injected = 0
        for e in data.get("model_elements", []):
            matches = []
            g = e.get("ifc_guid")
            if g and g in by_guid:
                matches.extend(by_guid[g])
            nm = e.get("name")
            if nm and nm in by_name:
                matches.extend(by_name[nm])
            if matches:
                e["review"] = matches[:5]
                injected += 1
        logger.info("review.injected: %d/%d elements", injected, len(data.get("model_elements", [])))
        return data

    @staticmethod
    async def review(elements: list[dict], source: str = "drawing",
                     regulation_context: str = "") -> dict:
        """完整审查：硬规则 + LLM 软审查——合并结果"""
        hard = ReviewEngine.hard_review(elements, source=source)
        try:
            soft = await ReviewEngine.soft_review(elements, regulation_context=regulation_context)
        except Exception as ex:
            logger.warning("review.soft_failed: %s", str(ex)[:100])
            soft = []
        return {
            "hard_violations": hard,
            "soft_violations": soft,
            "total": len(hard) + len(soft),
        }
