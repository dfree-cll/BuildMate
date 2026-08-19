"""投标文件审查 Agent 状态
直线流水线：解析 → 结构化提取 → 四维并行评审 → 问题诊断 → 整体评价
"""
from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field


class DimensionScore(BaseModel):
    dimension: str = ""
    score: int = Field(description="得分 0-100")
    weight: float = 0.0
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class BidReviewStructured(BaseModel):
    project_name: str = ""
    bidder: str = ""
    bid_amount: float = 0.0
    technical_solution: str = ""
    qualifications: str = ""
    bid_documents: list[str] = Field(default_factory=list)


class BidReviewState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    tenant_id: str
    review_id: str
    doc_text: str                        # 解析出的投标文件文本
    structured: Optional[dict]           # 结构化提取
    dimension_scores: list[dict]         # 四维评分
    weighted_score: float
    issues: list[dict]                   # 风险问题
    summary: Optional[dict]              # 整体评价
    answer: str                          # 最终汇总输出
    structured_output: Optional[dict]
    fallback_used: bool
