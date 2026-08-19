"""知识问答 Agent 节点
classify_query（规则+LLM 判 PRECISE/VAGUE/BROAD/GENERAL）
→ retrieve（本地向量检索）
→ generate（RAG 或直答）
"""
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
from backend.core.knowledge_base import search
from backend.core.logger import get_logger

logger = get_logger(__name__)

_GENERAL_KEYWORDS = ("你好", "谢谢", "你是谁", "你能做什么", "再见", "今天天气", "今天是几号", "现在几点", "讲个笑话")
_VAGUE_HINTS = ("没懂", "不懂", "不太懂", "讲讲", "解释一下", "啥意思", "什么意思")
_BROAD_HINTS = ("全面", "系统", "总结", "梳理", "对比", "区别", "有哪些", "全景")


def _extract_query(state: QAState) -> str:
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, HumanMessage):
            return _get_message_content(msg)
    return state.get("original_query", "")


async def _llm_strategy(query: str) -> str:
    try:
        llm = get_llm("qa", temperature=0)
        resp = await llm.ainvoke([HumanMessage(content=RAG_STRATEGY_PROMPT.format(query=query))])
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

    # 其余 → LLM 精判（失败默认 PRECISE）
    strategy = await _llm_strategy(query)
    logger.info("qa.classified", query=query[:40], strategy=strategy)
    return {**base, "query_type": strategy}


async def _hyde_generate(state: QAState) -> str:
    query = state["original_query"]
    history = "\n".join(_get_message_content(m)[:100] for m in state.get("messages", [])[-6:] if isinstance(m, HumanMessage))
    llm = get_llm("qa", temperature=0.3)
    resp = await llm.ainvoke([HumanMessage(content=HYDE_PROMPT.format(history=history or "（无）", query=query))])
    return _get_message_content(resp).strip()


async def _multi_query(state: QAState) -> list[str]:
    query = state["original_query"]
    llm = get_llm("qa", temperature=0.3)
    resp = await llm.ainvoke([HumanMessage(content=MULTI_QUERY_REWRITE_PROMPT.format(last_answer="（无）", query=query))])
    raw = _get_message_content(resp).strip()
    queries = [l.lstrip("0123456789.-）)、 ").strip() for l in raw.split("\n") if l.strip() and len(l.strip()) > 3]
    return queries[:3] or [query]


# ═══════════════ 真实价格直答节点（接入 material_prices 表真实行情）═══════════════
_PRICE_KEYWORDS = ("多少钱", "价格", "行情", "报价", "每吨", "一吨", "每方", "一方", "单价", "时价", "今日价", "价格走势", "价格多少")
_PRICE_MATERIALS = ("螺纹钢", "钢筋", "钢材", "水泥", "混凝土", "商砼", "砂石", "河砂", "机制砂", "碎石", "热卷", "线材", "铁矿石", "铜", "矿渣粉")


def _is_price_query(query: str) -> bool:
    """检测是否为建材价格类问题：含价格词 + 材料词"""
    q = query.lower()
    has_price_kw = any(k in q for k in _PRICE_KEYWORDS)
    has_material = any(m in q for m in _PRICE_MATERIALS)
    return has_price_kw and has_material


def _extract_material(query: str) -> str:
    """从问题中提取材料名（按词典顺序匹配）"""
    for m in _PRICE_MATERIALS:
        if m in query:
            return m
    return ""


def _extract_market(query: str) -> str:
    """提取城市/市场：西安/上海/北京/广州/深圳/杭州/南京/合肥/郑州/成都/重庆/武汉/长沙/济南/沈阳/天津"""
    for city in ("西安", "上海", "北京", "广州", "深圳", "杭州", "南京", "合肥", "郑州", "成都", "重庆", "武汉", "长沙", "济南", "沈阳", "天津"):
        if city in query:
            return city
    return ""


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

    llm = get_llm("qa", streaming=True)
    resp = await llm.ainvoke([SystemMessage(content=SYSTEM_PROMPT),
                              HumanMessage(content=REAL_PRICE_ANSWER_PROMPT.format(
                                  price_context=price_text, query=query))])
    answer = _get_message_content(resp).strip()
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
            "answer": answer, "sources": [r["source"] for r in rows[:3]],
            "confidence": 0.95, "answer_mode": "real_price",
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
        for q in queries:
            all_chunks.extend(await search(q, tenant_id=tenant_id, top_k=4))
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
                "rewritten_queries": queries}

    chunks = await search(query, tenant_id=tenant_id, top_k=8)   # 召回 8 条 → 精排
    # ── Reranker 精排──
    try:
        from backend.core.reranker import rerank_results
        ranked, confidence = await rerank_results(query, chunks, top_k=3)
        logger.info("qa.retrieved+reranked", query_type=query_type,
                    recalled=len(chunks), reranked=len(ranked), confidence=round(confidence, 4))
    except Exception as e:
        # reranker 不可用时回退：直接取混合检索前 3
        ranked = chunks[:3]
        confidence = ranked[0]["score"] if ranked else 0.0
        logger.warning("qa.rerank_fallback", error=str(e))
    return {"ranked_chunks": ranked, "confidence": confidence}


