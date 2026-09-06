"""Evidence-first knowledge ingestion and search APIs."""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from backend.application.artifact_service import get_artifact_service
from backend.application.context import build_request_context
from backend.dependencies import get_current_user, require_role
from backend.rag.contracts import KnowledgeDocumentCreate, KnowledgeSearchRequest
from backend.rag.service import get_rag_service
from backend.rag.parsers import parse_knowledge_file

router = APIRouter(prefix="/knowledge", tags=["v2-knowledge"])


class ArtifactIngestRequest(BaseModel):
    artifact_id: str = Field(..., min_length=1, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    scope: str = Field(default="tenant", pattern=r"^(global|tenant|project)$")
    source_type: str = Field(default="document", min_length=1, max_length=32)
    document_version: str = Field(default="1", min_length=1, max_length=32)
    metadata: dict = Field(default_factory=dict)


class FeedbackRequest(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    chunk_id: str | None = Field(default=None, max_length=128)
    label: str = Field(..., pattern=r"^(relevant|irrelevant|incorrect|missing_knowledge)$")


@router.post("/documents", status_code=201)
async def ingest_document(
    body: KnowledgeDocumentCreate,
    current_user: dict = Depends(require_role("admin", "reviewer", "project")),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=body.project_id, trace_id=x_trace_id
    )
    document, created = await get_rag_service().ingest(context, body)
    return {"document": document, "created": created}


@router.post("/documents/from-artifact", status_code=201)
async def ingest_artifact_document(
    body: ArtifactIngestRequest,
    current_user: dict = Depends(require_role("admin", "reviewer", "project")),
    x_trace_id: str | None = Header(default=None),
):
    from backend.rag.contracts import KnowledgeScope

    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    artifact, path = await get_artifact_service().resolve(context, body.artifact_id)
    parsed = parse_knowledge_file(path)
    request = KnowledgeDocumentCreate(
        project_id=body.project_id,
        scope=KnowledgeScope(body.scope),
        source_type=body.source_type,
        source_uri=f"artifact://{body.artifact_id}",
        title=artifact["filename"],
        document_version=body.document_version,
        content=parsed.content,
        metadata={**body.metadata, **parsed.metadata, "artifact_id": body.artifact_id},
    )
    document, created = await get_rag_service().ingest(
        context, request, parser_version=parsed.parser_version
    )
    return {"document": document, "created": created}


@router.post("/search")
async def search_knowledge(
    body: KnowledgeSearchRequest,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=body.project_id, trace_id=x_trace_id
    )
    hits, retrieval_run_id = await get_rag_service().search(context, body)
    return {
        "retrieval_run_id": retrieval_run_id,
        "hits": [hit.model_dump(mode="json") for hit in hits],
        "abstained": not bool(hits),
    }


@router.get("/retrieval-runs/{run_id}")
async def get_retrieval_run(
    run_id: str,
    project_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(
        current_user, project_id=project_id, trace_id=x_trace_id
    )
    result = await get_rag_service().get_retrieval(context, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="检索记录不存在")
    return result


@router.post("/retrieval-runs/{run_id}/feedback", status_code=201)
async def add_retrieval_feedback(
    run_id: str,
    body: FeedbackRequest,
    current_user: dict = Depends(get_current_user),
    x_trace_id: str | None = Header(default=None),
):
    context = build_request_context(current_user, project_id=body.project_id, trace_id=x_trace_id)
    feedback_id = await get_rag_service().add_feedback(
        context,
        retrieval_run_id=run_id,
        chunk_id=body.chunk_id,
        label=body.label,
    )
    return {"feedback_id": feedback_id}
