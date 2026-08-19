"""LLM Factory
- 有 API Key：走真实 OpenAI 兼容接口（ChatOpenAI）
- 无 API Key：Mock 模式，返回 MockChatModel（本地规则模板），保证 demo 全链路离线可跑
"""
import json
from typing import Type, Any, Optional
from pydantic import BaseModel
from langchain_openai import ChatOpenAI
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

_AGENT_MODEL_ROUTING: dict[str, str] = {
    "qa": "deepseek-chat",
    "bid_review": "deepseek-chat",
    "procurement": "deepseek-chat",
    "negotiation": "deepseek-chat",
    "intent": "deepseek-chat",
    "summarize": "deepseek-chat",
}


# ── Provider 注册表（可插拔）──────────────────────────────────────────────
# 第三方可注入自定义 Provider（Azure/ollama/私有网关…）：
#   from backend.core.llm_factory import register_llm_provider
#   register_llm_provider("myprovider", factory)   # factory(agent_type, temperature, streaming) -> BaseChatModel
#   并设环境变量 LLM_PROVIDER=myprovider
# 内置：openai（OpenAI 兼容 API）、mock（离线规则模板）
ProviderFactory = Any  # Callable[[str, float, bool], BaseChatModel]

_PROVIDER_REGISTRY: dict[str, ProviderFactory] = {}


def register_llm_provider(name: str, factory: ProviderFactory) -> None:
    _PROVIDER_REGISTRY[name] = factory


def _openai_compatible_factory(agent_type: str, temperature: float, streaming: bool) -> BaseChatModel:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=temperature,
        streaming=streaming,
        max_retries=0,
    )


def _mock_factory(agent_type: str, temperature: float, streaming: bool) -> BaseChatModel:
    return MockChatModel(temperature=temperature)


register_llm_provider("openai", _openai_compatible_factory)
register_llm_provider("mock", _mock_factory)


def _resolve_provider_name() -> str:
    """provider 选择：LLM_PROVIDER 显式指定 > 自动（无 key → mock，有 key → openai）"""
    configured = get_settings().llm_provider.strip().lower()
    if configured and configured != "auto":
        if configured not in _PROVIDER_REGISTRY:
            raise ValueError(
                f"未知 LLM_PROVIDER '{configured}'，可用：{sorted(_PROVIDER_REGISTRY)}；"
                f"如需自定义请先 register_llm_provider()")
        return configured
    return "mock" if get_settings().mock_mode else "openai"