async def generate_node(state: QAState) -> dict:
    """生成：有检索结果且分数够 → RAG；否则 LLM 直答"""
    query = state["original_query"]
    query_type = state.get("query_type", "PRECISE").upper()
    chunks = state.get("ranked_chunks", [])
    messages = state.get("messages", [])

    llm_messages = [SystemMessage(content=SYSTEM_PROMPT)]
    for msg in messages[-6:-1]:
        if not isinstance(msg, SystemMessage):
            llm_messages.append(msg)

    answer_mode = "llm_direct"
    sources: list[str] = []
    top_score = chunks[0]["score"] if chunks else 0.0

    if chunks and (top_score >= 0.6 or (chunks[0].get("dense_score", 0) or 0) >= 0.6):
        # 高置信度 RAG：严格基于知识库回答
        context = "\n\n".join(f"【参考{i + 1}】\n{c['content']}" for i, c in enumerate(chunks))
        sources = [c["metadata"].get("source_name", "") for c in chunks if c["metadata"].get("source_name")]
        llm_messages.append(HumanMessage(content=RAG_ANSWER_PROMPT.format(context=context, query=query)))
        answer_mode = "rag"
    elif chunks and top_score >= 0.4:
        # 混合模式：知识库内容作为参考，真实 LLM 组织回答（解决"螺纹钢 0.578"等边界命中）
        context = "\n\n".join(f"【知识库参考{i + 1}】\n{c['content']}" for i, c in enumerate(chunks[:3]))
        sources = [c["metadata"].get("source_name", "") for c in chunks if c["metadata"].get("source_name")]
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
            web_results = await call_mcp_tool(
                "http://127.0.0.1:8002", "web_search",
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

    llm = get_llm("qa", streaming=True)
    resp = await llm.ainvoke(llm_messages)
    answer = _get_message_content(resp).strip()

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
            "answer": answer, "sources": sources,
            "confidence": state.get("confidence", 0), "answer_mode": answer_mode,
        },
    }


# ═══════════════ 记忆节点═══════════════
async def save_memory_node(state: QAState) -> dict:
    """记忆保存：每轮把对话与摘要 UPSERT 到 qa_sessions（失败静默）"""
    from sqlalchemy import text
    from backend.db.session import engine as _engine
    from backend.core.memory import build_thread_id, should_trigger_summary, compress_to_summary

    user_id = state.get("user_id") or state.get("student_id", "")
    session_id = state.get("session_id", "")
    tenant_id = state.get("tenant_id", "tenant_default")
    thread_id = build_thread_id(user_id, session_id)
    messages = state.get("messages", [])

    # 摘要压缩（每 10 轮触发）
    summary = None
    try:
        if should_trigger_summary(messages):
            summary = await compress_to_summary(messages)
    except Exception as e:
        logger.warning("qa.save_memory.compress_failed", error=str(e))

    try:
        async with _engine.begin() as conn:
            from backend.db.dialect import upsert_qa_session_sql
            await conn.execute(text(
                upsert_qa_session_sql()
            ), {
                "id": str(uuid.uuid4()), "tenant_id": tenant_id, "user_id": user_id,
                "thread_id": thread_id, "summary": summary,
            })
    except Exception as e:
        logger.warning("qa.save_memory.db_failed", error=str(e))
    return {}


async def load_memory_node(state: QAState) -> dict:
    """加载历史摘要（供 SystemMessage 注入，实现跨轮记忆）"""
    from sqlalchemy import text
    from backend.db.session import engine as _engine
    from backend.core.memory import build_thread_id
    user_id = state.get("user_id") or state.get("student_id", "")
    session_id = state.get("session_id", "")
    thread_id = build_thread_id(user_id, session_id)
    try:
        async with _engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT summary FROM qa_sessions WHERE thread_id = :tid"
            ), {"tid": thread_id})).fetchone()
        if row and row[0]:
            return {"existing_summary": row[0]}
    except Exception as e:
        logger.warning("qa.load_memory.failed", error=str(e))
    return {"existing_summary": None}


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
    try:
        async with _engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO knowledge_pending_queue (id, tenant_id, user_id, question, confidence, status)
                VALUES (:id, :tenant_id, :user_id, :question, :confidence, 'pending')
                ON CONFLICT DO NOTHING
            """), {
                "id": str(uuid.uuid4()),
                "tenant_id": state.get("tenant_id", "tenant_default"),
                "user_id": state.get("user_id") or state.get("student_id", ""),
                "question": query[:500],
                "confidence": round(confidence, 4),
            })
        logger.info("qa.enqueue_pending", question=query[:40], confidence=round(confidence, 4))
    except Exception as e:
        logger.warning("qa.enqueue_pending_failed", error=str(e))
    return {}
