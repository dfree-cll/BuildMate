"""采购审批 Agent 节点
parallel_review（规则引擎 + LLM 双轨 asyncio.gather）→ merge → [interrupt 人工审批] → publish
"""
import asyncio
import json
import uuid
from langchain_core.messages import HumanMessage
from langgraph.types import interrupt, Command

from backend.agents.procurement.state import ProcurementState
from backend.agents.procurement.prompts import LLM_REVIEW_PROMPT, APPROVAL_PROMPT
from backend.core.llm_factory import get_llm
from backend.core.llm_text import msg_text as _msg_text, parse_json_loose as _parse_json
from backend.db.session import engine
from backend.core.logger import get_logger
from sqlalchemy import text

logger = get_logger(__name__)


# ── 规则引擎──────────────────
THRESHOLD_HIGH = 5000.0   # 材料单价红线（元/吨，无行情时的兜底）
THRESHOLD_AMOUNT = 100000.0  # 大额采购线（元）
LOW_AMOUNT_AUTO = 10000.0    # 低金额自动通过线（元/品类）
PRICE_DEVIATION = 0.30       # 行情偏离阈值（±30%）


def _normalize_items(state: ProcurementState) -> list[dict]:
    """统一品类明细：多品类 items 优先（原引用，允许注入 _market）；旧单品类字段兜底"""
    items = state.get("items")
    if items:
        return items
    return [{"material_name": state.get("material_name", ""), "quantity": state.get("quantity", 0),
             "unit_price": state.get("unit_price", 0.0), "unit": "吨", "spec": "", "supplier": ""}]


async def _query_market_price(material_hint: str) -> dict | None:
    """查行情：返回 {market_price(中间价), price_low, price_high, unit, source} 或 None"""
    try:
        from backend.core.material_prices import query_prices
        rows = await query_prices(material_hint)
        if not rows:
            return None
        r = rows[0]
        low, high = float(r["price_low"] or 0), float(r["price_high"] or 0)
        if low <= 0 or high <= 0:
            return None
        return {"market_price": round((low + high) / 2, 2), "price_low": low, "price_high": high,
                "unit": r.get("unit", ""), "source": r.get("source", ""),
                "material": r.get("material", ""), "spec": r.get("spec", "")}
    except Exception as e:
        logger.warning("procurement.market_query_failed", material=material_hint, error=str(e))
        return None


def _run_rule_engine(state: ProcurementState) -> dict:
    """规则引擎：品类级行情对比（偏离>±30% 判异常）+ 金额阈值 + 数量/材料名校验（确定性，不调 LLM）"""
    items = _normalize_items(state)
    issues = []
    per_item = []
    total = 0.0

    for it in items:
        name = it.get("material_name", "")
        qty = it.get("quantity", 0)
        price = it.get("unit_price", 0.0)
        amount = qty * price
        total += amount
        item_issues = []
        verdict = "pass"

        if not name or not name.strip():
            item_issues.append("材料名称缺失")
            verdict = "review"
        if qty <= 0:
            item_issues.append(f"数量非法（{qty}<=0）")
            verdict = "review"
        if price <= 0:
            item_issues.append(f"单价非法（{price}<=0）")
            verdict = "review"
        if amount >= THRESHOLD_AMOUNT:
            item_issues.append("金额达到大额采购标准（>=10万元），需人工审批")
            verdict = "review"
        # 行情对比（有行情时：偏离>±30% 判异常；无行情时：红线兜底）
        market = it.get("_market")
        if market:
            mp = market["market_price"]
            unit = it.get("unit", "吨")
            munit = market.get("unit", "").replace("元/", "")   # 行情单位"元/吨"→"吨"再比较
            if mp > 0:
                if munit and unit != munit:
                    item_issues.append(f"计量单位与行情不一致（行情 {munit}，当前 {unit}），偏离判定仅供参考")
                    verdict = "review"
                else:
                    dev = (price - mp) / mp
                    it["deviation"] = round(dev, 4)
                    if abs(dev) > PRICE_DEVIATION:
                        direction = "高于" if dev > 0 else "低于"
                        item_issues.append(f"单价{direction}行情价 {dev*100:.0f}%（行情 {mp} 元/{munit}）")
                        verdict = "review"
        elif price >= THRESHOLD_HIGH:
            item_issues.append(f"单价高于市场红线（>=5000元）且无行情参考，需重点核查")
            verdict = "review"

        per_item.append({"material_name": name, "spec": it.get("spec", ""), "unit": it.get("unit", "吨"),
                         "quantity": qty, "unit_price": price, "amount": round(amount, 2),
                         "verdict": verdict, "issues": item_issues,
                         "deviation": it.get("deviation"),
                         "market_price": market["market_price"] if market else None,
                         "market_unit": market.get("unit", "") if market else ""})
        issues.extend(f"{name}: {i}" for i in item_issues)

    any_review = any(p["verdict"] == "review" for p in per_item)
    return {"issues": issues, "verdict": "review" if any_review else "pass",
            "per_item": per_item, "total_amount": round(total, 2)}


