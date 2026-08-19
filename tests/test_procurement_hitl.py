"""采购审批 HitL 跨用户审批回归测试（H1 修复验收）

H1 根因：创建订单 thread 用【下单人】user_id，confirm 用【审批人】user_id，
        两个线程 → Command(resume) 落在审批人空线程上必挂。
        冒烟测试此前用同一 admin 账户下单+审批，掩盖了该 bug。

覆盖：
1. 主路径：买家下单 interrupt → 管理员跨用户审批 → 200 + DB 落库断言（归属/审批人/留痕）
2. 重复审批 → 409
3. DB 与 checkpoint 漂移（状态改回 pending 但中断已消费）→ 409
4. 不存在单号 → 404；跨租户同 404
5. 买家（非 admin）审批 → 403（角色控制回归）
6. modify 分支跨用户：管理员改单 → 金额重算并视为批准
"""
import httpx
import pytest
from sqlalchemy import text

from backend.main import app
from backend.db.session import engine
from backend.db.schema import METADATA

BASE = "/api/v1"
BIG_ORDER = {"material_name": "螺纹钢 HRB400", "quantity": 500, "unit_price": 3600.0}


@pytest.fixture(autouse=True)
async def _isolated_env():
    """每用例独立：建表（幂等）→ lifespan（saver/迁移）→ 用后释放跨事件循环的全局单例"""
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    async with app.router.lifespan_context(app):
        yield
    # pytest-asyncio 每个用例新事件循环：engine 连接池 / checkpoint saver / orchestrator
    # 图编译都绑定旧 loop，必须重置，否则第二个用例报 "attached to a different loop"
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


async def _create_big_order(api, buyer_headers, session):
    r = await api.post(f"{BASE}/procurement/orders", headers=buyer_headers,
                       json={**BIG_ORDER, "session_id": session})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["needs_human_approval"] is True, f"大额单应进入人工审批: {j}"
    return j


async def test_hitl_buyer_create_admin_confirm(api):
    """主路径：买家下单 interrupt → 管理员跨用户审批（修复前此处 500）"""
    buyer = await _login(api, "buyer01")
    j = await _create_big_order(api, buyer, "hitl-main")

    admin = await _login(api, "admin")
    r = await api.post(f"{BASE}/procurement/orders/{j['order_no']}/confirm", headers=admin,
                       json={"decision": "approved", "comment": "同意采购"})
    assert r.status_code == 200, r.text
    assert r.json()["final_verdict"] == "approved"

    # DB 断言：状态、审批人=管理员、归属仍是买家（未被覆盖）、审批留痕 operator=管理员
    async with engine.connect() as conn:
        po = (await conn.execute(text(
            "SELECT status, approved_by, user_id FROM purchase_orders WHERE order_no = :n"
        ), {"n": j["order_no"]})).fetchone()
        rec = (await conn.execute(text(
            "SELECT action, operator FROM approval_records WHERE order_id = :i"
        ), {"i": j["order_id"]})).fetchone()
    assert po, "订单应已落库"
    assert po[0] == "approved"
    assert po[1] == "u-admin"
    assert po[2] == "u-buyer01"
    assert rec and rec[0] == "approved" and rec[1] == "u-admin"


async def test_confirm_twice_returns_409(api):
    """重复审批同一单 → 409（状态已非 pending）"""
    buyer = await _login(api, "buyer01")
    j = await _create_big_order(api, buyer, "hitl-twice")
    admin = await _login(api, "admin")
    r = await api.post(f"{BASE}/procurement/orders/{j['order_no']}/confirm", headers=admin,
                       json={"decision": "approved"})
    assert r.status_code == 200, r.text
    r = await api.post(f"{BASE}/procurement/orders/{j['order_no']}/confirm", headers=admin,
                       json={"decision": "approved"})
    assert r.status_code == 409, r.text


async def test_confirm_with_drifted_status_returns_409(api):
    """DB 与 checkpoint 漂移：状态被手工改回 pending 但中断已消费 → 明确 409（而非神秘 500）"""
    buyer = await _login(api, "buyer01")
    j = await _create_big_order(api, buyer, "hitl-drift")
    admin = await _login(api, "admin")
    r = await api.post(f"{BASE}/procurement/orders/{j['order_no']}/confirm", headers=admin,
                       json={"decision": "approved"})
    assert r.status_code == 200, r.text
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE purchase_orders SET status = 'pending' WHERE order_no = :n"
        ), {"n": j["order_no"]})
    r = await api.post(f"{BASE}/procurement/orders/{j['order_no']}/confirm", headers=admin,
                       json={"decision": "approved"})
    assert r.status_code == 409, r.text


async def test_confirm_unknown_order_returns_404(api):
    admin = await _login(api, "admin")
    r = await api.post(f"{BASE}/procurement/orders/PO-NOTEXIST/confirm", headers=admin,
                       json={"decision": "approved"})
    assert r.status_code == 404, r.text


async def test_confirm_forbidden_for_buyer(api):
    """买家无审批权 → 403（角色控制回归）"""
    buyer = await _login(api, "buyer01")
    r = await api.post(f"{BASE}/procurement/orders/PO-ANY/confirm", headers=buyer,
                       json={"decision": "approved"})
    assert r.status_code == 403, r.text


async def test_hitl_modify_branch_cross_user(api):
    """modify 分支跨用户：管理员改单（数量）→ 金额重算并视为批准"""
    buyer = await _login(api, "buyer01")
    j = await _create_big_order(api, buyer, "hitl-modify")
    admin = await _login(api, "admin")
    r = await api.post(f"{BASE}/procurement/orders/{j['order_no']}/confirm", headers=admin,
                       json={"decision": "modify", "comment": "减量采购", "new_quantity": 100})
    assert r.status_code == 200, r.text
    assert r.json()["final_verdict"] == "approved"
    async with engine.connect() as conn:
        po = (await conn.execute(text(
            "SELECT quantity, total_amount FROM purchase_orders WHERE order_no = :n"
        ), {"n": j["order_no"]})).fetchone()
    assert po[0] == 100
    assert float(po[1]) == 100 * 3600.0
