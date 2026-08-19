"""谈判交底（状态机）Agent REST 接口（对标 EduAgent 7.12）"""
import json
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage

from backend.core.orchestrator import AgentType, get_orchestrator
from backend.core.memory import build_config
from backend.dependencies import get_current_user, llm_rate_limit
from backend.core.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


class NegotiationBody(BaseModel):
    message: str = Field(..., min_length=1)
    material: str = "螺纹钢 HRB400"
    session_id: str = "default"
    reset: bool = False


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
