"""BGE-Reranker 精排（对标 EduAgent 5.7）
混合检索召回 top10 → BGE-Reranker 精排 top3 + 置信度（0.75 阈值）
模型：D:\\新建文件夹 (2)\\models\\reranker\\bge-reranker-large（2.1GB，CPU 可跑）
"""
import asyncio
import os
from typing import Optional

from backend.core.logger import get_logger

logger = get_logger(__name__)

RERANK_MAX_INPUT_CHARS = 1200   # 截断过长文档，防止超出 max_length
RERANK_CONFIDENCE_THRESHOLD = 0.75   # 高置信度阈值（对标 EduAgent）


def _resolve_model_path(name: str) -> str:
    """按 config.models_root 解析模型路径（容器挂载 /models 时自动生效）"""
    from backend.config import get_settings
    return os.path.join(get_settings().models_root, name)


class BGEReranker:
    """BGE-Reranker-Large 精排服务（单例，CPU 推理）"""
    _instance: Optional["BGEReranker"] = None

    def __init__(self):
        from sentence_transformers import CrossEncoder
        from backend.config import get_settings
        self._model_path = _resolve_model_path(os.path.join("reranker", "bge-reranker-large"))
        if not os.path.isdir(self._model_path):
            raise FileNotFoundError(f"Reranker 模型未找到: {self._model_path}")
        logger.info("reranker.loading", model_path=self._model_path)
        self._model = CrossEncoder(self._model_path, max_length=512)
        logger.info("reranker.loaded")

    @classmethod
    def get_instance(cls) -> "BGEReranker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def rerank(self, query: str, candidates: list[dict], top_k: int = 3) -> tuple[list[dict], float]:
        """精排：query + 候选文档 → 相关性分数 → top_k
        candidates: [{content, score, metadata}]（Hybrid 召回结果）
        返回 (精排后 top_k, 置信度=Top1 分数)
        """
        if not candidates:
            return [], 0.0
        pairs = [[query, c["content"][:RERANK_MAX_INPUT_CHARS]] for c in candidates]
        scores = self._model.predict(pairs)   # CrossEncoder 直接输出 [0,1]（sigmoid）
        # 排序：保留原 metadata，分数用 rerank 分数覆盖
        ranked = []
        for c, s in zip(candidates, scores):
            item = dict(c)
            item["score"] = round(float(s), 4)
            item["dense_score"] = c.get("dense_score", 0)
            ranked.append(item)
        ranked.sort(key=lambda x: x["score"], reverse=True)
        top = ranked[:top_k]
        confidence = top[0]["score"] if top else 0.0
        return top, confidence


async def rerank_results(query: str, candidates: list[dict], top_k: int = 3) -> tuple[list[dict], float]:
    """异步精排入口：CPU 密集操作丢线程池，避免阻塞事件循环（对标 EduAgent rerank_with_confidence）"""
    if not candidates:
        return [], 0.0
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, BGEReranker.get_instance().rerank, query, candidates, top_k)
