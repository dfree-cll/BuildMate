"""BIM 审图 + 知识待补闭环 回归测试

BIM：ifc_parser 解析示例模型 → 规则检查 → API 上传/轮询（mock LLM 双轨）
知识闭环：低置信度问题 → 审核补充答案 → chunk 入库 → 可被检索 → 队列 resolved
"""
import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from backend.db.session import engine
from backend.main import app
from backend.engines.ifc_parser import parse_ifc
from backend.core.bim_review import run_rule_checks
from backend.db.schema import METADATA

BASE = "/api/v1"
_ROOT = Path(__file__).resolve().parent.parent
DEMO_IFC = _ROOT / "data" / "bim" / "demo_wall.ifc"


# ── 纯函数 / 解析层（无需 app）─────────────────────────────
async def test_ifc_parser_demo_wall():
    """示例模型可解析：IFC4、1 堵墙"""
    parsed = await parse_ifc(str(DEMO_IFC))
    assert parsed["schema"] == "IFC4"
    assert parsed["total_elements"] >= 1
    assert any(e["type"] == "墙" for e in parsed["elements"])


def test_rule_checks_flag_incomplete_model():
    """规则引擎：空模型/无空间/无属性 → 对应问题"""
    issues = run_rule_checks({"schema": "IFC4", "total_elements": 0,
                              "elements": [], "total_spaces": 0, "properties": []})
    priorities = {i["priority"] for i in issues}
    assert "high" in priorities, "空模型应有 high 问题"
    descs = " ".join(i["description"] for i in issues)
    assert "IFCSPACE" in descs and "IFCPROPERTYSET" in descs


def test_rule_checks_pass_complete_model():
    """规则引擎：完备模型 → 无问题"""
    issues = run_rule_checks({
        "schema": "IFC4", "total_elements": 3,
        "elements": [{"type": "墙", "name": "W-1"}, {"type": "柱", "name": "C-1"}, {"type": "梁", "name": "B-1"}],
        "total_spaces": 2, "properties": [{"set": "Pset", "name": "材料", "value": "C30"}],
    })
    assert issues == []


# ── API 层（同款隔离夹具）──────────────────────────────────
@pytest.fixture(autouse=True)
async def _isolated_env():
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    async with app.router.lifespan_context(app):   # 初始化 memory savers（drawing2bim 图需要）
        yield
    await engine.dispose()


@pytest.fixture
async def api():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


