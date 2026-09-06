"""Memory acceptance tests: persistence, isolation, prompt use, API restoration and safe BIM settings."""
import asyncio
import json
import uuid

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from backend.adapters.agent_memory_repository import AgentMemoryRepository
from backend.application.agent_memory import memory_nodes, context_text, contextual_query
from backend.domain.contracts import RequestContext
from backend.domain.agent_memory import MemoryPreferences


def ctx(**changes):
    return RequestContext(tenant_id="mem-" + uuid.uuid4().hex, user_id="owner", project_id="project",
                          trace_id="test", correlation_id="test").model_copy(update=changes)


@pytest.mark.parametrize("agent", ["qa", "bid_review", "procurement", "negotiation", "drawing2bim", "router", "chat"])
async def test_every_agent_persists_and_restores_after_connection_restart(agent):
    from backend.db.session import engine
    context = ctx()
    repo = AgentMemoryRepository()
    await repo.append(context, agent, "session", "turn", "预算 20 万", "已记录，未审批", {"status": "unapproved"})
    await engine.dispose()
    history = await AgentMemoryRepository().read(context, agent, "session")
    assert history["turns"][0]["user_text"] == "预算 20 万"
    assert history["turns"][0]["result"]["status"] == "unapproved"


@pytest.mark.parametrize("changes", [
    {"tenant_id": "another"}, {"project_id": "another"}, {"project_id": None}, {"user_id": "another"},
])
async def test_memory_cannot_cross_scope(changes):
    context = ctx()
    repo = AgentMemoryRepository()
    await repo.append(context, "qa", "same", "1", "secret", "secret answer")
    assert not (await repo.read(context.model_copy(update=changes), "qa", "same"))["turns"]
    assert not await repo.list_sessions(context.model_copy(update=changes), "qa")
    assert not (await repo.read(context, "negotiation", "same"))["turns"]
    assert not (await repo.read(context, "qa", "new"))["turns"]


async def test_concurrent_idempotent_writers_and_summary():
    context = ctx()
    repo = AgentMemoryRepository()
    await asyncio.gather(*(repo.append(context, "qa", "s", str(i), f"问题{i}", f"答复{i}") for i in range(15)))
    await asyncio.gather(*(repo.append(context, "qa", "s", "repeat", "相同请求", "一次") for _ in range(4)))
    history = await repo.read(context, "qa", "s", limit=100)
    assert len(history["turns"]) == 16
    assert [t["seq"] for t in history["turns"]] == list(range(1, 17))
    assert history["summary"]
    assert "历史回复（未复核）" in history["summary"]
    assert "较早对话摘要" in context_text(history)


@pytest.mark.parametrize("agent", ["qa", "bid_review", "procurement", "negotiation", "drawing2bim"])
async def test_load_save_nodes_feed_history_but_not_geometry_or_approval(agent):
    context = ctx()
    repo = AgentMemoryRepository()
    await repo.append(context, agent, "s", "old", "构件必须按原图编号", "旧回复不能作为图纸证据")
    await repo.preferences(context, agent, "s", {"note": "回答简洁"})
    state = {**context.model_dump(), "session_id": "s", "messages": [HumanMessage("继续")],
             "original_query": "继续", "memory_turn_id": "new"}
    load, save = memory_nodes(agent)
    loaded = await load(state)
    assert "构件必须按原图编号" in loaded["memory_context"]
    assert "回答简洁" in loaded["memory_context"]
    assert not {"golden_baseline", "geometry", "reviewer_decision", "final_verdict"} & loaded.keys()
    await save({**state, **loaded, "answer": "本次回复"})
    assert len((await repo.read(context, agent, "s"))["turns"]) == 2


async def test_qa_prompt_really_contains_memory(monkeypatch):
    from backend.agents.qa import nodes
    calls = []
    class LLM:
        async def ainvoke(self, messages):
            calls.append(messages)
            return AIMessage("收到")
    monkeypatch.setattr(nodes, "get_llm", lambda *a, **kw: LLM())
    await nodes.generate_node({"original_query": "继续", "query_type": "GENERAL",
                               "existing_summary": "预算上限为 20 万，尚未审批"})
    assert any("预算上限为 20 万" in str(m.content) for m in calls[0])


