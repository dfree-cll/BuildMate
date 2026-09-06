"""Durable workflow and event-stream APIs."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from backend.adapters.task_repository import TaskRepository
from backend.application.context import build_request_context
from backend.application.task_service import TaskService
from backend.dependencies import get_current_user, require_role
from backend.api.v2.event_stream import normalize_event_cursor
from backend.domain.task_plan import TaskPlan

router = APIRouter(tags=["v2-workflows"])
repository = TaskRepository()
service = TaskService(repository)

_WORKFLOWS = frozenset({
    "qa",
    "bid_review",
    "procurement",
    "negotiation",
    "contract_review",
    "wall_pipeline",
    "modeling",
})


class WorkflowCreate(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    workflow: str = Field(..., min_length=1, max_length=64)
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    options: dict = Field(default_factory=dict)


class ResumeBody(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    decision: str = Field(default="approved", pattern=r"^(approved|rejected)$")
    reason: str | None = Field(default=None, max_length=2000)
    payload: dict = Field(default_factory=dict)


class CompositeWorkflowCreate(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    plan: TaskPlan
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=20)


@router.post("/workflows", status_code=202)
async def submit_workflow(
    body: WorkflowCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    if body.workflow not in _WORKFLOWS:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="未知工作流")
    context = build_request_context(
        current_user, project_id=body.project_id, trace_id=x_trace_id
    )
    run, created = await service.submit(
        context,
        workflow=body.workflow,
        input_artifact_ids=body.input_artifact_ids,
        options=body.options,
        idempotency_key=idempotency_key,
    )
    return {"task": run, "created": created}


@router.post("/workflows/composite", status_code=202)
async def submit_composite_workflow(
    body: CompositeWorkflowCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    """Create a confirmed plan as one durable parent and child workflows."""
    context = build_request_context(
        current_user, project_id=body.project_id, trace_id=x_trace_id
    )
    parent, children, created = await service.submit_composite(
        context,
        plan=body.plan,
        input_artifact_ids=body.input_artifact_ids,
        idempotency_key=idempotency_key,
    )
    return {"task": parent, "children": children, "created": created}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    project_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=project_id, trace_id=x_trace_id
    )
    return await repository.get(context, task_id)


@router.get("/tasks/{task_id}/steps")
async def get_task_steps(
    task_id: str,
    project_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=project_id, trace_id=x_trace_id
    )
    return {"steps": await repository.list_steps(context, task_id)}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    project_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=project_id, trace_id=x_trace_id
    )
    return await service.cancel(context, task_id)


@router.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: str,
    body: ResumeBody,
    current_user: dict = Depends(require_role("admin", "project", "reviewer")),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=body.project_id, trace_id=x_trace_id
    )
    payload = {**body.payload, "decision": body.decision}
    if body.reason:
        payload["reason"] = body.reason
    return await service.resume(context, task_id, payload=payload)


@router.get("/tasks/{task_id}/events")
async def stream_task_events(
    request: Request,
    task_id: str,
    project_id: str | None = None,
    after_seq: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=project_id, trace_id=x_trace_id
    )
    cursor = normalize_event_cursor(last_event_id, after_seq)

    async def events():
        nonlocal cursor
        while not await request.is_disconnected():
            batch = await repository.list_events(context, task_id, cursor)
            for event in batch:
                cursor = event.seq
                yield {
                    "id": str(event.seq),
                    "event": event.type,
                    "data": json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
                }
            task = await repository.get(context, task_id)
            if task["terminal"] and not batch:
                return
            await asyncio.sleep(1)

    return EventSourceResponse(events())
