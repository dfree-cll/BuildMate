"""Compatibility facade for the canonical :mod:`backend.rag.reranking` API.

New code must import reranking from ``backend.rag``.  This module remains so
older agents and integrations can migrate without a breaking import change.
"""

from backend.rag.reranking import (
    BGEReranker,
    RERANK_CONFIDENCE_THRESHOLD,
    RERANK_MAX_INPUT_CHARS,
    rerank_results,
)

__all__ = [
    "BGEReranker", "RERANK_MAX_INPUT_CHARS", "RERANK_CONFIDENCE_THRESHOLD",
    "rerank_results",
]