def test_scoped_checkpoint_keys_and_contextual_retrieval():
    from backend.core.memory import build_config
    keys = {build_config("u", "s", tenant_id=t, project_id=p, agent=a)["configurable"]["thread_id"]
            for t in ("a", "b") for p in (None, "project") for a in ("qa", "negotiation")}
    assert len(keys) == 8
    assert "钢筋" in contextual_query("它的验收要求呢？", ["钢筋进场检查什么？"])
    assert contextual_query("混凝土抗压试验怎么做？", ["钢筋进场检查什么？"]) == "混凝土抗压试验怎么做？"


@pytest.mark.parametrize("bim", [
    {"wall_coordinates": [1, 2]}, {"approved": True}, {"elevation_range": "0~-6.4"},
    {"pdf_scale_denominator": float("nan")}, {"elevation_range": "0~100"},
])
def test_bim_memory_rejects_geometry_approvals_and_invalid_values(bim):
    with pytest.raises(ValidationError):
        MemoryPreferences.model_validate({"bim": bim})


async def test_bim_settings_and_successful_model_receipt_are_restorable():
    from backend.db.schema import workflow_runs
    from backend.db.session import engine
    context = ctx()
    repo = AgentMemoryRepository()
    settings = MemoryPreferences.model_validate({"bim": {"floor_code": "B1", "elevation_range": "-6.4~0"}})
    await repo.preferences(context, "drawing2bim", "floor", settings.model_dump(exclude_none=True))
    async with engine.begin() as conn:
        for status in ("succeeded", "failed"):
            await conn.execute(workflow_runs.insert().values(
                id=status + uuid.uuid4().hex, tenant_id=context.tenant_id, project_id=context.project_id,
                actor_id=context.user_id, workflow="wall_pipeline", status=status,
                idempotency_key=uuid.uuid4().hex, correlation_id="test",
                options=json.dumps({"memory_session_id": "floor", "level": {"elevation_m": -6.4}}),
                result=json.dumps({"answer": "完成", "artifact_ids": ["receipt-rvt"]}),
            ))
    await repo.sync_bim(context)
    await repo.sync_bim(context)
    history = await repo.read(context, "drawing2bim", "floor")
    assert len(history["turns"]) == 1
    assert history["turns"][0]["result"]["task_id"].startswith("succeeded")
    assert history["preferences"]["bim"]["elevation_range"] == "-6.4~0"
    assert not (await repo.read(context.model_copy(update={"user_id": "other"}), "drawing2bim", "floor"))["turns"]


@pytest.fixture
async def checkpoints(tmp_path, monkeypatch):
    from backend.core import memory, orchestrator
    await memory.close_memory_savers()
    monkeypatch.setattr(memory, "_CHECKPOINTS_DB", str(tmp_path / "checkpoints.db"))
    orchestrator._orchestrator = None
    await memory.init_memory_savers()
    yield
    await memory.close_memory_savers()
    orchestrator._orchestrator = None


async def test_negotiation_restores_stage_after_saver_restart(checkpoints):
    from backend.core.memory import init_memory_savers, close_memory_savers, build_config
    from backend.agents.negotiation.graph import build_negotiation_graph
    context = ctx()
    state = {**context.model_dump(), "session_id": "s", "material": "塔吊", "messages": [HumanMessage("报价100")],
             "memory_turn_id": "one"}
    config = build_config(context.user_id, "s", tenant_id=context.tenant_id, project_id=context.project_id, agent="negotiation")
    result = await build_negotiation_graph().ainvoke(state, config)
    await close_memory_savers()
    await init_memory_savers()
    restored = await build_negotiation_graph().aget_state(config)
    assert restored.values["stage"] == result["stage"]
    assert restored.values["context"]["rounds_in_stage"] == 1
    continued = await build_negotiation_graph().ainvoke({"messages": [HumanMessage("交货要准时")], "memory_turn_id": "two"}, config)
    assert "报价100" in continued["memory_context"]
    assert len((await AgentMemoryRepository().read(context, "negotiation", "s"))["turns"]) == 2


