"""采购审批 Agent 图装配（HitL：interrupt/resume，对标行业范式）"""
from langgraph.graph import StateGraph, START, END
from backend.application.agent_memory import memory_nodes
from backend.agents.procurement.state import ProcurementState
from backend.agents.procurement.nodes import (
    parallel_review_node, merge_node, human_in_the_loop_node, publish_node,
)
from backend.core.memory import get_memory_saver


def build_procurement_graph():
    builder = StateGraph(ProcurementState)
    load_memory, save_memory = memory_nodes("procurement")
    builder.add_node("load_memory", load_memory)
    builder.add_node("save_memory", save_memory)
    builder.add_node("parallel_review", parallel_review_node)
    builder.add_node("merge", merge_node)
    builder.add_node("human_in_the_loop", human_in_the_loop_node)
    builder.add_node("publish", publish_node)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "parallel_review")
    builder.add_edge("parallel_review", "merge")
    builder.add_edge("merge", "human_in_the_loop")
    builder.add_edge("human_in_the_loop", "publish")
    builder.add_edge("publish", "save_memory")
    builder.add_edge("save_memory", END)

    checkpointer = get_memory_saver("procurement")
    return builder.compile(checkpointer=checkpointer)