async def _login(api, username, password="demo123"):
    r = await api.post(f"{BASE}/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


async def _wait_done(api, headers, review_id, tries=30):
    """新 bim_api 异步轮询：pending → done/pending_confirmation/failed"""
    for _ in range(tries):
        await asyncio.sleep(0.5)
        r = await api.get(f"{BASE}/bim/reviews/{review_id}", headers=headers)
        assert r.status_code == 200, r.text
        j = r.json()
        if j["status"] in ("done", "pending_confirmation", "failed"):
            return j
    pytest.fail("BIM 审查超时未完成")


async def test_bim_upload_review_flow(api):
    """上传示例 IFC → 后台 drawing2bim 管线审查 → done/pending_confirmation → 历史列表"""
    headers = await _login(api, "admin")
    with open(DEMO_IFC, "rb") as f:
        r = await api.post(f"{BASE}/bim/upload", headers=headers,
                           files={"files": ("demo_wall.ifc", f, "application/octet-stream")})
    assert r.status_code == 202, r.text
    review_id = r.json()["reviews"][0]["review_id"]

    result = await _wait_done(api, headers, review_id)
    assert result["status"] in ("done", "pending_confirmation")
    sd = result.get("structured_data") or {}
    assert "verdict" in sd or "risk_level" in sd or "compliance_report" in sd
    assert result["file_name"] == "demo_wall.ifc"

    r = await api.get(f"{BASE}/bim/reviews", headers=headers)
    assert r.status_code == 200 and any(i["review_id"] == review_id for i in r.json()["reviews"])


async def test_bim_upload_rejects_non_ifc(api):
    """不支持的格式（.txt）→ 400；DWG → 400 + 指引（PDF 已支持）"""
    headers = await _login(api, "admin")
    r = await api.post(f"{BASE}/bim/upload", headers=headers,
                       files={"files": ("evil.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    r = await api.post(f"{BASE}/bim/upload", headers=headers,
                       files={"files": ("plan.dwg", b"AC1027fake", "application/octet-stream")})
    assert r.status_code == 400 and "DXF" in r.json()["detail"]


async def test_bim_review_scoped_to_owner(api):
    """B 审查记录仅归属人可查（buyer 查他人记录 → 403/404）"""
    a = await _login(api, "admin")
    with open(DEMO_IFC, "rb") as f:
        r = await api.post(f"{BASE}/bim/upload", headers=a,
                           files={"files": ("demo_wall.ifc", f, "application/octet-stream")})
    rid = r.json()["reviews"][0]["review_id"]
    b = await _login(api, "buyer01")
    resp = await api.get(f"{BASE}/bim/reviews/{rid}", headers=b)
    assert resp.status_code in (403, 404), "他人记录应被拒"


async def test_bim_review_list_is_scoped_to_tenant_and_owner():
    """v1 历史列表不能泄露同租户他人或其他租户的记录。"""
    from backend.api.v1.bim_api import list_bim_reviews

    suffix = uuid.uuid4().hex
    tenant = f"tenant-list-{suffix}"
    other_tenant = f"tenant-other-{suffix}"
    owner = f"user-list-{suffix}"
    review_rows = [
        (f"BIM-LIST-OWN-{suffix}", tenant, owner, "own.ifc", "done"),
        (f"BIM-LIST-PEER-{suffix}", tenant, f"peer-{suffix}", "peer.ifc", "pending"),
        (f"BIM-LIST-CROSS-{suffix}", other_tenant, owner, "cross.ifc", "done"),
    ]
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO bim_reviews "
            "(id, tenant_id, user_id, file_name, status) "
            "VALUES (:id, :tenant_id, :user_id, :file_name, :status)"),
            [{"id": rid, "tenant_id": tid, "user_id": uid,
              "file_name": name, "status": status}
             for rid, tid, uid, name, status in review_rows])

    result = await list_bim_reviews({"tenant_id": tenant, "user_id": owner, "role": "buyer"})
    ids = [item["review_id"] for item in result["reviews"]]
    assert ids == [review_rows[0][0]]
    assert result["items"] == result["reviews"]


async def test_knowledge_pending_answer_loop(api):
    """知识闭环：插一条 pending 问题 → 审核答案入库 → 状态 resolved → chunk 可检索"""
    from backend.core.knowledge_base import search
    pid = str(uuid.uuid4())
    q = "塔吊 QTZ80 的最大起重量是多少？"
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO knowledge_pending_queue (id, tenant_id, user_id, question, confidence, status)"
            " VALUES (:id, 'tenant_default', 'u-buyer01', :q, 0.1, 'pending')"),
            {"id": pid, "q": q})

    t = await _login(api, "reviewer01")
    r = await api.post(f"{BASE}/knowledge/pending/{pid}/answer", headers=t,
                       json={"answer": "QTZ80 塔吊最大起重量 8 吨，臂长 56 米，详见 QTZ80 技术参数表。"})
    assert r.status_code == 200 and r.json()["chunk_added"] is True, r.text

    async with engine.connect() as conn:
        st = (await conn.execute(text(
            "SELECT status FROM knowledge_pending_queue WHERE id = :id"), {"id": pid})).fetchone()[0]
    assert st == "resolved"

    hits = await search("QTZ80 塔吊最大起重量", tenant_id="tenant_default", top_k=3)
    assert hits and any("8 吨" in h["content"] for h in hits), "答案应已可被检索"

    # buyer 无权限补答
    pid2 = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO knowledge_pending_queue (id, tenant_id, user_id, question, confidence, status)"
            " VALUES (:id, 'tenant_default', 'u-buyer01', :q, 0.1, 'pending')"),
            {"id": pid2, "q": "测试问题？"})
    b = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/knowledge/pending/{pid2}/answer", headers=b,
                       json={"answer": "买家不应有权补答"})
    assert r.status_code == 403
