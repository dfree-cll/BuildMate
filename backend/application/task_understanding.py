"""Deterministic task understanding and bounded decomposition.

The LLM may later propose a plan, but this boundary only returns registered
routes and never creates tasks, reads documents, or executes tools.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from backend.application.intent_router import classify_intent
from backend.domain.contracts import EvidenceRef, RequestContext
from backend.domain.errors import ValidationFailure
from backend.domain.intent import IntentRoute
from backend.domain.task_plan import FactValue, TaskDomain, TaskHandoffPackage, TaskPlan, TaskStep

_BOUNDARY = re.compile(r"(?:然后|并且|接着|最后|之后|同时|[，,；;\n])")
_DOMAIN_BY_INTENT: dict[str, TaskDomain] = {
    "building_standard_query": "knowledge",
    "general_question": "knowledge",
    "bid_query": "bid",
    "contract_review": "contract",
    "procurement_query": "procurement",
    "negotiation_query": "negotiation",
    "bim_command": "bim",
}


def _segments(query: str) -> list[str]:
    return [part.strip(" \t。！？.!?") for part in _BOUNDARY.split(query) if part.strip(" \t。！？.!?")]


def understand_task(
    query: str,
    *,
    project_id: str | None = None,
    artifact_ids: Iterable[str] = (),
) -> TaskPlan:
    """Build a bounded plan from registered intent routes only."""

    clean = query.strip()
    parts = _segments(clean)
    if not parts:
        raise ValidationFailure("task query cannot be blank")
    if len(parts) > 20:
        raise ValidationFailure("task plan exceeds 20 clauses; split the request into smaller tasks")
    artifacts = tuple(artifact_ids)
    routes = [classify_intent(part, project_id=project_id, artifact_ids=artifacts) for part in parts]
    # Combine adjacent requirements of one domain without losing their text.
    unique: list[IntentRoute] = []
    for route in routes:
        if unique and (route.intent, route.route) == (unique[-1].intent, unique[-1].route):
            previous = unique[-1]
            unique[-1] = previous.model_copy(update={"parameters": {
                **previous.parameters,
                "query": str(previous.parameters["query"]) + "；" + str(route.parameters["query"]),
            }})
        else:
            unique.append(route)
    steps = [
        TaskStep(
            step=index,
            intent=route.intent,
            domain=_DOMAIN_BY_INTENT[route.intent],
            route=route.route,
            parameters=route.parameters,
            requires_confirmation=route.requires_confirmation,
            missing_parameters=route.missing_parameters,
        )
        for index, route in enumerate(unique, start=1)
    ]
    return TaskPlan(
        mode="composite" if len(steps) > 1 else "single",
        original_query=clean,
        steps=steps,
        needs_confirmation=any(step.requires_confirmation for step in steps),
        needs_clarification=any(step.missing_parameters for step in steps),
    )


def build_handoff(
    context: RequestContext,
    *,
    source_task: str,
    target_domain: TaskDomain,
    facts: dict[str, FactValue] | None = None,
    evidence_refs: list[EvidenceRef] | None = None,
    risk_summary: list[str] | None = None,
) -> TaskHandoffPackage:
    """Create a scope-bound handoff; callers still need target-workflow ACLs."""

    return TaskHandoffPackage.for_context(
        context,
        source_task=source_task,
        target_domain=target_domain,
        facts=facts,
        evidence_refs=evidence_refs,
        risk_summary=risk_summary,
    )
