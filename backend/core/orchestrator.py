"""编排器 Orchestrator
- 单 Agent 直达 _run_single_agent（@with_retry 包裹）
- 多 Agent 串联 Pipeline _run_pipeline（前序 structured 注入后序 context）
"""
import enum
from typing import Any, Optional
from pydantic import BaseModel, Field

from backend.core.logger import get_logger
from backend.core.retry import with_retry

logger = get_logger(__name__)


class AgentType(str, enum.Enum):
    QA = "qa"
    BID_REVIEW = "bid_review"
    PROCUREMENT = "procurement"
    NEGOTIATION = "negotiation"


class ExecutionMode(str, enum.Enum):
    SINGLE = "single"
    PIPELINE = "pipeline"
    CLARIFY = "clarify"


class AgentRequest(BaseModel):
    user_id: str
    tenant_id: str = "tenant_default"
    session_id: str = ""
    agent_type: AgentType
    mode: ExecutionMode = ExecutionMode.SINGLE
    input_text: str = ""
    pipeline_name: Optional[str] = None
    extra: dict = Field(default_factory=dict)


class AgentResponse(BaseModel):
    agent_type: str
    content: str = ""
    structured_output: Optional[dict] = None
    fallback_used: bool = False
    metadata: dict = Field(default_factory=dict)


class PipelineResult(BaseModel):
    pipeline_name: str
    results: dict = Field(default_factory=dict)   # {step: AgentResponse}
    final_content: str = ""


class Orchestrator:
    """编排器单例：懒加载 Agent 图，对外只暴露 handle()"""

    def __init__(self):
        self._graphs: dict[str, Any] = {}

    # ── 懒加载 Agent 图 ──────────────────────────────────────────
    def _get_agent_graph(self, agent_type: AgentType):
        if agent_type.value not in self._graphs:
            if agent_type == AgentType.QA:
                from backend.agents.qa.graph import build_qa_graph
                self._graphs["qa"] = build_qa_graph()
            elif agent_type == AgentType.BID_REVIEW:
                from backend.agents.bid_review.graph import build_bid_review_graph
                self._graphs["bid_review"] = build_bid_review_graph()
            elif agent_type == AgentType.PROCUREMENT:
                from backend.agents.procurement.graph import build_procurement_graph
                self._graphs["procurement"] = build_procurement_graph()
            elif agent_type == AgentType.NEGOTIATION:
                from backend.agents.negotiation.graph import build_negotiation_graph
                self._graphs["negotiation"] = build_negotiation_graph()
            logger.info("orchestrator.graph_loaded", agent=agent_type.value)
        return self._graphs[agent_type.value]

    # ── 统一入口 ─────────────────────────────────────────────────
    async def handle(self, request: AgentRequest) -> AgentResponse | PipelineResult:
        if request.mode == ExecutionMode.PIPELINE:
            return await self._run_pipeline(request)
        return await self._run_single_agent(request)

    # ── 单 Agent 直达 ────────────────────────────────────────────
    async def _run_single_agent(self, request: AgentRequest) -> AgentResponse:
        graph = self._get_agent_graph(request.agent_type)
        initial_state = self._build_initial_state(request)

        @with_retry(agent_type=request.agent_type.value)
        async def _invoke():
            return await graph.ainvoke(initial_state, config=self._build_config(request))

        result_state = await _invoke()

        # 从最终 State 提取响应
        response = AgentResponse(agent_type=request.agent_type.value)
        response.content = result_state.get("answer") or result_state.get("content") or ""
        response.structured_output = result_state.get("structured_output")
        response.fallback_used = bool(result_state.get("fallback_used", False))
        response.metadata = {
            "answer_mode": result_state.get("answer_mode", ""),
            "confidence": result_state.get("confidence", 0),
            "sources": result_state.get("sources", []),
        }
        logger.info("orchestrator.single_done", agent=request.agent_type.value)
        return response

    # ── 多 Agent 串联 Pipeline ───────────────────────────────────
    async def _run_pipeline(self, request: AgentRequest) -> PipelineResult:
        pipeline_name = request.pipeline_name or "bid_preparation"
        steps = self._get_pipeline_steps(pipeline_name)
        results: dict[str, AgentResponse] = {}

        # 累积上下文：前序 Agent 的 structured 结果注入后序
        accumulated_context = dict(request.extra)
        for idx, (step_name, agent_type) in enumerate(steps):
            step_request = request.model_copy(deep=True)
            step_request.agent_type = agent_type
            # ★ 每步独立 session_id，防止检查点串台
            step_request.session_id = f"{request.session_id}_step{idx + 1}"
            step_request.extra = dict(accumulated_context)
            # ★ 采购步骤补必填字段（Pipeline 场景无表单提交，用上下文合理默认防 KeyError）
            if agent_type == AgentType.PROCUREMENT:
                step_request.extra.setdefault("material_name",
                                              accumulated_context.get("material_name", "螺纹钢 HRB400"))
                step_request.extra.setdefault("quantity", accumulated_context.get("quantity", 1))
                step_request.extra.setdefault("unit_price", accumulated_context.get("unit_price", 3600.0))
                step_request.extra.setdefault("total_amount",
                                              accumulated_context.get("total_amount",
                                                                      step_request.extra["quantity"] * step_request.extra["unit_price"]))
            try:
                resp = await self._run_single_agent(step_request)
                results[f"step{idx + 1}"] = resp
                # ★ 前序结构化输出注入累积上下文（{agent_type}_result 键）
                if resp.structured_output:
                    accumulated_context[f"{agent_type.value}_result"] = resp.structured_output
                    logger.info("orchestrator.pipeline_context_passed",
                                from_agent=agent_type.value,
                                keys=list(resp.structured_output.keys()))
                if not resp.content and resp.fallback_used:
                    break
            except Exception as e:
                logger.error("orchestrator.pipeline_step_failed", step=step_name, error=str(e))
                results[f"step{idx + 1}"] = AgentResponse(
                    agent_type=agent_type.value, content=f"步骤 {step_name} 执行失败：{e}"
                )
                break

        final_content = "\n\n".join(
            r.content for r in results.values() if r.content
        )
        return PipelineResult(pipeline_name=pipeline_name, results=results, final_content=final_content)

    # ── 预置 Pipeline ────────────────────────────────────────────
    def _get_pipeline_steps(self, name: str) -> list[tuple[str, AgentType]]:
        if name == "bid_preparation":
            return [("投标文件审查", AgentType.BID_REVIEW), ("采购审批", AgentType.PROCUREMENT)]
        if name == "price_negotiation":
            return [("建材价格问答", AgentType.QA), ("供应商谈判", AgentType.NEGOTIATION)]
        return [("投标文件审查", AgentType.BID_REVIEW)]

    # ── State / config 构造 ─────────────────────────────────────
    def _build_initial_state(self, request: AgentRequest) -> dict:
        state = {
            "user_id": request.user_id,
            "tenant_id": request.tenant_id,
            "session_id": request.session_id,
            "messages": [],
            "original_query": request.input_text,
            "input_text": request.input_text,
            "extra": request.extra,
        }
        # ★ 采购/投标等需要业务字段：extra 平铺进 state（Pipeline 场景必备）
        for k, v in (request.extra or {}).items():
            if k not in state:
                state[k] = v
        return state

    def _build_config(self, request: AgentRequest) -> dict:
        return {"configurable": {"thread_id": f"user_{request.user_id}_session_{request.session_id}"}}


_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
