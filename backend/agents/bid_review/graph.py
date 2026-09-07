"""投标审查 Agent 图装配（直线流水线，无分支）"""
from langgraph.graph import StateGraph, START, END
from backend.application.agent_memory import memory_nodes
from backend.core.memory import get_memory_saver
from backend.agents.bid_review.state import BidReviewState
from backend.agents.bid_review.nodes import (
    parse_node, extract_node, parallel_review_node, diagnose_node, summarize_node, format_node,
)


def build_bid_review_graph():
    builder = StateGraph(BidReviewState)
    load_memory, save_memory = memory_nodes("bid_review")
    builder.add_node("load_memory", load_memory)
    builder.add_node("save_memory", save_memory)
    builder.add_node("parse", parse_node)
    builder.add_node("extract", extract_node)
    builder.add_node("parallel_review", parallel_review_node)
    builder.add_node("diagnose", diagnose_node)
    builder.add_node("summarize", summarize_node)
    builder.add_node("format", format_node)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "parse")
    builder.add_edge("parse", "extract")
    builder.add_edge("extract", "parallel_review")
    builder.add_edge("parallel_review", "diagnose")
    builder.add_edge("diagnose", "summarize")
    builder.add_edge("summarize", "format")
    builder.add_edge("format", "save_memory")
    builder.add_edge("save_memory", END)
    return builder.compile(checkpointer=get_memory_saver("bid_review"))
