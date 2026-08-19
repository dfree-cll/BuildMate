"""三层兜底机制（对标 EduAgent 3.5）：自动重试 → Agent 级降级 → 系统级兜底"""
import asyncio
from functools import wraps
from typing import Callable, Any, Optional

from backend.core.exceptions import (
    LLMAPIError, InvalidInputError, AuthenticationError,
)
from backend.core.logger import get_logger

logger = get_logger(__name__)

RETRYABLE_ERRORS = (LLMAPIError, TimeoutError, ConnectionError)
NON_RETRYABLE_ERRORS = (InvalidInputError, AuthenticationError)

MAX_RETRIES = 2
RETRY_DELAYS = [1.0, 3.0]
TIMEOUT_PER_ATTEMPT = 30.0


def with_retry(agent_type: str = ""):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_error: Optional[Exception] = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    result = await asyncio.wait_for(func(*args, **kwargs), timeout=TIMEOUT_PER_ATTEMPT)
                    if attempt > 0:
                        logger.info("retry.succeeded", agent_type=agent_type, attempt=attempt + 1)
                    return result
                except NON_RETRYABLE_ERRORS as e:
                    logger.warning("retry.non_retryable_error", agent_type=agent_type, error=str(e))
                    raise
                except RETRYABLE_ERRORS as e:
                    last_error = e
                    if attempt < MAX_RETRIES:
                        delay = RETRY_DELAYS[attempt]
                        logger.warning("retry.failed", agent_type=agent_type, attempt=attempt + 1,
                                       error=str(e), retry_in=delay)
                        await asyncio.sleep(delay)
                    else:
                        break
                except Exception as e:
                    # 程序 bug：不重试，直接交给降级/兜底（避免掩盖真实错误）
                    logger.warning("retry.non_retryable_unknown", agent_type=agent_type, error=str(e))
                    return await AgentFallbackHandler.handle(agent_type, original_error=e)
            try:
                return await AgentFallbackHandler.handle(agent_type, original_error=last_error)
            except Exception:
                return _system_fallback_response(agent_type)
        return wrapper
    return decorator


class AgentFallbackHandler:
    """第二层：Agent 级降级（返回纯字典，对接 LangGraph 节点约定）"""

    @classmethod
    async def handle(cls, agent_type: str, original_error: Exception) -> Any:
        fallback_map = {
            "qa": cls._qa_fallback,
            "bid_review": cls._bid_review_fallback,
            "procurement": cls._procurement_fallback,
            "negotiation": cls._negotiation_fallback,
        }
        handler = fallback_map.get(agent_type)
        if handler:
            return await handler()
        raise original_error

    @classmethod
    async def _qa_fallback(cls) -> dict:
        logger.info("fallback.qa_unavailable")
        return {"fallback_used": True, "answer": "⚠️ 知识库检索暂时不可用，请稍后重试。", "structured_output": None}

    @classmethod
    async def _bid_review_fallback(cls) -> dict:
        return {"fallback_used": True, "content": "投标审查服务暂时不可用，请稍后重试。", "structured_output": None}

    @classmethod
    async def _procurement_fallback(cls) -> dict:
        return {"fallback_used": True, "content": "审批服务暂时不可用，已标记需人工复核。",
                "needs_review": True, "fallback_note": "审批服务暂时不可用，已标记需人工复核。"}

    @classmethod
    async def _negotiation_fallback(cls) -> dict:
        return {"fallback_used": True, "content": "谈判服务暂时不可用，已记录本次对话。"}


def _system_fallback_response(agent_type: str) -> dict:
    messages = {
        "qa": "非常抱歉，问答服务暂时不可用，请稍后再试。",
        "bid_review": "非常抱歉，投标审查服务暂时不可用，请稍后再试。",
        "procurement": "非常抱歉，采购审批服务暂时不可用，您的单据已保存。",
        "negotiation": "非常抱歉，谈判服务暂时不可用，请稍后重新开始。",
    }
    return {
        "messages": [],
        "content": messages.get(agent_type, "服务暂时不可用，请稍后再试。"),
        "fallback_used": True,
        "system_fallback": True,
        "structured_output": None,
    }
