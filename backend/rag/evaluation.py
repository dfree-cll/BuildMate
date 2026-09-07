"""Deterministic retrieval metrics used by the golden RAG evaluation set."""

from __future__ import annotations

import math


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 1.0
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    for rank, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def citation_coverage(claim_citation_counts: list[int]) -> float:
    if not claim_citation_counts:
        return 1.0
    return sum(count > 0 for count in claim_citation_counts) / len(claim_citation_counts)


def ndcg_at_k(retrieved: list[str], relevance: dict[str, float], k: int) -> float:
    """Normalized discounted cumulative gain for graded golden-set labels."""
    scores = [max(0.0, relevance.get(item, 0.0)) for item in retrieved[:k]]
    dcg = sum((2 ** score - 1) / math.log2(rank + 2) for rank, score in enumerate(scores))
    ideal = sorted((max(0.0, value) for value in relevance.values()), reverse=True)[:k]
    idcg = sum((2 ** score - 1) / math.log2(rank + 2) for rank, score in enumerate(ideal))
    return dcg / idcg if idcg else 1.0
