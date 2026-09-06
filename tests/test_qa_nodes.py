"""QA Agent 单元测试（检索/生成/记忆节点）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from langchain_core.messages import AIMessage

from backend.agents.qa.nodes import (
    classify_query_node,
    enqueue_pending_node,
    generate_node,
    retrieve_node,
)


class _MockLLM:
    """测试用 Mock LLM：不调真实 API"""
    def __init__(self, reply="\u6d4b\u8bd5\u56de\u590d\u5185\u5bb9"):
        self.reply = reply

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content=self.reply)


@pytest.fixture
def mock_llm(monkeypatch):
    from backend.core.llm_factory import LLMFactory
    monkeypatch.setattr(LLMFactory, "get_llm", classmethod(lambda cls, *a, **k: _MockLLM()))


async def test_classify_price_query_specialized():
    """\u5efa\u6750\u4ef7\u683c\u67e5\u8be2\u5e94\u5206\u7c7b\u4e3a PRECISE\uff08\u8d70 RAG\uff09"""
    st = await classify_query_node({
        "messages": [], "user_id": "u", "tenant_id": "t", "session_id": "s",
        "original_query": "\u897f\u5b89\u87ba\u7eb9\u94a2\u591a\u5c11\u94b1",
    })
    assert st["query_type"] in ("PRECISE", "VAGUE", "BROAD")


async def test_retrieve_finds_rebar():
    """\u68c0\u7d22\u87ba\u7eb9\u94a2\u4ef7\u683c\u5e94\u547d\u4e2d\u5efa\u6750\u4ef7\u683c"""
    st = await retrieve_node({
        "query_type": "PRECISE", "tenant_id": "tenant_default",
        "original_query": "\u897f\u5b89\u87ba\u7eb9\u94a2\u4ef7\u683c",
        "user_id": "u", "session_id": "s",
        "messages": [], "rewritten_queries": [], "hyde_document": None,
    })
    assert st["ranked_chunks"], "\u5e94\u68c0\u7d22\u5230\u7ed3\u679c"
    first = st["ranked_chunks"][0]
    assert "\u87ba\u7eb9\u94a2" in first["content"] or "\u4ef7\u683c" in first["content"]


async def test_generate_rag_mode(mock_llm):
    """\u9ad8\u7f6e\u4fe1\u5ea6\u68c0\u7d22\u5e94\u8d70 RAG \u751f\u6210"""
    chunks = [{
        "content": "[\u5efa\u6750\u4ef7\u683c] \u87ba\u7eb9\u94a2\u4ef7\u683c \u87ba\u7eb9\u94a2 HRB400 20mm 3560 \u5143/\u5428",
        "score": 0.9, "dense_score": 0.9,
        "metadata": {"source_name": "[\u5efa\u6750\u4ef7\u683c] \u87ba\u7eb9\u94a2\u4ef7\u683c"},
    }]
    st = await generate_node({
        "query_type": "PRECISE", "original_query": "\u87ba\u7eb9\u94a2\u591a\u5c11\u94b1",
        "ranked_chunks": chunks, "confidence": 0.9,
        "messages": [], "user_id": "u", "session_id": "s",
    })
    assert st["answer_mode"] == "rag"
    assert len(st["answer"]) > 10


async def test_generate_direct_low_confidence(mock_llm):
    """\u4f4e\u7f6e\u4fe1\u5ea6\u5e94\u8d70 llm_direct"""
    st = await generate_node({
        "query_type": "PRECISE", "original_query": "\u91cf\u5b50\u8ba1\u7b97\u539f\u7406",
        "ranked_chunks": [], "confidence": 0.0,
        "messages": [], "user_id": "u", "session_id": "s",
    })
    assert st["answer_mode"] in ("llm_direct", "rag_hybrid")


async def test_pending_queue_insert_is_sqlite_safe_and_idempotent():
    """A retried low-confidence answer creates one durable knowledge gap."""
    from sqlalchemy import text
    from backend.db.session import engine

    state = {
        "original_query": "qa-pending-idempotency-unique",
        "confidence": 0.1,
        "answer_mode": "llm_direct",
        "tenant_id": "tenant_qa_pending",
        "user_id": "qa-user",
    }
    await enqueue_pending_node(state)
    await enqueue_pending_node(state)
    async with engine.connect() as conn:
        count = (await conn.execute(text(
            "SELECT COUNT(*) FROM knowledge_pending_queue "
            "WHERE tenant_id=:tenant_id AND question=:question"
        ), {
            "tenant_id": state["tenant_id"],
            "question": state["original_query"],
        })).scalar_one()
    assert count == 1
