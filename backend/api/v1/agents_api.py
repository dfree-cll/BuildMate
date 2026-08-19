"""各 Agent 的 REST 接口（对标 EduAgent 4.8/5.16/6.12/7.12）"""
import asyncio
import json
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend.core.orchestrator import AgentType, ExecutionMode, AgentRequest, get_orchestrator
from backend.core.memory import build_config
from backend.dependencies import get_current_user, require_role, llm_rate_limit
from backend.core.logger import get_logger
from sqlalchemy import text
from backend.db.session import engine

router = APIRouter()
logger = get_logger(__name__)


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str = "default"


class BidReviewBody(BaseModel):
    doc_text: str = Field("", description="投标文件文本（留空则用 mock 文档）")
    session_id: str = "default"


class ProcurementBody(BaseModel):
    # ★ H2 修复：order_no 一律服务端生成。客户端传入的同名字段会被忽略（pydantic 默认丢弃未知字段），
    # 防止用他人单号覆盖 purchase_orders 行（旧 INSERT OR REPLACE 以 order_no 为冲突键）
    material_name: str = "螺纹钢 HRB400"
    quantity: int = 100
    unit_price: float = 3600.0
    session_id: str = "default"


class ProcurementDecision(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected|modify)$", description="审批决定：approved/rejected/modify")
    comment: str = ""
    # 注意：operator 由服务端从当前登录用户获取，客户端不可指定（防伪造审批人）
    new_quantity: int | None = None
    new_unit_price: float | None = None


class NegotiationBody(BaseModel):
    message: str = Field(..., min_length=1)
    material: str = "螺纹钢 HRB400"
    session_id: str = "default"
    reset: bool = False


# ── 投标审查 ──────────────────────────────────────────────
# ── 投标审查（后台任务 + 轮询，对标 EduAgent 4.8/4.9 简历审查范式）──
_bid_tasks: set = set()          # 后台任务 GC 保护


def _get_bid_graph():
    """投标图复用 orchestrator 的懒加载缓存（删掉本模块的重复缓存层）"""
    return get_orchestrator()._get_agent_graph(AgentType.BID_REVIEW)


async def _persist_bid(review_id: str, user_id: str, tenant_id: str, status: str, **fields):
    """写/更新 bid_reviews 表（JSONB 用 json.dumps ensure_ascii=False）"""
    async with engine.begin() as conn:
        # upsert
        await conn.execute(text("""
            INSERT INTO bid_reviews (id, tenant_id, user_id, doc_name, structured_data, scores, issues, summary, status, error_msg, created_at, updated_at)
            VALUES (:id, :tenant_id, :user_id, :doc_name, :structured_data, :scores, :issues, :summary, :status, :error_msg, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
              status=excluded.status, scores=excluded.scores, issues=excluded.issues,
              summary=excluded.summary, error_msg=excluded.error_msg, updated_at=CURRENT_TIMESTAMP
        """), {
            "id": review_id, "tenant_id": tenant_id, "user_id": user_id,
            "doc_name": fields.get("doc_name", "投标文件"),
            "structured_data": fields.get("structured_data"),
            "scores": json.dumps(fields.get("scores", {}), ensure_ascii=False) if fields.get("scores") else None,
            "issues": json.dumps(fields.get("issues", []), ensure_ascii=False) if fields.get("issues") else None,
            "summary": json.dumps(fields.get("summary", {}), ensure_ascii=False) if fields.get("summary") else None,
            "status": status, "error_msg": fields.get("error_msg"),
        })


def _on_bid_task_done(task):
    _bid_tasks.discard(task)
    try:
        if not task.cancelled() and task.exception() is not None:
            # 后台失败 → 标记 failed（回写真实 user_id/tenant_id，保证查询端可见）
            exc = task.exception()
            meta = getattr(task, "_bid_meta", {})
            asyncio.ensure_future(_persist_bid(
                meta.get("review_id", task.get_name()),
                meta.get("user_id", ""), meta.get("tenant_id", ""),
                "failed", error_msg=str(exc)[:500]))
    except Exception:
        pass


