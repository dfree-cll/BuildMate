"""知识问答 Agent 节点
classify_query（规则+LLM 判 PRECISE/VAGUE/BROAD/GENERAL）
→ retrieve（本地向量检索）
→ generate（RAG 或直答）
"""
import asyncio
import hashlib
import uuid
from datetime import datetime
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from backend.agents.qa.state import QAState
from backend.agents.qa.prompts import (
    SYSTEM_PROMPT, RAG_ANSWER_PROMPT, DIRECT_ANSWER_PROMPT,
    GENERAL_ANSWER_PROMPT, RAG_STRATEGY_PROMPT, HYDE_PROMPT, MULTI_QUERY_REWRITE_PROMPT,
    REAL_PRICE_ANSWER_PROMPT,
)
from backend.core.llm_factory import get_llm
from backend.core.llm_text import msg_text as _get_message_content
from backend.core.logger import get_logger
from backend.config import get_settings

logger = get_logger(__name__)


async def _invoke_llm(messages, *, temperature: float = 0, streaming: bool = False):
    """Run an optional model call with one shared, bounded timeout."""

    timeout = getattr(get_settings(), "llm_request_timeout_seconds", 30.0)
    return await asyncio.wait_for(
        get_llm("qa", temperature=temperature, streaming=streaming).ainvoke(messages),
        timeout=timeout,
    )


async def _knowledge_search(state: QAState, query: str, top_k: int) -> tuple[list[dict], str]:
    """Use the v2 evidence-first RAG service while preserving QA state shape."""
    import uuid

    from backend.domain.contracts import RequestContext
    from backend.rag.contracts import KnowledgeScope, KnowledgeSearchRequest
    from backend.rag.service import get_rag_service

    tenant_id = state.get("tenant_id", "tenant_default")
    project_id = state.get("project_id")
    trace_id = uuid.uuid4().hex
    context = RequestContext(
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=str(state.get("user_id") or state.get("student_id") or "qa-agent"),
        role=str(state.get("role") or "user"),
        trace_id=trace_id,
        correlation_id=str(state.get("session_id") or trace_id),
    )
    from backend.application.agent_memory import contextual_query
    request = KnowledgeSearchRequest(
        query=contextual_query(query, state.get("memory_questions", [])),
        tenant_id=tenant_id,
        project_id=project_id,
        scope=KnowledgeScope.PROJECT if project_id else KnowledgeScope.TENANT,
        top_k=top_k,
    )
    hits, retrieval_run_id = await get_rag_service().search(context, request)
    return [
        {
            "content": hit.content,
            "score": hit.score,
            "dense_score": hit.dense_score,
            "metadata": {
                **hit.metadata,
                "source_name": hit.source_name,
                "doc_id": hit.document_id,
                "chunk_id": hit.chunk_id,
                "page_no": hit.page_no,
            },
        }
        for hit in hits
    ], retrieval_run_id

_GENERAL_KEYWORDS = ("你好", "谢谢", "你是谁", "你能做什么", "再见", "今天天气", "今天是几号", "现在几点", "讲个笑话")
_VAGUE_HINTS = ("没懂", "不懂", "不太懂", "讲讲", "解释一下", "啥意思", "什么意思",
                "不了解", "不清楚", "不明白", "啥是", "什么是", "给我讲讲", "介绍一下",
                "科普", "原理", "机制", "怎么理解", "聊聊")
_BROAD_HINTS = ("全面", "系统", "总结", "梳理", "对比", "区别", "有哪些", "全景",
                "介绍", "概述", "整体", "框架", "体系", "全貌", "所有", "全部",
                "汇总", "盘点", "要点", "目录")


def _extract_query(state: QAState) -> str:
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, HumanMessage):
            return _get_message_content(msg)
    return state.get("original_query", "")