async def _llm_review(state: ProcurementState, market_refs: dict) -> dict:
    from backend.application.agent_memory import memory_prompt
    """LLM 快审（多品类 + 行情参考；失败重试 1 次）"""
    items = _normalize_items(state)
    items_text = "\n".join(
        f"- {it['material_name']}（{it.get('spec', '')}）数量 {it['quantity']} {it.get('unit', '吨')} 单价 {it['unit_price']} 元/{it.get('unit', '吨')}"
        for it in items)
    market_text = "\n".join(
        f"- {k}: 行情 {v['market_price']} 元/{v['unit']}（{v['source']}）"
        for k, v in market_refs.items() if v) or "（无匹配行情）"

    for attempt in range(2):
        try:
            llm = get_llm("procurement", temperature=0)
            prompt = LLM_REVIEW_PROMPT.format(
                items_text=items_text, market_text=market_text,
                total_amount=state.get("total_amount", 0),
            )
            resp = await llm.ainvoke([HumanMessage(content=memory_prompt(state, prompt))])
            return _parse_json(_msg_text(resp)) or {}
        except Exception as e:
            logger.warning("procurement.llm_review_failed", attempt=attempt + 1, error=str(e))
            if attempt == 0:
                await asyncio.sleep(1)
    return {"compliance_issues": [], "reasonableness": "LLM 审查失败",
            "suggestion": "转人工审核", "verdict": "review", "reason": "LLM 异常"}


async def parallel_review_node(state: ProcurementState) -> dict:
    """三轨并行：行情查询 + 规则引擎 + LLM 快审（asyncio.gather，对标行业范式-6.7）"""
    items = _normalize_items(state)
    # 每品类查行情（并行）
    market_refs = {}
    for it in items:
        if it.get("material_name"):
            market_refs[it["material_name"]] = await _query_market_price(it["material_name"])
    # 行情注入规则引擎
    for it in items:
        it["_market"] = market_refs.get(it.get("material_name", ""))

    rule_result, llm_result = await asyncio.gather(
        asyncio.to_thread(_run_rule_engine, state),
        _llm_review(state, market_refs),
    )
    logger.info("procurement.parallel_done",
                rule_verdict=rule_result["verdict"], llm_verdict=llm_result.get("verdict", "pass"),
                items=len(items), total=rule_result["total_amount"])
    return {"rule_result": rule_result, "llm_result": llm_result}


async def merge_node(state: ProcurementState) -> dict:
    """合并双轨结论（品类级）"""
    rule = state.get("rule_result", {})
    llm = state.get("llm_result", {})
    llm_verdict = llm.get("verdict", "pass")
    per_item = rule.get("per_item", [])
    issues = list(rule.get("issues", [])) + list(llm.get("compliance_issues", []))

    if rule.get("verdict") == "review" or llm_verdict == "review":
        verdict = "review"
    elif llm_verdict == "reject" or rule.get("verdict") == "reject":
        verdict = "reject"
    else:
        verdict = "pass"

    conclusion = {
        "verdict": verdict,
        "issues": issues[:10],
        "reasonableness": llm.get("reasonableness", ""),
        "suggestion": llm.get("suggestion", ""),
        "total_amount": rule.get("total_amount", state.get("total_amount", 0)),
        "per_item": per_item,
    }
    logger.info("procurement.merged", verdict=verdict, issues=len(issues), items=len(per_item))
    return {"ai_conclusion": conclusion, "ai_verdict": verdict}


