"""BIM 模型合规审查（规则引擎 + LLM 双轨，对标采购审批的 parallel_review 范式）

规则轨（确定性，不调 LLM）：基于 ifc_parser 提取的结构化数据做完整性/质量检查
  - 模型为空 / 无构件 → high
  - 无空间（IFCSPACE）→ medium（无法做房间级合规核查）
  - 无属性集（IFCPROPERTYSET）→ medium（缺材料/尺寸参数，后续规则无从校验）
  - 未命名构件占比 > 50% → low（建模规范问题，影响构件追溯）
  - schema 为 IFC2X3 → low（建议 IFC4，交付/协同兼容性更好）
LLM 轨：把构件统计 + 属性样本交给模型，输出 JSON（合规观察/建议/风险等级）
"""
import json

from backend.core.llm_factory import get_llm
from backend.core.llm_text import msg_text, parse_json_loose
from backend.core.logger import get_logger
from langchain_core.messages import HumanMessage

logger = get_logger(__name__)

BIM_REVIEW_PROMPT = """你是 BIM 模型合规审查专家。请基于以下 IFC 模型提取数据，完成 BIM 模型合规审查，
输出 JSON（不要有其他内容）：
{{"risk_level": "low/medium/high", "observations": ["观察1"], "suggestions": ["建议1"],
  "verdict": "pass/review", "summary": "一句话结论"}}

审查要点：
- 模型完整性与建模质量（构件命名、空间定义、属性完备性）
- 交付合规性（schema 版本、信息深度是否满足协同/算量/审查需要）
- 结合规则引擎已发现的问题（如有），不要遗漏但可以补充

规则引擎已发现问题：
{rule_issues}

IFC 模型提取数据：
{model_data}
"""


def run_rule_checks(parsed: dict) -> list[dict]:
    """规则引擎：对解析结果做确定性检查，返回 [{priority, description}]"""
    issues: list[dict] = []
    total = parsed.get("total_elements", 0)
    elements = parsed.get("elements", [])
    if total == 0:
        issues.append({"priority": "high", "description": "模型中未提取到任何建筑构件（墙/柱/梁/板/门窗等），模型可能为空或类型缺失"})
    if not parsed.get("total_spaces"):
        issues.append({"priority": "medium", "description": "模型未定义空间（IFCSPACE），无法进行房间级功能/面积合规核查"})
    if not parsed.get("properties"):
        issues.append({"priority": "medium", "description": "模型无属性集（IFCPROPERTYSET），缺少材料/尺寸等参数信息，深度合规校验无据可依"})
    unnamed = sum(1 for e in elements if not e.get("name") or e["name"] == "未命名")
    if elements and unnamed / len(elements) > 0.5:
        issues.append({"priority": "low",
                       "description": f"未命名构件占比过高（{unnamed}/{len(elements)}），影响构件追溯与审查"})
    if str(parsed.get("schema", "")).upper().startswith("IFC2X3"):
        issues.append({"priority": "low", "description": "模型使用 IFC2X3 schema，建议升级 IFC4 以获得更好的交付与协同兼容性"})
    return issues


async def llm_compliance_review(parsed: dict, rule_issues: list[dict]) -> dict:
    """LLM 轨：模型数据摘要 → JSON 审查结论；失败返回兜底（纯规则结论）"""
    model_data = json.dumps({
        "schema": parsed.get("schema"),
        "building": parsed.get("building"),
        "elements_count": parsed.get("elements_count"),
        "total_elements": parsed.get("total_elements"),
        "total_spaces": parsed.get("total_spaces"),
        "properties": (parsed.get("properties") or [])[:15],
    }, ensure_ascii=False)
    prompt = BIM_REVIEW_PROMPT.format(
        rule_issues=json.dumps(rule_issues, ensure_ascii=False) or "（无）",
        model_data=model_data,
    )
    try:
        llm = get_llm("bid_review", temperature=0)
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        result = parse_json_loose(msg_text(resp)) or {}
    except Exception as e:
        logger.warning("bim.llm_review_failed", error=str(e)[:120])
        result = {}
    if not result:
        # LLM 不可用 → 纯规则兜底结论
        high = [i for i in rule_issues if i["priority"] == "high"]
        result = {
            "risk_level": "high" if high else ("medium" if rule_issues else "low"),
            "observations": [i["description"] for i in rule_issues] or ["规则引擎未发现问题"],
            "suggestions": ["补充完善模型信息后重新提交审查"],
            "verdict": "review" if rule_issues else "pass",
            "summary": "LLM 审查不可用，以上为规则引擎结论",
        }
    return result


async def run_bim_review(parsed: dict) -> dict:
    """双轨合并：规则问题 + LLM 结论 → 统一审查结果"""
    rule_issues = run_rule_checks(parsed)
    llm_result = await llm_compliance_review(parsed, rule_issues)
    # 规则发现的 high 问题强制进入观察列表（LLM 不应吞掉确定性缺陷）
    observations = list(llm_result.get("observations", []))
    for i in rule_issues:
        if i["priority"] == "high" and i["description"] not in observations:
            observations.insert(0, i["description"])
    risk = llm_result.get("risk_level", "low")
    if any(i["priority"] == "high" for i in rule_issues) and risk != "high":
        risk = "high"
    return {
        "rule_issues": rule_issues,
        "observations": observations,
        "suggestions": llm_result.get("suggestions", []),
        "risk_level": risk,
        "verdict": llm_result.get("verdict", "pass"),
        "summary": llm_result.get("summary", ""),
    }
