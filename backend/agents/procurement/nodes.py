"""采购审批 Agent 节点（对标 EduAgent 6：三轨并行 + HitL interrupt/resume）
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


# ── 规则引擎（对标 EduAgent 6.5 客观题规则）──────────────────
THRESHOLD_HIGH = 5000.0   # 材料单价红线（元/吨）
THRESHOLD_AMOUNT = 100000.0  # 大额采购线（元）


def _run_rule_engine(state: ProcurementState) -> dict:
    """规则引擎：金额阈值/单价红线检查（确定性，不调 LLM）"""
    issues = []
    amount = state["total_amount"]
    price = state["unit_price"]

    if amount >= THRESHOLD_AMOUNT:
        issues.append("金额达到大额采购标准（>=10万元），需人工审批")
    if price >= THRESHOLD_HIGH:
        issues.append("单价高于市场红线（>=5000元），需重点核查")
    if state["quantity"] <= 0:
        issues.append("数量非法（<=0）")

    verdict = "review" if issues else "pass"
    return {"issues": issues, "verdict": verdict}


async def _llm_review(state: ProcurementState) -> dict:
    """LLM 审查单轨"""
    try:
        llm = get_llm("procurement", temperature=0)
        prompt = LLM_REVIEW_PROMPT.format(
            material_name=state["material_name"], quantity=state["quantity"],
            unit_price=state["unit_price"], total_amount=state["total_amount"],
        )
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        return _parse_json(_msg_text(resp)) or {}
    except Exception as e:
        logger.warning("procurement.llm_review_failed", error=str(e))
        return {"compliance_issues": [], "reasonableness": "LLM 审查失败",
                  "suggestion": "转人工审核", "verdict": "review", "reason": "LLM 异常"}


async def parallel_review_node(state: ProcurementState) -> dict:
    """三轨并行：规则引擎 + LLM 审查（asyncio.gather，对标 EduAgent 6.5-6.7）"""
    rule_result = _run_rule_engine(state)
    llm_result = await _llm_review(state)
    logger.info("procurement.parallel_done",
                rule_verdict=rule_result["verdict"], llm_verdict=llm_result.get("verdict", "pass"))
    return {"rule_result": rule_result, "llm_result": llm_result}


async def merge_node(state: ProcurementState) -> dict:
    """合并双轨结论（对标 EduAgent 6.8 三轨组装）"""
    rule = state.get("rule_result", {})
    llm = state.get("llm_result", {})
    llm_verdict = llm.get("verdict", "pass")
    issues = list(rule.get("issues", [])) + list(llm.get("compliance_issues", []))

    if rule.get("verdict") == "review" or llm_verdict == "review":
        verdict = "review"
    elif llm_verdict == "reject" or rule.get("verdict") == "reject":
        verdict = "reject"
    else:
        verdict = "pass"

    conclusion = {
        "verdict": verdict,
        "issues": issues,
        "reasonableness": llm.get("reasonableness", ""),
        "suggestion": llm.get("suggestion", ""),
        "total_amount": state["total_amount"],
    }
    logger.info("procurement.merged", verdict=verdict, issues=len(issues))
    return {"ai_conclusion": conclusion, "ai_verdict": verdict}


async def human_in_the_loop_node(state: ProcurementState) -> dict:
    """HitL：interrupt 冻结等待人工审批（对标 EduAgent 6.9）
    小额低风险（<1万且双轨均 pass）自动通过；否则 interrupt 人工审批
    """
    conclusion = state.get("ai_conclusion", {})
    amount = state["total_amount"]
    verdict = state.get("ai_verdict", "review")

    if verdict == "pass" and amount < 10000:
        return {
            "teacher_decision": {"decision": "approved", "comment": "小额自动通过"},
            "final_verdict": "approved",
        }

    display_data = {
        "order_no": state["order_no"],
        "material_name": state["material_name"],
        "quantity": state["quantity"],
        "unit_price": state["unit_price"],
        "total_amount": amount,
        "ai_verdict": verdict,
        "ai_conclusion": conclusion,
    }
    decision = interrupt(display_data)   # ← 冻结在此，等待 Command(resume=...)
    final_verdict = decision.get("decision", "rejected")
    updates = {
        "teacher_decision": decision,
        "final_verdict": final_verdict,
        "final_comment": decision.get("comment", ""),
    }
    # modify 分支：教师改单（改数量/单价 → 重算金额），对齐 EduAgent 6.10 modify 决策
    if final_verdict == "modify":
        new_qty = decision.get("new_quantity")
        new_price = decision.get("new_unit_price")
        if new_qty is not None and new_qty > 0:
            updates["quantity"] = new_qty
        if new_price is not None and new_price > 0:
            updates["unit_price"] = new_price
        new_total = updates.get("quantity", state["quantity"]) * updates.get("unit_price", state["unit_price"])
        updates["total_amount"] = round(new_total, 2)
        # modify 视为通过（改单后批准）
        updates["final_verdict"] = "approved"
        updates["final_comment"] = f"教师改单后批准：{decision.get('comment', '')}"
        logger.info("procurement.modified",
                    quantity=updates.get("quantity"), unit_price=updates.get("unit_price"),
                    total_amount=updates["total_amount"])
    return updates


async def publish_node(state: ProcurementState) -> dict:
    """发布：写库 + 生成批复文案（对标 EduAgent 6.10 合并发布）"""
    decision = state.get("teacher_decision", {})
    final_verdict = state.get("final_verdict", "rejected")
    comment = state.get("final_comment", "")
    conclusion = state.get("ai_conclusion", {})

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
                "material_name": state["material_name"],
                "quantity": state["quantity"],
                "unit_price": state["unit_price"],
                "total_amount": state["total_amount"],
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
        answer = "采购单 " + state["order_no"] + " 审批" + verdict_cn + "。" + ("备注：" + comment if comment else "")

    return {
        "answer": answer,
        "structured_output": {
            "order_no": state["order_no"],
            "verdict": final_verdict,
            "ai_conclusion": conclusion,
            "decision": decision,
        },
    }