async def human_in_the_loop_node(state: ProcurementState) -> dict:
    """HitL：分级审批——低金额品类全 pass → LLM 快速审批直接通过；高金额/review → interrupt 人工确认
    多品类：任一品类高金额（>=10万）或 review → 人工逐项批"""
    conclusion = state.get("ai_conclusion", {})
    per_item = conclusion.get("per_item", []) or [{"material_name": state.get("material_name", ""),
                                                   "quantity": state.get("quantity", 0),
                                                   "unit_price": state.get("unit_price", 0.0),
                                                   "amount": state.get("total_amount", 0),
                                                   "verdict": "pass"}]
    total_amount = conclusion.get("total_amount", state.get("total_amount", 0))
    verdict = state.get("ai_verdict", "review")

    # 分级判定：全部品类低金额 + 规则引擎品类级全 pass → LLM 快速审批直批
    # （LLM 的保守意见（如"接近警戒线"）记录在 issues 中作提示，不阻塞低金额快审）
    all_low = all(p.get("amount", 0) < LOW_AMOUNT_AUTO for p in per_item)
    all_pass = all(p.get("verdict") == "pass" for p in per_item)
    if all_pass and all_low:
        return {
            "reviewer_decision": {"decision": "approved", "comment": "低金额品类 LLM 快速审批通过"},
            "final_verdict": "approved",
        }

    display_data = {
        "order_no": state["order_no"],
        "items": [{"material_name": p.get("material_name"), "spec": p.get("spec", ""),
                   "quantity": p.get("quantity"), "unit_price": p.get("unit_price"),
                   "amount": p.get("amount"), "verdict": p.get("verdict"),
                   "deviation": p.get("deviation"),
                   "market_price": p.get("market_price"), "market_unit": p.get("market_unit", ""),
                   "issues": p.get("issues", [])} for p in per_item],
        "total_amount": total_amount,
        "ai_verdict": verdict,
        "ai_conclusion": conclusion,
    }
    decision = interrupt(display_data)   # ← 冻结在此，等待 Command(resume=...)
    final_verdict = decision.get("decision", "rejected")
    updates = {
        "reviewer_decision": decision,
        "final_verdict": final_verdict,
        "final_comment": decision.get("comment", ""),
    }
    # modify 分支：按品类改单（new_items 按 index 改；兼容旧 new_quantity/new_unit_price 全改）
    if final_verdict == "modify":
        new_items = decision.get("new_items")
        items = _normalize_items(state)
        if isinstance(new_items, list) and new_items:
            for change in new_items:
                idx = change.get("index")
                if isinstance(idx, int) and 0 <= idx < len(items):
                    if change.get("new_quantity") is not None and change["new_quantity"] > 0:
                        items[idx]["quantity"] = change["new_quantity"]
                    if change.get("new_unit_price") is not None and change["new_unit_price"] > 0:
                        items[idx]["unit_price"] = change["new_unit_price"]
            new_total = round(sum(i["quantity"] * i["unit_price"] for i in items), 2)
            updates["items"] = items
            updates["total_amount"] = new_total
        else:   # 兼容旧式全局改单
            new_qty = decision.get("new_quantity")
            new_price = decision.get("new_unit_price")
            for it in items:
                if new_qty is not None and new_qty > 0:
                    it["quantity"] = new_qty
                if new_price is not None and new_price > 0:
                    it["unit_price"] = new_price
            updates["total_amount"] = round(sum(i["quantity"] * i["unit_price"] for i in items), 2)
            updates["items"] = items
        # modify 视为通过（改单后批准）
        updates["final_verdict"] = "approved"
        updates["final_comment"] = f"审核改单后批准：{decision.get('comment', '')}"
        logger.info("procurement.modified", items=len(items), total_amount=updates["total_amount"])
    return updates