async def _llm_strategy(query: str) -> str:
    try:
        resp = await _invoke_llm(
            [HumanMessage(content=RAG_STRATEGY_PROMPT.format(query=query))]
        )
        label = _get_message_content(resp).strip().upper()
        return label if label in ("PRECISE", "VAGUE", "BROAD") else "PRECISE"
    except Exception as e:
        logger.warning("qa.strategy_failed", error=str(e))
        return "PRECISE"


async def classify_query_node(state: QAState) -> dict:
    """三层分类：规则 GENERAL → 规则 VAGUE/BROAD → LLM 精判"""
    query = _extract_query(state).strip()
    base = {"original_query": query, "rewritten_queries": [], "hyde_document": None}

    q = query.lower()
    if any(kw in q for kw in _GENERAL_KEYWORDS):
        return {**base, "query_type": "GENERAL"}

    # 规则快判
    if len(q) <= 6 and any(h in q for h in _VAGUE_HINTS):
        return {**base, "query_type": "VAGUE"}
    if any(h in q for h in _BROAD_HINTS):
        return {**base, "query_type": "BROAD"}

    # 其余问题默认按精确查询处理。检索本身已经包含 dense + BM25 +
    # reranker，额外调用一次 LLM 只为选择 PRECISE/VAGUE/BROAD 会让每次
    # 发送多出一个网络往返，并且在模型不可用时把发送按钮卡在“理解问题”。
    # 保留 _llm_strategy 供离线实验/显式调用，但不让它成为主链路依赖。
    logger.info("qa.classified", query=query[:40], strategy="PRECISE", source="deterministic")
    return {**base, "query_type": "PRECISE"}


async def _hyde_generate(state: QAState) -> str:
    query = state["original_query"]
    history = "\n".join(_get_message_content(m)[:100] for m in state.get("messages", [])[-6:] if isinstance(m, HumanMessage))
    try:
        resp = await _invoke_llm(
            [HumanMessage(content=HYDE_PROMPT.format(history=history or "（无）", query=query))],
            temperature=0.3,
        )
        return _get_message_content(resp).strip() or query
    except Exception as exc:
        logger.warning("qa.hyde_fallback", error=str(exc)[:160])
        return query


async def _multi_query(state: QAState) -> list[str]:
    query = state["original_query"]
    try:
        resp = await _invoke_llm(
            [HumanMessage(content=MULTI_QUERY_REWRITE_PROMPT.format(last_answer="（无）", query=query))],
            temperature=0.3,
        )
        raw = _get_message_content(resp).strip()
    except Exception as exc:
        logger.warning("qa.multi_query_fallback", error=str(exc)[:160])
        return [query]
    queries = [l.lstrip("0123456789.-）)、 ").strip() for l in raw.split("\n") if l.strip() and len(l.strip()) > 3]
    return queries[:3] or [query]


# ═══════════════ 真实价格直答节点（接入 material_prices 表真实行情）═══════════════
_PRICE_KEYWORDS = ("多少钱", "价格", "行情", "报价", "每吨", "一吨", "每方", "一方", "单价", "时价", "今日价", "价格走势", "价格多少")
_PRICE_MATERIALS = ("螺纹钢", "钢筋", "钢材", "水泥", "混凝土", "商砼", "砂石", "河砂", "机制砂", "碎石",
                    "热卷", "线材", "铁矿石", "铜", "矿渣粉", "铝", "玻璃", "防水卷材", "保温板", "加气块",
                    "角钢", "槽钢", "工字钢", "H型钢", "钢管", "脚手架")
# 口语/规格变体 → 标准物料名（正则优先匹配，避免 '425' 误中 '4250' 类数字）
_MATERIAL_SYNONYMS = [
    (r"HRB400E?|三级钢|二级钢|四级钢|盘螺", "螺纹钢"),
    (r"高线|盘条|盘圆", "线材"),
    (r"P\.?O\s*42\.?5|42\.?5\s*水泥", "水泥"),
    (r"C3[05]|C25|C40", "混凝土"),
    (r"铝锭|铝合金|铝材", "铝"),
    (r"河沙", "河砂"), (r"机制沙", "机制砂"), (r"石子|碎石", "碎石"),
    (r"钢化玻璃|中空玻璃", "玻璃"),
]


