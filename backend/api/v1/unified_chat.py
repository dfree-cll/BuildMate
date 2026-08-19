"""统一 AI 助手入口
SSE 事件类型：routing_decision / progress / token / guidance / pipeline_plan / meta / done / error
"""
import json
import re
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from langchain_core.messages import HumanMessage

from backend.core.orchestrator import AgentType, ExecutionMode, get_orchestrator
from backend.core.llm_factory import get_llm
from backend.dependencies import get_current_user, llm_rate_limit
from backend.core.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)

_AGENT_DISPLAY = {
    AgentType.QA: "建筑知识问答",
    AgentType.BID_REVIEW: "投标文件审查",
    AgentType.PROCUREMENT: "采购审批",
    AgentType.NEGOTIATION: "供应商谈判",
}

_GUIDANCE = {
    AgentType.BID_REVIEW: {
        "message": "检测到您需要进行投标文件审查。请前往「投标审查」页面提交投标文件，AI 将从商务/技术/资质/报价四个维度并行评审并给出风险提示。",
        "action_label": "前往投标审查",
        "action_url": "#/bid-review",
    },
    AgentType.PROCUREMENT: {
        "message": "检测到您需要进行采购审批。请前往「采购审批」页面提交采购单，AI 将完成规则引擎+LLM 双轨审核，大额采购需人工审批。",
        "action_label": "前往采购审批",
        "action_url": "#/procurement",
    },
    AgentType.NEGOTIATION: {
        "message": "检测到您需要进行供应商谈判/施工交底。请前往「谈判交底」页面，AI 将按多阶段状态机推进谈判流程。",
        "action_label": "前往谈判交底",
        "action_url": "#/negotiation",
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
    "**单 Agent 直达**\n"
    "- 直接输入建筑问题 → 知识问答（RAG 检索建材价格/规范/政策）\n"
    "- 「帮我审查投标文件」 → 四维并行评审 + 风险提示\n"
    "- 「提交采购单」 → 规则引擎 + LLM 双轨审核，大额需人工审批\n"
    "- 「开始供应商谈判」 → 多阶段状态机推进\n\n"
    "**多 Agent 协同**：描述综合需求（如「投标准备」），AI 将串联多个 Agent 协作。"
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


# ── LLM 路由 ─────────────────────────────────────────────────────
_ROUTE_PROMPT = """判断建筑行业用户需求应路由到哪个功能。

可选功能：
- qa          : 建筑知识问答（用户直接提问建材价格/规范/政策/施工方案，不涉及文件上传）
- bid_review  : 投标文件审查（用户提到"投标""标书""审查标书"等）
- procurement : 采购审批（用户提到"采购""下单""审批""买材料"等）
- negotiation : 供应商谈判/施工交底（用户提到"谈判""交底""谈价格"等）
- multi_agent : 综合任务（用户同时提到多个功能，或"投标准备""一条龙"等）
- clarify     : 意图不明确，需要追问

严格按以下 JSON 格式返回，不要有其他内容：
{{"label": "功能名", "reason": "一句话说明判断依据"}}

用户输入：{message}"""

_LABEL_TO_AGENT: dict[str, AgentType] = {
    "qa": AgentType.QA, "bid_review": AgentType.BID_REVIEW,
    "procurement": AgentType.PROCUREMENT, "negotiation": AgentType.NEGOTIATION,
    "multi_agent": AgentType.QA, "clarify": AgentType.QA,
}
_LABEL_TO_MODE: dict[str, ExecutionMode] = {
    "qa": ExecutionMode.SINGLE, "bid_review": ExecutionMode.SINGLE,
    "procurement": ExecutionMode.SINGLE, "negotiation": ExecutionMode.SINGLE,
    "multi_agent": ExecutionMode.PIPELINE, "clarify": ExecutionMode.CLARIFY,
}
_VALID_LABELS = frozenset(_LABEL_TO_AGENT.keys())


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


async def _llm_route(message: str) -> dict:
    """LLM 路由：返回 {label, agent_type, execution_mode, reason}；异常降级 qa"""
    try:
        llm = get_llm("intent", temperature=0)
        resp = await llm.ainvoke([HumanMessage(content=_ROUTE_PROMPT.format(message=message))])
        raw = resp.text.strip() if hasattr(resp, "text") and not callable(resp.text) else str(resp.content)
        parsed = _parse_route_json(raw)
        if not parsed:
            raise ValueError(f"路由输出无法解析: {raw[:80]}")
        label = str(parsed.get("label", "qa")).strip().lower()
        reason = str(parsed.get("reason", "LLM 路由判断"))
        if label not in _VALID_LABELS:
            logger.warning("unified_chat.llm_route_unknown_label", label=label, fallback="qa")
            label = "qa"
    except Exception as e:
        logger.warning("unified_chat.llm_route_failed", error=str(e), fallback="qa")
        label, reason = "qa", "路由异常，降级为知识问答"
    return {
        "label": label,
        "agent_type": _LABEL_TO_AGENT[label],
        "execution_mode": _LABEL_TO_MODE[label],
        "reason": reason,
    }


def _sse(data: dict) -> dict:
    return {"data": json.dumps(data, ensure_ascii=False)}


class UnifiedChatRequest(BaseModel):
    session_id: str = Field("default", description="会话 ID")
    message: str = Field(..., min_length=1, max_length=2000)


@router.post("/stream", dependencies=[Depends(llm_rate_limit)])
async def unified_chat_stream(req: UnifiedChatRequest, current_user: dict = Depends(get_current_user)):
    """统一 AI 助手流式接口（SSE）：前置拦截 → LLM 路由 → 四分支分发"""

    async def event_generator():
        # Step 0：规则前置拦截
        pre_reply = _pre_filter(req.message)
        if pre_reply is not None:
            yield _sse({"type": "token", "content": pre_reply})
            yield _sse({"type": "done"})
            return

        # Step 1：LLM 路由
        decision = await _llm_route(req.message)

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

        # 3b：非 QA 引导跳转
        elif label in ("bid_review", "procurement", "negotiation"):
            guidance = _GUIDANCE.get(decision["agent_type"], {})
            yield _sse({
                "type": "guidance",
                "message": guidance.get("message", "请前往对应功能页面操作"),
                "action_label": guidance.get("action_label", ""),
                "action_url": guidance.get("action_url", "/dashboard"),
            })

        # 3c：multi_agent → Pipeline 计划
        elif label == "multi_agent":
            yield _sse({
                "type": "pipeline_plan",
                "title": "投标准备全链路",
                "intro": "已为您规划「投标准备」，建议按顺序完成：投标审查结果将作为采购审批的参考依据。",
                "steps": [
                    {"step": 1, "agent_type": "bid_review", "label": "投标文件审查",
                     "desc": "提交投标文件，四维并行评审+风险提示", "action_label": "开始投标审查", "action_url": "#/bid-review"},
                    {"step": 2, "agent_type": "procurement", "label": "采购审批",
                     "desc": "提交采购单，规则引擎+LLM 双轨审核", "action_label": "开始采购审批", "action_url": "#/procurement"},
                ],
            })

        # 3d：clarify → 追问
        else:
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
        "original_query": req.message,
    }
    config = {"configurable": {"thread_id": f"user_{current_user['user_id']}_session_{req.session_id}"}}

    _PROGRESS_LABELS = {"classify_query": "理解问题中...", "retrieve": "检索知识库...", "generate": "生成回答中..."}
    _GENERATE_NODES = {"generate", "real_price"}   # real_price 节点同样产出最终答案，需推送 token

    answer_mode = "llm_direct"
    confidence = 0.0
    sources: list[str] = []
    token_sent = False          # 是否已推送过 token（兜底判断用）
    final_answer = ""

    try:
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
    except Exception as e:
        logger.error("unified_chat.stream_error", error=str(e), exc_info=True)
        yield _sse({"type": "error", "message": "流式输出异常，请重试"})
        return

    yield _sse({"type": "meta", "answer_mode": answer_mode, "confidence": confidence, "sources": sources})
    yield _sse({"type": "done"})
