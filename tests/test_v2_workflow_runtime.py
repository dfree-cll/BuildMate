import uuid

import pytest
from sqlalchemy import text

from backend.adapters.task_repository import TaskRepository
from backend.application.task_service import TaskService
from backend.application.workflow_runtime import WorkflowRuntime, WorkflowStepDefinition
from backend.config import get_settings
from backend.domain.contracts import AgentResult, RequestContext, TaskEnvelope, TaskStatus
from backend.domain.errors import PolicyFailure, ValidationFailure
from backend.db.session import engine
from backend.workers.workflows import get_default_runtime


def context(suffix: str) -> RequestContext:
    return RequestContext(
        tenant_id=f"tenant_runtime_{suffix}", project_id=f"project_runtime_{suffix}",
        user_id="runtime_user", role="admin", trace_id=suffix, correlation_id=suffix,
    )


def test_wall_pipeline_revit_delivery_has_its_own_long_timeout():
    """The Revit write step must include Bridge, persistence and overlay audit."""

    runtime = get_default_runtime(TaskRepository())
    step = runtime._workflows["wall_pipeline"][2]  # noqa: SLF001 - definition contract
    settings = get_settings()
    assert step.timeout_seconds == settings.wall_pipeline_revit_write_timeout_seconds
    assert step.timeout_seconds > 120.0


def test_legacy_drawing_review_rows_have_a_bounded_compatibility_runtime():
    """Existing approval rows must not become permanently failed on resume."""

    runtime = get_default_runtime(TaskRepository())
    assert [step.name for step in runtime._workflows["drawing_review"]] == [
        "drawing_review_prepare",
        "drawing_review_approval",
        "drawing_review_write",
    ]


async def test_runtime_persists_steps_and_validated_result():
    suffix = uuid.uuid4().hex
    ctx = context(suffix)
    repository = TaskRepository()
    run, _ = await TaskService(repository).submit(
        ctx, workflow="fixture_review", input_artifact_ids=["art_fixture"],
        idempotency_key=f"runtime-{suffix}",
    )
    seen = []

    async def perceive(step_context, envelope, budget):
        assert step_context.tenant_id == ctx.tenant_id
        budget.consume_tool_call("fixture.parser")
        seen.append(envelope.task_id)
        return AgentResult(
            status="succeeded", answer="evidence-backed",
            evidence_refs=[{"kind": "artifact", "artifact_id": "art_fixture"}],
            confidence=1.0,
        )

    runtime = WorkflowRuntime(repository)
    runtime.register("fixture_review", [WorkflowStepDefinition("perception", perceive)])
    completed = await runtime.execute(TaskEnvelope(
        task_id=run["id"], tenant_id=ctx.tenant_id, project_id=ctx.project_id,
        actor_id=ctx.user_id, workflow="fixture_review",
        input_artifact_ids=["art_fixture"], idempotency_key=f"runtime-{suffix}",
        correlation_id=suffix,
    ))

    assert completed["status"] == "succeeded"
    assert completed["result"]["evidence_refs"][0]["artifact_id"] == "art_fixture"
    assert seen == [run["id"]]
    steps = await repository.list_steps(ctx, run["id"])
    assert [(step["name"], step["status"]) for step in steps] == [("perception", "succeeded")]


async def test_runtime_rejects_unregistered_workflow_explicitly():
    suffix = uuid.uuid4().hex
    ctx = context(suffix)
    repository = TaskRepository()
    run, _ = await TaskService(repository).submit(
        ctx, workflow="missing", input_artifact_ids=[],
        idempotency_key=f"runtime-missing-{suffix}",
    )
    failed = await WorkflowRuntime(repository).execute(TaskEnvelope(
        task_id=run["id"], tenant_id=ctx.tenant_id, project_id=ctx.project_id,
        actor_id=ctx.user_id, workflow="missing", input_artifact_ids=[],
        idempotency_key=f"runtime-missing-{suffix}", correlation_id=suffix,
    ))
    assert failed["status"] == "failed"
    assert failed["error_type"] == "validation"


async def test_hitl_resume_is_persisted_and_continues_next_step():
    suffix = uuid.uuid4().hex
    ctx = context(suffix)
    repository = TaskRepository()
    service = TaskService(repository)
    run, _ = await service.submit(
        ctx, workflow="fixture_hitl", input_artifact_ids=[],
        idempotency_key=f"runtime-hitl-{suffix}",
    )
    seen: list[str] = []

    async def gate(step_context, envelope, budget):
        return AgentResult(status="waiting_human", next_action="approve")

    async def finish(step_context, envelope, budget):
        seen.append(envelope.resume_payload.get("decision", ""))
        return AgentResult(status="succeeded", answer="approved")

    runtime = WorkflowRuntime(repository)
    runtime.register("fixture_hitl", [
        WorkflowStepDefinition("gate", gate, max_attempts=1),
        WorkflowStepDefinition("finish", finish, max_attempts=1),
    ])
    envelope = TaskEnvelope(
        task_id=run["id"], tenant_id=ctx.tenant_id, project_id=ctx.project_id,
        actor_id=ctx.user_id, workflow="fixture_hitl", input_artifact_ids=[],
        idempotency_key=f"runtime-hitl-{suffix}", correlation_id=suffix,
    )
    waiting = await runtime.execute(envelope)
    assert waiting["status"] == "waiting_human"
    resumed = await service.resume(ctx, run["id"], payload={"decision": "approved"})
    assert resumed["status"] == "resumed"
    async with engine.connect() as conn:
        approval = (await conn.execute(text("""
            SELECT action, decision, reason, created_by
            FROM approvals WHERE run_id=:run_id
        """), {"run_id": run["id"]})).mappings().one()
    assert dict(approval) == {
        "action": "fixture_hitl_hitl",
        "decision": "approved",
        "reason": None,
        "created_by": ctx.user_id,
    }
    completed = await runtime.execute(envelope.model_copy(update={
        "resume_payload": {"decision": "approved"},
    }))
    assert completed["status"] == "succeeded"
    assert seen == ["approved"]