def _has_price_intent(query: str) -> bool:
    """检测是否为价格类问题（含价格词即可，材料可缺——缺则走澄清）"""
    q = query.lower()
    return any(k in q for k in _PRICE_KEYWORDS)


def _is_price_query(query: str) -> bool:
    """完整价格查询：价格词 + 材料词"""
    q = query.lower()
    has_price_kw = any(k in q for k in _PRICE_KEYWORDS)
    has_material = any(m in q for m in _PRICE_MATERIALS) or _match_synonym(query) is not None
    return has_price_kw and has_material


def _match_synonym(query: str) -> str | None:
    """同义词/规格变体 → 标准物料名（正则，带边界）"""
    import re
    for pattern, standard in _MATERIAL_SYNONYMS:
        if re.search(pattern, query):
            return standard
    return None


def _extract_material(query: str) -> str:
    """从问题中提取材料名：先同义词归一（'三级钢'→螺纹钢），再词典匹配"""
    alias = _match_synonym(query)
    if alias:
        return alias
    for m in _PRICE_MATERIALS:
        if m in query:
            return m
    return ""


def _extract_market(query: str) -> str:
    """提取城市/市场"""
    for city in ("西安", "上海", "北京", "广州", "深圳", "杭州", "南京", "合肥", "郑州",
                 "成都", "重庆", "武汉", "长沙", "济南", "沈阳", "天津", "昆明", "贵阳",
                 "南宁", "太原", "石家庄", "哈尔滨", "长春", "兰州", "乌鲁木齐", "福州",
                 "厦门", "南昌", "青岛", "大连"):
        if city in query:
            return city
    return ""


def _build_clarify(query: str) -> str | None:
    """价格意图但缺关键实体 → 澄清问题；实体齐全返回 None"""
    material = _extract_material(query)
    if not material:
        return "您要查哪种材料的价格呢？（如：螺纹钢、水泥、混凝土、砂石…）"
    if not _extract_market(query):
        return f"您要查哪个城市的{material}价格呢？（如：西安、上海、北京…）"
    return None


async def clarify_node(state: QAState) -> dict:
    """澄清节点：价格问题缺材料/城市 → 反问澄清（不查库、不入待补）"""
    query = state.get("original_query", "")
    question = _build_clarify(query) or "请补充材料或城市信息，我才能帮您查价格。"
    logger.info("qa.clarify", query=query[:40], question=question[:30])
    return {
        "answer": question,
        "sources": [],
        "answer_mode": "clarify",
        "messages": [AIMessage(content=question)],
        "ranked_chunks": [],
        "confidence": 1.0,  # 澄清不算低置信度，不入知识待补
        "structured_output": {
            "summary": question, "verdict": "pass", "risk_level": "low",
            "issues": [],
            "metrics": {"answer_mode": "clarify", "confidence": 1.0},
            "sources": [],
            "meta": {"fallback_used": False},
        },
    }


