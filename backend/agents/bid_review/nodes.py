"""投标审查 Agent 节点
parse → extract → 四维并行评审 → 汇总
"""
import asyncio
import json
from langchain_core.messages import HumanMessage

from backend.agents.bid_review.state import BidReviewState
from backend.agents.bid_review.prompts import EXTRACT_PROMPT, DIMENSIONS, DIMENSION_PROMPT, SUMMARY_PROMPT
from backend.core.llm_factory import get_llm
from backend.core.llm_text import msg_text as _msg_text, parse_json_loose as _parse_json
from backend.core.logger import get_logger

logger = get_logger(__name__)


async def parse_node(state: BidReviewState) -> dict:
    """从输入文本或 mock 文档获取投标文件内容"""
    doc_text = state.get("doc_text", "") or state.get("original_query", "")
    if not doc_text:
        doc_text = (
            "某市政道路工程投标文件：投标人 XX建设集团，投标报价 8500 万元。"
            "技术方案包含施工组织设计、路基路面施工工艺、工期 18 个月。"
            "资质：市政公用工程施工总承包一级。业绩：近三年 5 个同类项目。"
        )
    logger.info("bid_review.parsed", chars=len(doc_text))
    return {"doc_text": doc_text}


async def extract_node(state: BidReviewState) -> dict:
    try:
        llm = get_llm("bid_review", temperature=0)
        resp = await llm.ainvoke([HumanMessage(content=EXTRACT_PROMPT.format(doc_text=state["doc_text"][:3000]))])
        structured = _parse_json(_msg_text(resp)) or {}
    except Exception as e:
        logger.warning("bid_review.extract_failed", error=str(e))
        structured = {"project_name": "未知项目", "bidder": "未知企业"}
    return {"structured": structured}


async def review_dimension(dim: dict, doc_text: str):
    """单个维度评审（并行执行）"""
    try:
        llm = get_llm("bid_review", temperature=0)
        prompt = DIMENSION_PROMPT.format(dimension=dim["dimension"], instruction=dim["instruction"],
                                         doc_text=doc_text[:3000])
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        result = _parse_json(_msg_text(resp)) or {}
        score = max(0, min(100, int(result.get("score", 50))))
        return {
            "dimension": dim["dimension"],
            "weight": dim["weight"],
            "score": score,
            "issues": result.get("issues", []),
            "suggestions": result.get("suggestions", []),
        }
    except Exception as e:
        logger.warning("bid_review.dimension_failed", dimension=dim["dimension"], error=str(e))
        return {"dimension": dim["dimension"], "weight": dim["weight"], "score": 50,
                "issues": ["该维度评审失败，按基准分 50 计"], "suggestions": []}


async def parallel_review_node(state: BidReviewState) -> dict:
    """四维并行评审（asyncio.gather，四维度并行）"""
    doc_text = state["doc_text"]
    results = await asyncio.gather(*[review_dimension(d, doc_text) for d in DIMENSIONS])
    weighted = round(sum(r["score"] * r["weight"] for r in results), 2)
    logger.info("bid_review.parallel_done", weighted_score=weighted)
    return {"dimension_scores": results, "weighted_score": weighted}


async def diagnose_node(state: BidReviewState) -> dict:
    """汇总各维度问题，按优先级排序"""
    issues = []
    for r in state.get("dimension_scores", []):
        for iss in r.get("issues", []):
            issues.append({
                "priority": "high" if r["score"] < 60 else ("medium" if r["score"] < 75 else "low"),
                "dimension": r["dimension"],
                "description": iss,
            })
    issues.sort(key=lambda x: {"high": 0, "medium": 1, "low": 2}[x["priority"]])
    return {"issues": issues}


async def summarize_node(state: BidReviewState) -> dict:
    try:
        llm = get_llm("bid_review", temperature=0)
        dim_text = json.dumps(state.get("dimension_scores", []), ensure_ascii=False)
        resp = await llm.ainvoke([HumanMessage(content=SUMMARY_PROMPT.format(dimension_results=dim_text))])
        summary = _parse_json(_msg_text(resp)) or {}
    except Exception as e:
        logger.warning("bid_review.summary_failed", error=str(e))
        summary = {"overall_comment": "综合评审完成", "risk_level": "medium", "recommendation": "谨慎投标"}
    return {"summary": summary}


async def format_node(state: BidReviewState) -> dict:
    """汇总输出"""
    lines = []
    lines.append("## 投标文件评审报告：" + str(state.get("structured", {}).get("project_name", "未知项目")))
    lines.append("**投标人**：" + str(state.get("structured", {}).get("bidder", "未知")))
    lines.append("**加权综合得分**：" + str(state.get("weighted_score", 0)) + " / 100")
    for r in state.get("dimension_scores", []):
        lines.append("")
        lines.append("### " + r["dimension"] + "（权重 " + str(r["weight"]) + "）：" + str(r["score"]) + " 分")
        if r.get("issues"):
            lines.append("问题：" + "；".join(r["issues"][:3]))
        if r.get("suggestions"):
            lines.append("建议：" + "；".join(r["suggestions"][:2]))
    if state.get("issues"):
        high = [i for i in state["issues"] if i["priority"] == "high"]
        lines.append("")
        lines.append("**高风险问题 " + str(len(high)) + " 项**：" + "；".join(i["description"] for i in high[:5]))
    sm = state.get("summary", {}) or {}
    if sm:
        lines.append("")
        lines.append("**综合评语**：" + str(sm.get("overall_comment", "")))
        lines.append("**风险等级**：" + str(sm.get("risk_level", "")) + "｜**结论**：" + str(sm.get("recommendation", "")))
    answer = "\n".join(lines)

    return {
        "answer": answer,
        "structured_output": {
            "weighted_score": state.get("weighted_score", 0),
            "dimensions": state.get("dimension_scores", []),
            "issues": state.get("issues", []),
            "summary": state.get("summary", {}),
        },
    }