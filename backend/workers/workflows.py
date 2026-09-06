"""Adapters that migrate existing Agents behind the bounded v2 runtime."""

from __future__ import annotations

import json
from pathlib import Path

from backend.application.artifact_service import get_artifact_service
from backend.application.workflow_runtime import ExecutionBudget, WorkflowRuntime, WorkflowStepDefinition
from backend.core.orchestrator import AgentRequest, AgentType, ExecutionMode, get_orchestrator
from backend.domain.contracts import (
    AgentResult,
    EvidenceRef,
    RequestContext,
    ReviewFindingInput,
    TaskEnvelope,
)
from backend.domain.errors import DependencyFailure, ValidationFailure
from backend.rag.parsers import parse_knowledge_file
from backend.adapters.task_repository import TaskRepository


_AGENT_TYPES = {
    "qa": AgentType.QA,
    "bid_review": AgentType.BID_REVIEW,
    "procurement": AgentType.PROCUREMENT,
    "negotiation": AgentType.NEGOTIATION,
    # Internal migration adapter only.  It is intentionally not registered
    # by get_default_runtime and is not a public workflow.
    "drawing_review": AgentType.DRAWING2BIM,
}


async def _load_agent_input(context: RequestContext, envelope: TaskEnvelope) -> tuple[str, dict, list[EvidenceRef]]:
    if not envelope.input_artifact_ids:
        raise ValidationFailure(f"{envelope.workflow} requires at least one input artifact")
    artifact_service = get_artifact_service()
    artifact, path = await artifact_service.resolve(context, envelope.input_artifact_ids[0])
    evidence = [EvidenceRef(kind="artifact", artifact_id=item) for item in envelope.input_artifact_ids]
    suffix = Path(artifact["filename"]).suffix.lower()
    extra: dict = {"project_id": context.project_id, "role": context.role}
    if envelope.workflow == "drawing_review":
        # PDF/DWG/DXF must enter the deterministic WallEvidence → WallModel
        # workflow.  Keep this lower-level guard as defense in depth for any
        # caller that invokes the legacy adapter directly instead of going
        # through drawing_review_prepare_step.
        if suffix in {".pdf", ".dwg", ".dxf"}:
            raise ValidationFailure(
                "legacy Drawing2BIM accepts only IFC/image artifacts; "
                "use wall_pipeline for PDF/DWG/DXF"
            )
        if suffix not in {".png", ".jpg", ".jpeg", ".ifc"}:
            raise ValidationFailure(
                "drawing_review requires an IFC or image artifact for the legacy path"
            )
        if suffix == ".ifc":
            extra["ifc_path"] = str(path)
        else:
            extra["drawing_path"] = str(path)
        extra.update({"perception_mode": "auto", "generate_ifc": False})
        return f"审查项目图纸 {artifact['filename']}", extra, evidence
    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationFailure("agent input JSON is invalid") from exc
        if not isinstance(payload, dict):
            raise ValidationFailure("agent input JSON must be an object")
        extra.update(payload)
        text = str(payload.get("input_text") or payload.get("message") or "")
    else:
        parsed = parse_knowledge_file(path)
        text = parsed.content
    if not text.strip() and envelope.workflow in {"qa", "bid_review", "negotiation"}:
        raise ValidationFailure("agent input contains no usable text")
    return text[:100_000], extra, evidence


async def legacy_agent_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    agent_type = _AGENT_TYPES.get(envelope.workflow)
    if agent_type is None:
        raise ValidationFailure(f"unsupported agent workflow: {envelope.workflow}")
    input_text, extra, evidence = await _load_agent_input(context, envelope)
    # Uploaded JSON cannot override the authenticated scope or memory identity.
    extra.update({"project_id": context.project_id, "role": context.role,
                  "memory_session_id": envelope.options.get("memory_session_id") or envelope.task_id,
                  "memory_turn_id": envelope.task_id})
    budget.consume_tool_call(f"agent.{envelope.workflow}")
    response = await get_orchestrator().handle(AgentRequest(
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        session_id=envelope.task_id,
        agent_type=agent_type,
        mode=ExecutionMode.SINGLE,
        input_text=input_text,
        extra=extra,
    ))
    if getattr(response, "fallback_used", False):
        raise DependencyFailure(response.content or "agent used a failure fallback")
    structured = getattr(response, "structured_output", None)
    content = getattr(response, "content", "") or ""
    # drawing2bim interrupts before final output when HITL is required.
    waiting = envelope.workflow == "drawing_review" and not content and not structured
    if waiting and envelope.workflow == "drawing_review":
        # A LangGraph interrupt may not return a structured payload.  Persist a
        # small, explicit marker so the durable v2 drawing workflow can resume
        # after the process is restarted instead of losing the only state that
        # identifies the legacy review as a human-gated step.
        structured = {
            **(structured if isinstance(structured, dict) else {}),
            "pipeline": "legacy_drawing2bim",
            "stage": "legacy_review_pending",
            "requires_human_review": True,
        }
    metadata = getattr(response, "metadata", {}) or {}
    return AgentResult(
        status="waiting_human" if waiting else "succeeded",
        answer=content,
        structured_output=structured,
        artifact_ids=envelope.input_artifact_ids,
        evidence_refs=evidence,
        confidence=max(0.0, min(1.0, float(metadata.get("confidence") or 0.0))),
        next_action="review_decision" if waiting else None,
        fallback_used=False,
    )


