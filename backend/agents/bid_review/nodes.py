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

# ═══════════════ 多文件归类 + 维度喂料 ═══════════════
# 文件名关键词 → 文件类别（确定性规则，不靠 LLM）
_FILE_TYPE_RULES = [
    ("技术标", ("技术标", "施工组织", "施工方案", "技术方案", "工艺", "专项方案")),
    ("商务标", ("商务标", "投标函", "报价", "商务", "价格", "清单", "开标一览表")),
    ("资质文件", ("资质", "业绩", "证书", "人员", "证件", "营业执照", "授权")),
]
_DEFAULT_FILE_TYPE = "通用"


def classify_bid_file(filename: str) -> str:
    """按文件名关键词归类：技术标/商务标/资质文件/通用"""
    name = (filename or "").lower()
    for ftype, kws in _FILE_TYPE_RULES:
        if any(k in name for k in kws):
            return ftype
    return _DEFAULT_FILE_TYPE


def _file_texts_by_type(state: BidReviewState) -> dict:
    """把 documents 按类型归集：{类别: [文件名, 文本]}（无 documents 时退化用 doc_text）"""
    docs = state.get("documents")
    if not docs:
        return {"通用": [("全文", state.get("doc_text", ""))]}
    grouped: dict[str, list] = {}
    for d in docs:
        ftype = classify_bid_file(d.get("filename", ""))
        grouped.setdefault(ftype, []).append((d.get("filename", ""), d.get("raw_text", "")))
    return grouped


# 维度 → 喂料文件类型（缺失时回退全部）
_DIMENSION_FEED = {
    "技术方案": "技术标",
    "商务响应": "商务标",
    "资质业绩": "资质文件",
    "合规风险": None,   # None = 全部文件
}

# 分段评审阈值（超过则按 3000 字符切块提炼要点，聚合后一次评审——替代旧 [:3000] 截断）
_CHUNK_SIZE = 3000
# 每维度最多提炼块数（块越多 LLM 调用越多；真实标书前 30 块覆盖大部分内容）
_MAX_CHUNKS_PER_DIMENSION = 30


def _pick_dimension_text(dim: str, grouped: dict) -> tuple[str, str]:
    """取维度评审喂料：优先对应类型文件，缺失回退全部；返回 (喂料文本, 依据说明)"""
    want = _DIMENSION_FEED.get(dim["dimension"])
    if want and want in grouped:
        parts = [f"【{name}】\n{text}" for name, text in grouped[want]]
        return "\n\n".join(parts), f"依据：{want}文件（{'、'.join(n for n, _ in grouped[want])}）"
    # 回退全部文件（并注明该类型未提供）
    all_parts = [f"【{name}】\n{text}" for ftype, items in grouped.items() for name, text in items]
    note = f"（未提供{want}文件，已回退全部）" if want else ""
    return "\n\n".join(all_parts), f"依据：全部文件{note}"


def _split_chunks(text: str, size: int = _CHUNK_SIZE) -> list[str]:
    """按字符切块（保留块间重叠 200 字符，避免切断上下文）"""
    text = text or ""
    if len(text) <= size:
        return [text] if text.strip() else []
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i + size])
        i += size - 200
    return chunks


# ═══════════════ 废标规则轨（确定性规则，不依赖 LLM）═══════════════
# 关键材料清单：缺失即废标/高风险（标书完整性检测）
_REQUIRED_MATERIALS = [
    ("投标函", ("投标函", "投标书")),
    ("营业执照", ("营业执照",)),
    ("授权委托书", ("授权委托书", "法人授权")),
    ("投标保证金", ("保证金", "投标保函", "保函")),
    ("资质证书", ("资质证书", "资质等级", "资质")),
    ("法定代表人身份证明", ("法定代表人", "法人身份")),
]
# 格式性风险关键词
_FORMAT_RISK_KWS = ("未盖章", "未签字", "未加盖", "无盖章", "无签字")


def _check_disqualify(state: BidReviewState) -> list[dict]:
    """确定性废标规则检测：返回命中规则列表 [{rule, severity, detail}]"""
    docs = state.get("documents") or [{"filename": "全文", "raw_text": state.get("doc_text", "")}]
    full_text = "\n".join(d.get("raw_text", "") for d in docs)
    hits = []

    # R1 关键材料缺失（完整性）
    for name, kws in _REQUIRED_MATERIALS:
        if not any(k in full_text for k in kws):
            hits.append({"rule": f"缺失{name}", "severity": "critical",
                         "detail": f"标书中未检测到「{name}」相关内容（可能为废标项）"})

    # R2 格式性废标
    if any(k in full_text for k in _FORMAT_RISK_KWS):
        hits.append({"rule": "格式性风险", "severity": "critical",
                     "detail": "标书含'未盖章/未签字'等表述，存在格式性废标风险"})

    # R3 报价缺失（商务标无金额数字）
    import re
    amounts = re.findall(r"(\d+(?:\.\d+)?)\s*(?:万元|元)", full_text)
    if not amounts:
        hits.append({"rule": "报价缺失", "severity": "high",
                     "detail": "未检测到投标报价金额（万元/元），商务标可能不完整"})
    return hits


