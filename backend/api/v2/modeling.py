"""Model IR, review and human-approved build lifecycle APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from backend.adapters.build_repository import BuildRepository
from backend.adapters.model_ir_repository import ModelIRRepository
from backend.adapters.review_repository import ReviewRepository
from backend.application.context import build_request_context
from backend.dependencies import get_current_user, require_role
from backend.domain.contracts import ReviewFindingInput
from backend.domain.model_ir import ModelIRV2

router = APIRouter(tags=["v2-modeling"])
model_ir_repository = ModelIRRepository()
review_repository = ReviewRepository()
build_repository = BuildRepository()


class ModelIRCreate(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64)
    source_artifact_id: str = Field(..., min_length=1, max_length=64)
    model: ModelIRV2


class ReviewCreate(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64)
    model_ir_version_id: str = Field(..., min_length=1, max_length=64)
    findings: list[ReviewFindingInput] = Field(default_factory=list, max_length=1000)
    report: dict = Field(default_factory=dict)


class DecisionBody(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64)
    decision: str = Field(..., pattern=r"^(approved|rejected)$")
    reason: str | None = Field(default=None, max_length=2000)


class BuildCreate(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64)
    model_ir_version_id: str = Field(..., min_length=1, max_length=64)


@router.post("/model-ir", status_code=201)
async def create_model_ir(
    body: ModelIRCreate,
    current_user: dict = Depends(require_role("admin", "project")),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    model, created = await model_ir_repository.save(context, body.source_artifact_id, body.model)
    return {"model_ir": model, "created": created}


@router.get("/model-ir/{model_ir_id}")
async def get_model_ir(
    model_ir_id: str,
    project_id: str,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=project_id, trace_id=x_trace_id)
    return await model_ir_repository.get(context, model_ir_id)


@router.post("/reviews", status_code=201)
async def create_review(
    body: ReviewCreate,
    current_user: dict = Depends(require_role("admin", "project")),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    return await review_repository.create(
        context, body.model_ir_version_id,
        [finding.model_dump(mode="json") for finding in body.findings], body.report
    )


@router.get("/reviews/{review_id}")
async def get_review(
    review_id: str,
    project_id: str,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=project_id, trace_id=x_trace_id)
    return await review_repository.get(context, review_id)


@router.post("/reviews/{review_id}/decision")
async def decide_review(
    review_id: str,
    body: DecisionBody,
    current_user: dict = Depends(require_role("admin", "project", "reviewer")),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    return await review_repository.decide(context, review_id, body.decision, body.reason)


@router.post("/builds", status_code=201)
async def create_build(
    body: BuildCreate,
    current_user: dict = Depends(require_role("admin", "project")),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    return await build_repository.create(context, body.model_ir_version_id)


@router.get("/builds/{build_id}")
async def get_build(
    build_id: str,
    project_id: str,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=project_id, trace_id=x_trace_id)
    return await build_repository.get(context, build_id)


@router.post("/builds/{build_id}/approve")
async def approve_build(
    build_id: str,
    body: DecisionBody,
    current_user: dict = Depends(require_role("admin", "project")),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    return await build_repository.approve(context, build_id, body.decision, body.reason)


@router.get("/builds/{build_id}/diff")
async def get_build_diff(
    build_id: str,
    project_id: str,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=project_id, trace_id=x_trace_id)
    build = await build_repository.get(context, build_id)
    return {"build_id": build_id, "status": build["status"], "diff": build["diff"]}