async def real_price_node(state: QAState) -> dict:
    """价格类问题：优先查真实行情表（material_prices），命中则直答，未命中回退正常检索"""
    from backend.core.material_prices import query_prices, format_price_text
    query = state["original_query"]
    material = _extract_material(query)
    market = _extract_market(query)

    rows = await query_prices(material) if material else []
    if not rows:
        logger.info("qa.real_price_miss", query=query[:40], material=material)
        return {}  # 未命中 → 正常走 retrieve

    # 优先展示与用户城市匹配的行；无城市现货时回退全部（如期货主连价仍为有效市场参考）
    if market:
        matched = [r for r in rows if market in r["market"]]
        if matched:
            rows = matched
        else:
            logger.info("qa.real_price_no_city", query=query[:40], material=material, market=market)
    price_text = format_price_text(rows[:4])

    llm_fallback = False
    try:
        resp = await _invoke_llm(
            [SystemMessage(content=SYSTEM_PROMPT),
             HumanMessage(content=REAL_PRICE_ANSWER_PROMPT.format(
                 price_context=price_text, query=query))],
            streaming=True,
        )
        answer = _get_message_content(resp).strip()
    except Exception as exc:
        # Prices are already deterministic structured facts.  If the prose
        # model is unavailable, return the table values with an explicit
        # fallback marker instead of failing the whole send operation.
        logger.warning("qa.real_price_llm_fallback", error=str(exc)[:160])
        llm_fallback = True
        answer = "根据实时行情表查询到：\n" + price_text
    answer += "\n\n📈 **实时行情来源**\n" + "\n".join(
        f"  • {r['source']}（{r['price_date']}）" for r in rows[:3])

    logger.info("qa.real_price_hit", query=query[:40], material=material, market=market or "全国", rows=len(rows))
    return {
        "answer": answer,
        "sources": [r["source"] for r in rows[:3]],
        "answer_mode": "real_price",
        "messages": [AIMessage(content=answer)],
        "ranked_chunks": [],
        "confidence": 0.95,
        "structured_output": {
            "summary": answer, "verdict": "pass", "risk_level": "low",
            "issues": [],
            "metrics": {"answer_mode": "real_price", "confidence": 0.95,
                        "material": material, "market": market or "全国"},
            "sources": [r["source"] for r in rows[:3]],
            "meta": {"fallback_used": llm_fallback},
        },
    }


async def retrieve_node(state: QAState) -> dict:
    """检索：VAGUE 用 HyDE 文档 / BROAD 多 Query 合并 / PRECISE 直检"""
    query_type = state.get("query_type", "PRECISE").upper()
    tenant_id = state.get("tenant_id", "tenant_default")
    query = state["original_query"]

    if query_type == "VAGUE" and not state.get("hyde_document"):
        hyde = await _hyde_generate(state)
        state["hyde_document"] = hyde
        query = hyde
    elif query_type == "BROAD":
        queries = state.get("rewritten_queries") or await _multi_query(state)
        all_chunks = []
        retrieval_run_ids = []
        for q in queries:
            results, run_id = await _knowledge_search(state, q, top_k=4)
            all_chunks.extend(results)
            retrieval_run_ids.append(run_id)
        # 去重
        seen, merged = set(), []
        for c in all_chunks:
            key = c["content"][:80]
            if key not in seen:
                seen.add(key)
                merged.append(c)
        merged.sort(key=lambda x: x["score"], reverse=True)
        ranked = merged[:3]
        confidence = ranked[0]["score"] if ranked else 0.0
        return {"ranked_chunks": ranked, "confidence": confidence,
                "rewritten_queries": queries,
                "retrieval_run_id": retrieval_run_ids[-1] if retrieval_run_ids else None}

    ranked, retrieval_run_id = await _knowledge_search(state, query, top_k=3)
    confidence = ranked[0]["score"] if ranked else 0.0
    logger.info("qa.retrieved+reranked", query_type=query_type,
                reranked=len(ranked), confidence=round(confidence, 4),
                retrieval_run_id=retrieval_run_id)
    return {"ranked_chunks": ranked, "confidence": confidence,
            "retrieval_run_id": retrieval_run_id}


