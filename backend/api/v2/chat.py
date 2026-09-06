"""Persistent evidence-first chat v2 APIs."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from backend.adapters.chat_repository import ChatRepository
from backend.application.context import build_request_context
from backend.application.intent_router import classify_intent
from backend.application.task_understanding import understand_task
from backend.domain.task_plan import TaskPlanPreview
from backend.dependencies import get_current_user, llm_rate_limit
from backend.api.v2.event_stream import normalize_event_cursor
from backend.rag.contracts import KnowledgeScope, KnowledgeSearchRequest
from backend.rag.service import get_rag_service
from backend.ports.knowledge import KnowledgeService

router = APIRouter(prefix="/chat", tags=["v2-chat"])
repository = ChatRepository()


class SessionCreate(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    title: str = Field(default="新对话", min_length=1, max_length=256)


class MessageCreate(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    content: str = Field(..., min_length=2, max_length=4000)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("content", mode="before")
    @classmethod
    def _content_not_blank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("content cannot be blank")
        return value.strip() if isinstance(value, str) else value


@router.post("/tasks/preview", response_model=TaskPlanPreview)
async def preview_task_plan(
    body: MessageCreate,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    """Return a registered single/composite plan without starting a workflow."""

    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    plan = understand_task(
        body.content,
        project_id=context.project_id,
        artifact_ids=body.artifact_ids,
    )
    return {"plan": plan.model_dump(mode="json"), "trace_id": context.trace_id}


@router.post("/intents/preview")
async def preview_intent(
    body: MessageCreate,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    """Preview the registered capability before a question creates side effects."""

    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    route = classify_intent(
        body.content,
        project_id=context.project_id,
        artifact_ids=body.artifact_ids,
    )
    return route.model_dump(mode="json")


@router.post("/sessions", status_code=201)
async def create_session(
    body: SessionCreate,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    return await repository.create_session(context, body.title)


@router.post("/sessions/{session_id}/messages", status_code=201)
async def create_message(
    session_id: str,
    body: MessageCreate,
    current_user: dict = Depends(llm_rate_limit),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    scope = KnowledgeScope.PROJECT if body.project_id else KnowledgeScope.TENANT
    # Check ownership BEFORE retrieval/generation, and actually use saved dialogue.
    history = await repository.list_messages(context, session_id, limit=40, latest=True)
    from backend.application.agent_memory import contextual_query
    questions = [m["content"] for m in history if m["role"] == "user"]
    memory_context = "\n".join(f"{m['role']}（历史、未复核）：{m['content'][:500]}" for m in history)[-12000:]
    knowledge: KnowledgeService = get_rag_service()
    answer = await knowledge.answer(context, KnowledgeSearchRequest(
        query=contextual_query(body.content, questions),
        tenant_id=context.tenant_id,
        project_id=body.project_id,
        scope=scope,
        top_k=8,
    ), memory_context=memory_context)
    messages = await repository.append_user_and_answer(context, session_id, body.content, answer)
    return {"messages": messages, "answer": answer.model_dump(mode="json")}


@router.get("/sessions/{session_id}/events")
async def stream_messages(
    request: Request,
    session_id: str,
    project_id: str | None = None,
    after_seq: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=project_id, trace_id=x_trace_id)
    cursor = normalize_event_cursor(last_event_id, after_seq)

    async def events():
        nonlocal cursor
        while not await request.is_disconnected():
            messages = await repository.list_messages(context, session_id, cursor)
            for message in messages:
                cursor = message["seq"]
                yield {
                    "id": str(cursor), "event": "chat.message",
                    "data": json.dumps(message, ensure_ascii=False, default=str),
                }
            await asyncio.sleep(1)

    return EventSourceResponse(events())
