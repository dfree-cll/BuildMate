"""知识问答 + 知识库 Agent REST 接口（对标 EduAgent 5.16/8.2）"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.core.orchestrator import AgentType, ExecutionMode, AgentRequest, get_orchestrator
from backend.dependencies import get_current_user, require_role, llm_rate_limit
from backend.core.logger import get_logger
from sqlalchemy import text
from backend.db.session import engine

router = APIRouter()
logger = get_logger(__name__)


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str = "default"


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

    from backend.core.knowledge_base import add_chunks
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