def _build_issues(dimension_scores: list, disqualify: list) -> list[dict]:
    """汇总结构化 issues（统一契约子项）"""
    issues = []
    sev_map = {"critical": "critical", "high": "warning", "medium": "warning"}
    for d in disqualify:
        issues.append({"subject": d["rule"], "description": d["detail"],
                       "severity": sev_map.get(d["severity"], "warning"),
                       "suggestion": "补充缺失材料或核实标书完整性后重新审查",
                       "confidence": 1.0})
    for r in dimension_scores:
        for iss in r.get("issues", []):
            issues.append({"subject": f"{r['dimension']}问题",
                           "description": str(iss),
                           "severity": "warning" if r["score"] < 60 else "info",
                           "suggestion": "、".join(r.get("suggestions", [])[:1]) or "完善该维度内容",
                           "confidence": 0.8})
    return issues[:20]


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


async def review_dimension(dim: dict, text: str, feed_note: str = ""):
    """单个维度评审：文本超长时先分块提炼要点（map），要点聚合后一次评审（reduce）
    替代旧 4×N 段独立评审——LLM 调用从 60 次降到 块数+1 次，时间减半"""
    if not text or not text.strip():
        return {"dimension": dim["dimension"], "weight": dim["weight"], "score": 50,
                "issues": ["未提供该维度评审材料"], "suggestions": [], "feed_note": feed_note}

    all_chunks = _split_chunks(text)
    truncated = len(all_chunks) > _MAX_CHUNKS_PER_DIMENSION
    chunks = all_chunks[:_MAX_CHUNKS_PER_DIMENSION]

    # ── map：每块提炼要点（并行，Semaphore(5) 限流防 DeepSeek 打爆）──
    sem = asyncio.Semaphore(5)

    async def _extract_one(chunk):
        async with sem:
            try:
                llm = get_llm("bid_review", temperature=0)
                prompt = (
                    "你是投标文件评审助手。请从下面标书片段中提炼与【{dim}】相关的要点，"
                    "包括：关键数据（金额/工期/资质/有效期等）、承诺、缺失项。\n"
                    "用简洁条目列出（每条不超过 40 字），只输出要点本身，不要解释。\n\n"
                    "标书片段：\n{chunk}"
                ).format(dim=dim["dimension"], chunk=chunk)
                resp = await llm.ainvoke([HumanMessage(content=prompt)])
                return _msg_text(resp).strip()
            except Exception as e:
                logger.warning("bid_review.chunk_extract_failed", dimension=dim["dimension"], error=str(e))
                return ""

    extracted = [r for r in await asyncio.gather(*[_extract_one(c) for c in chunks]) if r]

    # ── reduce：要点合并后一次评审（LLM 失败重试 1 次）──
    key_points = "\n".join(extracted) if extracted else text[:_CHUNK_SIZE]
    if not key_points.strip():
        key_points = text[:_CHUNK_SIZE]

    score, issues, suggestions = 50, ["该维度评审失败（LLM 异常）"], []
    for attempt in range(2):
        try:
            llm = get_llm("bid_review", temperature=0)
            prompt = DIMENSION_PROMPT.format(dimension=dim["dimension"], instruction=dim["instruction"],
                                             doc_text=key_points)
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            result = _parse_json(_msg_text(resp)) or {}
            score = max(0, min(100, int(result.get("score", 50))))
            issues = result.get("issues", []) or []
            suggestions = result.get("suggestions", []) or []
            break
        except Exception as e:
            logger.warning("bid_review.dimension_failed", dimension=dim["dimension"],
                           attempt=attempt + 1, error=str(e))
            if attempt == 0:
                await asyncio.sleep(1)   # 退避后重试一次
            else:
                score, issues, suggestions = 50, ["该维度评审失败（LLM 异常）"], []

    return {
        "dimension": dim["dimension"], "weight": dim["weight"], "score": score,
        "issues": issues[:5], "suggestions": suggestions[:3],
        "feed_note": feed_note, "chunk_count": len(extracted),
        "truncated": truncated,
    }


