"""统一 AI 助手入口
SSE 事件类型：task_plan / routing_decision / progress / token / guidance / pipeline_plan / meta / done / error
"""
import json
import asyncio
import re
import uuid
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from langchain_core.messages import HumanMessage

from backend.core.orchestrator import AgentType, ExecutionMode, get_orchestrator
from backend.dependencies import get_current_user, llm_rate_limit
from backend.core.logger import get_logger
from backend.application.agent_memory import repository as memory_repository, context_text
from backend.application.context import build_request_context
from backend.application.task_understanding import understand_task
from backend.domain.errors import ValidationFailure
from backend.core.memory import build_config
from backend.config import get_settings

router = APIRouter()
logger = get_logger(__name__)

_AGENT_DISPLAY = {
    AgentType.QA: "建筑知识问答",
    AgentType.BID_REVIEW: "投标文件审查",
    AgentType.PROCUREMENT: "采购审批",
    AgentType.NEGOTIATION: "供应商谈判",
    AgentType.DRAWING2BIM: "BIM Agent",
}

_GUIDANCE = {
    AgentType.BID_REVIEW: {
        "message": "已识别为投标文件审查。请在当前工作台的投标审查面板补充文件并确认提交；商务、技术、资质和报价分别评审，任务记录独立保存。",
        "action_label": "打开投标审查",
        "action_url": "/qa?capability=bid_review",
        "capability": "bid_review",
    },
    AgentType.DRAWING2BIM: {
        "message": "检测到您需要进行图纸审查/BIM 建模。请前往「BIM Agent」页面上传 PDF、DWG 或 DXF，系统将按统一 WallEvidence→WallModel→Revit 链路处理。",
        "action_label": "前往 BIM Agent",
        "action_url": "/bim",
        "capability": "bim",
    },
    AgentType.PROCUREMENT: {
        "message": "已识别为采购审批。请在当前工作台的采购面板确认材料、数量、价格等信息后提交；审批规则和采购任务上下文保持独立。",
        "action_label": "打开采购审批",
        "action_url": "/qa?capability=procurement",
        "capability": "procurement",
    },
    AgentType.NEGOTIATION: {
        "message": "已识别为供应商谈判。请在当前工作台的谈判面板补充材料和供应商信息；谈判会话与问答、投标和采购分别保存。",
        "action_label": "打开供应商谈判",
        "action_url": "/qa?capability=negotiation",
        "capability": "negotiation",
    },
}

# ── 规则前置拦截（零 Token）──────────────────────────────────────
_STRIP_TAIL_RE = re.compile(r"[\s!！?？。~～,.，。]+$")
_HELLO_KEYWORDS = frozenset(["你好", "您好", "hi", "hello", "hey", "哈喽", "嗨", "在吗", "在不在"])
_THANKS_KEYWORDS = frozenset(["谢谢", "感谢", "多谢", "谢了", "非常感谢", "辛苦了", "太棒了", "好的", "收到", "明白了"])
_BYE_KEYWORDS = frozenset(["再见", "拜拜", "bye", "goodbye", "下次见", "下次再聊"])
# M7 修复：身份/能力类问题由"子串 search"改为"整句 fullmatch"——
# 旧正则 search 全文，"你是谁家的供应商？"会被模板回复拦截，绕过 LLM 路由
_PUNCT_TAIL = r"[!！?？。~～\s,.，啊呀呢吧吗呀]*"
_IDENTITY_FULL = re.compile(
    r"(你|您)(是谁|叫什么名字|叫什么|叫啥|是什么)" + _PUNCT_TAIL
    + r"|(请)?(你|您)?介绍(一下)?(你|您)?自己" + _PUNCT_TAIL
    + r"|自我介绍" + _PUNCT_TAIL)
_CAPABILITY_FULL = re.compile(
    r"(你|您)(能|可以|会)(做|帮|干)(什么|啥|忙)?" + _PUNCT_TAIL
    + r"|(你|您)有(什么|哪些)(功能|用途|能力)?" + _PUNCT_TAIL
    + r"|怎么(用|使用)(你|您)?" + _PUNCT_TAIL)

