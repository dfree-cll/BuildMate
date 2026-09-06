"""drawing2bim 第二期测试：图纸感知通道

覆盖：
1. 结构化提取器（行格式解析/未知类型/空文本）
2. 版本合并器（追加/覆盖/低置信度保留）
3. 变更检测（哈希归一化 + first/changed/unchanged 状态机）
4. perception 节点图纸通道（桩 MCP：首次感知/未变更跳过/变更合并/网关失败回退）
5. API 级图纸审查流程（含违规图纸 → HITL / 合规图纸 → 自动放行）
"""
import asyncio
import httpx
import pytest

from backend.agents.drawing2bim.nodes.extractors import extract_baseline_from_text
from backend.agents.drawing2bim.nodes.version_merge import merge_baseline
from backend.agents.drawing2bim.nodes.change_detection import (
    compute_content_hash, detect_change, save_content_hash,
)

BASE = "/api/v1"


async def _poll_review(api, headers, review_id, timeout=20):
    """新 bim_api 为异步模式：提交返回 pending → 轮询到终态"""
    for _ in range(timeout * 2):
        r = await api.get(f"{BASE}/bim/reviews/{review_id}", headers=headers)
        assert r.status_code == 200, r.text
        j = r.json()
        if j["status"] in ("done", "pending_confirmation", "failed"):
            return j
        await asyncio.sleep(0.5)
    raise AssertionError(f"轮询超时: {review_id} 仍 {r.json()['status']}")

DRAWING_TEXT_BAD = "[墙] 名称: 外墙-W1; 材料: 烧结多孔砖\n[板] 名称: 楼板-B1; 材料: C30"
DRAWING_TEXT_GOOD = ("[墙] 名称: 外墙-W1; 厚度: 240; 材料: 烧结多孔砖\n"
                     "[板] 名称: 楼板-B1; 标高: 3.0; 材料: C30")


# ── 结构化提取器 ────────────────────────────────────────────────────────────

def test_extractor_parses_elements():
    baseline = extract_baseline_from_text(DRAWING_TEXT_GOOD)
    assert len(baseline) == 2
    wall = baseline[0]
    assert wall["ifc_type"] == "IFCWALL"
    assert wall["name"] == "外墙-W1"
    assert wall["properties"]["Thickness"] == 240.0
    assert wall["properties"]["Material"] == "烧结多孔砖"
    slab = baseline[1]
    assert slab["ifc_type"] == "IFCSLAB"
    assert slab["properties"]["Elevation"] == 3.0


def test_extractor_ignores_unknown_type():
    text = "[管道] 名称: P1; 材料: PVC\n" + DRAWING_TEXT_GOOD
    baseline = extract_baseline_from_text(text)
    assert len(baseline) == 2  # 未知类型"管道"被跳过
    assert all(b["ifc_type"] in ("IFCWALL", "IFCSLAB") for b in baseline)


def test_extractor_empty_text():
    assert extract_baseline_from_text("") == []
    assert extract_baseline_from_text("没有任何构件表的普通文字") == []


def test_extractor_english_colon_and_semicolon():
    """中英文冒号/分号混用"""
    text = "[门] 名称: M1；材料: 木质"
    baseline = extract_baseline_from_text(text)
    assert len(baseline) == 1
    assert baseline[0]["ifc_type"] == "IFCDOOR"


# ── 版本合并器 ──────────────────────────────────────────────────────────────

def test_merge_adds_new_elements():
    existing = [{"element_id": "a", "confidence": 0.9}]
    incoming = [{"element_id": "b", "confidence": 0.8}]
    merged = merge_baseline(existing, incoming)
    assert len(merged) == 2


def test_merge_updates_with_higher_confidence():
    existing = [{"element_id": "a", "confidence": 0.5, "name": "旧"}]
    incoming = [{"element_id": "a", "confidence": 0.9, "name": "新"}]
    merged = merge_baseline(existing, incoming)
    assert len(merged) == 1
    assert merged[0]["name"] == "新"
    assert merged[0]["source"] == "merged"


def test_merge_keeps_old_when_lower_confidence():
    existing = [{"element_id": "a", "confidence": 0.9, "name": "旧"}]
    incoming = [{"element_id": "a", "confidence": 0.5, "name": "新"}]
    merged = merge_baseline(existing, incoming)
    assert merged[0]["name"] == "旧"


# ── 变更检测 ────────────────────────────────────────────────────────────────

def test_hash_whitespace_normalized():
    assert compute_content_hash("a\nb") == compute_content_hash("a  \n\n  b\n\n")


async def test_change_detection_state_machine():
    h1 = compute_content_hash("图纸v1")
    h2 = compute_content_hash("图纸v2")
    assert await detect_change("k1", h1) == "first"
    await save_content_hash("k1", h1)
    assert await detect_change("k1", h1) == "unchanged"
    assert await detect_change("k1", h2) == "changed"


# ── perception 节点（桩 MCP）────────────────────────────────────────────────

@pytest.fixture
def stub_mcp(monkeypatch):
    """桩掉 backend.mcp.client.call_mcp_tool"""
    calls = []

    async def fake_call(server_url, tool_name, arguments, timeout=None, role=None, auth_token=None):
        calls.append((server_url, tool_name, arguments))
        if tool_name == "parse_drawing":
            return [{"kind": "pdf", "text": fake_call.text, "page_count": 1}]
        raise AssertionError(f"unexpected tool {tool_name}")

    fake_call.text = DRAWING_TEXT_GOOD
    monkeypatch.setattr("backend.mcp.client.call_mcp_tool", fake_call)
    return fake_call


