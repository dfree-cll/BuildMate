import pytest

from backend.application.task_understanding import build_handoff, understand_task
from backend.domain.contracts import EvidenceRef, RequestContext
from backend.domain.errors import ValidationFailure
from backend.domain.task_plan import TaskHandoffPackage, TaskStep


async def test_v2_task_preview_is_read_only_and_returns_trace():
    from backend.api.v2.chat import MessageCreate, preview_task_plan

    result = await preview_task_plan(
        MessageCreate(
            content="查询规范，然后给我采购建议",
            project_id="p1",
        ),
        {"tenant_id": "t1", "user_id": "u1", "role": "user"},
        None,
    )
    assert result["trace_id"]
    assert result["plan"]["mode"] == "composite"
    assert [step["domain"] for step in result["plan"]["steps"]] == ["knowledge", "procurement"]


def test_composite_query_is_decomposed_into_registered_domains():
    plan = understand_task(
        "审查这个招标文件，然后查供应商报价，最后给我谈判建议",
        project_id="p1",
        artifact_ids=["a1"],
    )
    assert plan.mode == "composite"
    assert [step.domain for step in plan.steps] == ["bid", "procurement", "negotiation"]
    assert [step.route for step in plan.steps] == [
        "workflow.bid_review", "workflow.procurement", "workflow.negotiation"
    ]
    assert plan.needs_confirmation is True


def test_task_plan_does_not_execute_side_effects_for_bim():
    plan = understand_task("把图纸建成 Revit 模型", project_id="p1", artifact_ids=["a1"])
    assert plan.mode == "single"
    assert plan.steps[0].domain == "bim"
    assert plan.steps[0].route == "workflow.wall_pipeline"
    assert plan.steps[0].requires_confirmation is True


def test_handoff_is_bound_to_the_context_and_carries_only_evidence():
    context = RequestContext(
        tenant_id="t1", project_id="p1", user_id="u1", role="user",
        trace_id="trace-1", correlation_id="corr-1",
    )
    package = build_handoff(
        context,
        source_task="contract-1",
        target_domain="negotiation",
        facts={"risk": "付款条款"},
        evidence_refs=[EvidenceRef(kind="chunk", chunk_id="chunk-1", page_no=2)],
        risk_summary=["付款节点需复核"],
    )
    package.assert_context(context)
    assert package.facts == {"risk": "付款条款"}
    assert package.evidence_refs[0].chunk_id == "chunk-1"
    with pytest.raises(ValueError, match="tenant/project"):
        package.assert_context(context.model_copy(update={"project_id": "other"}))


def test_empty_query_is_rejected_at_the_contract_boundary():
    with pytest.raises(ValidationFailure):
        understand_task("  ")


def test_adjacent_same_domain_requirements_are_not_dropped():
    plan = understand_task("审核甲合同，然后审核乙合同", project_id="p1", artifact_ids=iter(["a1"]))
    assert plan.mode == "single"
    assert "甲合同" in plan.steps[0].parameters["query"]
    assert "乙合同" in plan.steps[0].parameters["query"]
    assert plan.steps[0].route == "workflow.contract_review"


def test_recycled_material_is_not_misread_as_a_sequence_word():
    plan = understand_task("查询再生混凝土规范")
    assert plan.steps[0].parameters["query"] == "查询再生混凝土规范"


def test_clause_budget_fails_explicitly_instead_of_truncating_work():
    with pytest.raises(ValidationFailure, match="20"):
        understand_task("然后".join(["审核合同", "采购材料"] * 11))


def test_plan_step_rejects_unregistered_tool_routes():
    with pytest.raises(ValueError):
        TaskStep(step=1, domain="knowledge", intent="general_question", route="execute_arbitrary_script")


def test_handoff_is_draft_and_rejects_extra_history_or_unsupported_scope():
    context = RequestContext(tenant_id="t", project_id="p", user_id="u", trace_id="tr", correlation_id="c")
    package = build_handoff(context, source_task="source", target_domain="negotiation")
    assert package.status == "draft"
    assert package.requires_evidence_revalidation is True
    for update in ({"tenant_id": "other"}, {"project_id": "other"}, {"user_id": "other"}):
        with pytest.raises(ValueError):
            package.assert_context(context.model_copy(update=update))
    with pytest.raises(ValueError):
        TaskHandoffPackage.model_validate({**package.model_dump(), "history": ["private text"]})
    with pytest.raises(ValueError, match="evidence"):
        build_handoff(context, source_task="source", target_domain="negotiation", facts={"price": 12})
    with pytest.raises(ValueError, match="history"):
        build_handoff(context, source_task="source", target_domain="negotiation",
                      facts={"history": "private text"}, evidence_refs=[EvidenceRef(kind="artifact", artifact_id="a")])


