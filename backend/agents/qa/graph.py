"""知识问答 Agent 图装配（对标 EduAgent 5.15）
load_memory → classify_query → (PRECISE/VAGUE/BROAD → retrieve | GENERAL → generate) → generate → enqueue_pending → save_memory
低置信度问题 → knowledge_pending_queue（知识待补闭环）
"""
from langgraph.graph import StateGraph, START, END
from backend.agents.qa.state import QAState
from backend.agents.qa.nodes import (
    classify_query_node, retrieve_node, generate_node, save_memory_node,
    load_memory_node, enqueue_pending_node, real_price_node, _is_price_query,
)
from backend.core.memory import get_memory_saver


def _route_by_query_type(state: QAState) -> str:
    return state.get("query_type", "PRECISE").upper()


def _route_after_classify(state: QAState) -> str:
    """价格意图优先于策略分类 → 真实行情直答；其余按策略正常检索"""
    query = state.get("original_query", "")
    if _is_price_query(query):
        return "real_price"
    return state.get("query_type", "PRECISE").upper()


def build_qa_graph():
    builder = StateGraph(QAState)
    builder.add_node("load_memory", load_memory_node)
    builder.add_node("classify_query", classify_query_node)
    builder.add_node("real_price", real_price_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("generate", generate_node)
    builder.add_node("enqueue_pending", enqueue_pending_node)
    builder.add_node("save_memory", save_memory_node)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "classify_query")
    builder.add_conditional_edges(
        "classify_query",
        _route_after_classify,
        {
            "PRECISE": "retrieve", "VAGUE": "retrieve", "BROAD": "retrieve",
            "GENERAL": "generate", "real_price": "real_price",
        },
    )
    # real_price 命中（answer_mode=real_price）→ 答案已生成，跳过 generate 直接收尾；
    # 未命中（返回空 state）→ 回退 retrieve 正常检索
    builder.add_conditional_edges(
        "real_price",
        lambda s: "enqueue_pending" if s.get("answer_mode") == "real_price" else "retrieve",
        {"enqueue_pending": "enqueue_pending", "retrieve": "retrieve"},
    )
    builder.add_edge("retrieve", "generate")
    builder.add_edge("generate", "enqueue_pending")
    builder.add_edge("enqueue_pending", "save_memory")
    builder.add_edge("save_memory", END)

    checkpointer = get_memory_saver("qa")
    return builder.compile(checkpointer=checkpointer)