async def test_perception_drawing_first(stub_mcp):
    from backend.agents.drawing2bim.nodes.perception import perception_node
    result = await perception_node({
        "drawing_path": "/tmp/d.pdf", "drawing_review_key": "pt-first",
        "session_id": "s1", "role": "user",
    })
    assert result["change_status"] == "first"
    assert result["drawing_source"] == "drawing_text"
    assert len(result["golden_baseline"]) == 2
    assert result["baseline_version"] == 1


async def test_perception_drawing_unchanged_skip(stub_mcp):
    """同一图纸二次提交：unchanged 且复用既有基准，不重新提取"""
    from backend.agents.drawing2bim.nodes.perception import perception_node
    existing = [{"element_id": "cached-1", "confidence": 0.99, "name": "缓存构件"}]
    state = {
        "drawing_path": "/tmp/d.pdf", "drawing_review_key": "pt-skip",
        "session_id": "s1", "role": "user",
        "golden_baseline": existing, "baseline_version": 3,
    }
    r1 = await perception_node(state)
    assert r1["change_status"] == "first"

    # 第二次：内容相同 → unchanged，直接复用上轮基准（跳过重感知）
    state["golden_baseline"] = r1["golden_baseline"]
    state["baseline_version"] = r1["baseline_version"]
    r2 = await perception_node(state)
    assert r2["change_status"] == "unchanged"
    assert r2["golden_baseline"] is r1["golden_baseline"]
    assert "baseline_version" not in r2   # 未变更不递增版本


async def test_perception_drawing_changed_merges(stub_mcp):
    """图纸内容变更 → 重新提取并与既有基准合并"""
    from backend.agents.drawing2bim.nodes.perception import perception_node
    stub_mcp.text = DRAWING_TEXT_GOOD
    r1 = await perception_node({
        "drawing_path": "/tmp/d.pdf", "drawing_review_key": "pt-change",
        "session_id": "s1", "role": "user",
    })
    assert len(r1["golden_baseline"]) == 2

    stub_mcp.text = DRAWING_TEXT_GOOD + "\n[门] 名称: M1; 材料: 木质"
    r2 = await perception_node({
        "drawing_path": "/tmp/d.pdf", "drawing_review_key": "pt-change",
        "session_id": "s1", "role": "user",
        "golden_baseline": r1["golden_baseline"],
        "baseline_version": r1["baseline_version"],
    })
    assert r2["change_status"] == "changed"
    assert len(r2["golden_baseline"]) == 3   # 原 2 + 新增门 1
    assert r2["baseline_version"] == r1["baseline_version"] + 1


async def test_perception_gateway_failure_fallback(monkeypatch):
    """MCP 网关失败 → 回退注入基准/内置样例"""
    async def failing_call(*args, **kwargs):
        raise ConnectionError("server down")
    monkeypatch.setattr("backend.mcp.client.call_mcp_tool", failing_call)

    from backend.agents.drawing2bim.nodes.perception import perception_node
    result = await perception_node({
        "drawing_path": "/tmp/d.pdf", "session_id": "s-fb", "role": "user",
    })
    assert result["drawing_source"] == "demo"
    assert len(result["golden_baseline"]) == 2  # 内置样例兜底


# ── API 级图纸审查流程 ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
async def _isolated_env():
    from backend.main import app
    from backend.db.session import engine
    from backend.db.schema import METADATA
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    async with app.router.lifespan_context(app):
        yield
    await engine.dispose()
    from backend.core import memory as _mem, orchestrator as _orch
    _mem._memory_savers.clear()
    _orch._orchestrator = None


@pytest.fixture
async def api():
    from backend.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


async def _login(api, username, password="demo123"):
    r = await api.post(f"{BASE}/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


async def test_api_drawing_with_violations_triggers_hitl(api, stub_mcp):
    """含违规图纸（板缺标高）→ 需人工确认 → admin 批准"""
    stub_mcp.text = DRAWING_TEXT_BAD
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"drawing_path": "/tmp/bad.pdf",
                             "drawing_review_key": "api-bad",
                             "session_id": "api-bad"})
    assert r.status_code == 200, r.text
    review_id = r.json()["review_id"]
    j = await _poll_review(api, user, review_id)
    assert j["status"] == "pending_confirmation"
    assert (j.get("structured_data") or {}).get("hard_count", 0) >= 1

    admin = await _login(api, "admin")
    r2 = await api.post(f"{BASE}/bim/reviews/{review_id}/confirm",
                        headers=admin, json={"decision": "approved"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["verdict"] == "approved"


async def test_api_clean_drawing_auto_passes(api, stub_mcp):
    """全合规图纸 → 自动放行"""
    stub_mcp.text = DRAWING_TEXT_GOOD
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"drawing_path": "/tmp/good.pdf",
                             "drawing_review_key": "api-good",
                             "session_id": "api-good"})
    assert r.status_code == 200, r.text
    review_id = r.json()["review_id"]
    j = await _poll_review(api, user, review_id)
    assert j["status"] == "done"
    assert (j.get("structured_data") or {}).get("verdict") == "pass"
