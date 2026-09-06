"""The merged entry must not send users to removed standalone business pages."""

import json
from unittest.mock import AsyncMock

import pytest

from backend.api.v1.unified_chat import _GUIDANCE
from backend.core.orchestrator import AgentType, ExecutionMode


@pytest.mark.parametrize(
    "agent, capability",
    [
        (AgentType.BID_REVIEW, "bid_review"),
        (AgentType.PROCUREMENT, "procurement"),
        (AgentType.NEGOTIATION, "negotiation"),
    ],
)
def test_business_guidance_opens_an_isolated_workbench_panel(agent, capability):
    guidance = _GUIDANCE[agent]
    assert guidance["capability"] == capability
    assert guidance["action_url"] == f"/qa?capability={capability}"
    assert guidance["action_label"]


def test_bim_guidance_keeps_the_dedicated_entry():
    assert _GUIDANCE[AgentType.DRAWING2BIM]["action_url"] == "/bim"
    assert _GUIDANCE[AgentType.DRAWING2BIM]["capability"] == "bim"


async def test_stream_opens_procurement_panel_without_executing_business(monkeypatch):
    from backend.api.v1 import unified_chat

    monkeypatch.setattr(unified_chat.memory_repository, "read", AsyncMock(return_value={"turns": []}))
    remember = AsyncMock()
    monkeypatch.setattr(unified_chat.memory_repository, "append", remember)
    monkeypatch.setattr(unified_chat, "_llm_route", AsyncMock(return_value={
        "label": "procurement", "agent_type": AgentType.PROCUREMENT,
        "execution_mode": ExecutionMode.SINGLE, "reason": "purchase request",
    }))
    response = await unified_chat.unified_chat_stream(
        unified_chat.UnifiedChatRequest(message="submit a purchase", session_id="s", project_id="p"),
        {"tenant_id": "t", "user_id": "u", "role": "user"},
    )
    events = [json.loads(event["data"]) async for event in response.body_iterator]
    guidance = next(event for event in events if event["type"] == "guidance")
    assert guidance["capability"] == "procurement"
    assert guidance["action_url"] == "/qa?capability=procurement"
    # Only router/QA history is written; routing must not create/approve an order.
    assert {call.args[1] for call in remember.await_args_list} == {"router", "qa"}
    assert events[-1]["type"] == "done"