_REPLY_HELLO = (
    "您好！我是 **BuildMate 建筑行业智能助手**。\n\n"
    "我可以帮您：\n"
    "- **建筑知识问答**：直接输入问题，查询建材价格、规范条文、招投标政策、施工方案\n"
    "- **投标文件审查**：告诉我「帮我审查投标文件」，四维并行评审+风险提示\n"
    "- **采购审批**：告诉我「提交采购单」，规则引擎+LLM 双轨审核\n"
    "- **供应商谈判**：告诉我「开始谈判」，按阶段推进报价→技术→交付→签约\n"
    "\n请问有什么可以帮到您？"
)
_REPLY_THANKS = "不客气，很高兴能帮到您！如果有其他问题随时告诉我。"
_REPLY_BYE = "再见！希望今天的咨询对您有帮助，期待下次交流。"
_REPLY_IDENTITY = (
    "我是 **BuildMate AI 助手**，建筑行业综合智能助手。\n"
    "由多个专业 Agent 协同构成：知识问答（RAG）、投标审查（并行评审）、采购审批（人在环中）、供应商谈判（状态机）。"
)
_REPLY_CAPABILITY = (
    "我能为您提供以下功能：\n"
    "**智能工作台**\n"
    "- 直接输入建筑问题 → 规范/政策资料检索；建材价格查询结构化行情\n"
    "- 「帮我审查投标文件」 → 四维并行评审 + 风险提示\n"
    "- 「提交采购单」 → 规则引擎 + LLM 双轨审核，大额需人工审批\n"
    "- 「开始供应商谈判」 → 多阶段状态机推进\n\n"
    "- 图纸建模 → 独立 BIM Agent 入口\n\n"
    "描述综合需求（如「投标准备」）可获得分步建议；业务面板各自保存记录，补齐资料并确认后才提交任务。"
)


def _pre_filter(text: str) -> str | None:
    t = text.strip()
    t_lower = _STRIP_TAIL_RE.sub("", t.lower())
    if t_lower in _HELLO_KEYWORDS:
        return _REPLY_HELLO
    if t_lower in _THANKS_KEYWORDS:
        return _REPLY_THANKS
    if t_lower in _BYE_KEYWORDS:
        return _REPLY_BYE
    if _IDENTITY_FULL.fullmatch(t):
        return _REPLY_IDENTITY
    if _CAPABILITY_FULL.fullmatch(t):
        return _REPLY_CAPABILITY
    return None


