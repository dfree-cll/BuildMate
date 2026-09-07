"""采购审批（HitL）Agent REST 接口"""
import json
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from langgraph.types import Command

from backend.core.orchestrator import AgentType, get_orchestrator
from backend.core.memory import build_config
from backend.dependencies import get_current_user, require_role
from backend.core.logger import get_logger
from sqlalchemy import text
from backend.db.session import engine

router = APIRouter()
logger = get_logger(__name__)


class ProcurementItem(BaseModel):
    material_name: str
    quantity: int = 1
    unit_price: float = 0.0
    unit: str = "吨"          # 计量单位（吨/米/个/立方米/袋…）
    spec: str = ""
    supplier: str = ""


class ProcurementBody(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    # ★ H2 修复：order_no 一律服务端生成。客户端传入的同名字段会被忽略（pydantic 默认丢弃未知字段），
    # 防止用他人单号覆盖 purchase_orders 行（旧 INSERT OR REPLACE 以 order_no 为冲突键）
    material_name: str = "螺纹钢 HRB400"
    quantity: int = 100
    unit_price: float = 3600.0
    session_id: str = Field("default", min_length=1, max_length=128)
    items: Optional[list[ProcurementItem]] = None   # 多品类（技术标/商务标等分开投）


class ProcurementDecision(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected|modify)$", description="审批决定：approved/rejected/modify")
    comment: str = ""
    # 注意：operator 由服务端从当前登录用户获取，客户端不可指定（防伪造审批人）
    new_quantity: int | None = None
    new_unit_price: float | None = None


# ── 采购审批（HitL）──────────────────────────────────────
@router.post("/procurement/orders")
async def create_procurement_order(body: ProcurementBody, current_user: dict = Depends(get_current_user)):
    """创建采购单并启动 AI 双轨审核；大额单会 interrupt 等待人工审批"""
    orchestrator = get_orchestrator()
    graph = orchestrator._get_agent_graph(AgentType.PROCUREMENT)
    order_id = str(uuid.uuid4())
    order_no = "PO-" + str(uuid.uuid4())[:8].upper()   # 服务端生成（H2）
    items = [it.model_dump() for it in body.items] if body.items else None
    if items:
        total = round(sum(i["quantity"] * i["unit_price"] for i in items), 2)
        material_label = "、".join(i["material_name"] for i in items)
    else:
        total = round(body.quantity * body.unit_price, 2)
        material_label = body.material_name
    state = {
        "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
        "session_id": body.session_id, "order_id": order_id, "order_no": order_no,
        "project_id": body.project_id, "memory_turn_id": order_id,
        "material_name": material_label, "quantity": body.quantity,
        "unit_price": body.unit_price, "total_amount": total,
        "items": items,   # 多品类（None 时规则引擎回退单品类字段）
    }
    config = build_config(current_user["user_id"], order_no, tenant_id=current_user["tenant_id"], agent="procurement")
    result = await graph.ainvoke(state, config=config)
    # ★ HitL 修复：interrupt 挂起的订单从未落库 → pending 列表恒空
    # 无论是否需人工审批，先写一条订单记录（pending/approved 状态由 result 决定）
    needs_human = result.get("final_verdict") is None
    try:
        from backend.db.dialect import upsert_purchase_order_sql
        async with engine.begin() as conn:
            await conn.execute(text(upsert_purchase_order_sql()), {
                "id": order_id, "tenant_id": current_user["tenant_id"],
                "user_id": current_user["user_id"], "order_no": order_no,
                "material_name": material_label, "quantity": body.quantity,
                "unit_price": body.unit_price, "total_amount": total,
                "status": "pending" if needs_human else (result.get("final_verdict", "approved")),
                "ai_result": json.dumps(result.get("ai_conclusion", {}), ensure_ascii=False),
                "approved_by": None,
            })
    except Exception as e:
        # 订单记录是后续人工审批（confirm 反查归属/状态）的前提，落库失败必须显式失败而非吞掉，
        # 否则 needs_human 的单会变成"审批人永远查无此单"
        logger.error("procurement.persist_failed", order_no=order_no, error=str(e)[:150])
        raise HTTPException(status_code=500,
                            detail="订单已审核但落库失败，请联系管理员核对后再提交")
    return {
        "order_id": order_id, "order_no": order_no, "total_amount": total,
        "ai_verdict": result.get("ai_verdict", "review"),
        "ai_conclusion": result.get("ai_conclusion", {}),
        "needs_human_approval": needs_human,
        "message": result.get("answer", ""),
    }


@router.post("/procurement/orders/{order_no}/confirm")
async def confirm_procurement(order_no: str, decision: ProcurementDecision,
                              current_user: dict = Depends(require_role("admin", "reviewer"))):
    """人工审批：Command(resume=...) 恢复被 interrupt 的图
    安全：仅 admin/reviewer 可审批；operator 强制取当前用户（客户端不可伪造）
    ★ H1 修复：中断 checkpoint 在【创建者】线程上，thread 归属必须反查订单创建者重建，
    不能用审批人的 user_id（否则 resume 落在空线程，买家下单/管理员审批必挂）"""
    # ① 反查订单归属与状态（purchase_orders 是归属的事实源）
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT user_id, tenant_id, status FROM purchase_orders WHERE order_no = :no"
        ), {"no": order_no})).fetchone()
    if not row or row[1] != current_user["tenant_id"]:
        # 不存在 / 跨租户统一 404，不泄漏他租户单号存在性
        raise HTTPException(status_code=404, detail="采购单不存在")
    owner_user_id, order_status = row[0], row[2]
    if order_status != "pending":
        raise HTTPException(status_code=409, detail=f"该订单已处理（状态：{order_status}），不可重复审批")

    orchestrator = get_orchestrator()
    graph = orchestrator._get_agent_graph(AgentType.PROCUREMENT)
    config = build_config(owner_user_id, order_no, tenant_id=current_user["tenant_id"], agent="procurement")

    # ② checkpoint 预检：线程上确有待恢复的中断（防 DB 与 checkpoint 漂移：中断已消费/丢失时给出明确 409）
    snap = await graph.aget_state(config)
    if snap is None or not snap.next:
        # In-flight orders from before scoped keys must still be reviewable.
        # Never load legacy state unless its saved ownership and order match DB.
        legacy_config = build_config(owner_user_id, order_no)
        legacy = await graph.aget_state(legacy_config)
        values = legacy.values if legacy else {}
        if (legacy and legacy.next and values.get("tenant_id") == current_user["tenant_id"]
                and values.get("user_id") == owner_user_id and values.get("order_no") == order_no):
            config, snap = legacy_config, legacy
    if snap is None or not snap.next:
        raise HTTPException(status_code=409, detail="该订单没有待审批任务（中断已被消费或检查点缺失）")

    # operator 强制取当前登录用户，杜绝客户端伪造审批人（自审自批）
    resume_data = {"decision": decision.decision, "comment": decision.comment,
                   "operator": current_user["user_id"]}
    if decision.new_quantity is not None:
        resume_data["new_quantity"] = decision.new_quantity
    if decision.new_unit_price is not None:
        resume_data["new_unit_price"] = decision.new_unit_price
    result = await graph.ainvoke(
        Command(resume=resume_data),
        config=config,
    )
    return {
        "order_no": order_no,
        "final_verdict": result.get("final_verdict", decision.decision),
        "message": result.get("answer", "审批完成"),
        "structured_output": result.get("structured_output", {}),
    }


