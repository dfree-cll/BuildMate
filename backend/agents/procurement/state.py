"""采购审批 Agent 状态
规则引擎 + LLM 双轨审核 → interrupt 人工审批 → 发布
"""
from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class ProcurementState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    tenant_id: str
    order_id: str
    order_no: str
    material_name: str
    quantity: int
    unit_price: float
    total_amount: float
    # 双轨审核结果
    rule_result: Optional[dict]          # 规则引擎结果
    llm_result: Optional[dict]           # LLM 审查结果
    ai_conclusion: Optional[dict]        # 合并结论
    ai_verdict: str                      # pass / review / reject
    # HitL
    teacher_decision: Optional[dict]     # interrupt 返回值
    final_verdict: str                   # approved / rejected
    final_comment: str
    answer: str
    structured_output: Optional[dict]
    fallback_used: bool
