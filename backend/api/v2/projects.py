"""Project APIs with mandatory tenant scope."""

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from backend.adapters.project_repository import ProjectRepository
from backend.application.context import build_request_context
from backend.dependencies import get_current_user

router = APIRouter(prefix="/projects", tags=["v2-projects"])
repository = ProjectRepository()


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class LevelCreate(BaseModel):
    floor_code: str = Field(..., min_length=1, max_length=32)
    elevation_mm: int = Field(..., ge=-100_000, le=1_000_000)


@router.post("", status_code=201)
async def create_project(
    body: ProjectCreate,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, trace_id=x_trace_id)
    return await repository.create(context, body.name)


@router.get("")
async def list_projects(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, trace_id=x_trace_id)
    return {"projects": await repository.list(context, limit)}


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=project_id, trace_id=x_trace_id)
    return await repository.get(context, project_id)


@router.post("/{project_id}/levels", status_code=201)
async def add_project_level(
    project_id: str,
    body: LevelCreate,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=project_id, trace_id=x_trace_id)
    return await repository.add_level(
        context, project_id,
        floor_code=body.floor_code, elevation_mm=body.elevation_mm,
    )


@router.get("/{project_id}/levels")
async def list_project_levels(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=project_id, trace_id=x_trace_id)
    return {"levels": await repository.list_levels(context, project_id)}
