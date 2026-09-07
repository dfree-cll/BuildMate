"""Artifact upload and download APIs."""

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from fastapi.responses import FileResponse

from backend.application.artifact_service import get_artifact_service
from backend.application.context import build_request_context
from backend.dependencies import get_current_user

router = APIRouter(prefix="/artifacts", tags=["v2-artifacts"])
service = get_artifact_service()


@router.post("", status_code=201)
async def upload_artifact(
    file: UploadFile = File(...),
    kind: str = Form("document"),
    project_id: str | None = Form(default=None),
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=project_id, trace_id=x_trace_id
    )
    return await service.store_upload(context, file, kind=kind)


@router.get("/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    project_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=project_id, trace_id=x_trace_id
    )
    return await service.get(context, artifact_id)


@router.get("/{artifact_id}/content")
async def download_artifact(
    artifact_id: str,
    project_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=project_id, trace_id=x_trace_id
    )
    artifact, path = await service.resolve(context, artifact_id)
    return FileResponse(path, filename=artifact["filename"], media_type=artifact["media_type"])
