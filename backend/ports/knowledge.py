"""Shared knowledge/ evidence port used by QA and domain workflows.

The concrete implementation is ``backend.rag.service.RAGService``.  Business
agents depend on this boundary instead of importing a vector database client;
this keeps retrieval scope, citation validation and future index changes in a
single platform capability.
"""

from __future__ import annotations

from typing import Protocol

from backend.domain.contracts import RequestContext
from backend.rag.contracts import KnowledgeHit, KnowledgeSearchRequest, RAGAnswer


class KnowledgeService(Protocol):
    async def search(
        self, context: RequestContext, request: KnowledgeSearchRequest
    ) -> tuple[list[KnowledgeHit], str]:
        """Return tenant/project-scoped hits and the persisted retrieval run ID."""

    async def answer(
        self,
        context: RequestContext,
        request: KnowledgeSearchRequest,
        *,
        min_score: float = 0.05,
        memory_context: str = "",
    ) -> RAGAnswer:
        """Generate a grounded answer or an explicit abstention."""
