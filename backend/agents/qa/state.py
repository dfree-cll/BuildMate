"""知识问答 Agent 状态"""
from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


from backend.application.agent_memory import MemoryState


class QAState(MemoryState):
    messages: Annotated[list[BaseMessage], add_messages]
    student_id: str
    tenant_id: str
    project_id: Optional[str]
    session_id: str
    original_query: str
    query_type: str                # GENERAL / PRECISE / VAGUE / BROAD
    rewritten_queries: list[str]
    ranked_chunks: list[dict]
    confidence: float
    answer: str
    sources: list[str]
    answer_mode: str               # rag / llm_direct / general
    fallback_used: bool
    structured_output: Optional[dict]
    retrieval_run_id: Optional[str]
    existing_summary: Optional[str]   # ★ 跨轮记忆：load_memory 注入的历史摘要（修复被丢弃）
