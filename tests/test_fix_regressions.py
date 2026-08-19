"""批量修复回归测试（H2/H3/H4/H6/M6/M7 + upsert 语义）

覆盖：
- H3：谈判总结报告真实换行（不再出现字面量 \n）
- H2：客户端传入 order_no 被忽略（服务端生成），不可伪造他人单号覆盖
- H2：purchase_orders upsert 改 ON CONFLICT 后保留 id/created_at（旧 INSERT OR REPLACE 会重置）
- H4：teacher01 角色可访问待审批列表（角色此前名存实亡），buyer 仍 403
- M6：路由 JSON 解析容错 code fence
- M7：前置拦截不再误伤"你是谁家的供应商"类长句，仍拦截纯身份/能力问句
- H6：PyJWT 签发/校验（所有 API 用例的登录 token 隐式覆盖）
"""
import httpx
import pytest
from langchain_core.messages import HumanMessage, AIMessage
from sqlalchemy import text

from backend.api.v1.unified_chat import _pre_filter, _parse_route_json
from backend.agents.negotiation.nodes import done_node
from backend.db.session import engine
from backend.db.dialect import upsert_purchase_order_sql
from backend.main import app
from backend.db.schema import METADATA

BASE = "/api/v1"
SMALL_ORDER = {"material_name": "砂石", "quantity": 1, "unit_price": 100.0}


# ── 纯函数用例（无需 app/lifespan）──────────────────────────────
def test_pre_filter_no_false_positive():
    """M7：长句中的身份/能力字样不再触发模板拦截"""
    assert _pre_filter("你是谁家的供应商？") is None
    assert _pre_filter("你能帮我审查投标文件吗") is None


def test_pre_filter_still_catches_short_phrases():
    assert _pre_filter("你是谁") is not None
    assert _pre_filter("你好") is not None
    assert _pre_filter("你能做什么") is not None
    assert _pre_filter("介绍一下你自己") is not None


def test_parse_route_json_strips_fence():
    """M6：LLM 返回 markdown fence 包裹的 JSON 也能解析"""
    assert _parse_route_json('```json\n{"label": "qa", "reason": "r"}\n```') == {"label": "qa", "reason": "r"}
    assert _parse_route_json('好的，结果是 {"label":"bid_review"} 以上') == {"label": "bid_review"}
    assert _parse_route_json("完全不是 JSON") is None


async def test_negotiation_report_real_newlines():
    """H3：总结报告用真实换行渲染，无字面量反斜杠 n"""
    st = await done_node({
        "quotes": [{"stage": "quote", "summary": "报价 100", "rounds": 2}],
        "messages": [HumanMessage(content="开始谈判"), AIMessage(content="报价 100 元/吨")],
        "material": "螺纹钢 HRB400",
    })
    assert "\\n" not in st["answer"], f"不应出现字面量 \\n：{st['answer'][:100]}"
    assert "\n" in st["answer"], "应有真实换行"


# ── API 用例（与 test_procurement_hitl 相同的隔离夹具）────────────
@pytest.fixture(autouse=True)
async def _isolated_env():
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


async def test_client_order_no_is_ignored(api):
    """H2：客户端伪造 order_no 无效，两次提交产生两个独立订单，无覆盖"""
    buyer = await _login(api, "buyer01")
    seen = set()
    for i in range(2):
        r = await api.post(f"{BASE}/procurement/orders", headers=buyer,
                           json={"order_no": "PO-EVIL", **SMALL_ORDER, "session_id": f"evil-{i}"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["order_no"] != "PO-EVIL", "服务端必须自行生成 order_no"
        seen.add(j["order_no"])
    assert len(seen) == 2, "两笔订单单号应不同"
    async with engine.connect() as conn:
        n = (await conn.execute(text(
            "SELECT COUNT(*) FROM purchase_orders WHERE order_no = 'PO-EVIL'"
        ))).scalar()
    assert n == 0, "伪造单号不应落库"


async def test_purchase_upsert_preserves_id_and_created_at():
    """H2：SQLite upsert 改 ON CONFLICT DO UPDATE——id/created_at 保留（旧 INSERT OR REPLACE 会重置）"""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM purchase_orders WHERE order_no = 'PO-UPSERT'"))
        await conn.execute(text(
            "INSERT INTO purchase_orders (id, tenant_id, user_id, order_no, material_name,"
            " quantity, unit_price, total_amount, status, created_at)"
            " VALUES ('t-upsert-1', 'tenant_default', 'u-buyer01', 'PO-UPSERT', '砂石',"
            " 1, 100, 100, 'pending', '2026-01-01 00:00:00')"
        ))
    async with engine.begin() as conn:
        await conn.execute(text(upsert_purchase_order_sql()), {
            "id": "t-upsert-2", "tenant_id": "tenant_default", "user_id": "u-buyer01",
            "order_no": "PO-UPSERT", "material_name": "砂石", "quantity": 2,
            "unit_price": 100, "total_amount": 200, "status": "approved",
            "ai_result": "{}", "approved_by": "u-admin",
        })
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT id, status, quantity, created_at FROM purchase_orders WHERE order_no = 'PO-UPSERT'"
        ))).fetchone()
    assert row[0] == "t-upsert-1", "冲突更新应保留原 id"
    assert row[1] == "approved" and row[2] == 2, "可变列应已更新"
    assert str(row[3]).startswith("2026-01-01"), "created_at 不应被重置"


async def test_teacher_role_can_approve_view(api):
    """H4：teacher01 可访问待审批列表（此前 teacher 角色名存实亡），buyer 仍 403"""
    t = await _login(api, "teacher01")
    r = await api.get(f"{BASE}/procurement/pending", headers=t)
    assert r.status_code == 200, r.text
    b = await _login(api, "buyer01")
    r = await api.get(f"{BASE}/procurement/pending", headers=b)
    assert r.status_code == 403
