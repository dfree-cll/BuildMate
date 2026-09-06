"""drawing2bim 第四期测试：多模态视觉感知通道

覆盖：
1. 视觉提取器纯函数（结果转换/未知类型过滤/空输入）
2. perception_node 视觉通道（桩 MCP：成功/空结果回退/auto 模式优先级）
3. API 级视觉流程（perception_mode=vision/text/auto）
"""
import asyncio
import httpx
import pytest

from backend.core.drawing_vision import _vision_result_to_baseline

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


# ── 视觉提取器纯函数 ────────────────────────────────────────────────────────

def test_vision_result_to_baseline_converts():
    items = [
        {"type": "墙", "name": "W1", "thickness": 240, "material": "砖"},
        {"type": "板", "name": "B1", "elevation": 3.0, "material": "C30"},
    ]
    baseline = _vision_result_to_baseline(items)
    assert len(baseline) == 2
    assert baseline[0]["ifc_type"] == "IFCWALL"
    assert baseline[0]["properties"]["Thickness"] == 240.0
    assert baseline[0]["source"] == "drawing_vision"
    assert baseline[0]["confidence"] == 0.75
    assert baseline[1]["ifc_type"] == "IFCSLAB"
    assert baseline[1]["properties"]["Elevation"] == 3.0


def test_vision_result_filters_unknown_type():
    items = [{"type": "管道", "name": "P1"}, {"type": "门", "name": "D1"}]
    baseline = _vision_result_to_baseline(items)
    assert len(baseline) == 1
    assert baseline[0]["ifc_type"] == "IFCDOOR"


def test_vision_result_empty_input():
    assert _vision_result_to_baseline([]) == []
    assert _vision_result_to_baseline([{"not_a_dict": True}]) == []


# ── perception_node 视觉通道（桩 MCP）──────────────────────────────────────

@pytest.fixture
def vision_stub(monkeypatch):
    """桩掉 call_mcp_tool，模拟视觉工具返回"""
    import types
    stub = types.SimpleNamespace(calls=[], vision_response={
        "kind": "vision",
        "baseline": [
            {"element_id": "vis-w1", "ifc_type": "IFCWALL", "name": "W1",
             "properties": {"Thickness": 240}, "confidence": 0.75,
             "source": "drawing_vision", "review_status": "pending"},
        ],
        "elements_count": 1,
    })

    async def fake_call(server_url, tool_name, arguments, timeout=None, role=None, auth_token=None):
        stub.calls.append(tool_name)
        if tool_name == "parse_drawing_vision":
            return [stub.vision_response]
        if tool_name == "parse_drawing":
            return [{"kind": "pdf", "text": "[墙] 名称: W1; 厚度: 200; 材料: 砖", "page_count": 1}]
        raise AssertionError(f"unexpected tool {tool_name}")

    monkeypatch.setattr("backend.mcp.client.call_mcp_tool", fake_call)
    return stub


async def test_perception_vision_mode_success(vision_stub):
    from backend.agents.drawing2bim.nodes.perception import perception_node
    result = await perception_node({
        "drawing_path": "/tmp/d.png", "perception_mode": "vision",
        "session_id": "v1", "role": "user",
    })
    assert result["drawing_source"] == "drawing_vision"
    assert len(result["golden_baseline"]) == 1


async def test_perception_vision_empty_falls_back_to_text_in_auto(vision_stub):
    """auto 模式（PDF）：视觉返回空 → 自动回退文本通道"""
    vision_stub.vision_response = {"kind": "vision", "baseline": [], "elements_count": 0}
    from backend.agents.drawing2bim.nodes.perception import perception_node
    result = await perception_node({
        "drawing_path": "/tmp/d.pdf", "perception_mode": "auto",
        "session_id": "v2", "role": "user",
    })
    assert result["drawing_source"] == "drawing_text"


async def test_perception_text_mode_skips_vision(vision_stub):
    """text 模式：不走视觉通道"""
    from backend.agents.drawing2bim.nodes.perception import perception_node
    result = await perception_node({
        "drawing_path": "/tmp/d.pdf", "perception_mode": "text",
        "session_id": "v3", "role": "user",
    })
    assert result["drawing_source"] == "drawing_text"
    assert "parse_drawing_vision" not in vision_stub.calls


async def test_perception_vision_only_mode_no_text_fallback(vision_stub):
    """vision 失败（图片格式）→ 错误短路（第五期：禁止静默回退假数据），且不走文本通道"""
    vision_stub.vision_response = {"error": "模型不可用"}
    from backend.agents.drawing2bim.nodes.perception import perception_node
    result = await perception_node({
        "drawing_path": "/tmp/d.png", "perception_mode": "vision",
        "session_id": "v4", "role": "user",
    })
    assert result["perception_error"]
    assert result["drawing_source"] == "unsupported"
    assert "golden_baseline" not in result
    assert "parse_drawing" not in vision_stub.calls


# ── API 级视觉流程 ──────────────────────────────────────────────────────────

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


@pytest.fixture
def vision_e2e_stub(monkeypatch):
    """进程内模拟视觉+文本 MCP（视觉返回合规构件，文本返回违规构件）"""
    async def fake_call(server_url, tool_name, arguments, timeout=None, role=None, auth_token=None):
        if tool_name == "parse_drawing_vision":
            return [{
                "kind": "vision",
                "baseline": [
                    {"element_id": "vis-w1", "ifc_type": "IFCWALL", "name": "W1",
                     "properties": {"Thickness": 240, "Material": "砖"},
                     "confidence": 0.75, "source": "drawing_vision", "review_status": "pending"},
                ],
                "elements_count": 1,
            }]
        if tool_name == "parse_drawing":
            return [{"kind": "pdf", "text": "[墙] 名称: W1; 材料: 砖", "page_count": 1}]
        raise AssertionError(f"unexpected tool {tool_name}")
    monkeypatch.setattr("backend.mcp.client.call_mcp_tool", fake_call)
    return fake_call


async def test_api_vision_mode_clean_passes(api, vision_e2e_stub):
    """vision 模式：视觉提取合规构件 → 自动放行"""
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"drawing_path": "/tmp/d.png",
                             "perception_mode": "vision",
                             "session_id": "api-vis-clean"})
    assert r.status_code == 200, r.text
    review_id = r.json()["review_id"]
    j = await _poll_review(api, user, review_id)
    assert j["status"] == "done"
    assert (j.get("structured_data") or {}).get("verdict") == "pass"


async def test_api_auto_mode_vision_first(api, vision_e2e_stub):
    """auto 模式：优先视觉（合规），不走文本通道"""
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"drawing_path": "/tmp/d.png",
                             "perception_mode": "auto",
                             "session_id": "api-auto"})
    review_id = r.json()["review_id"]
    j = await _poll_review(api, user, review_id)
    assert (j.get("structured_data") or {}).get("verdict") == "pass"   # 视觉合规 → pass；若走了文本通道会触发 HITL
