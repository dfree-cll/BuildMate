"""BuildMate v2 domain, durable task and evidence-first RAG regression tests."""

import hashlib
import uuid

import pytest

from backend.adapters.task_repository import TaskRepository
from backend.application.task_service import TaskService
from backend.domain.contracts import EvidenceRef, RequestContext, TaskStatus
from backend.domain.errors import CitationValidationFailure, ResourceNotFound
from backend.domain.model_ir import ModelIRV2
from backend.rag.contracts import (
    KnowledgeDocumentCreate,
    KnowledgeScope,
    KnowledgeSearchRequest,
)
from backend.rag.service import RAGService


def _context(tenant: str, project: str | None = None) -> RequestContext:
    trace = uuid.uuid4().hex
    return RequestContext(
        tenant_id=tenant,
        project_id=project,
        user_id="user_test",
        role="admin",
        trace_id=trace,
        correlation_id=trace,
    )


def test_model_ir_v2_is_evidence_bearing_and_reproducible():
    source_hash = hashlib.sha256(b"drawing").hexdigest()
    data = {
        "schema_version": "2.0",
        "project": {"id": "prj_1", "name": "示例项目"},
        "units": "m",
        "levels": [{"id": "L1", "name": "一层", "elevation_m": 0.0}],
        "grid": [],
        "model_elements": [{
            "element_id": "wall_1",
            "type": "Wall",
            "geometry": {"start": [0, 0, 0], "end": [5, 0, 0], "thickness_m": 0.2},
            "properties": {},
            "source_refs": [{"kind": "drawing_entity", "artifact_id": "art_1", "locator": "A-WALL:42"}],
            "confidence": 0.95,
            "review_status": "pending",
        }],
        "provenance": {
            "source_artifact_ids": ["art_1"],
            "source_sha256": source_hash,
            "parser_version": "2.0",
            "pipeline_version": "2.0",
        },
    }

    first = ModelIRV2.model_validate(data)
    second = ModelIRV2.model_validate(data)

    assert first.canonical_sha256() == second.canonical_sha256()
    assert first.model_elements[0].source_refs[0].artifact_id == "art_1"


async def test_task_runtime_is_idempotent_and_events_are_ordered():
    suffix = uuid.uuid4().hex
    context = _context(f"tenant_v2_task_{suffix}")
    repository = TaskRepository()
    service = TaskService(repository)

    first, created = await service.submit(
        context,
        workflow="drawing_review",
        input_artifact_ids=["art_1"],
        idempotency_key=f"idempotency-v2-task-{suffix}",
    )
    repeated, repeated_created = await service.submit(
        context,
        workflow="drawing_review",
        input_artifact_ids=["art_1"],
        idempotency_key=f"idempotency-v2-task-{suffix}",
    )

    assert created is True
    assert repeated_created is False
    assert repeated["id"] == first["id"]

    running = await repository.transition(
        context,
        first["id"],
        TaskStatus.RUNNING,
        event_type="task.running",
        stage="perception",
    )
    assert running["status"] == "running"
    events = await repository.list_events(context, first["id"])
    assert [event.seq for event in events] == [1, 2]
    assert [event.type for event in events] == ["task.created", "task.running"]


async def test_task_runtime_does_not_leak_between_tenants():
    repository = TaskRepository()
    suffix = uuid.uuid4().hex
    owner = _context(f"tenant_v2_owner_{suffix}")
    other = _context(f"tenant_v2_other_{suffix}")
    run, _ = await TaskService(repository).submit(
        owner,
        workflow="qa",
        input_artifact_ids=[],
        idempotency_key=f"idempotency-v2-isolation-{suffix}",
    )

    with pytest.raises(ResourceNotFound):
        await repository.get(other, run["id"])


async def test_rag_ingestion_search_and_citations_are_tenant_scoped():
    service = RAGService()
    suffix = uuid.uuid4().hex
    project_id = f"project_v2_rag_{suffix}"
    owner = _context(f"tenant_v2_rag_{suffix}", project_id)
    other = _context(f"tenant_v2_rag_other_{suffix}", project_id)
    request = KnowledgeDocumentCreate(
        project_id=project_id,
        scope=KnowledgeScope.PROJECT,
        source_type="regulation",
        source_uri="fixture://gb50010",
        title="混凝土结构设计规范摘录",
        content="""# 墙体要求

        混凝土墙体厚度应根据承载力、稳定性和构造要求确定。

        # 施工检查

        构件施工前应核对轴网、标高和材料强度等级。""",
        metadata={"discipline": "structure"},
    )
    document, created = await service.ingest(owner, request)
    repeated, repeated_created = await service.ingest(owner, request)

    assert created is True
    assert repeated_created is False
    assert repeated["id"] == document["id"]

    hits, retrieval_run_id = await service.search(owner, KnowledgeSearchRequest(
        query="墙体厚度和轴网检查要求",
        tenant_id=owner.tenant_id,
        project_id=owner.project_id,
        scope=KnowledgeScope.PROJECT,
        filters={"discipline": "structure"},
        top_k=3,
    ))
    assert retrieval_run_id.startswith("ret_")
    assert hits
    assert all(hit.document_id == document["id"] for hit in hits)

    citations = service.validate_citations(
        [{"chunk_id": hits[0].chunk_id}], hits
    )
    assert citations[0].document_id == document["id"]
    with pytest.raises(CitationValidationFailure):
        service.validate_citations([{"chunk_id": "invented"}], hits)

    other_hits, _ = await service.search(other, KnowledgeSearchRequest(
        query="墙体厚度",
        tenant_id=other.tenant_id,
        project_id=other.project_id,
        scope=KnowledgeScope.PROJECT,
        top_k=3,
    ))
    assert other_hits == []
