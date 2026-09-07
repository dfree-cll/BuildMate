"""Canonical embedding and lexical-scoring primitives for the RAG layer.

The legacy ``backend.core.knowledge_base`` module still owns storage adapters
for compatibility, but vectorization is a RAG concern.  Keeping it here makes
retrieval, ingestion and evaluation use the same tokenizer and embedding
fallbacks without importing an application-level storage module.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re

from backend.config import get_settings
from backend.core.logger import get_logger


logger = get_logger(__name__)
DIM = 256


class TextVectorizer:
    """Text embedding with local BGE, OpenAI-compatible API and hash fallback."""

    _client = None
    _local_model = None
    _local_tokenizer = None
    # Missing optional models are common in the lightweight local profile.
    # Cache the failed path so every query does not re-import torch/transformers.
    _local_unavailable_path: str | None = None

    @staticmethod
    def _hash_vec(text: str) -> list[float]:
        """Character n-gram hash vector used for deterministic offline mode."""

        vec = [0.0] * DIM
        tokens = re.findall(r"[\u4e00-\u9fa5]|[a-zA-Z0-9]+", text.lower())
        for token in tokens:
            grams = [token[i:i + 2] for i in range(max(1, len(token) - 1))] or [token]
            for gram in grams:
                digest = int(hashlib.md5(gram.encode()).hexdigest()[:8], 16)
                vec[digest % DIM] += 1.0
        norm = math.sqrt(sum(value * value for value in vec)) or 1.0
        return [value / norm for value in vec]

    @classmethod
    async def embed(cls, texts: list[str]) -> list[list[float]]:
        settings = get_settings()
        if getattr(settings, "rag_local_models_enabled", False):
            try:
                return await cls._local_bge_embed(texts)
            except Exception as exc:
                logger.warning("vectorizer.local_fallback", error=str(exc)[:160])
        else:
            logger.debug("vectorizer.local_model_disabled")
        if settings.embedding_api_key:
            try:
                return await cls._api_embed(texts)
            except Exception as exc:
                logger.warning("vectorizer.api_fallback", error=str(exc)[:160])
        return [cls._hash_vec(text) for text in texts]

    @classmethod
    def _get_local_bge(cls):
        """Load the configured local BGE model exactly once."""

        import os
        if cls._local_model is not None:
            return cls._local_model, cls._local_tokenizer

        models_root = get_settings().models_root
        model_root = os.path.join(models_root, "embedding", "bge-m3")
        # Check the filesystem before importing heavyweight optional packages.
        if cls._local_unavailable_path == model_root:
            raise FileNotFoundError("本地 BGE 模型未找到")
        if not os.path.isdir(model_root):
            cls._local_unavailable_path = model_root
            raise FileNotFoundError("本地 BGE 模型未找到")
        model_dir = model_root
        if os.path.isdir(os.path.join(model_dir, "snapshots")):
            snapshots = sorted(os.listdir(os.path.join(model_dir, "snapshots")))
            if snapshots:
                model_dir = os.path.join(model_dir, "snapshots", snapshots[-1])
        if not os.path.isfile(os.path.join(model_dir, "config.json")):
            cls._local_unavailable_path = model_root
            raise FileNotFoundError("本地 BGE 模型配置不存在")
        try:
            from transformers import AutoModel, AutoTokenizer
            cls._local_tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
            cls._local_model = AutoModel.from_pretrained(model_dir, local_files_only=True)
        except (ImportError, OSError, RuntimeError, ValueError, NotImplementedError):
            # A partially downloaded/incompatible model must not be retried on
            # every chat request.  The caller will use the deterministic hash
            # embedding until the configured model path changes.
            cls._local_model = None
            cls._local_tokenizer = None
            cls._local_unavailable_path = model_root
            raise
        cls._local_model.eval()
        logger.info("vectorizer.local_bge_loaded", model_dir=model_dir)
        return cls._local_model, cls._local_tokenizer

    @classmethod
    async def _local_bge_embed(cls, texts: list[str]) -> list[list[float]]:
        # Discovery/loading runs off the event loop.  A large local model must
        # not pause unrelated API requests while it is being initialized.
        model, tokenizer = await asyncio.to_thread(cls._get_local_bge)
        import torch

        def encode() -> list[list[float]]:
            with torch.no_grad():
                encoded = tokenizer(
                    texts, padding=True, truncation=True, max_length=512,
                    return_tensors="pt",
                )
                outputs = model(**encoded)
                vectors = outputs.last_hidden_state[:, 0].cpu().numpy()
                norms = (vectors ** 2).sum(axis=1, keepdims=True) ** 0.5
                return (vectors / norms).tolist()

        try:
            return await asyncio.to_thread(encode)
        except Exception:
            # Mark a bad local model unavailable just like a failed load.  This
            # prevents repeated multi-second attempts after a meta-tensor or
            # corrupt-weight error.
            import os
            cls._local_model = None
            cls._local_tokenizer = None
            cls._local_unavailable_path = os.path.join(
                get_settings().models_root, "embedding", "bge-m3"
            )
            raise

    @classmethod
    async def _api_embed(cls, texts: list[str]) -> list[list[float]]:
        import httpx

        settings = get_settings()
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            response = await client.post(
                f"{settings.embedding_base_url}/embeddings",
                headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
                json={"model": settings.embedding_model, "input": texts},
            )
            response.raise_for_status()
            data = response.json()
        return [item["embedding"] for item in data["data"]]


def cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    return sum(left * right for left, right in zip(a, b))


_STOP_WORDS = frozenset({
    "的", "了", "和", "与", "及", "是", "吗", "嘛", "怎么", "怎样",
    "什么", "多少", "请问", "可以", "能", "不能", "有", "没有", "一下",
    "？", "！", "?", "!", "、", "，", ",",
})
_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    """Tokenize English/numeric terms and Chinese 2/3-grams for BM25."""

    normalized = text.lower()
    tokens = re.findall(r"[a-z0-9]+", normalized)
    cjk = "".join(re.findall(r"[\u4e00-\u9fa5]", normalized))
    if cjk:
        tokens.extend(cjk[index:index + 2] for index in range(len(cjk) - 1))
        tokens.extend(cjk[index:index + 3] for index in range(len(cjk) - 2))
    return [token for token in tokens if token not in _STOP_WORDS and len(token) >= 2]


def _bm25_score(query_tokens: list[str], document: str, df: dict[str, int],
                document_count: int, average_length: float) -> float:
    document_lower = document.lower()
    document_length = max(1, len(_tokenize(document)))
    score = 0.0
    for token in set(query_tokens):
        if token not in df:
            continue
        term_frequency = document_lower.count(token)
        if term_frequency == 0:
            continue
        inverse_frequency = math.log(
            1 + (document_count - df[token] + 0.5) / (df[token] + 0.5)
        )
        normalized_frequency = term_frequency * (_K1 + 1) / (
            term_frequency + _K1 * (
                1 - _B + _B * document_length / average_length
            )
        )
        score += (2.0 if len(token) >= 3 else 1.0) * inverse_frequency * normalized_frequency
    return score


def _build_bm25_index(documents: list[str]) -> dict:
    document_frequency: dict[str, int] = {}
    total_length = 0
    for document in documents:
        tokens = set(_tokenize(document))
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
        total_length += max(1, len(_tokenize(document)))
    return {
        "df": document_frequency,
        "avg_dl": total_length / max(1, len(documents)),
        "doc_count": len(documents),
    }


def _sparse_bm25(query: str, contents: list[str]) -> list[float]:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return [0.0] * len(contents)
    index = _build_bm25_index(contents)
    return [
        _bm25_score(query_tokens, document, index["df"], index["doc_count"], index["avg_dl"])
        for document in contents
    ]


def _build_sparse_vec(value: str) -> dict[int, float]:
    """Build the sparse token vector expected by Milvus."""

    frequencies: dict[int, float] = {}
    for token in _tokenize(value):
        token_id = int(hashlib.md5(token.encode()).hexdigest()[:8], 16)
        frequencies[token_id] = frequencies.get(token_id, 0.0) + 1.0
    return frequencies or {0: 0.0}


__all__ = [
    "DIM", "TextVectorizer", "cosine_sim", "_tokenize", "_sparse_bm25",
    "_build_sparse_vec",
]
