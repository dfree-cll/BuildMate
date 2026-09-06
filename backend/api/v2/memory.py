"""User-visible memory. New sessions do not inherit unrelated sessions implicitly."""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from backend.application.agent_memory import AGENTS, repository
from backend.application.context import build_request_context
from backend.dependencies import get_current_user
from backend.domain.agent_memory import MemoryPreferences

router = APIRouter(prefix="/memory", tags=["v2-memory"])
SessionId = Annotated[str, Path(min_length=1, max_length=128)]


async def _context(agent, user, project_id):
    if agent not in AGENTS:
        raise HTTPException(status_code=422, detail="未知 Agent")
    context = build_request_context(user, project_id=project_id)
    if project_id:
        from backend.adapters.project_repository import ProjectRepository
        await ProjectRepository().get(context, project_id)
    return context


@router.get("/{agent}/sessions")
async def list_sessions(agent: str, project_id: str | None = None, user: dict = Depends(get_current_user)):
    context = await _context(agent, user, project_id)
    if agent == "drawing2bim":
        await repository.sync_bim(context)
    return {"sessions": await repository.list_sessions(context, agent)}


@router.get("/{agent}/sessions/{session_id}")
async def get_session(agent: str, session_id: SessionId, project_id: str | None = None,
                      user: dict = Depends(get_current_user)):
    context = await _context(agent, user, project_id)
    if agent == "drawing2bim":
        await repository.sync_bim(context)
    return await repository.read(context, agent, session_id, limit=100)


class PreferencesBody(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    preferences: MemoryPreferences


@router.put("/{agent}/sessions/{session_id}/preferences")
async def set_preferences(agent: str, session_id: SessionId, body: PreferencesBody,
                          user: dict = Depends(get_current_user)):
    context = await _context(agent, user, body.project_id)
    if body.preferences.bim is not None and agent != "drawing2bim":
        raise HTTPException(status_code=422, detail="工程参数只能保存到 BIM 会话")
    await repository.preferences(context, agent, session_id, body.preferences.model_dump(mode="json", exclude_none=True))
    return await repository.read(context, agent, session_id)
