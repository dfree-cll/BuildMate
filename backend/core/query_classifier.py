"""意图分类器（对标 EduAgent 5.8：MiniLM-L6-v2 二分类 general/specialized）
规则快通道 → MiniLM 本地分类 → LLM 精判，三层策略
模型：D:\\新建文件夹 (2)\\models\\classifier\\query-classifier-finetuned（微调版，general/specialized）
"""
import asyncio
import os
from typing import Optional

from backend.core.logger import get_logger

logger = get_logger(__name__)

GENERAL_CONFIDENCE_THRESHOLD = 0.85   # general 侧阈值（宁可多走 RAG 不漏专业问题）


def _resolve_model_path(name: str) -> str:
    """按 config.models_root 解析模型路径（容器挂载 /models 时自动生效）"""
    from backend.config import get_settings
    return os.path.join(get_settings().models_root, name)

LABEL2ID = {"general": 0, "specialized": 1}
ID2LABEL = {0: "general", 1: "specialized"}


class QueryClassifier:
    """MiniLM 本地二分类器（CPU 推理 <10ms/条，离线可用）"""
    _instance: Optional["QueryClassifier"] = None

    def __init__(self):
        from transformers import pipeline as hf_pipeline
        from backend.config import get_settings
        self._model_path = _resolve_model_path(os.path.join("classifier", "query-classifier-finetuned"))
        if not os.path.isdir(self._model_path):
            raise FileNotFoundError(f"分类器模型未找到: {self._model_path}")
        logger.info("query_classifier.loading", model_path=self._model_path)
        self._pipeline = hf_pipeline(
            task="text-classification",
            model=self._model_path,
            top_k=None,
            truncation=True,
            max_length=128,
        )
        logger.info("query_classifier.loaded")

    @classmethod
    def get_instance(cls) -> "QueryClassifier":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def classify(self, text: str) -> tuple[str, float]:
        """返回 (label, confidence)：general / specialized（EduAgent 5.8 规则）
        修复：pipeline 返回结构兼容 list[dict] 与 dict（原代码迭代 dict key 会 TypeError）"""
        out = self._pipeline(text)
        raw = out[0] if isinstance(out, list) and out else out
        # raw 可能是 list[dict] 或 dict
        items = raw if isinstance(raw, list) else [raw]
        general_score = None
        for item in items:
            if not isinstance(item, dict):
                continue
            lbl = str(item.get("label", "")).lower()
            if lbl in ("general", "label_0"):
                general_score = float(item.get("score", 0.0))
                break
        if general_score is None:
            logger.warning("query_classifier.unexpected", out_type=type(raw).__name__)
            return "specialized", 0.5
        if general_score >= GENERAL_CONFIDENCE_THRESHOLD:
            return "general", general_score
        return "specialized", 1.0 - general_score


# ── 规则快通道（对标 EduAgent Layer 0，不调模型）──────────────
_GENERAL_EXACT = {"你好", "谢谢", "再见", "hi", "hello", "谁"}
_GENERAL_KEYWORDS = ("今天天气", "现在几点", "讲个笑话")
_SPECIALIZED_KEYWORDS = ("价格", "规范", "招标", "租赁", "施工", "建筑")


def rule_classify(query: str) -> Optional[str]:
    """规则快通道：命中直接返回，未命中返回 None 交给 MiniLM"""
    q = query.strip().lower()
    if q in _GENERAL_EXACT or any(kw in q for kw in _GENERAL_KEYWORDS):
        return "general"
    if any(kw in q for kw in _SPECIALIZED_KEYWORDS):
        return "specialized"
    return None


async def classify_query_text(query: str) -> tuple[str, float]:
    """三层分类：规则 → MiniLM → (LLM 由上层处理)  返回 (label, confidence)"""
    rule = rule_classify(query)
    if rule:
        return rule, 0.99
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, QueryClassifier.get_instance().classify, query)


# ── 模块级单例便捷函数 ─────────────────────────────────
_classifier: Optional[QueryClassifier] = None


def get_query_classifier() -> QueryClassifier:
    global _classifier
    if _classifier is None:
        _classifier = QueryClassifier.get_instance()
    return _classifier