async def parallel_review_node(state: BidReviewState) -> dict:
    """四维并行评审：按文件归类喂料 + 废标规则轨检测"""
    grouped = _file_texts_by_type(state)
    feeds = {d["dimension"]: _pick_dimension_text(d, grouped) for d in DIMENSIONS}

    async def _one(dim):
        text, note = feeds[dim["dimension"]]
        return await review_dimension(dim, text, note)

    results = await asyncio.gather(*[_one(d) for d in DIMENSIONS])
    weighted = round(sum(r["score"] * r["weight"] for r in results), 2)

    # 废标规则轨（确定性检测，不依赖 LLM）
    disqualify = _check_disqualify(state)
    logger.info("bid_review.parallel_done", weighted_score=weighted,
                disqualify_hits=len(disqualify))
    return {"dimension_scores": results, "weighted_score": weighted,
            "disqualify_risks": disqualify}


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
    from backend.application.agent_memory import memory_prompt
    try:
        llm = get_llm("bid_review", temperature=0)
        dim_text = json.dumps(state.get("dimension_scores", []), ensure_ascii=False)
        resp = await llm.ainvoke([HumanMessage(content=memory_prompt(state, SUMMARY_PROMPT.format(dimension_results=dim_text)))])
        summary = _parse_json(_msg_text(resp)) or {}
    except Exception as e:
        logger.warning("bid_review.summary_failed", error=str(e))
        summary = {"overall_comment": "综合评审完成", "risk_level": "medium", "recommendation": "谨慎投标"}
    return {"summary": summary}


async def format_node(state: BidReviewState) -> dict:
    """汇总输出（统一输出契约七字段 + 废标规则轨结果）"""
    disqualify = state.get("disqualify_risks", [])
    weighted = state.get("weighted_score", 0)
    sm = state.get("summary", {}) or {}

    # verdict 判定：废标 critical 命中 → rejected；否则按分数分级
    has_critical = any(d.get("severity") == "critical" for d in disqualify)
    if has_critical:
        verdict = "rejected"
    elif weighted >= 80:
        verdict = "approved"
    elif weighted >= 60:
        verdict = "review"
    else:
        verdict = "rejected"
    risk_level = "high" if has_critical or weighted < 60 else ("medium" if weighted < 75 else "low")

    lines = []
    lines.append("## 投标文件评审报告：" + str(state.get("structured", {}).get("project_name", "未知项目")))
    lines.append("**投标人**：" + str(state.get("structured", {}).get("bidder", "未知")))
    lines.append("**加权综合得分**：" + str(weighted) + " / 100（**结论：" + verdict + "**）")
    if disqualify:
        lines.append("")
        lines.append("**⚠️ 废标规则检测命中 " + str(len(disqualify)) + " 项**：")
        for d in disqualify:
            lines.append(f"  - [{d['severity']}] {d['rule']}：{d['detail']}")
    for r in state.get("dimension_scores", []):
        lines.append("")
        lines.append("### " + r["dimension"] + "（权重 " + str(r["weight"]) + "）：" + str(r["score"]) + " 分")
        if r.get("feed_note"):
            lines.append("评审" + r["feed_note"])
        if r.get("truncated"):
            lines.append("（文档过长，已评审前 " + str(r.get("chunk_count", 0)) + " 段，覆盖约 4.5 万字符）")
        if r.get("issues"):
            lines.append("问题：" + "；".join(r["issues"][:3]))
        if r.get("suggestions"):
            lines.append("建议：" + "；".join(r["suggestions"][:2]))
    if state.get("issues"):
        high = [i for i in state["issues"] if i["priority"] == "high"]
        lines.append("")
        lines.append("**高风险问题 " + str(len(high)) + " 项**：" + "；".join(i["description"] for i in high[:5]))
    if sm:
        lines.append("")
        lines.append("**综合评语**：" + str(sm.get("overall_comment", "")))
        lines.append("**风险等级**：" + str(sm.get("risk_level", "")) + "｜**结论**：" + str(sm.get("recommendation", "")))
    answer = "\n".join(lines)

    # 统一输出契约 v1（QA 已落地，投标对齐）
    structured_output = {
        "summary": str(sm.get("overall_comment", "综合评审完成")),
        "verdict": verdict,
        "risk_level": risk_level,
        "issues": _build_issues(state.get("dimension_scores", []), disqualify),
        "metrics": {
            "weighted_score": weighted,
            "dimensions": state.get("dimension_scores", []),
            "disqualify_risks": disqualify,
        },
        "sources": [d.get("filename", "") for d in (state.get("documents") or [])],
        "meta": {"fallback_used": False},
    }
    return {
        "answer": answer,
        "structured_output": structured_output,
        # 兼容旧读取点（API 从 state 取 weighted_score / dimension_scores / summary）
        "weighted_score": weighted,
        "dimension_scores": state.get("dimension_scores", []),
        "summary": sm,
        "issues": state.get("issues", []),
    }
