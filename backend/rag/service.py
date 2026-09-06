"""RAG ingestion, retrieval audit and grounded-answer policy."""

from __future__ import annotations

import hashlib
import asyncio
import json
import re
import time
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import IntegrityError

from backend.config import get_settings
from backend.rag.vectorization import TextVectorizer
from backend.domain.contracts import RequestContext
from backend.domain.errors import (
    CitationValidationFailure,
    DependencyFailure,
    ResourceNotFound,
    ValidationFailure,
)
from backend.rag.chunking import CHUNKING_VERSION, split_text
from backend.rag.contracts import (
    Citation,
    KnowledgeDocumentCreate,
    KnowledgeHit,
    KnowledgeSearchRequest,
    RAGAnswer,
)
from backend.rag.repository import RAGRepository
from backend.rag.retrieval import ScopedHybridRetriever
from backend.core.metrics import RAG_ABSTAINS, RAG_HITS, RAG_REQUESTS

PARSER_VERSION = "2.0-text"
PIPELINE_VERSION = "2.0"


class _GeneratedRAGPayload(BaseModel):
    answer: str = Field(..., min_length=1, max_length=8000)
    citations: list[dict[str, str]] = Field(min_length=1, max_length=12)
    confidence: float = Field(..., ge=0.0, le=1.0)