@pytest.mark.parametrize("query", [
    "审查这个招标文件，然后查供应商报价，最后给我谈判建议",
    "审核这份合同",
    "把图纸建成 Revit 模型",
])
async def test_stream_and_preview_use_one_plan_without_llm_or_business_execution(monkeypatch, query):
    import json
    from unittest.mock import AsyncMock
    from backend.api.v1 import unified_chat

    monkeypatch.setattr(unified_chat.memory_repository, "read", AsyncMock(return_value={"turns": []}))
    remember = AsyncMock()
    monkeypatch.setattr(unified_chat.memory_repository, "append", remember)
    llm_route = AsyncMock(side_effect=AssertionError("registered plan must not be routed a second time"))
    monkeypatch.setattr(unified_chat, "_llm_route", llm_route)
    monkeypatch.setattr(unified_chat, "get_orchestrator", lambda: pytest.fail("must not execute any agent"))
    response = await unified_chat.unified_chat_stream(
        unified_chat.UnifiedChatRequest(message=query, session_id="s", project_id="p"),
        {"tenant_id": "t", "user_id": "u", "role": "user"},
    )
    events = [json.loads(event["data"]) async for event in response.body_iterator]
    assert [event["type"] for event in events] == ["task_plan", "done"]
    assert events[0]["plan"] == understand_task(query, project_id="p").model_dump(mode="json")
    llm_route.assert_not_awaited()
    assert {call.args[1] for call in remember.await_args_list} == {"qa"}
    assert remember.await_args.args[-1]["task_plan"] == events[0]["plan"]
    if "合同" in query:
        assert "尚未接入" in events[0]["message"]


def test_message_rejects_whitespace_only():
    from backend.api.v2.chat import MessageCreate
    with pytest.raises(ValueError, match="blank"):
        MessageCreate(content="    ")


async def test_http_preview_and_stream_preserve_plan_without_creating_workflows(monkeypatch):
    import json
    import uuid
    import httpx
    from fastapi import FastAPI
    from sqlalchemy import select, func
    from backend.api.v1 import unified_chat
    from backend.api.v2 import chat
    from backend.adapters.agent_memory_repository import AgentMemoryRepository
    from backend.db.schema import workflow_runs
    from backend.db.session import engine
    from backend.dependencies import get_current_user

    tenant = "plan-" + uuid.uuid4().hex
    user = {"tenant_id": tenant, "user_id": "owner", "role": "user"}
    app = FastAPI()
    app.include_router(chat.router, prefix="/api/v2")
    app.include_router(unified_chat.router, prefix="/api/v1/chat")
    query = "审查招标文件，然后采购报价，最后谈判建议"
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(unified_chat, "get_orchestrator", lambda: pytest.fail("unexpected LLM/agent call"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/v2/chat/tasks/preview", json={"content": query})).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: user
        for content in ("   ", "x" * 4001):
            assert (await client.post("/api/v2/chat/tasks/preview", json={"content": content})).status_code == 422
        preview = await client.post("/api/v2/chat/tasks/preview", json={"content": query, "project_id": "p"},
                                    headers={"X-Trace-ID": "trace-preview"})
        assert preview.status_code == 200
        assert preview.json()["trace_id"] == "trace-preview"
        response = await client.post("/api/v1/chat/stream", json={"message": query, "project_id": "p", "session_id": "s"})
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[0]["plan"] == preview.json()["plan"]

    await engine.dispose()
    context = RequestContext(tenant_id=tenant, user_id="owner", project_id="p", trace_id="t", correlation_id="c")
    repo = AgentMemoryRepository()
    history = await repo.read(context, "qa", "s")
    assert history["turns"][0]["result"]["task_plan"] == preview.json()["plan"]
    for agent in ("bid_review", "procurement", "negotiation"):
        assert not (await repo.read(context, agent, "s"))["turns"]
    for updates in ({"tenant_id": "other"}, {"project_id": "other"}, {"user_id": "other"}):
        assert not (await repo.read(context.model_copy(update=updates), "qa", "s"))["turns"]
    async with engine.connect() as connection:
        count = await connection.scalar(select(func.count()).select_from(workflow_runs).where(workflow_runs.c.tenant_id == tenant))
    assert count == 0