@router.post("/bid-review/upload", status_code=202, dependencies=[Depends(llm_rate_limit)])
async def bid_review_upload(file: UploadFile, current_user: dict = Depends(get_current_user)):
    """上传投标 PDF → 解析文本 → 后台四维评审（对齐 EduAgent 4.8：PDF 上传 + 202）"""
    import tempfile
    import os
    MAX_UPLOAD = 20 * 1024 * 1024
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 格式")
    # ★ H5 修复：分块读取边读边限额（旧代码先整体 read 再查大小，超大文件会先全量进内存）
    chunks: list[bytes] = []
    size = 0
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        size += len(block)
        if size > MAX_UPLOAD:
            raise HTTPException(status_code=413, detail="文件过大（>20MB）")
        chunks.append(block)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(status_code=400, detail="空文件")
    # ★ 魔数校验：PDF 以 %PDF- 开头（防伪装 .pdf 的其他文件）
    if not content[:5].startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="文件不是有效的 PDF（魔数校验失败）")

    review_id = str(uuid.uuid4())
    # 临时文件由 tempfile 随机命名（路径不含任何用户输入，无拼接）
    with tempfile.NamedTemporaryFile(delete=False, suffix="_bid.pdf") as tf:
        tf.write(content)
        tmp_path = tf.name

    # 解析 PDF（★ M5：解析失败立即清理临时文件，不留残骸）
    from backend.services.pdf_parser import extract_pdf_text
    try:
        parsed = await extract_pdf_text(tmp_path)
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise HTTPException(status_code=422, detail=f"PDF 解析失败：{str(e)[:200]}")
    doc_text = parsed["raw_text"]

    # 后台评审
    graph = _get_bid_graph()
    state = {
        "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
        "session_id": "bid-" + str(uuid.uuid4())[:8], "review_id": review_id,
        "doc_text": doc_text, "original_query": file.filename,
    }
    await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"], "processing",
                       doc_name=file.filename)

    async def _run():
        try:
            result = await graph.ainvoke(state)
            await _persist_bid(
                review_id, current_user["user_id"], current_user["tenant_id"], "done",
                doc_name=file.filename,
                scores={"weighted_score": result.get("weighted_score", 0),
                        "dimensions": result.get("dimension_scores", [])},
                issues=result.get("issues", []),
                summary=result.get("summary", {}),
            )
        finally:
            # 无论成败都清理临时 PDF（防泄漏）
            try: os.remove(tmp_path)
            except Exception: pass

    task = asyncio.create_task(_run(), name=review_id)
    task._bid_meta = {"review_id": review_id, "user_id": current_user["user_id"],
                      "tenant_id": current_user["tenant_id"]}
    _bid_tasks.add(task)
    task.add_done_callback(_on_bid_task_done)

    return {"review_id": review_id, "status": "processing", "pages": parsed["page_count"],
            "chars": len(doc_text), "message": "PDF 已上传并解析，正在四维评审中。"}


@router.post("/bid-review/review", status_code=202, dependencies=[Depends(llm_rate_limit)])
async def bid_review(body: BidReviewBody, current_user: dict = Depends(get_current_user)):
    """提交投标文件 → 后台四维并行评审 → 返回 review_id 供轮询（对标 EduAgent 4.8）"""
    review_id = str(uuid.uuid4())
    graph = _get_bid_graph()
    state = {
        "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
        "session_id": body.session_id, "review_id": review_id,
        "doc_text": body.doc_text, "original_query": body.doc_text[:200] or "投标文件审查",
    }
    await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"], "processing")

    async def _run():
        result = await graph.ainvoke(state)
        await _persist_bid(
            review_id, current_user["user_id"], current_user["tenant_id"], "done",
            doc_name=state["original_query"][:100],
            scores={"weighted_score": result.get("weighted_score", 0),
                    "dimensions": result.get("dimension_scores", [])},
            issues=result.get("issues", []),
            summary=result.get("summary", {}),
        )
        return result

    task = asyncio.create_task(_run(), name=review_id)
    _bid_tasks.add(task)
    task.add_done_callback(_on_bid_task_done)

    return {"review_id": review_id, "status": "processing",
            "message": "投标文件已提交，正在四维并行评审中，预计 30-60 秒完成。"}


