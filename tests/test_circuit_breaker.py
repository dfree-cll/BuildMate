"""熔断器单元测试 + 编排器集成测试（桩图模拟，无需真实 LLM）"""
import asyncio

import pytest

from backend.core.circuit_breaker import CircuitBreaker
from backend.core.orchestrator import (
    Orchestrator, AgentRequest, AgentType, ExecutionMode,
)


# ── 状态机 ──────────────────────────────────────────────────────────────────

def test_closed_allows_and_resets_on_success():
    cb = CircuitBreaker("t", failure_threshold=3, cooldown_seconds=60)
    assert cb.state == "closed" and cb.allow_request()
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.consecutive_failures == 0 and cb.state == "closed"


def test_opens_after_threshold_failures():
    cb = CircuitBreaker("t", failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == "open" and not cb.allow_request()


def test_half_open_after_cooldown(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("backend.core.circuit_breaker.time.monotonic", lambda: fake_now[0])
    cb = CircuitBreaker("t", failure_threshold=2, cooldown_seconds=30)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    fake_now[0] = 1030.0
    assert cb.state == "half_open" and cb.allow_request()


def test_half_open_success_closes(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("backend.core.circuit_breaker.time.monotonic", lambda: fake_now[0])
    cb = CircuitBreaker("t", failure_threshold=2, cooldown_seconds=10)
    cb.record_failure(); cb.record_failure()
    fake_now[0] = 1010.0
    cb.record_success()
    assert cb.state == "closed" and cb.consecutive_failures == 0


def test_half_open_failure_reopens(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("backend.core.circuit_breaker.time.monotonic", lambda: fake_now[0])
    cb = CircuitBreaker("t", failure_threshold=2, cooldown_seconds=10)
    cb.record_failure(); cb.record_failure()
    fake_now[0] = 1010.0
    assert cb.state == "half_open"
    cb.record_failure()
    assert cb.state == "open"


# ── 编排器集成（桩图） ──────────────────────────────────────────────────────

class _StubGraph:
    """ainvoke 行为可配：正常 / 降级 / 抛异常 / 超时"""
    def __init__(self, answer="ok", fallback=False, raise_exc=None, delay=0.0):
        self.answer = answer
        self.fallback = fallback
        self.raise_exc = raise_exc
        self.delay = delay

    async def ainvoke(self, state, config=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc:
            raise self.raise_exc
        return {"answer": self.answer, "fallback_used": self.fallback}


def _make_orchestrator(graph, threshold=2):
    orch = Orchestrator()
    orch._failure_threshold = threshold
    orch._cooldown_seconds = 60
    orch._graphs["qa"] = graph
    return orch


def _qa_request():
    return AgentRequest(user_id="u1", agent_type=AgentType.QA, input_text="hi")


async def test_success_keeps_breaker_closed():
    orch = _make_orchestrator(_StubGraph())
    resp = await orch.handle(_qa_request())
    assert resp.content == "ok" and not resp.fallback_used
    assert orch._get_breaker("qa").state == "closed"


async def test_fallback_counts_failure_and_opens():
    orch = _make_orchestrator(_StubGraph(fallback=True), threshold=2)
    r1 = await orch.handle(_qa_request())
    assert r1.fallback_used
    assert orch._get_breaker("qa").state == "closed"   # 1 次 < 阈值 2
    r2 = await orch.handle(_qa_request())
    assert orch._get_breaker("qa").state == "open"
    # 第 3 次：熔断打开 → 直接拒绝，不再执行图
    r3 = await orch.handle(_qa_request())
    assert r3.fallback_used and "熔断" in r3.content


async def test_breaker_isolated_per_agent():
    orch = _make_orchestrator(_StubGraph(fallback=True), threshold=1)
    await orch.handle(_qa_request())
    assert orch._get_breaker("qa").state == "open"
    assert orch._get_breaker("bid_review").state == "closed"


async def test_task_timeout_counts_failure(monkeypatch):
    from backend.config import get_settings
    orch = _make_orchestrator(_StubGraph(delay=5.0), threshold=1)
    monkeypatch.setattr(get_settings(), "orchestrator_task_timeout", 0.05)
    resp = await orch.handle(_qa_request())
    assert resp.fallback_used and "超时" in resp.content
    assert orch._get_breaker("qa").state == "open"