async def generate_node(state: QAState) -> dict:
    """生成：有检索结果且分数够 → RAG；否则 LLM 直答"""
    query = state["original_query"]
    query_type = state.get("query_type", "PRECISE").upper()
    chunks = state.get("ranked_chunks", [])
    messages = state.get("messages", [])

    llm_messages = [SystemMessage(content=SYSTEM_PROMPT)]
    from backend.application.agent_memory import memory_prompt
    if state.get("memory_context") or state.get("existing_summary"):
        llm_messages.append(HumanMessage(content=memory_prompt(state, "仅用于理解本次问题；引用必须来自本次召回资料。")))
    for msg in messages[-6:-1]:
        if not isinstance(msg, SystemMessage):
            llm_messages.append(msg)

    answer_mode = "llm_direct"
    llm_fallback = False
    sources: list[str] = []
    evidence_refs: list[dict] = []
    top_score = chunks[0]["score"] if chunks else 0.0

    if chunks and (top_score >= 0.6 or (chunks[0].get("dense_score", 0) or 0) >= 0.6):
        # 高置信度 RAG：严格基于知识库回答
        context = "\n\n".join(
            f"【参考{i + 1}｜chunk_id={c.get('metadata', {}).get('chunk_id', '')}】\n{c['content']}"
            for i, c in enumerate(chunks)
        )
        sources = [c["metadata"].get("source_name", "") for c in chunks if c["metadata"].get("source_name")]
        evidence_refs = [
            {
                "chunk_id": c["metadata"].get("chunk_id"),
                "document_id": c["metadata"].get("doc_id"),
                "source_name": c["metadata"].get("source_name", ""),
                "page_no": c["metadata"].get("page_no"),
            }
            for c in chunks if c.get("metadata", {}).get("chunk_id")
        ]
        llm_messages.append(HumanMessage(content=RAG_ANSWER_PROMPT.format(context=context, query=query)))
        answer_mode = "rag"
    elif chunks and top_score >= 0.4:
        # 混合模式：知识库内容作为参考，真实 LLM 组织回答（解决"螺纹钢 0.578"等边界命中）
        context = "\n\n".join(f"【知识库参考{i + 1}】\n{c['content']}" for i, c in enumerate(chunks[:3]))
        sources = [c["metadata"].get("source_name", "") for c in chunks if c["metadata"].get("source_name")]
        evidence_refs = [
            {
                "chunk_id": c["metadata"].get("chunk_id"),
                "document_id": c["metadata"].get("doc_id"),
                "source_name": c["metadata"].get("source_name", ""),
                "page_no": c["metadata"].get("page_no"),
            }
            for c in chunks[:3] if c.get("metadata", {}).get("chunk_id")
        ]
        llm_messages.append(HumanMessage(content=(
            "你是建筑行业智能助手。请回答用户问题。\n\n"
            "以下是从建筑知识库检索到的参考资料，请优先使用其中的数据（价格/规范/流程），"
            "如果资料充分则直接回答，不足时结合你的知识补充并说明。\n\n"
            + context + "\n\n【用户问题】\n" + query
        )))
        answer_mode = "rag_hybrid"
    elif query_type == "GENERAL":
        llm_messages.append(HumanMessage(content=GENERAL_ANSWER_PROMPT.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
            query=query,
            history="\n".join(_get_message_content(m)[:100] for m in messages[-6:] if isinstance(m, HumanMessage)),
        )))
        answer_mode = "general"
    else:
        # ── 低置信度：先试 Web 搜索兜底（web_augmented），失败/无结果则 llm_direct ──
        try:
            from backend.mcp.client import call_mcp_tool
            from backend.config import get_settings
            web_results = await call_mcp_tool(
                get_settings().mcp_web_search_url, "web_search",
                {"query": query, "max_results": 3}, timeout=10.0,
            )
        except Exception as e:
            logger.info("qa.web_search_skip", error=str(e)[:60])
            web_results = []
        if web_results:
            # 有 Web 结果：注入 prompt，标注 web_augmented
            web_ctx = "\n".join(
                f"[{i + 1}] {r.get('title', '')}（{r.get('url', '')}）\n{r.get('snippet', '')[:200]}"
                for i, r in enumerate(web_results[:3])
            )
            llm_messages.append(HumanMessage(content=(
                "你是建筑行业智能助手。用户问题在知识库中未找到答案，以下是 Web 搜索补充资料，"
                "请基于这些资料回答（如有不确定明确说明）。\n\n【Web 资料】\n"
                + web_ctx + "\n\n【用户问题】\n" + query
            )))
            answer_mode = "web_augmented"
            sources = [r.get("url", "") for r in web_results[:3] if r.get("url")]
        else:
            llm_messages.append(HumanMessage(content=DIRECT_ANSWER_PROMPT.format(query=query)))

    try:
        resp = await _invoke_llm(llm_messages, streaming=True)
        answer = _get_message_content(resp).strip()
    except Exception as exc:
        # Retrieval is still useful when the prose model/provider is down. Do
        # not turn a successful search into a blank response or a stuck SSE
        # request; return only the evidence we actually retrieved and mark the
        # response as a fallback so the UI/audit layer can distinguish it.
        logger.warning("qa.generate_llm_fallback", error=str(exc)[:160])
        llm_fallback = True
        evidence_text = [
            str(chunk.get("content", "")).strip()[:800]
            for chunk in chunks[:3]
            if str(chunk.get("content", "")).strip()
        ]
        if evidence_text:
            answer = "根据检索到的资料：\n" + "\n\n".join(evidence_text)
            answer_mode = "rag_extractive_fallback"
        else:
            answer = "当前模型服务暂时不可用，请稍后重试。"
            answer_mode = "llm_unavailable"

    if answer_mode == "rag" and sources:
        answer += "\n\n📚 **参考来源**\n" + "\n".join(f"  • {s}" for s in sources)
    elif answer_mode == "web_augmented" and sources:
        answer += "\n\n🌐 **Web 来源**\n" + "\n".join(f"  • {s}" for s in sources)

    return {
        "answer": answer,
        "sources": sources,
        "answer_mode": answer_mode,
        "messages": [AIMessage(content=answer)],
        "structured_output": {
            "summary": answer, "verdict": "pass",
            "risk_level": "low" if state.get("confidence", 0) >= 0.6 else "medium",
            "issues": [],
            "metrics": {"answer_mode": answer_mode,
                        "confidence": state.get("confidence", 0)},
            "sources": sources,
            "evidence_refs": evidence_refs,
            "retrieval_run_id": state.get("retrieval_run_id"),
            "meta": {"fallback_used": llm_fallback},
        },
    }


