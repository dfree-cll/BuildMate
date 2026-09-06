"""drawing2bim 第一期 API 级流程测试（HITL interrupt/resume）

覆盖：
1. 主路径：提交含违规基准 → interrupt → admin 确认 → verdict approved
2. 驳回分支：confirm rejected → verdict rejected
3. 全合规基准 → 直接 done（零人工介入）
4. 空输入 → 400
5. 非审批角色确认 → 403
6. 对不存在的 review_id 确认 → 404
"""
import asyncio
import httpx
import pytest

from backend.main import app
from backend.db.session import engine
from backend.db.schema import METADATA

BASE = "/api/v1"

# 含违规基准：墙体缺厚度（HR-001 critical）
BAD_BASELINE = [
    {"element_id": "w1", "ifc_type": "IFCWALL", "name": "外墙-W1",
     "properties": {"Material": "烧结多孔砖"}},
]

# 全合规基准：硬轨零违规，软轨 Mock 模式下返回空
GOOD_BASELINE = [
    {"element_id": "w1", "ifc_type": "IFCWALL", "name": "外墙-W1",
     "properties": {"Thickness": 240, "Material": "烧结多孔砖"}},
    {"element_id": "s1", "ifc_type": "IFCSLAB", "name": "楼板-B1",
     "properties": {"Thickness": 120, "Elevation": 3.0, "Material": "C30"}},
]


@pytest.fixture(autouse=True)
async def _isolated_env():
    """每用例独立：建表 → lifespan（saver 初始化）→ 用后释放跨事件循环单例"""
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


async def _login(api, username, password="demo123"):
    r = await api.post(f"{BASE}/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


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


async def test_hitl_submit_then_admin_confirms(api):
    """主路径：含违规基准 → 需人工确认 → admin 批准 → verdict approved"""
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"golden_baseline": BAD_BASELINE})
    assert r.status_code == 200, r.text
    review_id = r.json()["review_id"]

    j = await _poll_review(api, user, review_id)
    assert j["status"] == "pending_confirmation"
    sd = j["structured_data"] or {}
    assert sd["risk_level"] == "high"
    assert sd["hard_count"] >= 1
    review_id = j["review_id"]

    admin = await _login(api, "admin")
    r = await api.post(f"{BASE}/bim/reviews/{review_id}/confirm", headers=admin,
                       json={"decision": "approved", "comment": "已知悉，允许放行"})
    assert r.status_code == 200, r.text
    j2 = r.json()
    assert j2["status"] == "done"
    assert j2["verdict"] == "approved"
    assert j2["compliance_report"]["human_decision"] == "approved"


async def test_hitl_reject_gives_rejected_verdict(api):
    """驳回分支：confirm rejected → verdict rejected"""
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"golden_baseline": BAD_BASELINE})
    review_id = r.json()["review_id"]
    await _poll_review(api, user, review_id)

    admin = await _login(api, "admin")
    r = await api.post(f"{BASE}/bim/reviews/{review_id}/confirm", headers=admin,
                       json={"decision": "rejected", "comment": "墙体厚度必须补全"})
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "rejected"


async def test_clean_baseline_auto_passes(api):
    """全合规基准 → 直接 done，零人工介入"""
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user,
                       json={"golden_baseline": GOOD_BASELINE})
    assert r.status_code == 200, r.text
    review_id = r.json()["review_id"]

    j = await _poll_review(api, user, review_id)
    assert j["status"] == "done"
    sd = j["structured_data"] or {}
    assert sd["verdict"] == "pass"
    assert sd.get("structured_output", {}).get("elements_needs_fix") == 0


async def test_empty_input_returns_400(api):
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/review", headers=user, json={})
    assert r.status_code == 400, r.text


async def test_confirm_forbidden_for_non_reviewer(api):
    """buyer01 无确认权 → 403"""
    user = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/bim/reviews/BIM-ANY/confirm", headers=user,
                       json={"decision": "approved"})
    assert r.status_code == 403, r.text


async def test_confirm_unknown_review_returns_404(api):
    """不存在的 review_id → checkpoint 无中断 → 404"""
    admin = await _login(api, "admin")
    r = await api.post(f"{BASE}/bim/reviews/BIM-NOTEXIST/confirm", headers=admin,
                       json={"decision": "approved"})
    assert r.status_code == 404, r.text


