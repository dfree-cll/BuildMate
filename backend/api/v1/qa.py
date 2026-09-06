"""知识问答 + 知识库 Agent REST 接口"""
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
    project_id: str | None = Field(default=None, max_length=64)
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str = Field("default", min_length=1, max_length=128)


# ── 通用：Orchestrator 直达（测试用）─────────────────────
@router.post("/agents/{agent_type}/run", dependencies=[Depends(llm_rate_limit)])
async def run_agent(agent_type: str, body: ChatBody, current_user: dict = Depends(get_current_user)):
    """通过 Orchestrator 直接运行任意 Agent"""
    try:
        at = AgentType(agent_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="未知 agent_type")
    request = AgentRequest(
        user_id=current_user["user_id"], tenant_id=current_user["tenant_id"],
        session_id=body.session_id, agent_type=at,
        mode=ExecutionMode.SINGLE, input_text=body.message, extra={"project_id": body.project_id},
    )
    resp = await get_orchestrator().handle(request)
    return resp.model_dump()


# ── 知识待补队列────────
@router.get("/knowledge/pending")
async def list_knowledge_pending(status: str = "pending", current_user: dict = Depends(get_current_user)):
    """查询知识待补队列（审核视角：查看低置信度问题）"""
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
async def resolve_knowledge_pending(pending_id: str, current_user: dict = Depends(require_role("admin", "reviewer"))):
    """标记问题已解决（仅 admin/reviewer）"""
    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE knowledge_pending_queue SET status = 'resolved' WHERE id = :id AND status = 'pending'
        """), {"id": pending_id})
    return {"status": "resolved"}


class KnowledgeAnswerBody(BaseModel):
    answer: str = Field(..., min_length=5, max_length=4000)
    source_name: str = "审核补充"


@router.post("/knowledge/pending/{pending_id}/answer")
async def answer_knowledge_pending(pending_id: str, body: KnowledgeAnswerBody,
                                   current_user: dict = Depends(require_role("admin", "reviewer"))):
    """知识待补闭环（补齐）：审核回答 → 写入知识库（立即可被 RAG 检索）→ 标记 resolved。
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


# 可观测性接口已迁至 backend/api/v1/observability.py（URL 不变：/api/v1/observability/stats）