# ═══════════════ 记忆节点═══════════════
async def save_memory_node(state: QAState) -> dict:
    from backend.application.agent_memory import memory_nodes
    return await memory_nodes("qa")[1](state)


async def load_memory_node(state: QAState) -> dict:
    from backend.application.agent_memory import memory_nodes
    return await memory_nodes("qa")[0](state)


# ═══════════════ 知识待补节点═══════════════
async def enqueue_pending_node(state: QAState) -> dict:
    """低置信度问题写入 knowledge_pending_queue（知识待补闭环）"""
    from sqlalchemy import text
    from backend.db.session import engine as _engine
    query = state.get("original_query", "")
    confidence = state.get("confidence", 0.0)
    answer_mode = state.get("answer_mode", "")
    # 仅当知识库答不上来（低置信度 direct）才入队
    if not query or answer_mode not in ("llm_direct",):
        return {}
    # A stable key makes retries and duplicate low-confidence requests
    # idempotent.  The old random UUID made ``ON CONFLICT`` ineffective.
    pending_id = "pending_" + hashlib.sha256(
        f"{state.get('tenant_id', 'tenant_default')}\0{query.strip()}".encode("utf-8")
    ).hexdigest()
    try:
        from backend.db.dialect import insert_knowledge_pending_sql
        async with _engine.begin() as conn:
            await conn.execute(text(insert_knowledge_pending_sql()), {
                "id": pending_id,
                "tenant_id": state.get("tenant_id", "tenant_default"),
                "user_id": state.get("user_id") or state.get("student_id", ""),
                "question": query[:500],
                "confidence": round(confidence, 4),
            })
        logger.info("qa.enqueue_pending", question=query[:40], confidence=round(confidence, 4))
    except Exception as e:
        logger.warning("qa.enqueue_pending_failed", error=str(e))
    return {}
