"""谈判交底（状态机）Agent REST 接口"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage

from backend.core.orchestrator import AgentType, get_orchestrator
from backend.core.memory import build_config
from backend.dependencies import get_current_user, llm_rate_limit

router = APIRouter()


class NegotiationBody(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    message: str = Field(..., min_length=1)
    material: str = "螺纹钢 HRB400"
    session_id: str = Field("default", min_length=1, max_length=128)
    reset: bool = False


class NegotiationMinutesBody(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    material: str = "螺纹钢 HRB400"
    session_id: str = Field("default", min_length=1, max_length=128)


# ── 谈判：中途会议纪要 ──────────
@router.post("/negotiation/minutes", dependencies=[Depends(llm_rate_limit)])
async def negotiation_minutes(body: NegotiationMinutesBody, current_user: dict = Depends(get_current_user)):
    """中途生成会议纪要：基于当前谈判对话，把信息整理给人（不推进阶段）"""
    from backend.agents.negotiation.nodes import minutes_node

    orchestrator = get_orchestrator()
    graph = orchestrator._get_agent_graph(AgentType.NEGOTIATION)
    config = build_config(current_user["user_id"], body.session_id, tenant_id=current_user["tenant_id"],
                          project_id=body.project_id, agent="negotiation")
    st = await graph.aget_state(config)
    sv = st.values if st else {}
    if not sv.get("stage"):
        return {"minutes": None, "reply": "（尚无谈判内容，请先开始谈判）"}
    result = await minutes_node(dict(sv))
    return {"minutes": result.get("structured_output"), "reply": result.get("answer", "")}


# ── 谈判交底（状态机）─────────────────────────────────────
@router.post("/negotiation/chat", dependencies=[Depends(llm_rate_limit)])
async def negotiation_chat(body: NegotiationBody, current_user: dict = Depends(get_current_user)):
    """谈判多轮对话（状态机推进，对标行业范式）"""
    orchestrator = get_orchestrator()
    graph = orchestrator._get_agent_graph(AgentType.NEGOTIATION)
    config = build_config(current_user["user_id"], body.session_id, tenant_id=current_user["tenant_id"],
                          project_id=body.project_id, agent="negotiation")

    if body.reset:
        previous = await graph.aget_state(config)
        if previous and previous.values:
            raise HTTPException(status_code=409, detail="该会话已有谈判记录，请新建会话，不要覆盖历史")
        # 新建谈判会话：重新初始化
        state = {
            "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
            "session_id": body.session_id, "material": body.material, "project_id": body.project_id,
            "memory_turn_id": uuid.uuid4().hex,
            "messages": [HumanMessage(content="开始谈判")], "original_query": body.material,
        }
        result = await graph.ainvoke(state, config=config)
    else:
        # 续谈：从 checkpoint 恢复
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=body.message)],
             "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
             "project_id": body.project_id, "session_id": body.session_id,
             "material": body.material, "memory_turn_id": uuid.uuid4().hex}, config=config,
        )
    return {
        "stage": result.get("stage", "quote"),
        "stage_index": result.get("stage_index", 0),
        "reply": result.get("answer", ""),
        "done": result.get("stage") == "done",
    }
