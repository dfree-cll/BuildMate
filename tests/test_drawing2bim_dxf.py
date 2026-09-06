"""drawing2bim 第五期测试：DXF/DWG 原生矢量感知通道

覆盖：
1. 图层映射匹配（关键词/长词优先/未知图层）
2. 几何启发式：平行双线 → 墙厚精确推断（ezdxf 合成夹具）
3. 块引用门窗识别（M*/C* 前缀）
4. 完整合成 DXF 端到端提取（ezdxf 写 → 解析 → 提取）
5. perception 节点 DXF/DWG 通道（桩 MCP：成功/零构件短路/DWG 引导）
6. API 级：DWG 上传短路指引 + DXF 审查流程
"""
import asyncio
import httpx
import pytest

from backend.agents.drawing2bim.nodes.dxf_extractor import (
    _match_layer_type, extract_baseline_from_dxf,
    extract_walls_from_double_lines, extract_doors_windows_from_inserts,
    CONFIDENCE_LAYER, CONFIDENCE_GEOMETRY,
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


# ── 合成 DXF 夹具构造器 ─────────────────────────────────────────────────────

def _build_synthetic_dxf(tmp_path) -> str:
    """用 ezdxf 程序化生成合成图纸：墙层双线 + 门块 + 窗块"""
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.layers.add("WALL-墙体", color=1)
    doc.layers.add("DOOR-门窗", color=3)
    msp = doc.modelspace()

    # 一面 240mm 墙：两条平行线（y=0 与 y=240），长度 3600
    msp.add_line((0, 0), (3600, 0), dxfattribs={"layer": "WALL-墙体"})
    msp.add_line((0, 240), (3600, 240), dxfattribs={"layer": "WALL-墙体"})
    # 一面 120mm 内墙（垂直方向）
    msp.add_line((3600, 0), (3600, 2400), dxfattribs={"layer": "WALL-墙体"})
    msp.add_line((3720, 0), (3720, 2400), dxfattribs={"layer": "WALL-墙体"})
    # 无厚度单线墙（HR-001 图纸规则触发 HITL 用）
    msp.add_line((5000, 0), (5000, 3000), dxfattribs={"layer": "WALL-墙体"})

    # 门窗块（块名惯例 M1/C1）
    for name, pt in (("M1", (1000, 0)), ("M2", (2000, 0)), ("C1", (1500, 240))):
        if name not in doc.blocks:
            doc.blocks.new(name)
        msp.add_blockref(name, pt, dxfattribs={"layer": "DOOR-门窗"})

    path = str(tmp_path / "synthetic.dxf")
    doc.saveas(path)
    return path


# ── 图层映射 ────────────────────────────────────────────────────────────────

def test_layer_map_matches_keywords():
    assert _match_layer_type("WALL-墙体") == "IFCWALL"
    assert _match_layer_type("建-门窗") == "IFCDOOR"  # 门窗 → 长词优先命中"门"？见下
    assert _match_layer_type("柱子") == "IFCCOLUMN"
    assert _match_layer_type("0") is None
    assert _match_layer_type("轴线") is None


def test_layer_map_longest_keyword_wins():
    """'门窗'层应命中更具体的关键词（不先被单字'门'吃掉）"""
    # 默认表中 '门' 和 '窗' 都是单字，'门窗' 两个都含 → 长词优先规则保证稳定
    result = _match_layer_type("门窗")
    assert result in ("IFCDOOR", "IFCWINDOW")


# ── 几何启发式 ──────────────────────────────────────────────────────────────

def test_parallel_double_lines_infer_wall_thickness():
    parsed = {
        "lines": [
            {"handle": "L1", "type": "LINE", "layer": "WALL",
             "start": (0, 0), "end": (3600, 0)},
            {"handle": "L2", "type": "LINE", "layer": "WALL",
             "start": (0, 240), "end": (3600, 240)},
        ],
    }
    walls = extract_walls_from_double_lines(parsed, {"WALL"})
    assert len(walls) == 1
    assert walls[0]["ifc_type"] == "IFCWALL"
    assert walls[0]["properties"]["Thickness"] == 240.0
    assert walls[0]["properties"]["Length"] == 3600.0
    assert walls[0]["confidence"] == CONFIDENCE_GEOMETRY


def test_non_parallel_or_too_far_lines_not_walls():
    parsed = {
        "lines": [
            {"handle": "L1", "type": "LINE", "layer": "WALL",
             "start": (0, 0), "end": (3600, 0)},
            # 间距 900mm 超出墙厚合理范围
            {"handle": "L2", "type": "LINE", "layer": "WALL",
             "start": (0, 900), "end": (3600, 900)},
        ],
    }
    assert extract_walls_from_double_lines(parsed, {"WALL"}) == []


def test_lwpolyline_double_lines_are_split_before_pairing():
    parsed = {
        "lines": [
            {"handle": "P1", "type": "LWPOLYLINE", "layer": "A-WALL",
             "closed": False, "points": [(0, 0), (3000, 0)]},
            {"handle": "P2", "type": "LWPOLYLINE", "layer": "A-WALL",
             "closed": False, "points": [(0, 200), (3000, 200)]},
        ],
    }

    walls = extract_walls_from_double_lines(parsed, {"A-WALL"})

    assert len(walls) == 1
    assert walls[0]["properties"]["Thickness"] == 200.0
    assert walls[0]["properties"]["Length"] == 3000.0


def test_reversed_parallel_edge_keeps_valid_centerline():
    parsed = {
        "lines": [
            {"handle": "L1", "type": "LINE", "layer": "A-WALL",
             "start": (0, 0), "end": (3000, 0)},
            {"handle": "L2", "type": "LINE", "layer": "A-WALL",
             "start": (3000, 200), "end": (0, 200)},
        ],
    }

    walls = extract_walls_from_double_lines(parsed, {"A-WALL"})

    assert len(walls) == 1
    geometry = walls[0]["geometry"]
    assert geometry["center_start"] == (0.0, 100.0)
    assert geometry["center_end"] == (3000.0, 100.0)


def test_only_wall_layers_counted():
    """非墙层的双线不参与墙体推断"""
    parsed = {
        "lines": [
            {"handle": "L1", "type": "LINE", "layer": "轴线",
             "start": (0, 0), "end": (3600, 0)},
            {"handle": "L2", "type": "LINE", "layer": "轴线",
             "start": (0, 240), "end": (3600, 240)},
        ],
    }
    assert extract_walls_from_double_lines(parsed, {"WALL"}) == []


# ── 块引用门窗识别 ──────────────────────────────────────────────────────────

def test_insert_block_door_window_recognition():
    parsed = {"inserts": [
        {"handle": "I1", "layer": "门窗", "block_name": "M1", "insert_point": (0, 0)},
        {"handle": "I2", "layer": "门窗", "block_name": "C-2", "insert_point": (0, 0)},
        {"handle": "I3", "layer": "门窗", "block_name": "轴网", "insert_point": (0, 0)},
    ]}
    elements = extract_doors_windows_from_inserts(parsed)
    assert len(elements) == 2
    types = {e["ifc_type"] for e in elements}
    assert types == {"IFCDOOR", "IFCWINDOW"}
    assert all(e["confidence"] == CONFIDENCE_LAYER for e in elements)


# ── 完整合成 DXF 端到端 ─────────────────────────────────────────────────────

async def test_synthetic_dxf_full_extraction(tmp_path):
    """ezdxf 写合成图纸 → 真实解析 → 提取：2 面双线墙 + 1 面单线墙。"""
    from backend.engines.dxf_parser import parse_dxf

    path = _build_synthetic_dxf(tmp_path)
    parsed = await parse_dxf(path)
    baseline = extract_baseline_from_dxf(parsed)

    walls = [e for e in baseline if e["ifc_type"] == "IFCWALL"]
    doors = [e for e in baseline if e["ifc_type"] == "IFCDOOR"]
    windows = [e for e in baseline if e["ifc_type"] == "IFCWINDOW"]
    assert len(walls) == 3
    paired = [wall for wall in walls if wall["source"] == "dxf_geometry"]
    singles = [wall for wall in walls if wall["source"] == "dxf_single_line"]
    assert len(paired) == 2
    assert {wall["properties"]["Thickness"] for wall in paired} == {240.0, 120.0}
    assert len(singles) == 1
    assert singles[0]["start"][:2] == [5000.0, 0.0]
    assert singles[0]["end"][:2] == [5000.0, 3000.0]
    assert "Thickness" not in singles[0]["properties"]
    assert len(doors) == 2 and len(windows) == 1
    # 稳定性：element_id 由实体 handle 派生，两次提取一致
    baseline2 = extract_baseline_from_dxf(parsed)
    assert [e["element_id"] for e in baseline] == [e["element_id"] for e in baseline2]


async def test_wall_layer_after_first_hundred_layers_is_recognized(tmp_path):
    import ezdxf
    from backend.engines.dxf_parser import parse_dxf

    doc = ezdxf.new("R2010")
    for index in range(110):
        doc.layers.add(f"DUMMY-{index}")
    doc.layers.add("A-WALL-LATE")
    msp = doc.modelspace()
    msp.add_line((0, 0), (3000, 0), dxfattribs={"layer": "A-WALL-LATE"})
    msp.add_line((0, 200), (3000, 200), dxfattribs={"layer": "A-WALL-LATE"})
    path = tmp_path / "late-wall-layer.dxf"
    doc.saveas(path)

    parsed = await parse_dxf(str(path))
    walls = [element for element in extract_baseline_from_dxf(parsed)
             if element["ifc_type"] == "IFCWALL"]

    assert "A-WALL-LATE" in {layer["name"] for layer in parsed["layers"]}
    assert len(walls) == 1
    assert walls[0]["properties"]["Thickness"] == 200.0


async def test_named_nested_plan_uses_precise_world_coordinates(tmp_path):
    import ezdxf
    from backend.engines.dxf_parser import parse_dxf

    doc = ezdxf.new("R2010")
    unit = doc.blocks.new("WALL_UNIT")
    unit.add_line((0, 0), (2000, 0), dxfattribs={"layer": "A-WALL"})
    unit.add_line((0, 200), (2000, 200), dxfattribs={"layer": "A-WALL"})
    plan = doc.blocks.new("B1层平面图")
    plan.add_blockref("WALL_UNIT", (1000, 2000), dxfattribs={"rotation": 90})
    doc.modelspace().add_blockref(
        "B1层平面图", (10000, 20000), dxfattribs={"rotation": 90})
    path = tmp_path / "nested-plan.dxf"
    doc.saveas(path)

    parsed = await parse_dxf(str(path))
    walls = [element for element in extract_baseline_from_dxf(parsed)
             if element["ifc_type"] == "IFCWALL"]

    assert parsed["wall_extraction"]["mode"] == "precise_placed_plan"
    assert parsed["wall_extraction"]["wall_groups"] == ["A"]
    assert len(walls) == 1
    assert walls[0]["source"] == "dxf_precise_geometry"
    assert walls[0]["properties"]["Thickness"] == 200.0
    assert walls[0]["source_segment_refs"]
    assert walls[0]["source_segment_ids"] == [
        reference["source_segment_id"]
        for reference in walls[0]["source_segment_refs"]]
    assert {tuple(walls[0]["geometry"][key])
            for key in ("center_start", "center_end")} == {
                (6000.0, 20900.0), (8000.0, 20900.0)}


async def test_ambiguous_named_plans_fail_closed(tmp_path):
    import ezdxf
    from backend.engines.dxf_parser import parse_dxf

    doc = ezdxf.new("R2010")
    for name, offset in (("B1层平面图", 0), ("B2层平面图", 10000)):
        plan = doc.blocks.new(name)
        plan.add_line((0, 0), (3000, 0), dxfattribs={"layer": "A-WALL"})
        plan.add_line((0, 200), (3000, 200), dxfattribs={"layer": "A-WALL"})
        doc.modelspace().add_blockref(name, (offset, 0))
    path = tmp_path / "ambiguous-plans.dxf"
    doc.saveas(path)

    parsed = await parse_dxf(str(path))
    walls = [element for element in extract_baseline_from_dxf(parsed)
             if element["ifc_type"] == "IFCWALL"]

    assert parsed["wall_extraction"]["status"] == "BLOCKED"
    assert parsed["wall_extraction"]["mode"] == "blocked_ambiguous_plan"
    assert walls == []


# ── perception 节点（桩 MCP）────────────────────────────────────────────────

@pytest.fixture
def dxf_stub(monkeypatch):
    import types
    stub = types.SimpleNamespace(calls=[], dxf_response={
        "kind": "dxf",
        "baseline": [
            {"element_id": "dxf-w1", "ifc_type": "IFCWALL", "name": "墙1",
             "properties": {"Thickness": 240}, "confidence": 0.7,
             "source": "dxf_geometry", "review_status": "pending"},
        ],
        "elements_count": 1, "layers": ["WALL"],
    })

    async def fake_call(server_url, tool_name, arguments, timeout=None, role=None, auth_token=None):
        stub.calls.append(tool_name)
        if tool_name == "parse_drawing_dxf":
            return [stub.dxf_response]
        if tool_name == "parse_drawing":
            return [{"kind": "pdf", "text": "[墙] 名称: W1; 厚度: 200", "page_count": 1}]
        raise AssertionError(f"unexpected tool {tool_name}")

    monkeypatch.setattr("backend.mcp.client.call_mcp_tool", fake_call)
    return stub


async def test_perception_dxf_channel_success(dxf_stub):
    from backend.agents.drawing2bim.nodes.perception import perception_node
    result = await perception_node({
        "drawing_path": "/tmp/d.dxf", "session_id": "dxf1", "role": "user",
    })
    assert result["drawing_source"] == "drawing_dxf"
    assert len(result["golden_baseline"]) == 1
    assert dxf_stub.calls == ["parse_drawing_dxf"]  # DXF 最高优先，不走其他通道


async def test_perception_dxf_zero_elements_short_circuits(dxf_stub):
    """DXF 零构件 → perception_error 短路，不静默回退假数据"""
    dxf_stub.dxf_response = {"kind": "dxf", "baseline": [], "elements_count": 0, "layers": []}
    from backend.agents.drawing2bim.nodes.perception import perception_node
    result = await perception_node({
        "drawing_path": "/tmp/empty.dxf", "session_id": "dxf2", "role": "user",
    })
    assert result["perception_error"]
    assert "图层规范" in result["perception_error"]
    assert "golden_baseline" not in result


async def test_perception_dwg_short_circuits_with_guidance(dxf_stub):
    """DWG → 明确引导导出 DXF，禁止静默回退"""
    from backend.agents.drawing2bim.nodes.perception import perception_node
    result = await perception_node({
        "drawing_path": "/tmp/d.dwg", "session_id": "dwg1", "role": "user",
    })
    assert result["perception_error"]
    assert "DXF" in result["perception_error"]
    assert dxf_stub.calls == []  # 不调任何 MCP 工具


# ── API 级流程 ──────────────────────────────────────────────────────────────

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
def dxf_e2e_stub(monkeypatch):
    """进程内真实 DXF 解析（不走 HTTP）"""
    from backend.engines.dxf_parser import parse_dxf
    from backend.agents.drawing2bim.nodes.dxf_extractor import extract_baseline_from_dxf

    async def fake_call(server_url, tool_name, arguments, timeout=None, role=None, auth_token=None):
        if tool_name == "parse_drawing_dxf":
            parsed = await parse_dxf(arguments["path"])
            baseline = extract_baseline_from_dxf(parsed)
            return [{"kind": "dxf", "baseline": baseline,
                     "elements_count": len(baseline),
                     "layers": [l["name"] for l in parsed.get("layers", [])]}]
        if tool_name == "generate_ifc_model":
            from backend.engines.ifc_generator import generate_ifc_from_baseline
            res = await generate_ifc_from_baseline(arguments["baseline"], arguments["out_path"])
            return [res]
        if tool_name == "parse_ifc_model":
            import asyncio
            from backend.engines.ifc_parser import _sync_parse
            parsed = await asyncio.to_thread(_sync_parse, arguments["ifc_path"])
            return [parsed]
        raise AssertionError(f"unexpected tool {tool_name}")
    monkeypatch.setattr("backend.mcp.client.call_mcp_tool", fake_call)
    return fake_call


async def test_api_dwg_upload_returns_guidance(api, dxf_e2e_stub):
    """上传 DWG → verdict=error + 导出 DXF 指引（不静默降级）"""
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"drawing_path": "/tmp/plan.dwg", "session_id": "api-dwg"})
    assert r.status_code == 200, r.text
    review_id = r.json()["review_id"]
    j = await _poll_review(api, user, review_id)
    sd = j.get("structured_data") or {}
    assert sd.get("verdict") == "error"
    report = sd.get("compliance_report") or {}
    assert "DXF" in (report.get("error") or "")


