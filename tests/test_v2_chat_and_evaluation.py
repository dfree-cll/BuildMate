import uuid

from backend.adapters.chat_repository import ChatRepository
from backend.domain.contracts import RequestContext
from backend.rag.contracts import KnowledgeDocumentCreate, KnowledgeScope, KnowledgeSearchRequest
from backend.rag.evaluation import ndcg_at_k, recall_at_k, reciprocal_rank
from backend.rag.service import RAGService


def context(suffix: str) -> RequestContext:
    return RequestContext(
        tenant_id=f"tenant_chat_{suffix}", project_id=f"project_chat_{suffix}",
        user_id="chat_user", role="user", trace_id=suffix, correlation_id=suffix,
    )


async def test_offline_chat_answer_is_grounded_and_persisted():
    suffix = uuid.uuid4().hex
    ctx = context(suffix)
    rag = RAGService()
    await rag.ingest(ctx, KnowledgeDocumentCreate(
        project_id=ctx.project_id, scope=KnowledgeScope.PROJECT,
        source_type="project_manual", source_uri="fixture://manual",
        title="项目质量手册", content="墙体施工前必须核对轴网、标高和材料强度等级。",
    ))
    answer = await rag.answer(ctx, KnowledgeSearchRequest(
        query="墙体施工前核对什么？", tenant_id=ctx.tenant_id,
        project_id=ctx.project_id, scope=KnowledgeScope.PROJECT,
    ))
    assert answer.grounded is True
    assert answer.citations
    assert answer.citations[0].chunk_id

    repository = ChatRepository()
    session = await repository.create_session(ctx, "质量问答")
    messages = await repository.append_user_and_answer(
        ctx, session["id"], "墙体施工前核对什么？", answer
    )
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[1]["grounded"] is True
    assert messages[1]["citations"][0]["chunk_id"] == answer.citations[0].chunk_id


def test_rag_golden_metrics_are_deterministic():
    retrieved = ["c", "a", "b"]
    assert recall_at_k(retrieved, {"a", "b"}, 2) == 0.5
    assert reciprocal_rank(retrieved, {"a"}) == 0.5
    assert 0.0 < ndcg_at_k(retrieved, {"a": 3, "b": 1}, 3) <= 1.0