@router.get("/bid-review/reviews/{review_id}")
async def get_bid_review(review_id: str, current_user: dict = Depends(get_current_user)):
    """轮询评审结果（状态机 processing→done→failed→404；15 分钟超时兜底，对标 EduAgent 4.9）"""
    REVIEW_TIMEOUT_SECONDS = 15 * 60
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT status, scores, issues, summary, error_msg, created_at FROM bid_reviews WHERE id = :id AND user_id = :uid"
        ), {"id": review_id, "uid": current_user["user_id"]})).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="评审记录不存在")

    status = row[0]
    # 超时兜底：processing 且超过 15 分钟 → 标记 failed（created 可能是 str，用字符串比较）
    if status == "processing":
        created = row[5]
        from datetime import datetime, timezone
        if created:
            try:
                created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                # SQLite 返回 naive datetime → 补时区为 UTC（否则与 aware now 相减 TypeError）
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - created_dt).total_seconds() > REVIEW_TIMEOUT_SECONDS:
                    await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"], "failed", error_msg="评审超时")
                    status = "failed"
            except Exception:
                pass

    if status == "processing":
        return {"review_id": review_id, "status": "processing", "message": "评审进行中，请稍后重试"}
    if status == "failed":
        return {"review_id": review_id, "status": "failed", "error": row[4] or "评审失败"}
    # done
    def _json_loads(v):
        if not v: return None
        try: return json.loads(v)
        except Exception: return v
    return {
        "review_id": review_id, "status": "done",
        "weighted_score": (_json_loads(row[1]) or {}).get("weighted_score", 0) if row[1] else 0,
        "dimensions": (_json_loads(row[1]) or {}).get("dimensions", []) if row[1] else [],
        "issues": _json_loads(row[2]) or [],
        "summary": _json_loads(row[3]) or {},
    }


@router.get("/bid-review/reviews")
async def list_bid_reviews(current_user: dict = Depends(get_current_user)):
    """本人评审列表（倒序，对齐 EduAgent 4.9）"""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, doc_name, status, created_at FROM bid_reviews WHERE user_id = :uid ORDER BY created_at DESC LIMIT 20"
        ), {"uid": current_user["user_id"]})).fetchall()
    return {"items": [
        {"review_id": r[0], "doc_name": r[1], "status": r[2], "created_at": str(r[3]) if r[3] else None}
        for r in rows
    ]}