class MockChatModel(BaseChatModel):
    """离线 Mock：不调 API，按提示词类型返回规则化结果，让全链路可跑可测。
    识别三类提示词：意图路由（返回 JSON label）、检索策略（返回 PRECISE）、其余（模板回复）
    """
    model_name: str = "mock-chat"
    temperature: float = 0
    api_key: Optional[str] = None
    base_url: Optional[str] = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatResult, ChatGeneration
        text = "\n".join(
            m.content for m in messages
            if isinstance(m, (HumanMessage, SystemMessage)) and isinstance(m.content, str)
        )
        content = self._mock_reply(text)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """Mock 流式：把完整回复按 4 字符切块逐块 yield，模拟真实 LLM 打字效果。
        这样 astream_events 会产生 on_chat_model_stream 事件，SSE 前端能收到 token。
        """
        from langchain_core.outputs import ChatGenerationChunk
        from langchain_core.messages import AIMessageChunk
        text = "\n".join(
            m.content for m in messages
            if isinstance(m, (HumanMessage, SystemMessage)) and isinstance(m.content, str)
        )
        content = self._mock_reply(text)
        # 逐块输出（模拟流式；每块 4 字符）
        for i in range(0, len(content), 4):
            chunk_text = content[i:i + 4]
            if run_manager:
                run_manager.on_llm_new_token(chunk_text)
            yield ChatGenerationChunk(message=AIMessageChunk(content=chunk_text))

    def _mock_reply(self, text: str) -> str:
        # ① 意图路由提示词（统一入口 _llm_route）
        if "路由到哪个功能" in text or "判断用户需求应路由" in text:
            return self._mock_route(text)
        # ② RAG 检索策略判断
        if "检索策略" in text and "PRECISE" in text:
            return "PRECISE"
        # ③ 投标文件结构化提取
        if "投标文件文本" in text and "project_name" in text:
            return '{"project_name": "市政道路工程", "bidder": "XX建设集团", "bid_amount": 8500.0, "technical_solution": "施工组织设计+路基路面工艺", "qualifications": "市政一级", "bid_documents": ["商务标", "技术标"]}'
        # ④ 四维评审
        if "评审要求" in text or "投标文件评审专家" in text:
            dim = "商务" if "商务" in text else ("技术" if "技术" in text else ("资质" if "资质" in text else "合规"))
            score = 82 if dim == "商务" else (78 if dim == "技术" else (75 if dim == "资质" else 70))
            return json.dumps({"score": score,
                               "issues": [f"{dim}条款完整但细节不足", "缺少量化支撑数据"],
                               "suggestions": [f"补充{dim}证明材料", "细化关键参数"]}, ensure_ascii=False)
        # ⑤ 整体评价
        if "整体评审结论" in text or "综合评语" in text:
            return '{"overall_comment": "投标文件总体完整，技术方案可行，存在少量商务细节需完善。", "risk_level": "medium", "recommendation": "谨慎投标"}'
        # ⑥ 采购 LLM 审查
        if "采购审核专家" in text:
            amount_ok = "总金额：100000" not in text and "3600" not in text
            return json.dumps({"compliance_issues": ["价格合理性需复核"] if not amount_ok else [],
                               "reasonableness": "价格符合市场行情" if amount_ok else "单价偏高，需核查",
                               "suggestion": "建议人工复核后放行" if not amount_ok else "可放行",
                               "verdict": "pass" if amount_ok else "review",
                               "reason": "规则引擎判定"}, ensure_ascii=False)
        # ⑥b BIM 模型合规审查（bim_review.py）
        if "BIM 模型合规审查专家" in text:
            return json.dumps({
                "risk_level": "medium",
                "observations": ["（Mock）模型构件信息较简单，建议补充属性定义与空间划分"],
                "suggestions": ["补充材料/尺寸属性集", "定义 IFCSPACE 空间", "构件统一命名规范"],
                "verdict": "review",
                "summary": "（Mock）BIM 模型基本可读，信息深度有待补充",
            }, ensure_ascii=False)
        # ⑦ 审批文案
        if "最终批复" in text or "审批结果说明" in text:
            return "采购单已完成审批流程，AI 双轨审核结论已确认，最终决定已记录并留痕。"
        # ⑧ 谈判回应
        if "供应商谈判助手" in text or "当前阶段" in text:
            stage = "报价阶段" if "报价" in text else ("技术方案" if "技术" in text else ("交付条件" if "交付" in text else "签约阶段"))
            return f"（Mock 谈判回应）作为采购方代表，我们关注{stage}的关键条款。请问贵方对此有何具体方案？我们可以就付款方式与质保期进行协商。"
        # ⑨ 谈判阶段判断（未用，保留）
        if "判断当前谈判是否满足" in text:
            return '{"ready": true, "reason": "对话轮次已足够"}'
        # ⑩ 知识问答生成（RAG / 直答）
        if "知识库参考内容" in text or "基于以下知识库" in text:
            return self._mock_rag_answer(text)
        if "根据你的知识回答" in text or "通用知识回答" in text:
            return self._mock_direct_answer(text)
        if "当前时间" in text and "直接回答" in text:
            return "您好！我是 BuildMate 建筑行业智能助手，可以帮您查询建材价格、规范条文、招投标政策等建筑行业知识。"
        # ⑪ 通用兜底
        return f"[Mock 回复] 已收到您的消息（{len(text)} 字）：{text[:60]}……（Demo 未配置 LLM_API_KEY，启用 Mock 模式）"

    def _mock_route(self, text: str) -> str:
        # 提取用户输入部分（路由 prompt 中 "user input:" 之后的内容）
        if "用户输入：" in text:
            q = text.split("用户输入：")[-1].strip()
        else:
            q = text
        # 优先级：multi_agent > bid_review > procurement > negotiation > qa > clarify
        if "一条龙" in q or "投标准备" in q or ("综合" in q and "准备" in q):
            label, reason = "multi_agent", "用户提到多步骤综合任务"
        elif "投标" in q or "标书" in q or "审查投标" in q:
            label, reason = "bid_review", "用户提到投标文件审查"
        elif "采购" in q or "下单" in q or "审批" in q or "采购单" in q:
            label, reason = "procurement", "用户提到采购/下单/审批"
        elif "谈判" in q or "交底" in q or "谈价格" in q:
            label, reason = "negotiation", "用户提到谈判/交底"
        elif "问答" in q or "规范" in q or "价格" in q or "多少钱" in q or "怎么" in q or "?" in q or "？" in q:
            label, reason = "qa", "用户直接提问建筑知识"
        elif len(q) < 8:
            label, reason = "clarify", "输入过于简短，意图不明"
        else:
            label, reason = "qa", "默认按知识问答处理"
        return json.dumps({"label": label, "reason": reason}, ensure_ascii=False)

    @property
    def _llm_type(self) -> str:
        return "mock-chat"


    def _mock_rag_answer(self, text: str) -> str:
        """RAG Mock：从 prompt 中提取【知识库参考内容】的检索结果并回显，
        使 Mock 回答与真实检索一致（问规范回规范、问价格回价格）"""
        # 提取知识库参考内容（RAG_ANSWER_PROMPT 的 {context} 部分）
        ctx = ""
        if "知识库参考内容" in text:
            after = text.split("知识库参考内容")[-1].lstrip("】、: ：\n\r")
            # 参考内容到【用户问题】之间
            for sep in ["【用户问题】", "用户问题："]:
                if sep in after:
                    ctx = after.split(sep)[0].strip()
                    break
            if not ctx:
                ctx = after.strip()
        elif "基于以下知识库" in text:
            ctx = text.split("基于以下知识库")[-1].strip()
        # 提取用户问题
        query = ""
        if "用户问题：" in text:
            query = text.split("用户问题：")[-1].strip().split("\n")[0]
        # 从参考内容中挑最相关的一段（取第一条参考）
        lines = [l.strip() for l in ctx.split("\n") if l.strip() and not l.startswith("【")]
        if not lines:
            lines = [ctx.strip()]
        answer = "".join(lines[:8]) if lines else ctx[:200]
        # 提取来源（【参考N】前缀）
        sources = []
        for l in ctx.split("\n"):
            if l.strip().startswith("["):
                src = l.strip().split("]")[0] + "]"
                if src not in sources:
                    sources.append(src)
        src_text = "\n".join(f"  • {s}" for s in sources[:3]) if sources else ""
        result = f"根据知识库资料：{answer}"
        if src_text:
                        result += "\n\n" + chr(0x1F4DA) + " **参考来源**\n" + src_text
        return result

    def _mock_direct_answer(self, text: str) -> str:
        """直答 Mock：提取用户问题并给出模板化回应"""
        query = ""
        if "用户问题" in text:
            # 兼容【用户问题】与 用户问题： 两种格式，去掉残留的 】等符号
            q = text.split("用户问题")[-1].lstrip("【】:： \n\r\t").split("\n")[0].strip()
            if q:
                query = q[:50]
        if query:
            return f"对不起，知识库中暂无「{query}」相关内容。这是 Mock 模式的通用回复，配置 LLM_API_KEY 后将由真实模型回答。"
        return "知识库中暂无相关内容，请尝试其他问法。"

