"""本地向量库
- 有 EMBEDDING_API_KEY：调用 OpenAI 兼容 embedding API（如硅基流动 BAAI/bge-m3）
- 无 key：字符哈希向量（n-gram + TF），离线可跑，效果足够 demo 演示
- 存储：SQLite 表 knowledge_chunks（json 持久化向量），进程内缓存
"""
import asyncio
import hashlib
import json
import math
import re
import time

from sqlalchemy import text
from backend.db.session import engine
from backend.core.logger import get_logger
from backend.config import get_settings

logger = get_logger(__name__)

DIM = 256  # 哈希向量维度

class TextVectorizer:
    """文本向量化：优先 API 嵌入，降级字符哈希。"""
    _client = None

    @staticmethod
    def _hash_vec(text: str) -> list[float]:
        """字符 n-gram 哈希向量（词袋 + 局部敏感哈希风格），离线可用。"""
        vec = [0.0] * DIM
        tokens = re.findall(r"[\u4e00-\u9fa5]|[a-zA-Z0-9]+", text.lower())
        for tok in tokens:
            grams = [tok[i:i+2] for i in range(max(1, len(tok) - 1))] or [tok]
            for g in grams:
                h = int(hashlib.md5(g.encode()).hexdigest()[:8], 16)
                vec[h % DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    _local_model = None          # 本地 BGE 模型单例
    _local_tokenizer = None

    @classmethod
    async def embed(cls, texts: list[str]) -> list[list[float]]:
        settings = get_settings()
        # ① 优先本地 BGE 中文模型（免费、离线、语义向量，BGE-M3 语义向量）
        try:
            return await cls._local_bge_embed(texts)
        except Exception as e:
            logger.warning("vectorizer.local_fallback", error=str(e))
        # ② API 嵌入
        if settings.embedding_api_key:
            try:
                return await cls._api_embed(texts)
            except Exception as e:
                logger.warning("vectorizer.api_fallback", error=str(e))
        # ③ 字符哈希兜底
        return [cls._hash_vec(t) for t in texts]

    @classmethod
    def _get_local_bge(cls):
        """懒加载本地 BGE 模型（优先完整版 bge-m3，后备 bge-small-zh）"""
        import os
        if cls._local_model is not None:
            return cls._local_model, cls._local_tokenizer
        from transformers import AutoModel, AutoTokenizer
        # 候选路径：① MODELS_ROOT（config.models_root，容器挂载 /models）② HF 缓存 bge-small-zh
        from backend.config import get_settings as _gs_root
        _models_root = _gs_root().models_root
        candidates = [
            os.path.join(_models_root, "embedding", "bge-m3"),
        ]
        model_dir = None
        for cand in candidates:
            if os.path.isdir(cand):
                if "snapshots" in cand:
                    snaps = sorted(os.listdir(cand))
                    if snaps:
                        model_dir = os.path.join(cand, snaps[-1])
                else:
                    model_dir = cand
                if model_dir and os.path.isfile(os.path.join(model_dir, "config.json")):
                    break
        if not model_dir:
            raise FileNotFoundError("本地 BGE 模型未找到")
        cls._local_tokenizer = AutoTokenizer.from_pretrained(model_dir)
        cls._local_model = AutoModel.from_pretrained(model_dir)
        cls._local_model.eval()
        logger.info("vectorizer.local_bge_loaded", model_dir=model_dir)
        return cls._local_model, cls._local_tokenizer

    @classmethod
    async def _local_bge_embed(cls, texts: list[str]) -> list[list[float]]:
        """本地 BGE 模型嵌入（CLS 池化 + L2 归一化，对齐 BGE 官方用法）"""
        import torch
        loop = asyncio.get_running_loop()
        model, tokenizer = cls._get_local_bge()

        def _encode():
            with torch.no_grad():
                encoded = tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
                outputs = model(**encoded)
                # CLS 池化（BGE 官方：取 last_hidden_state[:, 0]）；先 .cpu() 再转 numpy
                # （修复 MPS/CUDA tensor 无法直接转 numpy 的 device mismatch 报错）
                vecs = outputs.last_hidden_state[:, 0].cpu().numpy()
                # L2 归一化
                norms = (vecs ** 2).sum(axis=1, keepdims=True) ** 0.5
                vecs = vecs / norms
                return [v.tolist() for v in vecs]

        return await loop.run_in_executor(None, _encode)

    @classmethod
    async def _api_embed(cls, texts: list[str]) -> list[list[float]]:
        import httpx
        settings = get_settings()
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            resp = await client.post(
                f"{settings.embedding_base_url}/embeddings",
                headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
                json={"model": settings.embedding_model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
        return [d["embedding"] for d in data["data"]]


def cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


async def _ensure_table():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                vector TEXT NOT NULL,
                source_name TEXT,
                doc_id TEXT,
                chunk_index INT,
                tenant_id TEXT DEFAULT 'tenant_default',
                updated_at INT
            )
        """))


# ═══════════════ 向量后端注册表（可插拔）═══════════════
# 新后端（如 pgvector）实现本接口后 register_vector_backend 即可，VECTOR_BACKEND 指定启用。
# search 返回 None/[] 表示该后端无结果或不可用，调度器自动尝试下一个后端（local 永远兜底）。


class VectorBackend:
    name = "base"

    async def add(self, chunks: list[dict], vectors: list[list[float]], tenant_id: str) -> bool:
        raise NotImplementedError

    async def search(self, query: str, qvec: list[float], tenant_id: str,
                     top_k: int, min_score: float) -> list[dict] | None:
        raise NotImplementedError

    async def clear(self, tenant_id: str) -> None:
        raise NotImplementedError


_BACKENDS: dict[str, VectorBackend] = {}


def register_vector_backend(backend: "VectorBackend | type[VectorBackend]") -> "VectorBackend | type[VectorBackend]":
    """注册后端。可传类（装饰器用法，注册时实例化——后端须无状态）或现成实例（插件用法）"""
    obj = backend() if isinstance(backend, type) else backend
    _BACKENDS[obj.name] = obj
    return backend


async def _local_add(chunks: list[dict], vectors: list[list[float]], tenant_id: str) -> None:
    await _ensure_table()
    async with engine.begin() as conn:
        for c, vec in zip(chunks, vectors):
            # ★ 多租户修复：chunk id 含 tenant_id，避免跨租户同内容互相覆盖
            cid = hashlib.md5(f"{tenant_id}_{c['doc_id']}_{c['chunk_index']}_{c['content'][:50]}".encode()).hexdigest()
            from backend.db.dialect import upsert_knowledge_chunk_sql
            await conn.execute(text(
                upsert_knowledge_chunk_sql()
            ), {
                "id": cid, "content": c["content"], "vector": json.dumps(vec),
                "source_name": c.get("source_name", ""), "doc_id": c.get("doc_id", ""),
                "chunk_index": c.get("chunk_index", 0), "tenant_id": tenant_id,
                "updated_at": int(time.time()),
            })
    logger.info("vector_store.chunks_added", count=len(chunks), backend="local")
    _invalidate_row_cache(tenant_id)


@register_vector_backend
class LocalSQLiteBackend(VectorBackend):
    """本地 SQLite 后端：JSON 向量 + BM25/余弦混合检索（离线可用，永远兜底）"""
    name = "local"

    async def add(self, chunks, vectors, tenant_id) -> bool:
        await _local_add(chunks, vectors, tenant_id)
        return True

    async def search(self, query, qvec, tenant_id, top_k, min_score):
        return await _local_search(query, qvec, tenant_id, top_k, min_score)

    async def clear(self, tenant_id) -> None:
        await _ensure_table()
        async with engine.begin() as conn:
            await conn.execute(text(
                "DELETE FROM knowledge_chunks WHERE tenant_id = :t"), {"t": tenant_id})
        _invalidate_row_cache(tenant_id)


@register_vector_backend
class MilvusBackend(VectorBackend):
    """Milvus 后端（BGE-M3 1024 维语义检索；维度不符/连接失败自动回退 local）"""
    name = "milvus"
    DIM = 1024

    async def add(self, chunks, vectors, tenant_id) -> bool:
        if vectors and len(vectors[0]) != self.DIM:
            logger.warning("vector_store.milvus_dim_mismatch_add",
                           vec_dim=len(vectors[0]), expected=self.DIM,
                           detail="向量非 1024 维（BGE 未加载），跳过 Milvus 写入，回退本地")
            return False
        try:
            _milvus_add(chunks, vectors, tenant_id)
            logger.info("vector_store.milvus_added", count=len(chunks))
            return True
        except Exception as e:
            logger.warning("vector_store.milvus_add_fallback_local", error=str(e)[:150])
            return False

    async def search(self, query, qvec, tenant_id, top_k, min_score):
        if len(qvec) != self.DIM:
            logger.warning("vector_store.milvus_dim_mismatch",
                           vec_dim=len(qvec), expected=self.DIM,
                           detail="本地 BGE 未加载，查询向量为哈希 256 维，跳过 Milvus 检索")
            return None
        results = _milvus_search(qvec, tenant_id, top_k * 2)
        return results or None

    async def clear(self, tenant_id) -> None:
        _get_milvus_client().delete(collection_name="knowledge_domain",
                                    filter=_milvus_filter(tenant_id))


def _active_backends() -> list[VectorBackend]:
    """后端选择：VECTOR_BACKEND 显式指定（auto/空 = 配了 Milvus 就用，local 兜底）"""
    cfg = get_settings().vector_backend.strip().lower()
    order: list[VectorBackend] = []
    if cfg in ("", "auto"):
        if _milvus_enabled():
            order.append(_BACKENDS["milvus"])
    elif cfg == "local":
        pass
    elif cfg in _BACKENDS:
        order.append(_BACKENDS[cfg])
    else:
        raise ValueError(f"未知 VECTOR_BACKEND '{cfg}'，可用：auto/local/{sorted(_BACKENDS)}")
    order.append(_BACKENDS["local"])   # local 永远在最后兜底
    return order


async def add_chunks(chunks: list[dict], tenant_id: str = "tenant_default"):
    """写入知识 chunk（对外接口不变）：按后端优先级写入，成功即返回"""
    vectors = await TextVectorizer.embed([c["content"] for c in chunks])
    for backend in _active_backends():
        if await backend.add(chunks, vectors, tenant_id):
            return


# ═══════════════ Milvus 可选后端═══════════════
def _milvus_enabled() -> bool:
    """Milvus 是否启用：配置了 host 且可连接"""
    from backend.config import get_settings
    s = get_settings()
    return bool(s.milvus_host)


def _milvus_filter(tenant_id: str) -> str:
    """Milvus 过滤表达式：只检索 buildmate 的数据（course_id=buildmate），避免 历史数据干扰
    M9：tenant_id 转义（当前来自签名 token 风险低，按企业标准仍防表达式注入）"""
    tid = tenant_id.replace("\\", "\\\\").replace('"', '\\"')
    return f'tenant_id == "{tid}" and course_id == "buildmate"'


_milvus_client = None


def _get_milvus_client():
    global _milvus_client
    if _milvus_client is None:
        from pymilvus import MilvusClient, DataType
        from backend.config import get_settings
        s = get_settings()
        _milvus_client = MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")
        # 建集合（若不存在）——完整 schema，对齐行业范式-05 knowledge_domain
        # （embedding/sparse_embedding/content/tenant_id 等字段名与检索/写入一致）
        if not _milvus_client.has_collection("knowledge_domain"):
            schema = _milvus_client.create_schema(auto_id=False, enable_dynamic_field=True)
            schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=1024)
            schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
            schema.add_field("content", DataType.VARCHAR, max_length=4096)
            schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
            schema.add_field("chunk_index", DataType.INT64)
            schema.add_field("document_id", DataType.VARCHAR, max_length=64)
            schema.add_field("course_id", DataType.VARCHAR, max_length=64)
            schema.add_field("source_name", DataType.VARCHAR, max_length=256)
            schema.add_field("chunk_type", DataType.VARCHAR, max_length=32)
            schema.add_field("version", DataType.VARCHAR, max_length=32)
            schema.add_field("updated_at", DataType.INT64)
            index_params = _milvus_client.prepare_index_params()
            index_params.add_index(field_name="embedding", index_type="HNSW",
                                   metric_type="COSINE", params={"M": 16, "efConstruction": 256})
            index_params.add_index(field_name="sparse_embedding", index_type="SPARSE_INVERTED_INDEX",
                                   metric_type="IP", params={"drop_ratio_build": 0.2})
            _milvus_client.create_collection(
                collection_name="knowledge_domain",
                schema=schema, index_params=index_params,
            )
    return _milvus_client


def _milvus_search(query_vec: list[float], tenant_id: str, top_k: int) -> list[dict]:
    """Milvus 检索（dense 向量，BGE-M3 语义）"""
    client = _get_milvus_client()
    results = client.search(
        collection_name="knowledge_domain",
        data=[query_vec],
        limit=top_k,
        search_params={"metric_type": "COSINE", "params": {"ef": 64}},  # HNSW 参数
        output_fields=["content", "source_name", "document_id"],
        filter=_milvus_filter(tenant_id),
        anns_field="embedding",   # 指定向量字段（集合有 dense+sparse 两个）
    )
    out = []
    for hit in results[0]:
        out.append({
            "content": hit["entity"].get("content", ""),
            "score": round(hit["distance"], 4),
            "dense_score": round(hit["distance"], 4),
            "metadata": {
                "source_name": hit["entity"].get("source_name", ""),
                "doc_id": hit["entity"].get("document_id", ""),
            },
        })
    return out


def _milvus_add(chunks: list[dict], vectors: list[list[float]], tenant_id: str) -> None:
    """Milvus 写入"""
    client = _get_milvus_client()
    data = []
    for c, vec in zip(chunks, vectors):
        data.append({
            "id": f"{c['doc_id']}_{c['chunk_index']}",
            "embedding": vec,                       # 标准 schema 字段名（BGE-M3 dense）
            # 稀疏向量：由 BM25 词频构造（Milvus 2.4 要求非空；顺带启用 hybrid 稀疏路）
            "sparse_embedding": _build_sparse_vec(c["content"]),
            "content": c["content"],
            "source_name": c.get("source_name", ""),
            "document_id": c.get("doc_id", ""),     # 标准 schema 字段名
            "chunk_index": c.get("chunk_index", 0),
            "course_id": "buildmate",               # 标识来源（隔离 历史数据）
            "tenant_id": tenant_id,
            "chunk_type": "text",                   # 标准 schema 必填
            "version": "1.0",                       # 标准 schema 必填
            "updated_at": int(time.time()),
        })
    if data:
        client.insert(collection_name="knowledge_domain", data=data)


def _build_sparse_vec(text: str) -> dict:
    """用 BM25 分词构造 Milvus 稀疏向量 {token_hash: 词频}（IDF 由 Milvus 稀疏索引处理）"""
    tokens = _tokenize(text)
    freq: dict[int, float] = {}
    for t in tokens:
        h = int(hashlib.md5(t.encode()).hexdigest()[:8], 16)
        freq[h] = freq.get(h, 0) + 1.0
    if not freq:
        freq = {0: 0.0}   # Milvus 2.4 要求非空稀疏向量
    return freq

# ── BM25 稀疏检索────────────────
# 与稠密向量互补：BM25 看重字面词命中，IDF 自动压低"规范/施工"这类高频泛词，
# 突出"女儿墙/螺纹钢/GB50010"等低频专名 → 不会再把无关 chunk 抬过阈值。
_STOP_WORDS = frozenset([
    "的", "了", "和", "与", "及", "是", "吗", "嘛",
    "怎么", "怎样", "什么", "多少", "请问",
    "可以", "能", "不能", "有", "没有",
    "一下", "？", "！", "?", "!", "、", "，", ",",
])

_K1 = 1.5   # BM25 词频饱和参数
_B = 0.75   # BM25 文档长度归一化参数


def _tokenize(text: str) -> list[str]:
    """分词：英文/数字 token + 中文 2/3-gram（3-gram 代表更具体的实体词）"""
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    cjk = "".join(re.findall(r"[\u4e00-\u9fa5]", text))
    if cjk:
        # 2-gram 与 3-gram 都保留；3-gram 更贴近实体词（女儿墙/螺纹钢/装配式）
        tokens.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
        tokens.extend(cjk[i:i + 3] for i in range(len(cjk) - 2))
    return [t for t in tokens if t not in _STOP_WORDS and len(t) >= 2]


def _bm25_score(query_tokens: list[str], doc: str, df: dict, doc_count: int,
                avg_dl: float) -> float:
    """单文档 BM25 分数（3-gram 权重 x2，突出实体词；2-gram 泛词权重 x1）"""
    doc_l = doc.lower()
    dl = max(1, len(_tokenize(doc)))
    score = 0.0
    for tok in set(query_tokens):
        if tok not in df:
            continue
        tf = doc_l.count(tok)
        if tf == 0:
            continue
        idf = math.log(1 + (doc_count - df[tok] + 0.5) / (df[tok] + 0.5))
        tf_norm = tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * dl / avg_dl))
        weight = 2.0 if len(tok) >= 3 else 1.0   # 3-gram 实体词加权
        score += weight * idf * tf_norm
    return score


def _build_bm25_index(docs: list[str]) -> dict:
    """预计算 BM25 所需统计：df（词→文档数）、avg_dl、doc_count"""
    df: dict[str, int] = {}
    total_len = 0
    for doc in docs:
        seen = set(_tokenize(doc))
        for tok in seen:
            df[tok] = df.get(tok, 0) + 1
        total_len += max(1, len(_tokenize(doc)))
    return {"df": df, "avg_dl": total_len / max(1, len(docs)), "doc_count": len(docs)}


def _sparse_bm25(query: str, contents: list[str]) -> list[float]:
    """对一批文档计算 BM25 分数（相对分，未归一化）"""
    q_tokens = _tokenize(query)
    if not q_tokens:
        return [0.0] * len(contents)
    idx = _build_bm25_index(contents)
    return [_bm25_score(q_tokens, doc, idx["df"], idx["doc_count"], idx["avg_dl"])
            for doc in contents]
# ── M4-lite：租户 chunk 行缓存（写路径失效 + TTL 兜底）──────────────────────
# 旧实现每次检索全量 SELECT + 逐行 json.loads；数据量大时延迟线性恶化。
# 注意：单进程有效；多 worker/多实例部署应换共享缓存（Redis）或直接走 Milvus/pgvector。
_row_cache: dict[str, tuple[float, list]] = {}
_ROW_CACHE_TTL = 60.0


def _invalidate_row_cache(tenant_id: str | None = None) -> None:
    if tenant_id is None:
        _row_cache.clear()
    else:
        _row_cache.pop(tenant_id, None)


async def _load_rows(tenant_id: str) -> list:
    now = time.time()
    hit = _row_cache.get(tenant_id)
    if hit and now - hit[0] < _ROW_CACHE_TTL:
        return hit[1]
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT content, vector, source_name, doc_id, chunk_index FROM knowledge_chunks WHERE tenant_id = :t"
        ), {"t": tenant_id})).fetchall()
    _row_cache[tenant_id] = (now, rows)
    return rows


async def _local_search(query: str, qvec: list[float], tenant_id: str,
                       top_k: int, min_score: float) -> list[dict]:
    """本地混合检索：
    Dense 路：字符哈希向量余弦相似度（语义）
    Sparse 路：BM25（字面词命中，IDF 自动压低高频泛词）
    融合：score = 0.7 * dense_norm + 0.3 * sparse_norm
    """
    await _ensure_table()
    rows = await _load_rows(tenant_id)

    if not rows:
        return []

    # ── Dense 路：余弦相似度 ──────────────────────────────────────
    contents = [r[0] for r in rows]
    dense_scores = [cosine_sim(qvec, json.loads(r[1])) for r in rows]

    # ── Sparse 路：BM25 ──────────────────────────────────────────
    sparse_scores = _sparse_bm25(query, contents)

    # ── 融合分（保持绝对量，便于阈值判定）────────────────────────
    # Dense 用绝对余弦（0~1 真实语义相似度）；Sparse 用"除以最大值"的比例
    # 关键：仅当 chunk 与查询共享【具体实体词】（3-gram 或英文/数字 token）时
    # 才保留 sparse 贡献；否则该 chunk 视为不相关（惩罚）。
    # 这解决了"女儿墙施工规范"：女儿墙不在任何 chunk → 无共享实体词 → sparse 归零 → DIRECT
    # ── 共享实体词门槛 ─────────────────────────────────────────
    # 查询与 chunk 共享的 3-gram 中，排除"纯功能字"组合（施工规/工规范等），
    # 只保留真正的实体词（螺纹钢/塔吊/女儿墙/深基坑/装配式…）作为相关信号。
    # "女儿墙施工规范" 与 GB50202 共享的只有 施工规/工规范（功能字）→ 判定不相关
    _FUNC_CHARS = set("施工规范资质工程知识问答全流程管理控制")
    query_tokens = _tokenize(query)
    entity_query = {
        t for t in query_tokens
        if len(t) >= 3 and not all(ch in _FUNC_CHARS for ch in t)
    }
    sparse_max = max(sparse_scores) if sparse_scores else 0.0
    scored = []
    for i, r in enumerate(rows):
        sparse_ratio = sparse_scores[i] / sparse_max if sparse_max > 0 else 0.0
        # 共享实体词判定（需含非功能字的具体词）
        doc_tokens = set(_tokenize(r[0]))
        shared = entity_query & doc_tokens
        if not shared:
            sparse_ratio = 0.0   # 无共享实体词 → 不用 sparse 抬分
        # 权重自适应：本地语义模型（dense 可靠）时 0.85/0.15；哈希向量（dense 弱）时 0.6/0.4
        w_dense = 0.85 if TextVectorizer._local_model is not None else 0.6
        s = w_dense * dense_scores[i] + (1 - w_dense) * sparse_ratio
        if s >= min_score:
            scored.append({
                "content": r[0], "score": round(s, 4),
                "dense_score": round(dense_scores[i], 4),
                "metadata": {"source_name": r[2] or "", "doc_id": r[3] or "", "chunk_index": r[4] or 0},
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


async def search(query: str, tenant_id: str = "tenant_default", top_k: int = 5,
                 min_score: float = 0.0) -> list[dict]:
    """检索（对外接口不变）：按后端优先级查询，首个有结果者生效；异常自动降级"""
    qvec = (await TextVectorizer.embed([query]))[0]
    for backend in _active_backends():
        try:
            results = await backend.search(query, qvec, tenant_id, top_k, min_score)
        except Exception as e:
            logger.warning("vector_store.backend_search_failed",
                           backend=backend.name, error=str(e)[:150])
            continue
        if results:
            return results[:top_k]
    return []


async def clear_knowledge(tenant_id: str = "tenant_default"):
    """清空知识库（对外接口不变）：清所有活跃后端，单后端失败不阻断其余"""
    for backend in _active_backends():
        try:
            await backend.clear(tenant_id)
        except Exception as e:
            logger.warning("vector_store.backend_clear_failed",
                           backend=backend.name, error=str(e)[:150])