async def publish_node(state: ProcurementState) -> dict:
    """发布：写库 + 生成批复文案（契约 v1：summary/verdict/risk_level/issues/metrics/sources/meta）"""
    decision = state.get("reviewer_decision", {})
    final_verdict = state.get("final_verdict", "rejected")
    comment = state.get("final_comment", "")
    conclusion = state.get("ai_conclusion", {})
    per_item = conclusion.get("per_item", [])
    items = state.get("items") or []
    total_amount = state.get("total_amount", conclusion.get("total_amount", 0))
    material_label = "、".join(i.get("material_name", "") for i in items) or state.get("material_name", "")

    try:
        async with engine.begin() as conn:
            from backend.db.dialect import upsert_purchase_order_sql
            await conn.execute(text(
                upsert_purchase_order_sql()
            ), {
                "id": state.get("order_id") or str(uuid.uuid4()),
                "tenant_id": state["tenant_id"],
                "user_id": state.get("user_id", "u-unknown"),
                "order_no": state["order_no"],
                "material_name": material_label,
                "quantity": sum(i.get("quantity", 0) for i in items) or state.get("quantity", 0),
                "unit_price": state.get("unit_price", 0),
                "total_amount": total_amount,
                "status": "approved" if final_verdict == "approved" else "rejected",
                "ai_result": json.dumps(conclusion, ensure_ascii=False),
                "approved_by": decision.get("operator", "system") if final_verdict == "approved" else None,
            })
            await conn.execute(text(
                "INSERT INTO approval_records (id, tenant_id, order_id, action, comment, operator) VALUES (:id, :tenant_id, :order_id, :action, :comment, :operator)"
            ), {
                "id": str(uuid.uuid4()),
                "tenant_id": state["tenant_id"],
                "order_id": state.get("order_id") or "",
                "action": final_verdict,
                "comment": comment,
                "operator": decision.get("operator", "system"),
            })
    except Exception as e:
        logger.warning("procurement.publish_db_failed", error=str(e))

    try:
        llm = get_llm("procurement", temperature=0)
        resp = await llm.ainvoke([HumanMessage(content=APPROVAL_PROMPT.format(
            ai_conclusion=json.dumps(conclusion, ensure_ascii=False),
            decision=final_verdict, comment=comment,
        ))])
        answer = _msg_text(resp).strip()
    except Exception:
        verdict_cn = "通过" if final_verdict == "approved" else "驳回"
        answer = "采购单 " + state["order_no"] + "（" + material_label + "）审批" + verdict_cn + "。" + ("备注：" + comment if comment else "")

    # 契约 v1
    risk_level = "high" if final_verdict == "rejected" else ("medium" if any(p.get("verdict") == "review" for p in per_item) else "low")
    structured_output = {
        "summary": f"采购单 {state['order_no']} 含 {len(per_item)} 个品类，总金额 {total_amount} 元，审批{'通过' if final_verdict == 'approved' else '驳回'}。",
        "verdict": final_verdict,
        "risk_level": risk_level,
        "issues": [{"subject": i.split(':', 1)[0], "description": i.split(':', 1)[1].strip() if ':' in i else i,
                    "severity": "warning", "suggestion": "核查后处理", "confidence": 1.0}
                   for i in (conclusion.get("issues") or [])][:10],
        "metrics": {
            "total_amount": total_amount,
            "per_item": per_item,
            "ai_conclusion": conclusion,
        },
        "sources": [p.get("market_unit") and f"行情:{p.get('market_price')}元/{p.get('market_unit')}" for p in per_item if p.get("market_price")][:5],
        "meta": {"fallback_used": bool(state.get("fallback_used")), "auto_approved": bool(decision.get("comment", "").startswith("低金额"))},
    }

    return {
        "answer": answer,
        "structured_output": structured_output,
        "final_verdict": final_verdict,
    }