class RAGService:
    def __init__(self, repository: RAGRepository | None = None, retriever=None):
        self._repository = repository or RAGRepository()
        self._retriever = retriever or ScopedHybridRetriever()

    async def ingest(
        self,
        context: RequestContext,
        request: KnowledgeDocumentCreate,
        *,
        parser_version: str = PARSER_VERSION,
    ) -> tuple[dict, bool]:
        from backend.db.rls import activate_rls_context
        activate_rls_context(context)
        content_hash = hashlib.sha256(request.content.encode("utf-8")).hexdigest()
        embedding_model = get_settings().embedding_model or "local-hash"
        existing = await self._repository.find_document_version(
            context,
            project_id=request.project_id,
            content_hash=content_hash,
            parser_version=parser_version,
            chunking_version=CHUNKING_VERSION,
            embedding_model=embedding_model,
        )
        if existing:
            return existing, False

        document_id = "doc_" + uuid.uuid4().hex
        job_id = "ing_" + uuid.uuid4().hex
        try:
            result = await self._repository.create_document_and_job(
                context,
                document_id=document_id,
                job_id=job_id,
                project_id=request.project_id,
                scope=request.scope.value,
                source_type=request.source_type,
                source_uri=request.source_uri,
                title=request.title,
                document_version=request.document_version,
                content_hash=content_hash,
                parser_version=parser_version,
                chunking_version=CHUNKING_VERSION,
                embedding_model=embedding_model,
                metadata=request.metadata,
                pipeline_version=PIPELINE_VERSION,
            )
        except IntegrityError:
            existing = await self._repository.find_document_version(
                context,
                project_id=request.project_id,
                content_hash=content_hash,
                parser_version=parser_version,
                chunking_version=CHUNKING_VERSION,
                embedding_model=embedding_model,
            )
            if existing:
                return existing, False
            raise
        try:
            raw_chunks = split_text(request.content)
            if not raw_chunks:
                raise ValueError("document contains no indexable text")
            vectors = await TextVectorizer.embed(raw_chunks)
            chunks = []
            now = int(time.time())
            for index, (content, vector) in enumerate(zip(raw_chunks, vectors)):
                chunk_id = hashlib.sha256(
                    f"{document_id}:{CHUNKING_VERSION}:{index}:{content}".encode("utf-8")
                ).hexdigest()
                chunks.append({
                    "id": chunk_id,
                    "content": content,
                    "vector": json.dumps(vector),
                    "chunk_index": index,
                    "page_no": (
                        int(page.group(1))
                        if (page := re.search(r"(?m)^# Page (\d+)\s*$", content))
                        else request.metadata.get("page_no")
                    ),
                    "element_refs": request.metadata.get("element_refs", []),
                    "token_count": max(1, len(content) // 2),
                    "chunk_version": CHUNKING_VERSION,
                    "updated_at": now,
                })
            await self._repository.save_chunks(
                context,
                document_id=document_id,
                job_id=job_id,
                project_id=request.project_id,
                scope=request.scope.value,
                source_name=request.title,
                embedding_model=embedding_model,
                metadata={**request.metadata, "source_type": request.source_type},
                chunks=chunks,
            )
            index_degradation = None
            settings = get_settings()
            if settings.milvus_host and settings.vector_backend in {"", "auto", "milvus"}:
                try:
                    from backend.rag.milvus import MilvusKnowledgeIndex

                    await asyncio.to_thread(
                        MilvusKnowledgeIndex().upsert,
                        tenant_id=context.tenant_id,
                        project_id=request.project_id,
                        scope=request.scope.value,
                        document_id=document_id,
                        source_name=request.title,
                        metadata={**request.metadata, "source_type": request.source_type},
                        chunks=chunks,
                    )
                except Exception as exc:
                    index_degradation = f"{type(exc).__name__}: {str(exc)[:200]}"
                    if settings.vector_backend == "milvus":
                        raise
        except Exception as error:
            await self._repository.fail_job(
                context, document_id, job_id, f"{type(error).__name__}: {error}"
            )
            raise DependencyFailure("knowledge ingestion failed") from error
        return {
            **result,
            "status": "active",
            "chunk_count": len(chunks),
            "index_degradation": index_degradation,
        }, True

    async def search(
        self, context: RequestContext, request: KnowledgeSearchRequest
    ) -> tuple[list[KnowledgeHit], str]:
        from backend.db.rls import activate_rls_context
        activate_rls_context(context)
        if request.tenant_id != context.tenant_id:
            raise ValidationFailure("tenant_id does not match authenticated context")
        if request.project_id != context.project_id:
            raise ValidationFailure("project_id does not match authenticated context")
        start = time.perf_counter()
        try:
            hits = await self._retriever.search(request)
            status = "succeeded"
        except Exception:
            hits = []
            status = "failed"
            raise
        finally:
            latency = (time.perf_counter() - start) * 1000
            run_id = await self._repository.record_retrieval(
                context,
                query=request.query,
                query_type="hybrid",
                scope=request.scope.value,
                filters=request.filters,
                hit_ids=[hit.chunk_id for hit in hits],
                index_version=PIPELINE_VERSION,
                latency_ms=latency,
                status=status,
            )
            RAG_REQUESTS.labels(request.scope.value, status).inc()
            RAG_HITS.observe(len(hits))
        return hits, run_id

    async def get_retrieval(self, context: RequestContext, run_id: str) -> dict | None:
        return await self._repository.get_retrieval(context, run_id)

    async def answer(
        self,
        context: RequestContext,
        request: KnowledgeSearchRequest,
        *,
        min_score: float = 0.05,
        memory_context: str = "",
    ) -> RAGAnswer:
        """Generate only from retrieved hits and validate every citation."""
        hits, run_id = await self.search(context, request)
        if not hits or max(hit.score for hit in hits) < min_score:
            RAG_ABSTAINS.labels(request.scope.value).inc()
            return self.abstain(run_id, "当前知识库没有足够证据，无法可靠回答。")

        settings = get_settings()
        if settings.mock_mode:
            # Offline mode performs extractive QA, never inventing unavailable facts.
            selected = hits[: min(3, len(hits))]
            citations = [
                Citation(
                    chunk_id=hit.chunk_id, document_id=hit.document_id,
                    source_name=hit.source_name, page_no=hit.page_no,
                ) for hit in selected
            ]
            answer = "\n\n".join(hit.content[:800] for hit in selected)
            return RAGAnswer(
                answer=answer,
                citations=citations,
                confidence=min(1.0, max(hit.score for hit in selected)),
                grounded=True,
                abstained=False,
                retrieval_run_id=run_id,
            )

        context_text = "\n\n".join(
            f"[chunk_id={hit.chunk_id}; source={hit.source_name}; page={hit.page_no}]\n{hit.content}"
            for hit in hits
        )
        system = SystemMessage(content=(
            "你是建筑工程知识助手。只能根据提供的证据回答。"
            "输出严格 JSON：answer、citations（每项仅含 chunk_id）、confidence。"
            "不得引用未提供的 chunk_id；证据不足必须明确拒答。"
        ))
        from backend.application.agent_memory import memory_prompt
        answer_prompt = memory_prompt({"memory_context": memory_context},
                                      f"问题：{request.query}\n\n证据（唯一允许引用来源）：\n{context_text}")
        prompt = HumanMessage(content=answer_prompt)
        last_error: Exception | None = None
        for _ in range(2):
            try:
                from backend.core.llm_factory import get_llm

                response = await asyncio.wait_for(
                    get_llm("qa", temperature=0).ainvoke([system, prompt]),
                    timeout=min(60.0, settings.orchestrator_task_timeout),
                )
                raw = str(getattr(response, "content", "")).strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                payload = _GeneratedRAGPayload.model_validate(json.loads(raw))
                citations = self.validate_citations(payload.citations, hits)
                return RAGAnswer(
                    answer=payload.answer, citations=citations,
                    confidence=payload.confidence, grounded=True,
                    abstained=False, retrieval_run_id=run_id,
                )
            except (asyncio.TimeoutError, json.JSONDecodeError, ValidationError, CitationValidationFailure) as exc:
                last_error = exc
                prompt = HumanMessage(content=(
                    f"上次输出不合法：{type(exc).__name__}。请仅输出规定 JSON。\n\n"
                    + answer_prompt
                ))
        raise DependencyFailure(f"grounded answer validation failed: {type(last_error).__name__}")

    async def add_feedback(
        self,
        context: RequestContext,
        *,
        retrieval_run_id: str,
        chunk_id: str | None,
        label: str,
    ) -> str:
        retrieval = await self._repository.get_retrieval(context, retrieval_run_id)
        if retrieval is None:
            raise ResourceNotFound("retrieval run not found in authenticated scope")
        if chunk_id is not None and chunk_id not in retrieval["hit_ids"]:
            raise CitationValidationFailure("feedback chunk was not part of the retrieval run")
        return await self._repository.record_feedback(
            context, retrieval_run_id, chunk_id, label
        )

    @staticmethod
    def validate_citations(
        citations: list[dict[str, Any]], hits: list[KnowledgeHit]
    ) -> list[Citation]:
        allowed = {hit.chunk_id: hit for hit in hits}
        validated: list[Citation] = []
        for citation in citations:
            chunk_id = str(citation.get("chunk_id", ""))
            hit = allowed.get(chunk_id)
            if not hit:
                raise CitationValidationFailure(
                    f"citation references an unretrieved chunk: {chunk_id}"
                )
            validated.append(Citation(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                source_name=hit.source_name,
                page_no=hit.page_no,
            ))
        return validated

    @staticmethod
    def abstain(retrieval_run_id: str, reason: str) -> RAGAnswer:
        return RAGAnswer(
            answer=reason,
            citations=[],
            confidence=0.0,
            grounded=False,
            abstained=True,
            retrieval_run_id=retrieval_run_id,
        )


_rag_service: RAGService | None = None


def get_rag_service() -> RAGService:
    global _rag_service
    if _rag_service is None:
        _rag_service = RAGService()
    return _rag_service
