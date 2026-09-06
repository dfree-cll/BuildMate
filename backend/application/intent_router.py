"""Deterministic v3 intent routing for the question entry.

This module only selects a registered capability.  It never executes a tool,
creates a task, reads a file, or decides a business conclusion.  Those actions
remain behind the existing application services and workflow ACLs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from backend.domain.intent import IntentRoute


_INTENT_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "contract_review",
        ("合同审核", "审核合同", "审合同", "合同条款", "违约条款", "付款条款", "合同"),
    ),
    (
        "bim_command",
        ("revit", "bim", "建模", "生成模型", "图纸转模型", "墙柱模型"),
    ),
    (
        "bid_query",
        ("标书", "投标文件", "招标文件", "投标", "响应文件", "商务标", "技术标"),
    ),
    (
        "negotiation_query",
        ("谈判", "议价", "谈判建议", "谈判纪要", "供应商谈判"),
    ),
    (
        "procurement_query",
        ("采购", "供应商", "报价", "采购申请", "供应商资质"),
    ),
    (
        "building_standard_query",
        ("建筑规范", "设计规范", "规范", "国标", "标准", "条文", "规程"),
    ),
)

_ROUTES: dict[str, tuple[str, bool, tuple[str, ...]]] = {
    "building_standard_query": ("rag.answer", False, ("rag",)),
    "bid_query": ("bid.search", False, ("rag",)),
    "contract_review": (
        "workflow.contract_review", True, ("rag", "structured", "rules")
    ),
    "procurement_query": (
        "workflow.procurement", True, ("rag", "structured", "rules")
    ),
    "negotiation_query": (
        "workflow.negotiation", True, ("rag", "structured", "rules")
    ),
    "bim_command": ("workflow.wall_pipeline", True, ("bim", "rules")),
    "general_question": ("rag.answer", False, ("rag",)),
}

_MIN_CONFIDENCE = 0.55


def _normalise(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower())


def _best_intent(query: str) -> tuple[str, float]:
    normalised = _normalise(query)
    scores: list[tuple[int, int, int, str]] = []
    for priority, (intent, terms) in enumerate(_INTENT_TERMS):
        matched = sum(1 for term in terms if _normalise(term) in normalised)
        if matched:
            # Longer phrases are stronger evidence.  Earlier entries win a
            # tie, keeping contract/BIM commands ahead of generic terms.
            weight = sum(len(_normalise(term)) for term in terms if _normalise(term) in normalised)
            scores.append((matched, weight, priority, intent))
    if not scores:
        # An unclassified question is still safe to send to evidence-first
        # RAG.  It is not a side-effecting workflow and can abstain when the
        # knowledge base has no supporting evidence.
        return "general_question", 0.6
    matched, weight, _priority, intent = max(
        scores, key=lambda item: (item[0], item[1], -item[2])
    )
    # Confidence is an explainable bounded heuristic, not a probability.
    confidence = min(0.98, 0.58 + 0.10 * matched + min(0.20, weight / 100))
    return intent, confidence


def classify_intent(
    query: str,
    *,
    project_id: str | None = None,
    artifact_ids: Iterable[str] = (),
) -> IntentRoute:
    """Return a registered route without producing any side effects."""

    intent, confidence = _best_intent(query)
    route, requires_confirmation, evidence_sources = _ROUTES[intent]
    artifacts = [str(item) for item in artifact_ids if str(item).strip()]
    parameters: dict[str, object] = {
        "query": query.strip(),
        "project_id": project_id,
        "artifact_ids": artifacts,
    }
    missing: list[str] = []
    if intent == "bid_query" and re.search(r"审查|审核|评审|审一下|审一审", query):
        route = "workflow.bid_review"
        requires_confirmation = True
        evidence_sources = ("rag", "structured", "rules")
        if not artifacts:
            missing.append("artifact_ids")
    if intent in {"contract_review", "bim_command"} and not artifacts:
        missing.append("artifact_ids")
    if intent == "bid_query" and not project_id:
        missing.append("project_id")
    if intent == "bim_command" and not project_id:
        missing.append("project_id")

    if confidence < _MIN_CONFIDENCE or missing:
        route = "clarify"
    return IntentRoute(
        intent=intent,
        confidence=confidence,
        parameters=parameters,
        route=route,
        evidence_sources=list(evidence_sources),
        requires_confirmation=requires_confirmation,
        missing_parameters=missing,
    )
