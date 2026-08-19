"""谈判交底 Agent 节点（对标 EduAgent 7：状态机推进 + SSE）
阶段流转：quote → tech → delivery → sign → done（每阶段至少 2 轮对话再推进）
修复：① JSON 报告解析用平衡花括号 ② 阶段推进需 >=2 轮
"""
import json
from langchain_core.messages import HumanMessage, AIMessage

from backend.agents.negotiation.state import NegotiationState, NegotiationStage, STAGE_ORDER
from backend.agents.negotiation.prompts import SYSTEM_PROMPT, STAGE_RESPONSE_PROMPT, STAGE_TRANSITION_PROMPT
from backend.core.llm_factory import get_llm
from backend.core.llm_text import msg_text as _msg_text, parse_json_loose as _extract_json_object
from backend.core.logger import get_logger

logger = get_logger(__name__)

STAGE_CN = {
    "quote": "报价阶段", "tech": "技术方案", "delivery": "交付条件", "sign": "签约阶段", "done": "已完成",
}


async def init_node(state: NegotiationState) -> dict:
    if state.get("stage"):
        return {}
    return {"stage": NegotiationStage.QUOTE.value, "stage_index": 0, "quotes": [],
            "context": {"rounds_in_stage": 0}}


async def respond_node(state: NegotiationState) -> dict:
    stage = state.get("stage", NegotiationStage.QUOTE.value)
    material = state.get("material", "QTZ63 塔吊")
    context = state.get("context", {})
    rounds = context.get("rounds_in_stage", 0)
    history = "\n".join(_msg_text(m)[:120] for m in state.get("messages", [])[-6:]) or "（开始谈判）"
    try:
        llm = get_llm("negotiation", temperature=0.3)
        quotes_text = json.dumps(state.get("quotes", []), ensure_ascii=False)
        resp = await llm.ainvoke([HumanMessage(content=STAGE_RESPONSE_PROMPT.format(
            stage=stage, material=material, quotes=quotes_text, message=history))])
        answer = _msg_text(resp).strip()
    except Exception as e:
        logger.warning("negotiation.respond_failed", error=str(e)[:100])
        answer = "（" + STAGE_CN.get(stage, stage) + "）已收到您的意见，我们继续推进。"
    context["rounds_in_stage"] = rounds + 1
    return {"answer": answer, "messages": [AIMessage(content=answer)],
            "context": dict(context)}


async def check_stage_node(state: NegotiationState) -> dict:
    """阶段推进：本阶段 >=2 轮后才允许进入下一阶段"""
    stage = state.get("stage", NegotiationStage.QUOTE.value)
    context = state.get("context", {})
    rounds = context.get("rounds_in_stage", 0)
    stage_index = state.get("stage_index", 0)
    quotes = state.get("quotes", [])
    if rounds < 2:
        return {"next_stage": stage}
    next_stage = stage
    if stage_index + 1 < len(STAGE_ORDER):
        try:
            llm = get_llm("negotiation", temperature=0)
            last = _msg_text(state.get("messages", [])[-1]) if state.get("messages") else ""
            resp = await llm.ainvoke([HumanMessage(content=STAGE_TRANSITION_PROMPT.format(
                stage=stage, last_message=last[:500]))])
            raw = _msg_text(resp).strip().upper()
            if "YES" in raw or "是" in raw[:20]:
                next_idx = stage_index + 1
                next_stage = STAGE_ORDER[next_idx].value
                quotes.append({"stage": stage, "summary": last[:200], "rounds": rounds})
                context["rounds_in_stage"] = 0
                return {"stage": next_stage, "stage_index": next_idx, "quotes": quotes,
                        "context": context, "next_stage": next_stage}
        except Exception as e:
            logger.warning("negotiation.transition_failed", error=str(e)[:100])
    if stage == NegotiationStage.SIGN.value and rounds >= 2:
        return {"stage": NegotiationStage.DONE.value, "stage_index": len(STAGE_ORDER) - 1,
                "next_stage": NegotiationStage.DONE.value}
    return {"next_stage": stage}


async def done_node(state: NegotiationState) -> dict:
    quotes = state.get("quotes", [])
    report_dict = None
    conversation = "\n".join(_msg_text(m)[:200] for m in state.get("messages", [])[-10:])
    material = state.get("material", "QTZ63 塔吊")
    try:
        llm = get_llm("negotiation", temperature=0)
        _REPORT = """你是商务谈判总结助手。基于以下谈判记录生成 JSON 报告：
{{"dimensions": [{{"name": "维度名", "score": 0-10, "comment": "评价"}}],
  "overall_score": 0-100, "strengths": [...], "improvements": [...],
  "recommendation": "结论", "next_steps": [...]}}

谈判标的：{material}
各阶段摘要：{quotes}
对话记录：{conversation}
只输出 JSON。"""
        prompt = _REPORT.format(material=material,
            quotes=json.dumps(quotes, ensure_ascii=False), conversation=conversation[:3000])
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        report_dict = _extract_json_object(_msg_text(resp).strip())
    except Exception as e:
        logger.warning("negotiation.report_failed", error=str(e))
    if not report_dict:
        report_dict = {"dimensions": [], "overall_score": 75,
            "strengths": ["完成了全阶段谈判流程"],
            "improvements": ["建议细化价格条款", "建议明确质保责任"],
            "recommendation": "谨慎签约",
            "next_steps": ["复核报价明细", "确认交付工期"]}
    answer = ("📋 **谈判总结报告**\n\n"
              + "- **综合评分**：" + str(report_dict.get("overall_score", 0)) + "/100\n"
              + "- **优势**：" + "；".join(report_dict.get("strengths", []) or ["（无）"]) + "\n"
              + "- **待改进**：" + "；".join(report_dict.get("improvements", []) or ["（无）"]) + "\n"
              + "- **结论**：" + str(report_dict.get("recommendation", "（无）")) + "\n"
              + "- **下一步**：" + "；".join(report_dict.get("next_steps", []) or ["（无）"]))
    return {"answer": answer, "structured_output": report_dict,
            "messages": [AIMessage(content=answer)]}