async def test_http_qa_and_negotiation_history_and_new_session(checkpoints):
    from backend.api.v1 import unified_chat, negotiation
    from backend.api.v2 import memory
    from backend.dependencies import get_current_user, llm_rate_limit
    context = ctx(project_id=None)
    user = {"user_id": context.user_id, "tenant_id": context.tenant_id, "role": "user"}
    app = FastAPI()
    app.include_router(memory.router, prefix="/api/v2")
    app.include_router(unified_chat.router, prefix="/api/v1/chat")
    app.include_router(negotiation.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[llm_rate_limit] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/v1/chat/stream", json={"session_id": "qa", "message": "你好"})
        assert response.status_code == 200
        history = (await client.get("/api/v2/memory/qa/sessions/qa")).json()
        assert history["turns"][0]["user_text"] == "你好"
        reply = await client.post("/api/v1/negotiation/chat", json={"session_id": "neg", "message": "报价100", "material": "钢筋"})
        assert reply.status_code == 200, reply.text
        history = (await client.get("/api/v2/memory/negotiation/sessions/neg")).json()
        assert history["turns"][0]["result"]["material"] == "钢筋"
        assert not (await client.get("/api/v2/memory/negotiation/sessions/new")).json()["turns"]
        response = await client.post("/api/v1/negotiation/chat", json={"session_id": "neg", "message": "重置", "reset": True})
        assert response.status_code == 409
        user["user_id"] = "another"
        assert not (await client.get("/api/v2/memory/negotiation/sessions/neg")).json()["turns"]


async def test_chat_v2_ownership_checked_before_llm(monkeypatch):
    from backend.adapters.chat_repository import ChatRepository
    from backend.api.v2.chat import create_message, MessageCreate
    from backend.domain.errors import ResourceNotFound
    context = ctx(project_id=None)
    session = await ChatRepository().create_session(context, "private")
    with pytest.raises(ResourceNotFound):
        await create_message(session["id"], MessageCreate(content="继续回答"),
                             current_user={"user_id": "intruder", "tenant_id": context.tenant_id}, x_trace_id="test")


async def test_chat_v2_uses_history_for_retrieval(monkeypatch):
    from backend.adapters.chat_repository import ChatRepository
    from backend.api.v2 import chat
    from backend.rag.contracts import RAGAnswer
    context = ctx(project_id=None)
    repo = ChatRepository()
    session = await repo.create_session(context, "steel")
    answer = RAGAnswer(answer="先检查合格证", citations=[], confidence=0, grounded=False, abstained=True, retrieval_run_id="r")
    await repo.append_user_and_answer(context, session["id"], "钢筋进场检查什么？", answer)
    calls = []
    class RAG:
        async def answer(self, context, request, **kwargs):
            calls.append((request.query, kwargs))
            return answer
    monkeypatch.setattr(chat, "get_rag_service", lambda: RAG())
    await chat.create_message(session["id"], chat.MessageCreate(content="它还需要什么？"),
                              current_user={"user_id": context.user_id, "tenant_id": context.tenant_id}, x_trace_id="test")
    assert "钢筋进场" in calls[0][0]
    assert "合格证" in calls[0][1]["memory_context"]


async def test_bid_versions_and_procurement_history_through_http(checkpoints):
    from backend.api.v1 import bid_review, procurement
    from backend.api.v2 import memory
    from backend.dependencies import get_current_user, llm_rate_limit
    context = ctx(project_id=None)
    user = {"user_id": context.user_id, "tenant_id": context.tenant_id, "role": "admin"}
    app = FastAPI()
    app.include_router(memory.router, prefix="/api/v2")
    app.include_router(bid_review.router, prefix="/api/v1")
    app.include_router(procurement.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[llm_rate_limit] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for version in (1, 2):
            response = await client.post("/api/v1/bid-review/review", json={
                "doc_text": f"项目名称：新项目，版本{version}；工期120天；资质齐全；报价100万元。", "session_id": "versions"})
            assert response.status_code == 202, response.text
            await asyncio.gather(*list(bid_review._bid_tasks))
            report = await client.get("/api/v1/bid-review/reviews/" + response.json()["review_id"])
            assert report.json()["status"] == "done", report.text
        history = (await client.get("/api/v2/memory/bid_review/sessions/versions")).json()
        assert len(history["turns"]) == 2
        assert history["turns"][0]["result"]["review_id"] != history["turns"][1]["result"]["review_id"]
        order = await client.post("/api/v1/procurement/orders", json={
            "material_name": "钢筋", "quantity": 500, "unit_price": 3600, "session_id": "orders"})
        assert order.status_code == 200, order.text
        assert order.json()["needs_human_approval"]
        approve = await client.post(f"/api/v1/procurement/orders/{order.json()['order_no']}/confirm",
                                    json={"decision": "approved", "comment": "已核实"})
        assert approve.status_code == 200, approve.text
        history = (await client.get("/api/v2/memory/procurement/sessions/orders")).json()
        assert history["turns"][0]["result"]["final_verdict"] == "approved"
        next_order = await client.post("/api/v1/procurement/orders", json={
            "material_name": "钢筋", "quantity": 500, "unit_price": 3600, "session_id": "orders"})
        assert next_order.json()["needs_human_approval"]  # Old approval never authorizes a new order.


async def test_bid_checkpoint_carries_previous_version_context(checkpoints):
    from backend.agents.bid_review.graph import build_bid_review_graph
    from backend.core.memory import build_config
    context = ctx()
    graph = build_bid_review_graph()
    for version in ("v1", "v2"):
        result = await graph.ainvoke({**context.model_dump(), "session_id": "bid", "review_id": version,
                                     "doc_text": "技术标：地下室施工方案完整，质量标准满足合同约定。", "memory_turn_id": version},
                                    build_config(context.user_id, version, tenant_id=context.tenant_id,
                                                 project_id=context.project_id, agent="bid_review"))
    assert "地下室施工方案" in result["memory_context"]
    assert len((await AgentMemoryRepository().read(context, "bid_review", "bid"))["turns"]) == 2


def test_memory_migration_on_empty_database_and_repeat(tmp_path, monkeypatch):
    import importlib
    from sqlalchemy import create_engine, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    migration = importlib.import_module("migrations.versions.20260905_0011_agent_memory")
    engine = create_engine("sqlite:///" + str(tmp_path / "migration.db"))
    try:
        with engine.begin() as conn:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
            migration.upgrade()
            migration.upgrade()
            assert {"agent_memory_sessions", "agent_memory_turns"} <= set(inspect(conn).get_table_names())
            migration.downgrade()
            assert not inspect(conn).get_table_names()
    finally:
        engine.dispose()


async def test_qa_graph_keeps_user_identity_and_conversation(checkpoints):
    from backend.agents.qa.graph import build_qa_graph
    from backend.core.memory import build_config
    context = ctx()
    graph = build_qa_graph()
    config = build_config(context.user_id, "qa", tenant_id=context.tenant_id,
                          project_id=context.project_id, agent="qa")
    await graph.ainvoke({**context.model_dump(), "session_id": "qa", "memory_turn_id": "one",
                         "messages": [HumanMessage("你好，我的预算是20万元")]}, config)
    result = await graph.ainvoke({"messages": [HumanMessage("谢谢，按这个预算继续")], "memory_turn_id": "two"}, config)
    assert result["user_id"] == context.user_id
    assert "预算是20万元" in result["memory_context"]
    assert len((await AgentMemoryRepository().read(context, "qa", "qa"))["turns"]) == 2