async def modeling_gate_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    if not envelope.input_artifact_ids:
        raise ValidationFailure("modeling requires an approved Model IR artifact")
    return AgentResult(
        status="waiting_human",
        answer="Modeling is paused before Revit write. Complete static validation and approve the build.",
        artifact_ids=envelope.input_artifact_ids,
        evidence_refs=[EvidenceRef(kind="model_ir", artifact_id=item) for item in envelope.input_artifact_ids],
        confidence=1.0,
        next_action="revit_write_approval",
    )


async def contract_review_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    """Run a bounded, evidence-linked contract pre-review.

    Legal conclusions remain a human decision.  This step only highlights
    common missing clauses and pauses the durable workflow for review.
    """
    text, _, evidence = await _load_agent_input(context, envelope)
    if not evidence:
        raise ValidationFailure("contract_review requires an input artifact")
    budget.consume_tool_call("contract.review")
    normalized = text.casefold()
    rules = (
        ("contract.payment_terms", ("付款", "payment"), "未发现付款条件条款"),
        ("contract.breach_terms", ("违约", "breach"), "未发现违约责任条款"),
        ("contract.term", ("期限", "工期", "term", "duration"), "未发现合同期限或工期条款"),
    )
    findings: list[dict] = []
    for rule_id, keywords, description in rules:
        if not any(keyword in normalized for keyword in keywords):
            finding = ReviewFindingInput(
                rule_id=rule_id,
                track="hard",
                severity="warning",
                confidence=1.0,
                description=description,
                suggestion="请人工核对合同原文及附件，并补充明确条款。",
                evidence_refs=evidence,
            )
            findings.append(finding.model_dump(mode="json"))
    return AgentResult(
        status="waiting_human",
        answer="合同预审已完成，请审核员核对条款并作出最终决定。",
        structured_output={"review_type": "contract_pre_review", "rule_count": len(rules)},
        findings=findings,
        artifact_ids=envelope.input_artifact_ids,
        evidence_refs=evidence,
        confidence=1.0,
        next_action="contract_review_decision",
    )


async def composite_parent_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    """Finalize the parent coordination record after child creation.

    Child workflows execute independently from their own outbox entries; the
    parent records the immutable plan and their identifiers for auditability.
    """
    child_ids = (envelope.options or {}).get("child_task_ids", [])
    if not isinstance(child_ids, list):
        raise ValidationFailure("composite child_task_ids must be a list")
    budget.consume_tool_call("composite.parent")
    return AgentResult(
        status="succeeded",
        answer="复合任务已创建，子任务将分别执行并保留独立审批记录。",
        structured_output={"child_task_ids": [str(item) for item in child_ids]},
        artifact_ids=envelope.input_artifact_ids,
        confidence=1.0,
    )


def get_default_runtime(repository: TaskRepository | None = None) -> WorkflowRuntime:
    runtime = WorkflowRuntime(repository or TaskRepository())
    # Keep the historical drawing_review name registered as a compatibility
    # workflow for rows that already exist in the database.  It is not a new
    # public entry point: PDF/DWG/DXF still route to wall_pipeline inside the
    # prepare step, while IFC/image rows use the bounded legacy adapter.  A
    # missing registration would let an old task be approved and then fail
    # permanently when the runner resumed it.
    from backend.application.wall_pipeline_workflow import (
        drawing_review_approval_step,
        drawing_review_prepare_step,
        drawing_review_write_step,
        wall_pipeline_approve_model_step,
        wall_pipeline_approve_write_step,
        wall_pipeline_prepare_step,
    )
    from backend.config import get_settings

    settings = get_settings()
    for workflow in _AGENT_TYPES:
        if workflow == "drawing_review":
            runtime.register("drawing_review", [
                WorkflowStepDefinition(
                    name="drawing_review_prepare", handler=drawing_review_prepare_step,
                    timeout_seconds=settings.wall_pipeline_prepare_timeout_seconds,
                    max_attempts=1,
                ),
                WorkflowStepDefinition(
                    name="drawing_review_approval", handler=drawing_review_approval_step,
                    timeout_seconds=120.0, max_attempts=1,
                ),
                WorkflowStepDefinition(
                    name="drawing_review_write", handler=drawing_review_write_step,
                    timeout_seconds=settings.wall_pipeline_revit_write_timeout_seconds,
                    max_attempts=1,
                ),
            ])
            continue
        runtime.register(workflow, [WorkflowStepDefinition(
            name="agent_decision", handler=legacy_agent_step,
            timeout_seconds=120.0, max_attempts=2,
        )])
    wall_steps = [
        WorkflowStepDefinition(
            name="wall_pipeline_prepare", handler=wall_pipeline_prepare_step,
            timeout_seconds=settings.wall_pipeline_prepare_timeout_seconds,
            max_attempts=1,
        ),
        WorkflowStepDefinition(
            name="wall_model_approval_and_dry_run", handler=wall_pipeline_approve_model_step,
            timeout_seconds=120.0, max_attempts=1,
        ),
        WorkflowStepDefinition(
            name="revit_write_approval", handler=wall_pipeline_approve_write_step,
            timeout_seconds=settings.wall_pipeline_revit_write_timeout_seconds,
            max_attempts=1,
        ),
    ]
    runtime.register("wall_pipeline", wall_steps)
    runtime.register("modeling", [WorkflowStepDefinition(
        name="static_dry_run_gate", handler=modeling_gate_step,
        timeout_seconds=30.0, max_attempts=1,
    )])
    runtime.register("contract_review", [WorkflowStepDefinition(
        name="contract_pre_review", handler=contract_review_step,
        timeout_seconds=120.0, max_attempts=1,
    )])
    runtime.register("composite", [WorkflowStepDefinition(
        name="composite_parent", handler=composite_parent_step,
        timeout_seconds=30.0, max_attempts=1,
    )])
    return runtime
