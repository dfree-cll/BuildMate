"""谈判交底 Agent 图装配（状态机：每次 ainvoke 走一轮 respond→check_stage，阶段推进经 checkpoint 保存）"""
from langgraph.graph import StateGraph, START, END
from backend.application.agent_memory import memory_nodes
from backend.agents.negotiation.state import NegotiationState, NegotiationStage
from backend.agents.negotiation.nodes import init_node, respond_node, check_stage_node, done_node
from backend.core.memory import get_memory_saver


def _route_after_check(state: NegotiationState) -> str:
    if state.get("stage") == NegotiationStage.DONE.value:
        return "done"
    return "end"


def build_negotiation_graph():
    builder = StateGraph(NegotiationState)
    load_memory, save_memory = memory_nodes("negotiation")
    builder.add_node("load_memory", load_memory)
    builder.add_node("save_memory", save_memory)
    builder.add_node("init", init_node)
    builder.add_node("respond", respond_node)
    builder.add_node("check_stage", check_stage_node)
    builder.add_node("done", done_node)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "init")
    builder.add_edge("init", "respond")
    builder.add_edge("respond", "check_stage")
    builder.add_conditional_edges("check_stage", _route_after_check, {"done": "done", "end": "save_memory"})
    builder.add_edge("done", "save_memory")
    builder.add_edge("save_memory", END)

    checkpointer = get_memory_saver("negotiation")
    return builder.compile(checkpointer=checkpointer)