# ── 采购审批（HitL）──────────────────────────────────────
@router.post("/procurement/orders")
async def create_procurement_order(body: ProcurementBody, current_user: dict = Depends(get_current_user)):
    """创建采购单并启动 AI 双轨审核；大额单会 interrupt 等待人工审批"""
    orchestrator = get_orchestrator()
    graph = orchestrator._get_agent_graph(AgentType.PROCUREMENT)
    order_id = str(uuid.uuid4())
    order_no = "PO-" + str(uuid.uuid4())[:8].upper()   # 服务端生成（H2）
    total = round(body.quantity * body.unit_price, 2)
    state = {
        "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
        "session_id": body.session_id, "order_id": order_id, "order_no": order_no,
        "material_name": body.material_name, "quantity": body.quantity,
        "unit_price": body.unit_price, "total_amount": total,
    }
    config = build_config(current_user["user_id"], order_no)  # 用 order_no 作 thread key，保证 confirm 能 resume
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
                "material_name": body.material_name, "quantity": body.quantity,
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
                              current_user: dict = Depends(require_role("admin", "teacher"))):
    """人工审批：Command(resume=...) 恢复被 interrupt 的图（对标 EduAgent 6.9/6.12）
    安全：仅 admin/teacher 可审批；operator 强制取当前用户（客户端不可伪造）
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
    config = build_config(owner_user_id, order_no)

    # ② checkpoint 预检：线程上确有待恢复的中断（防 DB 与 checkpoint 漂移：中断已消费/丢失时给出明确 409）
    snap = await graph.aget_state(config)
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


# ── 采购审批：列表/待审批（对标 EduAgent 6.12 /pending-reviews /my-submissions）──
@router.get("/procurement/pending")
async def procurement_pending(current_user: dict = Depends(require_role("admin", "teacher"))):
    """待人工审批的采购单列表（仅 admin/teacher）"""
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
    """我的采购单列表（对齐 EduAgent /my-submissions）"""
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


# ── 谈判交底（状态机）─────────────────────────────────────
@router.post("/negotiation/chat", dependencies=[Depends(llm_rate_limit)])
async def negotiation_chat(body: NegotiationBody, current_user: dict = Depends(get_current_user)):
    """谈判多轮对话（状态机推进，对标 EduAgent 7.12）"""
    orchestrator = get_orchestrator()
    graph = orchestrator._get_agent_graph(AgentType.NEGOTIATION)
    config = build_config(current_user["user_id"], body.session_id)

    if body.reset:
        # 新建谈判会话：重新初始化
        state = {
            "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
            "session_id": body.session_id, "material": body.material,
            "messages": [HumanMessage(content="开始谈判")], "original_query": body.material,
        }
        result = await graph.ainvoke(state, config=config)
    else:
        # 续谈：从 checkpoint 恢复
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=body.message)]}, config=config,
        )
    return {
        "stage": result.get("stage", "quote"),
        "stage_index": result.get("stage_index", 0),
        "reply": result.get("answer", ""),
        "done": result.get("stage") == "done",
    }


# ── 谈判：SSE 流式对话（对标 EduAgent 7.12 /chat/stream）─────────
@router.post("/negotiation/chat/stream", dependencies=[Depends(llm_rate_limit)])
async def negotiation_chat_stream(body: NegotiationBody, current_user: dict = Depends(get_current_user)):
    """谈判流式接口：token 级逐字输出 + done 附阶段/报告（对齐 EduAgent 7.12）"""
    from sse_starlette.sse import EventSourceResponse

    orchestrator = get_orchestrator()
    graph = orchestrator._get_agent_graph(AgentType.NEGOTIATION)
    config = build_config(current_user["user_id"], body.session_id)

    async def event_generator():
        # 构造状态：reset 则全新，否则续谈（checkpoint 恢复）
        if body.reset:
            state_in = {
                "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
                "session_id": body.session_id, "material": body.material,
                "messages": [HumanMessage(content="开始谈判")], "original_query": body.material,
            }
        else:
            state_in = {"messages": [HumanMessage(content=body.message)]}
        try:
            async for event in graph.astream_events(state_in, config=config, version="v2"):
                evt = event["event"]
                node = event.get("metadata", {}).get("langgraph_node", "")
                if evt == "on_chat_model_stream" and node == "respond":
                    chunk = event["data"].get("chunk")
                    if chunk and chunk.content:
                        yield {"data": json.dumps({"type": "token", "content": chunk.content}, ensure_ascii=False)}
        except Exception as e:
            logger.error("negotiation.stream_error", error=str(e), exc_info=True)
            yield {"data": json.dumps({"type": "error", "message": "流式输出异常"}, ensure_ascii=False)}
            return
        # done 事件带阶段 + 报告
        final = await graph.aget_state(config)
        sv = final.values if final else {}
        payload = {
            "type": "done",
            "stage": sv.get("stage", "quote"),
            "is_done": sv.get("stage") == "done",
        }
        if sv.get("report"):
            payload["report"] = sv["report"]
        yield {"data": json.dumps(payload, ensure_ascii=False)}

    return EventSourceResponse(event_generator())


# ── 通用：Orchestrator 直达（测试用）─────────────────────
@router.post("/agents/{agent_type}/run", dependencies=[Depends(llm_rate_limit)])
async def run_agent(agent_type: str, body: ChatBody, current_user: dict = Depends(get_current_user)):
    """通过 Orchestrator 直接运行任意 Agent（对标 EduAgent 8.2 handle）"""
    try:
        at = AgentType(agent_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="未知 agent_type")
    request = AgentRequest(
        user_id=current_user["user_id"], tenant_id=current_user["tenant_id"],
        session_id=body.session_id, agent_type=at,
        mode=ExecutionMode.SINGLE, input_text=body.message,
    )
    resp = await get_orchestrator().handle(request)
    return resp.model_dump()


# ── 知识问答：历史会话（对标 EduAgent 5.16）─────────────────
@router.get("/qa/sessions/{session_id}/history")
async def get_qa_history(session_id: str, current_user: dict = Depends(get_current_user)):
    """获取问答会话历史：DB 摘要 + MemorySaver 消息（对齐 EduAgent 5.16）"""
    from backend.core.memory import build_thread_id
    from langchain_core.messages import HumanMessage as HM, AIMessage as AM

    user_id = current_user["user_id"]
    thread_id = build_thread_id(user_id, session_id)

    # ① DB 摘要
    summary = None
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT summary FROM qa_sessions WHERE thread_id = :tid"
            ), {"tid": thread_id})).fetchone()
        if row: summary = row[0]
    except Exception: pass

    # ② MemorySaver 消息
    messages = []
    try:
        # 复用 orchestrator 的图缓存（原实现每请求重编译一次 QA 图）
        graph = get_orchestrator()._get_agent_graph(AgentType.QA)
        config = {"configurable": {"thread_id": thread_id}}
        state = await graph.aget_state(config)
        if state and state.values:
            for m in state.values.get("messages", []):
                content = m.content if isinstance(m.content, str) else str(m.content)
                role = "user" if isinstance(m, HM) else ("assistant" if isinstance(m, AM) else "system")
                messages.append({"role": role, "content": content})
    except Exception: pass

    return {"session_id": session_id, "summary": summary,
            "messages": messages, "total_turns": sum(1 for m in messages if m["role"] == "user")}


# ── 知识待补队列（对标 EduAgent knowledge_pending_queue）────────
@router.get("/knowledge/pending")
async def list_knowledge_pending(status: str = "pending", current_user: dict = Depends(get_current_user)):
    """查询知识待补队列（教师视角：查看低置信度问题）"""
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT id, question, confidence, status, created_at FROM knowledge_pending_queue
            WHERE status = :st ORDER BY created_at DESC LIMIT 50
        """), {"st": status})).fetchall()
    return {"items": [
        {"id": r[0], "question": r[1], "confidence": r[2], "status": r[3],
         "created_at": str(r[4]) if r[4] else None}
        for r in rows
    ]}