async def test_api_synthetic_dxf_review_full_flow(api, dxf_e2e_stub, tmp_path):
    """真实合成 DXF → 矢量提取 → 双轨审查 → HITL 确认 → IFC 生成闭环

    合成图含无厚度单线墙 → 硬轨 HR-001 critical → 触发人工确认（图纸规则，正确行为）
    """
    path = _build_synthetic_dxf(tmp_path)
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"drawing_path": path, "session_id": "api-dxf",
                             "generate_ifc": True})
    assert r.status_code == 200, r.text
    review_id = r.json()["review_id"]
    j = await _poll_review(api, user, review_id)
    sd = j.get("structured_data") or {}
    assert j["status"] == "pending_confirmation"
    assert sd.get("hard_count", 0) >= 1

    admin = await _login(api, "admin")
    confirmed = await api.post(
        f"{BASE}/bim/reviews/{review_id}/confirm", headers=admin,
        json={"decision": "approved", "comment": "单线墙厚度待建模复核"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "done"
    # generate_ifc=True → 人工批准后完成 IFC 生成闭环（真实生成 + 回读比对）
    gen = confirmed.json().get("ifc_generation") or {}
    assert gen.get("status") in ("verified", "diffs_found"), f"IFC 生成闭环: {gen.get('status')}"
