"""Tenant-scoped hybrid retrieval with optional BGE reranking."""

from __future__ import annotations

import json
import asyncio
import time
from typing import Any

from sqlalchemy import text

from backend.rag.vectorization import TextVectorizer, _sparse_bm25, cosine_sim
from backend.db.session import engine
from backend.rag.contracts import KnowledgeHit, KnowledgeScope, KnowledgeSearchRequest
from backend.config import get_settings
from backend.domain.errors import DependencyFailure


def _safe_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class ScopedHybridRetriever:
    """SQLite/PostgreSQL retrieval used by local mode and deterministic tests."""

    async def search(self, request: KnowledgeSearchRequest) -> list[KnowledgeHit]:
        params = {
            "tenant_id": request.tenant_id,
            "project_id": request.project_id,
        }
        if request.scope == KnowledgeScope.GLOBAL:
            scope_clause = "c.scope='global'"
        elif request.scope == KnowledgeScope.TENANT:
            scope_clause = "c.scope IN ('global', 'tenant')"
        else:
            scope_clause = "(c.scope IN ('global', 'tenant') OR (c.scope='project' AND c.project_id=:project_id))"

        async with engine.connect() as conn:
            rows = (await conn.execute(text(f"""
                SELECT c.id AS id, c.content AS content, c.vector AS vector,
                       c.source_name AS source_name, c.doc_id AS doc_id,
                       c.chunk_index AS chunk_index, c.project_id AS project_id,
                       c.scope AS scope, c.metadata_json AS metadata_json,
                       c.page_no AS page_no, c.element_refs AS element_refs,
                       c.embedding_model AS embedding_model,
                       c.chunk_version AS chunk_version
                FROM knowledge_chunks AS c
                LEFT JOIN knowledge_documents AS d ON d.id=c.doc_id AND d.tenant_id=c.tenant_id
                WHERE c.tenant_id=:tenant_id AND ({scope_clause})
                  AND (d.id IS NULL OR d.status='active')
            """), params)).mappings().all()
        if not rows:
            return []

        filtered = [row for row in rows if self._matches_filters(row, request.filters)]
        if not filtered:
            return []

        qvec = (await TextVectorizer.embed([request.query]))[0]
        vector_scores: dict[str, float] | None = None
        vector_degradation: str | None = None
        settings = get_settings()
        if settings.milvus_host and settings.vector_backend in {"", "auto", "milvus"}:
            try:
                from backend.rag.milvus import MilvusKnowledgeIndex

                vector_scores = await asyncio.to_thread(
                    MilvusKnowledgeIndex().search,
                    qvec,
                    tenant_id=request.tenant_id,
                    project_id=request.project_id,
                    scope=request.scope.value,
                    limit=max(request.top_k * 3, 12),
                )
            except Exception as exc:
                vector_degradation = f"{type(exc).__name__}: {str(exc)[:160]}"
                if settings.vector_backend == "milvus":
                    raise DependencyFailure(f"Milvus retrieval failed: {str(exc)[:300]}") from exc
        contents = [row["content"] for row in filtered]
        sparse = _sparse_bm25(request.query, contents)
        sparse_max = max(sparse) if sparse else 0.0
        candidates: list[dict] = []
        for index, row in enumerate(filtered):
            try:
                vector = json.loads(row["vector"])
                dense_score = (
                    vector_scores.get(row["id"], 0.0)
                    if vector_scores is not None
                    else max(0.0, cosine_sim(qvec, vector))
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                dense_score = 0.0
            sparse_score = sparse[index] / sparse_max if sparse_max > 0 else 0.0
            combined = 0.7 * dense_score + 0.3 * sparse_score
            candidates.append({
                "row": row,
                "content": row["content"],
                "score": combined,
                "dense_score": dense_score,
                "sparse_score": sparse_score,
                "metadata": _safe_json(row["metadata_json"], {}),
            })
        candidates.sort(key=lambda item: item["score"], reverse=True)
        recalled = candidates[:max(request.top_k * 3, 12)]

        rerank_scores: dict[str, float] = {}
        rerank_degradation: str | None = None
        if getattr(settings, "rag_reranker_enabled", False):
            try:
                from backend.rag.reranking import rerank_results

                ranked, _ = await rerank_results(request.query, recalled, top_k=request.top_k)
                for item in ranked:
                    rerank_scores[item["row"]["id"]] = float(item["score"])
                recalled.sort(
                    key=lambda item: rerank_scores.get(item["row"]["id"], item["score"]),
                    reverse=True,
                )
            except (FileNotFoundError, ImportError, OSError, RuntimeError, ValueError,
                    NotImplementedError) as exc:
                # The reranker is an optional optimization.  Keep deterministic
                # hybrid scores and expose the degradation in hit metadata.
                rerank_degradation = f"{type(exc).__name__}: {str(exc)[:160]}"
        else:
            rerank_degradation = "disabled_by_config"

        hits: list[KnowledgeHit] = []
        for item in recalled[:request.top_k]:
            row = item["row"]
            rerank = rerank_scores.get(row["id"], 0.0)
            score = rerank if rerank_scores else item["score"]
            hits.append(KnowledgeHit(
                chunk_id=row["id"],
                content=row["content"],
                source_name=row["source_name"] or "",
                document_id=row["doc_id"] or "",
                page_no=row["page_no"],
                score=round(float(score), 6),
                dense_score=round(float(item["dense_score"]), 6),
                sparse_score=round(float(item["sparse_score"]), 6),
                rerank_score=round(float(rerank), 6),
                metadata={
                    **item["metadata"],
                    "scope": row["scope"],
                    "project_id": row["project_id"],
                    "chunk_index": row["chunk_index"],
                    "element_refs": _safe_json(row["element_refs"], []),
                    "embedding_model": row["embedding_model"],
                    "chunk_version": row["chunk_version"],
                    "reranker_used": bool(rerank_scores),
                    "reranker_degradation": rerank_degradation,
                    "vector_backend": "milvus" if vector_scores is not None else "database-local",
                    "vector_degradation": vector_degradation,
                },
            ))
        return hits

    @staticmethod
    def _matches_filters(row, filters: dict) -> bool:
        if not filters:
            return True
        metadata = _safe_json(row["metadata_json"], {})
        return all(metadata.get(key) == value for key, value in filters.items())