# ── 采购审批：列表/待审批──
@router.get("/procurement/pending")
async def procurement_pending(current_user: dict = Depends(require_role("admin", "reviewer"))):
    """待人工审批的采购单列表（仅 admin/reviewer）"""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT order_no, material_name, quantity, unit_price, total_amount, ai_result, created_at "
            "FROM purchase_orders WHERE status = 'pending' AND tenant_id = :t ORDER BY created_at DESC LIMIT 20"
        ), {"t": current_user["tenant_id"]})).fetchall()
    items = []
    for r in rows:
        ai = {}
        try: ai = json.loads(r[5]) if r[5] else {}
        except Exception: pass
        items.append({
            "order_no": r[0], "material_name": r[1], "quantity": r[2],
            "unit_price": float(r[3]) if r[3] else 0, "total_amount": float(r[4]) if r[4] else 0,
            "ai_conclusion": ai, "created_at": str(r[6]) if r[6] else None,
        })
    return {"items": items}


@router.get("/procurement/my-orders")
async def procurement_my_orders(current_user: dict = Depends(get_current_user)):
    """我的采购单列表"""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT order_no, material_name, quantity, total_amount, status, created_at "
            "FROM purchase_orders WHERE user_id = :uid ORDER BY created_at DESC LIMIT 20"
        ), {"uid": current_user["user_id"]})).fetchall()
    return {"items": [
        {"order_no": r[0], "material_name": r[1], "quantity": r[2],
         "total_amount": float(r[3]) if r[3] else 0, "status": r[4],
         "created_at": str(r[5]) if r[5] else None}
        for r in rows
    ]}
