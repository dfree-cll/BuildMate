"""Project-bound workflow access and HITL resume regression tests."""

from __future__ import annotations

import uuid

import pytest

from backend.adapters.task_repository import TaskRepository
from backend.application.task_service import TaskService
from backend.domain.contracts import RequestContext, TaskStatus
from backend.domain.errors import PolicyFailure, ResourceNotFound


def _context(
    tenant_id: str,
    *,
    project_id: str | None,
    user_id: str = "reviewer",
    role: str = "admin",
) -> RequestContext:
    trace_id = uuid.uuid4().hex
    return RequestContext(
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=user_id,
        role=role,
        trace_id=trace_id,
        correlation_id=trace_id,
    )


async def _waiting_task(
    repository: TaskRepository,
    context: RequestContext,
    *,
    workflow: str,
) -> dict:
    run, created = await TaskService(repository).submit(
        context,
        workflow=workflow,
        input_artifact_ids=[],
        idempotency_key=f"project-scope-{workflow}-{uuid.uuid4().hex}",
    )
    assert created is True
    await repository.transition(
        context,
        run["id"],
        TaskStatus.RUNNING,
        event_type="task.running",
        stage="test",
    )
    return await repository.transition(
        context,
        run["id"],
        TaskStatus.WAITING_HUMAN,
        event_type="task.waiting_human",
        stage="test",
    )


async def test_project_scoped_task_is_not_readable_without_project_context():
    repository = TaskRepository()
    suffix = uuid.uuid4().hex
    owner = _context(
        f"tenant_project_scope_{suffix}", project_id=f"project_a_{suffix}"
    )
    run = await _waiting_task(repository, owner, workflow="wall_pipeline")

    with pytest.raises(ResourceNotFound):
        await repository.get(
            _context(owner.tenant_id, project_id=None),
            run["id"],
        )

    visible = await repository.get(owner, run["id"])
    assert visible["project_id"] == owner.project_id


async def test_global_task_remains_readable_without_project_context():
    repository = TaskRepository()
    suffix = uuid.uuid4().hex
    global_context = _context(f"tenant_global_scope_{suffix}", project_id=None)
    run, created = await TaskService(repository).submit(
        global_context,
        workflow="qa",
        input_artifact_ids=[],
        idempotency_key=f"global-scope-{suffix}",
    )
    assert created is True

    visible = await repository.get(global_context, run["id"])
    assert visible["project_id"] is None


@pytest.mark.parametrize("workflow", ["wall_pipeline", "drawing_review"])
async def test_project_bound_workflow_resume_requires_project_context(workflow: str):
    repository = TaskRepository()
    suffix = uuid.uuid4().hex
    owner = _context(
        f"tenant_resume_scope_{suffix}", project_id=f"project_resume_{suffix}"
    )
    run = await _waiting_task(repository, owner, workflow=workflow)
    reviewer_without_project = _context(owner.tenant_id, project_id=None)

    with pytest.raises(PolicyFailure, match="requires a project_id"):
        await TaskService(repository).resume(
            reviewer_without_project,
            run["id"],
            payload={"decision": "approved"},
        )

    still_waiting = await repository.get(owner, run["id"])
    assert still_waiting["status"] == TaskStatus.WAITING_HUMAN.value


async def test_global_workflow_can_resume_without_project_context():
    repository = TaskRepository()
    suffix = uuid.uuid4().hex
    context = _context(f"tenant_global_resume_{suffix}", project_id=None)
    run = await _waiting_task(repository, context, workflow="qa")

    resumed = await TaskService(repository).resume(
        context,
        run["id"],
        payload={"decision": "approved"},
    )

    assert resumed["status"] == TaskStatus.RESUMED.value


async def test_project_bound_resume_rejects_a_different_project():
    repository = TaskRepository()
    suffix = uuid.uuid4().hex
    owner = _context(
        f"tenant_resume_mismatch_{suffix}", project_id=f"project_owner_{suffix}"
    )
    run = await _waiting_task(repository, owner, workflow="drawing_review")
    other_project = _context(
        owner.tenant_id, project_id=f"project_other_{suffix}"
    )

    with pytest.raises(ResourceNotFound):
        await TaskService(repository).resume(
            other_project,
            run["id"],
            payload={"decision": "approved"},
        )


async def test_resume_persists_authenticated_approver_not_payload_operator():
    repository = TaskRepository()
    suffix = uuid.uuid4().hex
    submitter = _context(
        f"tenant_resume_actor_{suffix}",
        project_id=f"project_resume_actor_{suffix}",
        user_id="submitter",
        role="admin",
    )
    run = await _waiting_task(repository, submitter, workflow="wall_pipeline")
    approver = _context(
        submitter.tenant_id,
        project_id=submitter.project_id,
        user_id="real-approver",
        role="reviewer",
    )

    await TaskService(repository).resume(
        approver,
        run["id"],
        payload={"decision": "approved", "operator": "forged-user"},
    )
    events = await repository.list_events(approver, run["id"], 0)
    resumed = [event for event in events if event.type == "task.resumed"][-1]
    assert resumed.payload["operator"] == "real-approver"
    assert resumed.payload["operator_role"] == "reviewer"
