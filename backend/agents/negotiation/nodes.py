"""谈判交底 Agent 节点
阶段流转：quote → tech → delivery → sign → done（每阶段至少 2 轮对话再推进）
修复：① JSON 报告解析用平衡花括号 ② 阶段推进需 >=2 轮
"""
import json
from langchain_core.messages import HumanMessage, AIMessage
from pydantic import BaseModel, Field

from backend.agents.negotiation.state import NegotiationState, NegotiationStage, STAGE_ORDER
from backend.agents.negotiation.prompts import SYSTEM_PROMPT, STAGE_RESPONSE_PROMPT, STAGE_TRANSITION_PROMPT, MEETING_MINUTES_PROMPT
from backend.core.llm_factory import get_llm
from backend.core.llm_text import msg_text as _msg_text, parse_json_loose as _extract_json_object
from backend.core.logger import get_logger
from backend.application.agent_memory import memory_prompt

logger = get_logger(__name__)

STAGE_CN = {
    "quote": "报价阶段", "tech": "技术方案", "delivery": "交付条件", "sign": "签约阶段", "done": "已完成",
}


class _StageTransition(BaseModel):
    """Validated LLM decision for moving to the next negotiation stage."""

    ready: bool
    reason: str = Field(default="", max_length=1000)


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
        resp = await llm.ainvoke([HumanMessage(content=memory_prompt(state, STAGE_RESPONSE_PROMPT.format(
            stage=stage, material=material, quotes=quotes_text, message=history)))])
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
            payload = _extract_json_object(_msg_text(resp).strip())
            transition = _StageTransition.model_validate(payload)
            if transition.ready:
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


async def _gen_minutes(state: NegotiationState) -> dict:
    """会议纪要生成（LLM 失败降级为结构占位）"""
    quotes = state.get("quotes", [])
    material = state.get("material", "QTZ63 塔吊")
    conversation = "\n".join(_msg_text(m)[:200] for m in state.get("messages", [])[-16:]) or "（无对话记录）"
    try:
        llm = get_llm("negotiation", temperature=0)
        prompt = MEETING_MINUTES_PROMPT.format(material=material,
            quotes=json.dumps(quotes, ensure_ascii=False), conversation=conversation[:3000])
        resp = await llm.ainvoke([HumanMessage(content=memory_prompt(state, prompt))])
        report = _extract_json_object(_msg_text(resp).strip())
    except Exception as e:
        logger.warning("negotiation.minutes_failed", error=str(e)[:100])
        report = None
    if not report:
        report = {"basic": {"subject": material, "parties": "采购方 vs 分包商", "stages": "（生成失败）"},
                  "stage_minutes": [], "agreements": [], "open_items": [],
                  "key_info": [], "suggestion": "（纪要生成失败，请重试或继续谈判）", "next_steps": []}
    return report


def _format_minutes(report: dict) -> str:
    """会议纪要 → 文本展示"""
    basic = report.get("basic", {})
    stage_lines = "\n".join(f"  - **{s.get('stage', '')}**：{s.get('content', '')}" for s in report.get("stage_minutes", []) or []) or "  - （无）"
    lines = ["📋 **谈判会议纪要**",
             f"- **标的**：{basic.get('subject', '')}｜**参与方**：{basic.get('parties', '')}｜**阶段**：{basic.get('stages', '')}",
             "\n**【阶段纪要】**", stage_lines,
             "\n**【达成的共识】**", "；".join(report.get("agreements", []) or ["（无）"]),
             "\n**【未决事项】**", "；".join(report.get("open_items", []) or ["（无）"]),
             "\n**【关键信息】**", "；".join(report.get("key_info", []) or ["（无）"]),
             "\n**【给决策者的建议】**", str(report.get("suggestion", "（无）")),
             "\n**【下一步行动】**", "；".join(report.get("next_steps", []) or ["（无）"])]
    return "\n".join(lines)


async def minutes_node(state: NegotiationState) -> dict:
    """中途会议纪要：不推进阶段，基于当前对话生成纪要"""
    report = await _gen_minutes(state)
    answer = _format_minutes(report)
    return {"answer": answer, "minutes": report, "structured_output": report,
            "messages": [AIMessage(content=answer)]}


async def done_node(state: NegotiationState) -> dict:
    """谈判完成：生成完整会议纪要（给决策者）"""
    report = await _gen_minutes(state)
    answer = _format_minutes(report)
    return {"answer": answer, "structured_output": report, "report": report,
            "messages": [AIMessage(content=answer)]}