@router.post("/knowledge/pending/{pending_id}/resolve")
async def resolve_knowledge_pending(pending_id: str, current_user: dict = Depends(require_role("admin", "teacher"))):
    """标记问题已解决（仅 admin/teacher）"""
    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE knowledge_pending_queue SET status = 'resolved' WHERE id = :id AND status = 'pending'
        """), {"id": pending_id})
    return {"status": "resolved"}


class KnowledgeAnswerBody(BaseModel):
    answer: str = Field(..., min_length=5, max_length=4000)
    source_name: str = "教师补充"


@router.post("/knowledge/pending/{pending_id}/answer")
async def answer_knowledge_pending(pending_id: str, body: KnowledgeAnswerBody,
                                   current_user: dict = Depends(require_role("admin", "teacher"))):
    """知识待补闭环（补齐）：教师回答 → 写入知识库（立即可被 RAG 检索）→ 标记 resolved。
    此前队列只进不出，攒的低置信度问题没有任何途径变成知识——闭环断在最后一步。"""
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT question, tenant_id FROM knowledge_pending_queue "
            "WHERE id = :id AND status = 'pending'"
        ), {"id": pending_id})).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="待补问题不存在或已处理")
    question, tenant_id = row[0], row[1] or "tenant_default"

    from backend.services.vector_store import add_chunks
    await add_chunks([{
        "content": f"问：{question}\n答：{body.answer}",
        "source_name": body.source_name,
        "doc_id": f"pending_{pending_id}",
        "chunk_index": 0,
    }], tenant_id=tenant_id)
    logger.info("knowledge.answered", pending_id=pending_id, tenant=tenant_id)

    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE knowledge_pending_queue SET status = 'resolved' WHERE id = :id"
        ), {"id": pending_id})
    return {"status": "resolved", "chunk_added": True, "question": question}


# ── 可观测性：LLM 调用统计（对标 Langfuse 轻量替代）─────────────────
@router.get("/observability/stats")
async def obs_stats(hours: int = 24, current_user: dict = Depends(get_current_user)):
    """LLM 调用统计（需登录；内部指标不暴露未认证）"""
    from backend.core.observability import get_call_stats
    return await get_call_stats(hours)

# ── 会话历史查询 ─────────────────────────────────────────
@router.get("/sessions/{session_id}")
async def get_session(session_id: str, current_user: dict = Depends(get_current_user)):
    return {"session_id": session_id, "user_id": current_user["user_id"]}