"""drawing2bim 第三期测试：IFC 生成闭环

覆盖：
1. 生成器真实往返（结果级验证）：基准 → 生成 → 解析回读 → 差异为零
2. 差异比对器纯函数（missing/type_mismatch/name_mismatch/extra）
3. 修正回退纯函数（不可生成构件剔除）
4. ifc_generate 节点三态（verified / diffs_found / error）——桩 MCP
5. API 级端到端：合规图纸 + generate_ifc → verified；驳回 → 不生成
"""
import asyncio
import os
import tempfile

import httpx
import pytest

from backend.engines.ifc_generator import generate_ifc_from_baseline
from backend.engines.ifc_parser import _sync_parse
from backend.agents.drawing2bim.nodes.ifc_diff import (
    compute_ifc_diffs, apply_corrections,
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

GOOD_BASELINE = [
    {"element_id": "wall-001", "ifc_type": "IFCWALL", "name": "外墙-W1",
     "properties": {"Thickness": 240, "Material": "烧结多孔砖"},
     "confidence": 0.95, "source": "demo", "review_status": "pending"},
    {"element_id": "slab-001", "ifc_type": "IFCSLAB", "name": "楼板-B1",
     "properties": {"Thickness": 120, "Elevation": 3.0, "Material": "C30"},
     "confidence": 0.9, "source": "demo", "review_status": "pending"},
]

DRAWING_TEXT_GOOD = ("[墙] 名称: 外墙-W1; 厚度: 240; 材料: 烧结多孔砖\n"
                     "[板] 名称: 楼板-B1; 标高: 3.0; 材料: C30")


# ── 生成器真实往返（不桩，结果级验证）───────────────────────────────────────

async def test_generate_parse_roundtrip_zero_diff():
    """黄金基准 → IFC4 生成 → 解析回读 → 差异为零（含 GlobalId 追溯）"""
    tmp = os.path.join(tempfile.gettempdir(), "d2b_roundtrip_test.ifc")
    try:
        result = await generate_ifc_from_baseline(GOOD_BASELINE, tmp)
        assert result["elements_written"] == 2
        assert result["elements_skipped"] == 0
        assert os.path.isfile(tmp)

        parsed = await asyncio.to_thread(_sync_parse, tmp)
        diffs = compute_ifc_diffs(GOOD_BASELINE, parsed)
        assert diffs == [], f"往返应零差异，实际: {diffs}"
        # GlobalId 追溯：回读构件 ID 与基准一致
        rt_ids = {e["global_id"] for e in parsed["elements"]}
        assert {"wall-001", "slab-001"} <= rt_ids
    finally:
        if os.path.isfile(tmp):
            os.remove(tmp)


async def test_generate_skips_unsupported_type():
    """不可生成类型（如管道）被跳过且不落文件错误"""
    baseline = GOOD_BASELINE + [
        {"element_id": "pipe-001", "ifc_type": "IFCPIPE", "name": "P1",
         "properties": {}, "confidence": 0.8, "source": "test", "review_status": "pending"}]
    tmp = os.path.join(tempfile.gettempdir(), "d2b_skip_test.ifc")
    try:
        result = await generate_ifc_from_baseline(baseline, tmp)
        assert result["elements_written"] == 2
        assert result["elements_skipped"] == 1
    finally:
        if os.path.isfile(tmp):
            os.remove(tmp)


# ── 差异比对器纯函数 ────────────────────────────────────────────────────────

def test_diff_missing_element():
    parsed = {"elements": []}
    diffs = compute_ifc_diffs(GOOD_BASELINE, parsed)
    assert len(diffs) == 2
    assert all(d["kind"] == "missing" and d["category"] == "geometric" for d in diffs)


def test_diff_type_mismatch():
    parsed = {"elements": [
        {"global_id": "wall-001", "ifc_type": "IFCBEAM", "name": "外墙-W1", "type": "梁",
         "Psets": [{"Pset_Wall": {"Thickness": 240, "Material": "烧结多孔砖"}}], "Quantities": [], "Materials": []},
        {"global_id": "slab-001", "ifc_type": "IFCSLAB", "name": "楼板-B1", "type": "楼板",
         "Psets": [{"Pset_Slab": {"Thickness": 120, "Elevation": 3.0, "Material": "C30"}}], "Quantities": [], "Materials": []},
    ]}
    diffs = compute_ifc_diffs(GOOD_BASELINE, parsed)
    assert len(diffs) == 1
    assert diffs[0]["kind"] == "type_mismatch" and diffs[0]["category"] == "semantic"


def test_diff_name_mismatch():
    parsed = {"elements": [
        {"global_id": "wall-001", "ifc_type": "IFCWALL", "name": "别的名字", "type": "墙",
         "Psets": [{"Pset_Wall": {"Thickness": 240, "Material": "烧结多孔砖"}}], "Quantities": [], "Materials": []},
        {"global_id": "slab-001", "ifc_type": "IFCSLAB", "name": "楼板-B1", "type": "楼板",
         "Psets": [{"Pset_Slab": {"Thickness": 120, "Elevation": 3.0, "Material": "C30"}}], "Quantities": [], "Materials": []},
    ]}
    diffs = compute_ifc_diffs(GOOD_BASELINE, parsed)
    assert len(diffs) == 1 and diffs[0]["kind"] == "name_mismatch"


def test_diff_extra_element():
    parsed = {"elements": [
        {"global_id": "wall-001", "ifc_type": "IFCWALL", "name": "外墙-W1", "type": "墙",
         "Psets": [{"Pset_Wall": {"Thickness": 240, "Material": "烧结多孔砖"}}], "Quantities": [], "Materials": []},
        {"global_id": "slab-001", "ifc_type": "IFCSLAB", "name": "楼板-B1", "type": "楼板",
         "Psets": [{"Pset_Slab": {"Thickness": 120, "Elevation": 3.0, "Material": "C30"}}], "Quantities": [], "Materials": []},
        {"global_id": "ghost-001", "ifc_type": "IFCWALL", "name": "幽灵墙", "type": "墙",
         "Psets": [], "Quantities": [], "Materials": []},
    ]}
    diffs = compute_ifc_diffs(GOOD_BASELINE, parsed)
    assert len(diffs) == 1 and diffs[0]["kind"] == "extra"


def test_diff_exact_match_is_empty():
    parsed = {"elements": [
        {"global_id": "wall-001", "ifc_type": "IFCWALL", "name": "外墙-W1", "type": "墙",
         "Psets": [{"Pset_Wall": {"Thickness": 240, "Material": "烧结多孔砖"}}], "Quantities": [], "Materials": []},
        {"global_id": "slab-001", "ifc_type": "IFCSLAB", "name": "楼板-B1", "type": "楼板",
         "Psets": [{"Pset_Slab": {"Thickness": 120, "Elevation": 3.0, "Material": "C30"}}], "Quantities": [], "Materials": []},
    ]}
    assert compute_ifc_diffs(GOOD_BASELINE, parsed) == []


# ── 修正回退纯函数 ──────────────────────────────────────────────────────────

def test_corrections_exclude_unsupported_missing():
    """缺失且不可生成的构件 → 剔除；可生成的缺失构件保留（不可凭空修复）"""
    baseline = GOOD_BASELINE + [
        {"element_id": "pipe-001", "ifc_type": "IFCPIPE", "name": "P1",
         "properties": {}, "confidence": 0.8, "source": "t", "review_status": "pending"}]
    diffs = [
        {"element_id": "pipe-001", "kind": "missing", "category": "geometric", "detail": "缺失"},
        {"element_id": "wall-001", "kind": "missing", "category": "geometric", "detail": "缺失"},
    ]
    corrected, applied = apply_corrections(baseline, diffs)
    ids = {e["element_id"] for e in corrected}
    assert "pipe-001" not in ids          # 不可生成 → 剔除
    assert "wall-001" in ids              # 可生成的缺失保留（黄金基准不篡改）
    assert len(applied) == 1


def test_corrections_noop_without_missing():
    diffs = [{"element_id": "wall-001", "kind": "name_mismatch",
              "category": "semantic", "detail": "名称不一致"}]
    corrected, applied = apply_corrections(GOOD_BASELINE, diffs)
    assert len(corrected) == 2 and applied == []


# ── ifc_generate 节点三态（桩 MCP）──────────────────────────────────────────

def _make_mcp_stub(generate_ok=True, parse_elements=None):
    async def fake_call(server_url, tool_name, arguments, timeout=None, role=None, auth_token=None):
        if tool_name == "generate_ifc_model":
            if not generate_ok:
                return [{"error": "模拟生成失败"}]
            return [{"ifc_path": arguments["out_path"], "elements_written": 2,
                     "elements_skipped": 0}]
        if tool_name == "parse_ifc_model":
            return [{"elements": parse_elements if parse_elements is not None else []}]
        raise AssertionError(f"unexpected tool {tool_name}")
    return fake_call


async def test_node_verified_path(monkeypatch):
    from backend.agents.drawing2bim.nodes.ifc_generate import ifc_generate_node
    # parse 回读与基准完全一致（含属性）→ 零差异 → verified
    rt = [{"global_id": e["element_id"], "ifc_type": e["ifc_type"],
           "name": e["name"], "type": "x",
           "Psets": [{"Pset_Test": dict(e.get("properties") or {})}],
           "Quantities": [], "Materials": []} for e in GOOD_BASELINE]
    monkeypatch.setattr("backend.mcp.client.call_mcp_tool", _make_mcp_stub(parse_elements=rt))
    result = await ifc_generate_node({
        "generate_ifc": True, "final_baseline": GOOD_BASELINE,
        "session_id": "t-verified", "role": "user",
    })
    gen = result["ifc_generation"]
    assert gen["status"] == "verified"
    assert gen["iterations"] == 1 and gen["diffs"] == []


async def test_node_diffs_found_no_fixable(monkeypatch):
    """回读为空 → 全部 missing → 无可应用修正 → diffs_found（不死循环）"""
    from backend.agents.drawing2bim.nodes.ifc_generate import ifc_generate_node
    monkeypatch.setattr("backend.mcp.client.call_mcp_tool",
                        _make_mcp_stub(parse_elements=[]))
    result = await ifc_generate_node({
        "generate_ifc": True, "final_baseline": GOOD_BASELINE,
        "session_id": "t-diffs", "role": "user",
    })
    gen = result["ifc_generation"]
    assert gen["status"] == "diffs_found"
    assert len(gen["diffs"]) == 2
    assert gen["iterations"] == 1   # 无修正可做，未进入无效重试


async def test_node_error_on_generate_failure(monkeypatch):
    from backend.agents.drawing2bim.nodes.ifc_generate import ifc_generate_node
    monkeypatch.setattr("backend.mcp.client.call_mcp_tool",
                        _make_mcp_stub(generate_ok=False))
    result = await ifc_generate_node({
        "generate_ifc": True, "final_baseline": GOOD_BASELINE,
        "session_id": "t-err", "role": "user",
    })
    assert result["ifc_generation"]["status"] == "error"


async def test_node_skipped_when_not_requested():
    from backend.agents.drawing2bim.nodes.ifc_generate import ifc_generate_node
    result = await ifc_generate_node({"generate_ifc": False})
    assert result["ifc_generation"]["status"] == "skipped"


# ── API 级端到端 ────────────────────────────────────────────────────────────

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
def e2e_mcp_stub(monkeypatch):
    """进程内模拟 IFC/图纸 MCP Server（真实生成+解析，不走 HTTP）"""
    async def fake_call(server_url, tool_name, arguments, timeout=None, role=None, auth_token=None):
        if tool_name == "parse_drawing":
            return [{"kind": "pdf", "text": fake_call.drawing_text, "page_count": 1}]
        if tool_name == "generate_ifc_model":
            res = await generate_ifc_from_baseline(arguments["baseline"], arguments["out_path"])
            return [res]
        if tool_name == "parse_ifc_model":
            parsed = await asyncio.to_thread(_sync_parse, arguments["ifc_path"])
            return [parsed]
        raise AssertionError(f"unexpected tool {tool_name}")
    fake_call.drawing_text = DRAWING_TEXT_GOOD
    monkeypatch.setattr("backend.mcp.client.call_mcp_tool", fake_call)
    return fake_call


async def test_api_generate_ifc_verified_e2e(api, e2e_mcp_stub):
    """合规图纸 + generate_ifc → 审查通过 → 真实生成+回读 → verified"""
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"drawing_path": "/tmp/g.pdf",
                             "drawing_review_key": "e2e-gen",
                             "session_id": "e2e-gen",
                             "generate_ifc": True})
    assert r.status_code == 200, r.text
    review_id = r.json()["review_id"]
    j = await _poll_review(api, user, review_id)
    assert j["status"] == "done"
    sd = j.get("structured_data") or {}
    assert sd.get("verdict") == "pass"
    gen = sd.get("ifc_generation") or {}
    assert gen.get("status") == "verified"
    assert gen.get("diffs") == []
    assert os.path.isfile(gen.get("ifc_path", ""))


async def test_api_rejected_verdict_skips_generation(api, e2e_mcp_stub):
    """HITL 驳回 → verdict rejected → 不生成 IFC"""
    e2e_mcp_stub.drawing_text = "[墙] 名称: 外墙-W1; 材料: 砖"   # 缺厚度 → 违规 → HITL
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"drawing_path": "/tmp/bad.pdf",
                             "drawing_review_key": "e2e-rej",
                             "session_id": "e2e-rej",
                             "generate_ifc": True})
    review_id = r.json()["review_id"]
    await _poll_review(api, user, review_id)

    admin = await _login(api, "admin")
    r2 = await api.post(f"{BASE}/bim/reviews/{review_id}/confirm",
                        headers=admin, json={"decision": "rejected"})
    assert r2.status_code == 200, r2.text
    j2 = r2.json()
    assert j2["verdict"] == "rejected"
    assert j2["ifc_generation"] is None   # 驳回 → 条件边不触发生成
