"""Evidence-first retrieval augmented generation platform."""

from backend.rag.contracts import (
    KnowledgeHit,
    KnowledgeScope,
    KnowledgeSearchRequest,
    RAGAnswer,
)
from backend.rag.service import RAGService, get_rag_service

__all__ = [
    "KnowledgeHit",
    "KnowledgeScope",
    "KnowledgeSearchRequest",
    "RAGAnswer",
    "RAGService",
    "get_rag_service",
]