# ── 文件上传接口（前端测试入口）──────────────────────────────────────────────

def _make_dxf_bytes() -> bytes:
    """ezdxf 生成合成 DXF 字节流（墙层双线 + 门块；ezdxf 只能写路径，经临时文件中转）

    含 1 条无厚度单线墙（WALL 层单线 → 提取厚度 None → HR-001 图纸规则 → HITL）
    """
    import os
    import tempfile
    import ezdxf
    doc = ezdxf.new("R2010")
    doc.layers.add("WALL-墙体", color=1)
    doc.layers.add("DOOR-门窗", color=3)
    msp = doc.modelspace()
    msp.add_line((0, 0), (3600, 0), dxfattribs={"layer": "WALL-墙体"})
    msp.add_line((0, 240), (3600, 240), dxfattribs={"layer": "WALL-墙体"})
    msp.add_line((5000, 0), (5000, 3000), dxfattribs={"layer": "WALL-墙体"})  # 单线 → 无厚度
    if "M1" not in doc.blocks:
        doc.blocks.new("M1")
    msp.add_blockref("M1", (1000, 0), dxfattribs={"layer": "DOOR-门窗"})
    fd, tmp = tempfile.mkstemp(suffix=".dxf")
    os.close(fd)
    try:
        doc.saveas(tmp)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.remove(tmp)


async def test_upload_dxf_triggers_full_review(api, monkeypatch):
    """IFC 上传（墙缺材料）→ 感知 → 双轨审查 → HITL（HR-004 仅 IFC 源启用）"""
    import io

    def _make_ifc_bytes() -> bytes:
        import ifcopenshell
        f = ifcopenshell.file(schema="IFC4")
        f.createIfcWall("w-no-mat")
        return f.to_string().encode("utf-8")

    async def fake_call(server_url, tool_name, arguments, timeout=None, role=None, auth_token=None):
        if tool_name == "parse_ifc_model":
            return [{"elements": [{"global_id": "w-no-mat", "ifc_type": "IFCWALL",
                                   "name": "外墙-W1",
                                   "Psets": {}, "Quantities": {}, "Materials": []}]}]
        raise AssertionError(f"unexpected tool {tool_name}")
    monkeypatch.setattr("backend.mcp.client.call_mcp_tool", fake_call)

    user = await _login(api, "buyer01")
    files = {"files": ("wall_no_mat.ifc", _make_ifc_bytes(), "application/octet-stream")}
    r = await api.post(f"{BASE}/bim/upload", headers=user,
                       files=files, data={"generate_ifc": "false"})
    assert r.status_code == 202, r.text
    review_id = r.json()["reviews"][0]["review_id"]
    j = await _poll_review(api, user, review_id)
    assert j["status"] == "pending_confirmation"   # IFC 墙缺材料 → HR-004（ifc 源）→ HITL
    assert (j["structured_data"] or {}).get("hard_count", 0) >= 1


async def test_upload_dwg_rejected_with_guidance(api):
    """DWG 上传 → 400 + 导出 DXF 指引"""
    user = await _login(api, "buyer01")
    files = {"files": ("plan.dwg", b"AC1027fake", "application/octet-stream")}
    r = await api.post(f"{BASE}/bim/upload", headers=user, files=files)
    assert r.status_code == 400, r.text
    assert "DXF" in r.json()["detail"]


async def test_upload_pdf_and_dxf_require_v2_wall_pipeline(api):
    """Wall sources never enter the legacy Agent through the v1 upload route."""

    user = await _login(api, "buyer01")
    for filename, content_type, content in (
        ("plan.pdf", "application/pdf", b"%PDF-1.7"),
        ("plan.dxf", "application/dxf", b"0\nSECTION\n0\nENDSEC\n0\nEOF\n"),
    ):
        response = await api.post(
            f"{BASE}/bim/upload",
            headers=user,
            files={"files": (filename, content, content_type)},
        )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "/api/v2/artifacts" in detail
        assert "/api/v2/workflows" in detail


async def test_upload_unsupported_format_rejected(api):
    user = await _login(api, "buyer01")
    files = {"files": ("plan.txt", b"hello", "text/plain")}
    r = await api.post(f"{BASE}/bim/upload", headers=user, files=files)
    assert r.status_code == 400, r.text
