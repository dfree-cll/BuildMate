"""供应商谈判/施工交底 Agent 状态"""
import enum
from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class NegotiationStage(str, enum.Enum):
    QUOTE = "quote"            # 报价阶段
    TECH = "tech"             # 技术方案阶段
    DELIVERY = "delivery"     # 交付条件阶段
    SIGN = "sign"             # 签约阶段
    DONE = "done"             # 完成


STAGE_ORDER = [NegotiationStage.QUOTE, NegotiationStage.TECH,
                NegotiationStage.DELIVERY, NegotiationStage.SIGN, NegotiationStage.DONE]


class NegotiationState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    tenant_id: str
    session_id: str
    stage: str                  # NegotiationStage 值
    stage_index: int
    material: str               # 谈判标的（材料/设备）
    quotes: list[dict]          # 各阶段记录
    context: dict               # 谈判上下文
    answer: str
    next_stage: str
    structured_output: Optional[dict]