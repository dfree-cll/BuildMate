"""Persistence for document registry, retrieval audit and feedback."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from backend.db.session import engine
from backend.domain.contracts import RequestContext


class RAGRepository:
    async def find_document_version(
        self,
        context: RequestContext,
        *,
        project_id: str | None,
        content_hash: str,
        parser_version: str,
        chunking_version: str,
        embedding_model: str,
    ) -> dict | None:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, scope, source_type, source_uri,
                       title, document_version, content_hash, parser_version,
                       chunking_version, embedding_model, metadata_json, status,
                       created_at, updated_at, version
                FROM knowledge_documents
                WHERE tenant_id=:tenant_id
                  AND ((:project_id IS NULL AND project_id IS NULL) OR project_id=:project_id)
                  AND content_hash=:content_hash
                  AND parser_version=:parser_version
                  AND chunking_version=:chunking_version
                  AND embedding_model=:embedding_model
            """), {
                "tenant_id": context.tenant_id,
                "project_id": project_id,
                "content_hash": content_hash,
                "parser_version": parser_version,
                "chunking_version": chunking_version,
                "embedding_model": embedding_model,
            })).mappings().first()
        return self._document_row(row) if row else None

    async def create_document_and_job(
        self,
        context: RequestContext,
        *,
        document_id: str,
        job_id: str,
        project_id: str | None,
        scope: str,
        source_type: str,
        source_uri: str,
        title: str,
        document_version: str,
        content_hash: str,
        parser_version: str,
        chunking_version: str,
        embedding_model: str,
        metadata: dict,
        pipeline_version: str,
    ) -> dict:
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO knowledge_documents (
                    id, tenant_id, project_id, scope, source_type, source_uri,
                    title, document_version, content_hash, parser_version,
                    chunking_version, embedding_model, metadata_json, status,
                    created_by, version
                ) VALUES (
                    :id, :tenant_id, :project_id, :scope, :source_type, :source_uri,
                    :title, :document_version, :content_hash, :parser_version,
                    :chunking_version, :embedding_model, :metadata_json, 'indexing',
                    :created_by, 1
                )
            """), {
                "id": document_id,
                "tenant_id": context.tenant_id,
                "project_id": project_id,
                "scope": scope,
                "source_type": source_type,
                "source_uri": source_uri,
                "title": title,
                "document_version": document_version,
                "content_hash": content_hash,
                "parser_version": parser_version,
                "chunking_version": chunking_version,
                "embedding_model": embedding_model,
                "metadata_json": json.dumps(metadata, ensure_ascii=False),
                "created_by": context.user_id,
            })
            await conn.execute(text("""
                INSERT INTO knowledge_ingest_jobs (
                    id, document_id, tenant_id, project_id, status, attempts,
                    pipeline_version, created_by, version
                ) VALUES (
                    :id, :document_id, :tenant_id, :project_id, 'running', 1,
                    :pipeline_version, :created_by, 1
                )
            """), {
                "id": job_id,
                "document_id": document_id,
                "tenant_id": context.tenant_id,
                "project_id": project_id,
                "pipeline_version": pipeline_version,
                "created_by": context.user_id,
            })
        return {"id": document_id, "job_id": job_id, "status": "indexing"}

    async def save_chunks(
        self,
        context: RequestContext,
        *,
        document_id: str,
        job_id: str,
        project_id: str | None,
        scope: str,
        source_name: str,
        embedding_model: str,
        metadata: dict,
        chunks: list[dict],
    ) -> None:
        async with engine.begin() as conn:
            for chunk in chunks:
                await conn.execute(text("""
                    INSERT INTO knowledge_chunks (
                        id, content, vector, source_name, doc_id, chunk_index,
                        tenant_id, project_id, scope, metadata_json,
                        embedding_model, page_no, element_refs, token_count,
                        chunk_version, updated_at
                    ) VALUES (
                        :id, :content, :vector, :source_name, :doc_id, :chunk_index,
                        :tenant_id, :project_id, :scope, :metadata_json,
                        :embedding_model, :page_no, :element_refs, :token_count,
                        :chunk_version, :updated_at
                    ) ON CONFLICT (id) DO UPDATE SET
                        content=excluded.content,
                        vector=excluded.vector,
                        metadata_json=excluded.metadata_json,
                        embedding_model=excluded.embedding_model,
                        updated_at=excluded.updated_at
                """), {
                    **chunk,
                    "source_name": source_name,
                    "doc_id": document_id,
                    "tenant_id": context.tenant_id,
                    "project_id": project_id,
                    "scope": scope,
                    "metadata_json": json.dumps(metadata, ensure_ascii=False),
                    "embedding_model": embedding_model,
                    "page_no": chunk.get("page_no"),
                    "element_refs": json.dumps(chunk.get("element_refs", [])),
                })
            await conn.execute(text("""
                UPDATE knowledge_documents
                SET status='active', updated_at=CURRENT_TIMESTAMP, version=version+1
                WHERE id=:document_id AND tenant_id=:tenant_id
            """), {"document_id": document_id, "tenant_id": context.tenant_id})
            await conn.execute(text("""
                UPDATE knowledge_ingest_jobs
                SET status='succeeded', updated_at=CURRENT_TIMESTAMP, version=version+1
                WHERE id=:job_id AND tenant_id=:tenant_id
            """), {"job_id": job_id, "tenant_id": context.tenant_id})

    async def fail_job(
        self, context: RequestContext, document_id: str, job_id: str, error: str
    ) -> None:
        async with engine.begin() as conn:
            await conn.execute(text("""
                UPDATE knowledge_documents
                SET status='failed', updated_at=CURRENT_TIMESTAMP, version=version+1
                WHERE id=:document_id AND tenant_id=:tenant_id
            """), {"document_id": document_id, "tenant_id": context.tenant_id})
            await conn.execute(text("""
                UPDATE knowledge_ingest_jobs
                SET status='failed', error=:error, updated_at=CURRENT_TIMESTAMP,
                    version=version+1
                WHERE id=:job_id AND tenant_id=:tenant_id
            """), {
                "job_id": job_id,
                "tenant_id": context.tenant_id,
                "error": error[:1000],
            })

    async def record_retrieval(
        self,
        context: RequestContext,
        *,
        query: str,
        query_type: str,
        scope: str,
        filters: dict,
        hit_ids: list[str],
        index_version: str,
        latency_ms: float,
        status: str,
    ) -> str:
        run_id = "ret_" + uuid.uuid4().hex
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO retrieval_runs (
                    id, tenant_id, project_id, query, query_type, scope,
                    filters_json, hit_ids, index_version, latency_ms, status,
                    created_by, version
                ) VALUES (
                    :id, :tenant_id, :project_id, :query, :query_type, :scope,
                    :filters_json, :hit_ids, :index_version, :latency_ms, :status,
                    :created_by, 1
                )
            """), {
                "id": run_id,
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
                "query": query,
                "query_type": query_type,
                "scope": scope,
                "filters_json": json.dumps(filters, ensure_ascii=False),
                "hit_ids": json.dumps(hit_ids),
                "index_version": index_version,
                "latency_ms": latency_ms,
                "status": status,
                "created_by": context.user_id,
            })
        return run_id

    async def get_retrieval(self, context: RequestContext, run_id: str) -> dict | None:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, query, query_type, scope,
                       filters_json, hit_ids, index_version, latency_ms, status,
                       created_by, created_at
                FROM retrieval_runs
                WHERE id=:id AND tenant_id=:tenant_id
                  -- A missing project context means a tenant-scoped run, not
                  -- "any project".  Never let a caller omit project_id to
                  -- read another project's retrieval audit.
                  AND ((:project_id IS NULL AND project_id IS NULL)
                       OR project_id=:project_id)
            """), {
                "id": run_id,
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
            })).mappings().first()
        if not row:
            return None
        data = dict(row)
        data["filters"] = json.loads(data.pop("filters_json") or "{}")
        data["hit_ids"] = json.loads(data["hit_ids"] or "[]")
        return data

    async def record_feedback(
        self,
        context: RequestContext,
        retrieval_run_id: str,
        chunk_id: str | None,
        label: str,
    ) -> str:
        feedback_id = "kfb_" + uuid.uuid4().hex
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO knowledge_feedback (
                    id, retrieval_run_id, chunk_id, tenant_id, project_id,
                    label, operator, created_by, version
                ) VALUES (
                    :id, :retrieval_run_id, :chunk_id, :tenant_id, :project_id,
                    :label, :operator, :created_by, 1
                )
            """), {
                "id": feedback_id, "retrieval_run_id": retrieval_run_id,
                "chunk_id": chunk_id, "tenant_id": context.tenant_id,
                "project_id": context.project_id, "label": label,
                "operator": context.user_id, "created_by": context.user_id,
            })
        return feedback_id

    @staticmethod
    def _document_row(row) -> dict:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data