class LLMFactory:
    _instances: dict[str, BaseChatModel] = {}

    @classmethod
    def get_llm(cls, agent_type: str, temperature: float = 0, streaming: bool = False) -> BaseChatModel:
        if agent_type not in _AGENT_MODEL_ROUTING:
            raise ValueError(f"未知 agent_type: '{agent_type}'，可用：{list(_AGENT_MODEL_ROUTING.keys())}")
        cache_key = f"{agent_type}_{temperature}_{streaming}"
        if cache_key not in cls._instances:
            provider = _resolve_provider_name()
            llm = _PROVIDER_REGISTRY[provider](agent_type, temperature, streaming)
            logger.info("llm_factory.provider_resolved", agent_type=agent_type,
                        provider=provider, model=get_settings().llm_model)
            cls._instances[cache_key] = llm
        return cls._instances[cache_key]

    @classmethod
    def get_tracked_llm(cls, agent_type: str, temperature: float = 0, streaming: bool = False):
        """获取带调用追踪的 LLM（记录耗时/字符/成本到 observability）
        M10：包装类实现 __getattr__ 代理——旧的 SimpleNamespace 会丢掉
        with_structured_output / bind_tools 等能力"""
        llm = cls.get_llm(agent_type, temperature=temperature, streaming=streaming)

        class _TrackedLLM:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            async def ainvoke(self, messages, **kwargs):
                import time as _time
                from backend.core.observability import record_llm_call
                from backend.config import get_settings as _gs
                start = _time.time()
                input_chars = sum(len(getattr(m, "content", "") or "") for m in messages)
                try:
                    resp = await self._wrapped.ainvoke(messages, **kwargs)
                    output_chars = len(getattr(resp, "content", "") or "")
                    await record_llm_call(agent_type, _gs().llm_model, (_time.time() - start) * 1000,
                                          input_chars, output_chars, ok=True)
                    return resp
                except Exception as e:
                    await record_llm_call(agent_type, _gs().llm_model, (_time.time() - start) * 1000,
                                          input_chars, 0, ok=False, error=str(e))
                    raise

            async def astream(self, messages, **kwargs):
                import time as _time
                from backend.core.observability import record_llm_call
                from backend.config import get_settings as _gs
                start = _time.time()
                input_chars = sum(len(getattr(m, "content", "") or "") for m in messages)
                chunks = []
                try:
                    async for chunk in self._wrapped.astream(messages, **kwargs):
                        chunks.append(getattr(chunk, "content", "") or "")
                        yield chunk
                    await record_llm_call(agent_type, _gs().llm_model, (_time.time() - start) * 1000,
                                          input_chars, sum(len(c) for c in chunks), ok=True)
                except Exception as e:
                    await record_llm_call(agent_type, _gs().llm_model, (_time.time() - start) * 1000,
                                          input_chars, sum(len(c) for c in chunks), ok=False, error=str(e))
                    raise

            def __getattr__(self, name):
                # 其余能力（with_structured_output/bind_tools/属性）透传给被包装的 LLM
                if name == "_wrapped":   # 防 __init__ 前访问导致无限递归
                    raise AttributeError(name)
                return getattr(self._wrapped, name)

        return _TrackedLLM(llm)

    @classmethod
    def get_structured_llm(cls, agent_type: str, output_schema: Type[BaseModel], temperature: float = 0) -> Any:
        llm = cls.get_llm(agent_type, temperature=temperature)
        try:
            return llm.with_structured_output(output_schema, method="function_calling")
        except NotImplementedError:
            return llm

    @classmethod
    def clear_cache(cls) -> None:
        cls._instances.clear()


def get_llm(agent_type: str, temperature: float = 0, streaming: bool = False):
    """获取带 LLM 调用追踪的模型（自动记录耗时/字符/成本到 observability）"""
    return LLMFactory.get_tracked_llm(agent_type, temperature=temperature, streaming=streaming)


def get_structured_llm(agent_type: str, output_schema: Type[BaseModel]) -> Any:
    return LLMFactory.get_structured_llm(agent_type, output_schema)