async def test_rejected_wall_resume_records_model_approval_atomically():
    suffix = uuid.uuid4().hex
    ctx = context(suffix)
    repository = TaskRepository()
    service = TaskService(repository)
    run, _ = await service.submit(
        ctx,
        workflow="wall_pipeline",
        input_artifact_ids=[],
        idempotency_key=f"runtime-wall-reject-{suffix}",
    )
    await repository.transition(
        ctx, run["id"], TaskStatus.RUNNING,
        event_type="task.running", stage="wall_pipeline_prepare",
    )
    await repository.transition(
        ctx, run["id"], TaskStatus.WAITING_HUMAN,
        event_type="task.waiting_human", stage="wall_pipeline_prepare",
    )

    canceled = await service.resume(
        ctx,
        run["id"],
        payload={"decision": "rejected", "reason": "source needs correction"},
    )
    assert canceled["status"] == "canceled"
    async with engine.connect() as conn:
        approval = (await conn.execute(text("""
            SELECT action, decision, reason, created_by
            FROM approvals WHERE run_id=:run_id
        """), {"run_id": run["id"]})).mappings().one()
    assert dict(approval) == {
        "action": "wall_model_review",
        "decision": "rejected",
        "reason": "source needs correction",
        "created_by": ctx.user_id,
    }


async def test_drawing_review_wall_route_uses_wall_approval_labels():
    suffix = uuid.uuid4().hex
    ctx = context(suffix)
    repository = TaskRepository()
    service = TaskService(repository)
    run, _ = await service.submit(
        ctx,
        workflow="drawing_review",
        input_artifact_ids=[],
        idempotency_key=f"runtime-drawing-wall-{suffix}",
    )
    await repository.transition(
        ctx, run["id"], TaskStatus.RUNNING,
        event_type="task.running", stage="drawing_review_prepare",
    )
    await repository.transition(
        ctx,
        run["id"],
        TaskStatus.WAITING_HUMAN,
        event_type="task.waiting_human",
        stage="wall_pipeline_prepare",
        result={"structured_output": {"pipeline": "wall_pipeline"}},
    )

    resumed = await service.resume(
        ctx, run["id"], payload={"decision": "approved"}
    )
    assert resumed["status"] == "resumed"
    async with engine.connect() as conn:
        approval = (await conn.execute(text("""
            SELECT action FROM approvals WHERE run_id=:run_id
        """), {"run_id": run["id"]})).scalar_one()
    assert approval == "wall_model_review"

    await repository.transition(
        ctx, run["id"], TaskStatus.RUNNING,
        event_type="task.running", stage="hitl.resume",
    )
    await repository.transition(
        ctx,
        run["id"],
        TaskStatus.WAITING_HUMAN,
        event_type="task.waiting_human",
        stage="wall_model_approval_and_dry_run",
        result={"structured_output": {"pipeline": "wall_pipeline"}},
    )
    resumed_again = await service.resume(
        ctx, run["id"], payload={"decision": "approved", "reason": "dry-run reviewed"}
    )
    assert resumed_again["status"] == "resumed"
    async with engine.connect() as conn:
        actions = (await conn.execute(text("""
            SELECT action FROM approvals WHERE run_id=:run_id ORDER BY created_at, id
        """), {"run_id": run["id"]})).scalars().all()
    assert actions == ["wall_model_review", "revit_write"]


async def test_repository_resume_cannot_bypass_decision_or_actor_binding():
    """The repository remains safe even when called without TaskService."""

    suffix = uuid.uuid4().hex
    ctx = context(suffix)
    repository = TaskRepository()
    run, _ = await TaskService(repository).submit(
        ctx,
        workflow="fixture_direct_resume",
        input_artifact_ids=[],
        idempotency_key=f"runtime-direct-resume-{suffix}",
    )
    await repository.transition(
        ctx, run["id"], TaskStatus.RUNNING,
        event_type="task.running", stage="gate",
    )
    await repository.transition(
        ctx, run["id"], TaskStatus.WAITING_HUMAN,
        event_type="task.waiting_human", stage="gate",
    )

    with pytest.raises(ValidationFailure, match="only an approved decision"):
        await repository.resume(
            ctx, run["id"], payload={"decision": "rejected"},
            approver_id=ctx.user_id,
        )
    with pytest.raises(PolicyFailure, match="does not match"):
        await repository.resume(
            ctx, run["id"], payload={"decision": "approved"},
            approver_id="forged-operator",
        )
    still_waiting = await repository.get(ctx, run["id"])
    assert still_waiting["status"] == TaskStatus.WAITING_HUMAN.value