def _parse_route_json(raw: str) -> dict | None:
    """解析 LLM 路由 JSON（M6：容错 markdown code fence 与前后缀文本，
    旧实现直接 json.loads，LLM 返回 ```json 包裹时路由静默降级 qa）"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


async def _llm_route(message: str, history: str = "") -> dict:
    """统一调用编排器路由，避免 v1 入口维护第二套 prompt 和映射。"""
    decision = await get_orchestrator().route(message, history=history)
    return {
        "label": decision["label"],
        "agent_type": decision["agent_type"],
        "execution_mode": decision["mode"],
        "reason": decision["reason"],
    }


def _sse(data: dict) -> dict:
    return {"data": json.dumps(data, ensure_ascii=False)}


# A plan produced by the deterministic task-understanding boundary is already
# an audited route.  Do not send it through a second (and potentially slow)
# LLM classifier.  Only the legacy/general-question fallback below may call
# the model router.
_PLAN_DECISIONS = {
    "rag.answer": ("qa", AgentType.QA, "知识问答", "已识别为知识查询"),
    "bid.search": ("bid_review", AgentType.BID_REVIEW, "投标审查", "已识别为标书查询"),
    "workflow.bid_review": ("bid_review", AgentType.BID_REVIEW, "投标审查", "已识别为投标审查"),
    "workflow.contract_review": ("contract_review", AgentType.QA, "合同审核", "已识别为合同审核"),
    "workflow.procurement": ("procurement", AgentType.PROCUREMENT, "采购审批", "已识别为采购流程"),
    "workflow.negotiation": ("negotiation", AgentType.NEGOTIATION, "供应商谈判", "已识别为谈判流程"),
    "workflow.wall_pipeline": ("bim", AgentType.DRAWING2BIM, "BIM Agent", "已识别为 BIM 建模"),
    "clarify": ("clarify", AgentType.QA, "意图澄清", "需要补充信息"),
}


def _decision_from_plan(plan) -> dict | None:
    """Translate one safe plan step into the legacy stream decision shape."""

    if len(plan.steps) != 1:
        return None
    item = _PLAN_DECISIONS.get(plan.steps[0].route)
    if item is None:
        return None
    label, agent_type, _display, reason = item
    mode = ExecutionMode.CLARIFY if label == "clarify" else ExecutionMode.SINGLE
    return {
        "label": label,
        "agent_type": agent_type,
        "execution_mode": mode,
        "reason": reason,
    }


async def _safe_llm_route(message: str, history: str = "") -> dict:
    """Bound the compatibility router so a bad provider cannot freeze chat."""

    timeout = getattr(get_settings(), "chat_route_timeout_seconds", 8.0)
    try:
        return await asyncio.wait_for(_llm_route(message, history), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("unified_chat.route_timeout", timeout=timeout)
    except Exception as exc:
        logger.warning("unified_chat.route_fallback", error=str(exc)[:160])
    return {
        "label": "qa",
        "agent_type": AgentType.QA,
        "execution_mode": ExecutionMode.SINGLE,
        "reason": "路由服务暂时不可用，已转为知识问答",
    }


class UnifiedChatRequest(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    session_id: str = Field("default", min_length=1, max_length=128, description="会话 ID")
    message: str = Field(..., min_length=1, max_length=2000)


@router.post("/stream", dependencies=[Depends(llm_rate_limit)])
async def unified_chat_stream(req: UnifiedChatRequest, current_user: dict = Depends(get_current_user)):
    """统一 AI 助手流式接口（SSE）：前置拦截 → LLM 路由 → 四分支分发"""

    context = build_request_context(current_user, project_id=req.project_id)
    history = await memory_repository.read(context, "qa", req.session_id)

    async def remember(reply: str):
        await memory_repository.append(context, "qa", req.session_id, uuid.uuid4().hex, req.message, reply)

    async def event_generator():
        # Step 0：规则前置拦截
        pre_reply = _pre_filter(req.message)
        if pre_reply is not None:
            await remember(pre_reply)
            yield _sse({"type": "token", "content": pre_reply})
            yield _sse({"type": "done"})
            return

        # Use the same bounded planner as v2 preview. A recognised business
        # request must not be re-routed by a second LLM or execute as ordinary QA.
        try:
            plan = understand_task(req.message, project_id=context.project_id)
        except ValidationFailure as exc:
            yield _sse({"type": "error", "message": str(exc)})
            yield _sse({"type": "done"})
            return
        if plan.mode == "composite" or plan.needs_confirmation or plan.needs_clarification:
            labels = {"knowledge": "知识查询", "bim": "BIM 建模", "bid": "投标审查/查询",
                      "contract": "合同审核", "procurement": "采购分析", "negotiation": "谈判辅助"}
            summary = "已识别需求：" + " → ".join(labels[step.domain] for step in plan.steps)
            summary += "。这是待确认计划，尚未创建或执行业务任务；请在对应面板补齐资料并分别提交。"
            if any(step.domain == "contract" for step in plan.steps):
                summary += "合同正式审核流程尚未接入，当前不能自动执行。"
            # Persist only the preview in QA history, never another domain's
            # history or approval state. Execution continues in its own panel.
            await memory_repository.append(context, "qa", req.session_id, uuid.uuid4().hex,
                                           req.message, summary, {"task_plan": plan.model_dump(mode="json")})
            yield _sse({"type": "task_plan", "plan": plan.model_dump(mode="json"), "message": summary})
            yield _sse({"type": "done"})
            return

        # Send progress only after the read-only plan is ready.  Composite
        # plans keep their stable task_plan-first contract; single questions
        # get immediate feedback before any external dependency is contacted.
        yield _sse({"type": "progress", "stage": "正在理解您的问题..."})

        # Step 1：use the registered plan route.  General/legacy input keeps a
        # compatibility LLM fallback, but it is strictly time-bounded.
        step = plan.steps[0]
        decision = None if step.intent == "general_question" else _decision_from_plan(plan)
        if decision is None:
            decision = await _safe_llm_route(req.message, context_text(history))
        await memory_repository.append(context, "router", req.session_id, uuid.uuid4().hex,
                                       req.message, decision["reason"], {"agent": decision["agent_type"].value})

        # Step 2：推送路由决策卡片
        yield _sse({
            "type": "routing_decision",
            "agent_type": decision["agent_type"].value,
            "agent_display": _AGENT_DISPLAY.get(decision["agent_type"], ""),
            "reason": decision["reason"],
            "execution_mode": decision["execution_mode"].value,
        })

        label = decision["label"]

        # 3a：qa → 流式执行 QA Agent
        if label == "qa":
            async for event in _stream_qa_agent(req, current_user):
                yield event

        # Non-BIM capabilities stay inside the workbench.  Confirmation and
        # domain-specific inputs still go through their existing APIs/ACLs.
        elif label in ("bid_review", "procurement", "negotiation", "bim"):
            guidance = _GUIDANCE.get(decision["agent_type"], {})
            await remember(guidance.get("message", "请前往对应功能页面操作"))
            yield _sse({
                "type": "guidance",
                "message": guidance.get("message", "请前往对应功能页面操作"),
                "action_label": guidance.get("action_label", ""),
                "action_url": guidance.get("action_url", "/dashboard"),
                "capability": guidance.get("capability"),
            })

        # 3c：multi_agent → Pipeline 计划
        elif label == "multi_agent":
            await remember("建议先进行投标文件审查，再进行采购审批；每一步需要单独确认。")
            yield _sse({
                "type": "pipeline_plan",
                "title": "投标准备全链路",
                "intro": "已为您规划「投标准备」，建议按顺序完成：投标审查结果将作为采购审批的参考依据。",
                "steps": [
                    {"step": 1, "agent_type": "bid_review", "label": "投标文件审查",
                     "desc": "提交投标文件，四维并行评审+风险提示", "action_label": "开始投标审查", "action_url": "/qa?capability=bid_review", "capability": "bid_review"},
                    {"step": 2, "agent_type": "procurement", "label": "采购审批",
                     "desc": "提交采购单，规则引擎+LLM 双轨审核", "action_label": "开始采购审批", "action_url": "/qa?capability=procurement", "capability": "procurement"},
                ],
            })

        # 3d：clarify → 追问
        else:
            await remember("请补充您要使用的功能或具体问题。")
            yield _sse({
                "type": "guidance",
                "message": "您的问题我还不太确定应该用哪个功能来帮您，能否描述得更具体一些？例如：是想查建材价格、审查投标文件、提交采购单，还是进行供应商谈判？",
                "action_label": "", "action_url": "",
            })

        yield _sse({"type": "done"})

    return EventSourceResponse(event_generator())


async def _stream_qa_agent(req: UnifiedChatRequest, current_user: dict):
    """在统一入口中流式执行 QA Agent（透传 progress/token/meta/error 事件）"""
    orchestrator = get_orchestrator()
    graph = orchestrator._get_agent_graph(AgentType.QA)

    initial_state = {
        "messages": [HumanMessage(content=req.message)],
        "user_id": current_user["user_id"],
        "tenant_id": current_user["tenant_id"],
        "session_id": req.session_id,
        "project_id": req.project_id,
        "memory_turn_id": uuid.uuid4().hex,
        "original_query": req.message,
    }
    config = build_config(current_user["user_id"], req.session_id, tenant_id=current_user["tenant_id"],
                          project_id=req.project_id, agent="qa")

    _PROGRESS_LABELS = {"classify_query": "理解问题中...", "retrieve": "检索知识库...", "generate": "生成回答中..."}
    _GENERATE_NODES = {"generate", "real_price", "clarify"}   # 三节点均产出最终答案：generate/real_price/clarify 需推送 token 与 meta

    answer_mode = "llm_direct"
    confidence = 0.0
    sources: list[str] = []
    token_sent = False          # 是否已推送过 token（兜底判断用）
    final_answer = ""

    try:
        timeout = getattr(get_settings(), "chat_stream_timeout_seconds", 90.0)
        async with asyncio.timeout(timeout):
            async for event in graph.astream_events(initial_state, config=config, version="v2"):
                evt = event["event"]
                node = event.get("metadata", {}).get("langgraph_node", "")
                if evt == "on_chain_start" and node in _PROGRESS_LABELS:
                    yield _sse({"type": "progress", "stage": _PROGRESS_LABELS[node]})
                elif evt == "on_chat_model_stream" and node in _GENERATE_NODES:
                    chunk = event["data"].get("chunk")
                    if chunk and chunk.content:
                        token_sent = True
                        yield _sse({"type": "token", "content": chunk.content})
                elif evt == "on_chain_end" and node in _GENERATE_NODES:
                    output = event["data"].get("output", {})
                    if isinstance(output, dict):
                        if output.get("answer_mode"):
                            answer_mode = output["answer_mode"]
                        if output.get("sources") is not None:
                            sources = output["sources"]
                        conf = (output.get("structured_output") or {}).get("metrics", {}).get("confidence")
                        if conf is None:
                            conf = (output.get("structured_output") or {}).get("confidence")
                        if conf is not None:
                            confidence = conf
                        answer = output.get("answer", "")
                        if answer:
                            final_answer = answer
                            # 兜底：Mock/非流式路径无 token 事件时，把完整答案作为单个 token 推送
                            if not token_sent:
                                token_sent = True
                                yield _sse({"type": "token", "content": answer})
    except asyncio.TimeoutError:
        logger.warning("unified_chat.qa_timeout", timeout=timeout)
        yield _sse({"type": "error", "message": "知识问答处理超时，请稍后重试或缩小问题范围"})
        return
    except Exception as e:
        logger.error("unified_chat.stream_error", error=str(e), exc_info=True)
        yield _sse({"type": "error", "message": "流式输出异常，请重试"})
        return

    # The outer event generator owns the terminal event.  Emitting ``done``
    # here as well produced two terminal markers for ordinary QA responses;
    # clients that close on the first marker could miss the final metadata.
    yield _sse({"type": "meta", "answer_mode": answer_mode, "confidence": confidence, "sources": sources})
