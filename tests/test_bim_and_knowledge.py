"""BIM 审图 + 知识待补闭环 回归测试

BIM：ifc_parser 解析示例模型 → 规则检查 → API 上传/轮询（mock LLM 双轨）
知识闭环：低置信度问题 → 教师补充答案 → chunk 入库 → 可被检索 → 队列 resolved
"""
import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from backend.db.session import engine
from backend.main import app
from backend.core.ifc_parser import parse_ifc
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


async def _wait_done(api, headers, review_id, tries=20):
    for _ in range(tries):
        await asyncio.sleep(0.5)
        r = await api.get(f"{BASE}/bim/reviews/{review_id}", headers=headers)
        assert r.status_code == 200, r.text
        j = r.json()
        if j["status"] != "processing":
            return j
    pytest.fail("BIM 审查超时未完成")


async def test_bim_upload_review_flow(api):
    """上传示例 IFC → 后台双轨审查 → done（mock LLM）→ 历史列表"""
    headers = await _login(api, "admin")
    with open(DEMO_IFC, "rb") as f:
        r = await api.post(f"{BASE}/bim/upload", headers=headers,
                           files={"file": ("demo_wall.ifc", f, "application/octet-stream")})
    assert r.status_code == 202, r.text
    j = r.json()
    assert j["status"] == "processing" and j["size"] > 0   # 解析已后台化，schema 见轮询结果

    result = await _wait_done(api, headers, j["review_id"], tries=40)
    assert result["status"] == "done"
    assert result["file"]["schema"] == "IFC4"
    assert result["file"]["total_elements"] >= 1
    # 示例模型只有墙（无空间/无建筑）→ 规则应发现 medium 问题；LLM 为 mock 分支
    priorities = {i["priority"] for i in result["rule_issues"]}
    assert "medium" in priorities
    assert result["observations"], "LLM（mock）观察不应为空"
    assert result["risk_level"] in ("low", "medium", "high")

    r = await api.get(f"{BASE}/bim/reviews", headers=headers)
    assert r.status_code == 200 and any(i["review_id"] == j["review_id"] for i in r.json()["items"])


async def test_bim_upload_rejects_non_ifc(api):
    headers = await _login(api, "admin")
    r = await api.post(f"{BASE}/bim/upload", headers=headers,
                       files={"file": ("evil.pdf", b"%PDF-fake", "application/pdf")})
    assert r.status_code == 400
    r = await api.post(f"{BASE}/bim/upload", headers=headers,
                       files={"file": ("fake.ifc", b"not a step file", "text/plain")})
    assert r.status_code == 400, "缺 ISO-10303-21 头应被拒"


async def test_bim_review_scoped_to_owner(api):
    """B 审查记录仅归属人可查"""
    a = await _login(api, "admin")
    with open(DEMO_IFC, "rb") as f:
        r = await api.post(f"{BASE}/bim/upload", headers=a,
                           files={"file": ("demo_wall.ifc", f, "application/octet-stream")})
    rid = r.json()["review_id"]
    b = await _login(api, "buyer01")
    assert (await api.get(f"{BASE}/bim/reviews/{rid}", headers=b)).status_code == 404


async def test_knowledge_pending_answer_loop(api):
    """知识闭环：插一条 pending 问题 → 教师答案入库 → 状态 resolved → chunk 可检索"""
    from backend.core.knowledge_base import search
    pid = str(uuid.uuid4())
    q = "塔吊 QTZ80 的最大起重量是多少？"
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO knowledge_pending_queue (id, tenant_id, user_id, question, confidence, status)"
            " VALUES (:id, 'tenant_default', 'u-buyer01', :q, 0.1, 'pending')"),
            {"id": pid, "q": q})

    t = await _login(api, "teacher01")
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
