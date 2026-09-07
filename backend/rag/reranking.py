"""Optional semantic reranking for the RAG retrieval layer."""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from backend.core.logger import get_logger


logger = get_logger(__name__)
RERANK_MAX_INPUT_CHARS = 1200
RERANK_CONFIDENCE_THRESHOLD = 0.75


class RerankerUnavailable(RuntimeError):
    """Optional reranker cannot be loaded or executed; hybrid scores remain valid."""


def _resolve_model_path(name: str) -> str:
    from backend.config import get_settings

    return os.path.join(get_settings().models_root, name)


class BGEReranker:
    """BGE CrossEncoder reranker, loaded lazily and reused by the process."""

    _instance: Optional["BGEReranker"] = None
    _unavailable_path: str | None = None

    def __init__(self):
        self._model_path = _resolve_model_path(
            os.path.join("reranker", "bge-reranker-large")
        )
        # Avoid importing the heavyweight sentence-transformers stack when an
        # optional local reranker is not installed.
        if self.__class__._unavailable_path == self._model_path:
            raise FileNotFoundError(f"Reranker 模型未找到: {self._model_path}")
        if not os.path.isdir(self._model_path):
            self.__class__._unavailable_path = self._model_path
            raise FileNotFoundError(f"Reranker 模型未找到: {self._model_path}")
        try:
            from sentence_transformers import CrossEncoder
            logger.info("reranker.loading", model_path=self._model_path)
            self._model = CrossEncoder(self._model_path, max_length=512)
        except (ImportError, OSError, RuntimeError, ValueError, NotImplementedError):
            # A partially downloaded or incompatible optional model is not a
            # reason to fail knowledge search.  Cache this exact path so every
            # request does not import/load the heavyweight stack again.
            self.__class__._unavailable_path = self._model_path
            raise
        logger.info("reranker.loaded")

    @classmethod
    def get_instance(cls) -> "BGEReranker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def rerank(
        self, query: str, candidates: list[dict], top_k: int = 3
    ) -> tuple[list[dict], float]:
        if not candidates:
            return [], 0.0
        pairs = [
            [query, candidate.get("content", "")[:RERANK_MAX_INPUT_CHARS]]
            for candidate in candidates
        ]
        scores = self._model.predict(pairs)
        ranked = []
        for candidate, score in zip(candidates, scores):
            item = dict(candidate)
            item["score"] = round(float(score), 4)
            item["dense_score"] = candidate.get("dense_score", 0)
            ranked.append(item)
        ranked.sort(key=lambda item: item["score"], reverse=True)
        top = ranked[:top_k]
        return top, (top[0]["score"] if top else 0.0)


async def rerank_results(
    query: str, candidates: list[dict], top_k: int = 3
) -> tuple[list[dict], float]:
    """Run CPU-bound CrossEncoder work outside the async event loop."""

    if not candidates:
        return [], 0.0
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, BGEReranker.get_instance().rerank, query, candidates, top_k
        )
    except (FileNotFoundError, ImportError, OSError, RuntimeError, ValueError,
            NotImplementedError) as exc:
        # A model may exist but still be incompatible with the installed torch
        # runtime (for example a meta-tensor load).  Mark this exact path as
        # unavailable so subsequent requests immediately use hybrid scores.
        instance = BGEReranker._instance
        if instance is not None:
            BGEReranker._unavailable_path = instance._model_path
        BGEReranker._instance = None
        raise RerankerUnavailable(str(exc)) from exc


__all__ = [
    "BGEReranker", "RerankerUnavailable", "RERANK_MAX_INPUT_CHARS", "RERANK_CONFIDENCE_THRESHOLD",
    "rerank_results",
]
