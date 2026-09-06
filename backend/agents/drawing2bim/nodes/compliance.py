"""drawing2bim 合规审查节点（双轨制）

硬轨：确定性规则引擎（数值/几何/强条），不调 LLM，输出确定性违规清单
软轨：LLM 语义审查（命名规范/属性完整性/空间逻辑），输出带置信度的疑似问题

两轨独立运行，结果在 report_merge 节点融合。
"""
import json
import re

from backend.agents.drawing2bim.state import Drawing2BimState, ComplianceViolation
from backend.agents.drawing2bim.labels import IFC_TYPE_CN
from backend.core.llm_factory import get_llm
from backend.core.llm_text import msg_text, parse_json_loose
from backend.core.logger import get_logger
from langchain_core.messages import HumanMessage

logger = get_logger(__name__)


# IFC 属性键 → 中文（违规构件原始数据"原样翻译"）
_IFC_PROP_CN = {
    "IsExternal": "是否外墙", "LoadBearing": "是否承重", "Reference": "类型名称",
    "Thickness": "厚度", "Width": "宽度", "Height": "高度", "Elevation": "标高",
    "Offset": "偏移", "Material": "材料", "FireRating": "耐火等级",
    "ThermalTransmittance": "传热系数", "ExtendToStructure": "延伸到结构",
    "NominalWidth": "名义宽度", "Depth": "深度", "Area": "面积", "Volume": "体积",
    "Perimeter": "周长", "PitchAngle": "坡度角", "GrossArea": "毛面积",
    "NetArea": "净面积", "GrossVolume": "毛体积", "NetVolume": "净体积",
    "Length": "长度", "Status": "状态", "GlobalId": "IFC标识",
    "Name": "名称", "Description": "描述", "ObjectType": "对象类型",
    "PredefinedType": "预定义类型", "Pset_QuantityTakeOff": "工程量属性集",
    "Pset_SlabCommon": "楼板通用属性", "Pset_WallCommon": "墙体通用属性",
    "Pset_ColumnCommon": "柱通用属性", "Pset_BeamCommon": "梁通用属性",
    "Pset_DoorCommon": "门通用属性", "Pset_WindowCommon": "窗通用属性",
    "Pset_ReinforcementBarPitchOfSlab": "楼板配筋间距属性",
}


def _translate_props(props: dict, limit: int = 14) -> dict:
    """IFC 属性原样翻译：键 → 中文，布尔 → 是/否；保留原值（防丢失）"""
    out = {}
    for k, v in list(props.items())[:limit]:
        if k == "id":
            continue
        cn = _IFC_PROP_CN.get(str(k), str(k))
        if isinstance(v, bool):
            v = "是" if v else "否"
        elif isinstance(v, float):
            v = round(v, 2)
        out[cn] = v
    return out

# ── 硬轨：规则引擎 ────────────────────────────────────────────────────────

# 强条规则表：(rule_id, 检查函数, severity, description_template)
# 检查函数签名：(element: dict) -> bool，返回 True 表示违规
_HARD_RULES: list[tuple[str, callable, str, str]] = []


# 规则适用判定（审查依据统计用：每条规则查了哪些构件）
_RULE_APPPLY_TYPES = {
    "HR-001": ("IFCWALL",),
    "HR-002": ("IFCSLAB",),
    "HR-003": None,          # 所有构件
    "HR-004": ("IFCWALL", "IFCCOLUMN", "IFCBEAM"),
    "HR-005": ("IFCSLAB",),
    "HR-006": ("IFCCOLUMN",),
    "HR-007": ("IFCBEAM", "IFCMEMBER"),
}


def _rule_applies(check_fn, elem: dict) -> bool:
    """规则是否适用于该构件（按规则前置类型）"""
    rule_id = getattr(check_fn, "__rule_id__", "")
    types = _RULE_APPPLY_TYPES.get(rule_id)
    if types is None:
        return True
    return elem.get("ifc_type") in types


def _register_hard_rule(rule_id: str, severity: str, description: str):
    """装饰器：注册一条硬轨规则"""
    def decorator(fn):
        fn.__rule_id__ = rule_id
        _HARD_RULES.append((rule_id, fn, severity, description))
        return fn
    return decorator


def _extract_thickness(props: dict) -> float | None:
    """从合并后的属性中提取墙体厚度（mm）

    优先级：显式厚度键（Thickness/Width/厚度/NominalWidth）
            → Reference 文本解析（Revit 常见：'砌体墙-200mm' → 200）
    """
    for key in ("Thickness", "Width", "厚度", "NominalWidth"):
        v = props.get(key)
        if v is None:
            continue
        try:
            fv = float(str(v).strip())
            if fv > 0:
                return fv
        except (ValueError, TypeError):
            continue
    # Revit 导出：墙厚常藏在 Pset 的 Reference（'砌体墙-200mm'）
    ref = props.get("Reference")
    if isinstance(ref, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*mm", ref)
        if m and float(m.group(1)) > 0:
            return float(m.group(1))
    return None


def _has_material(props: dict) -> bool:
    """构件是否有材料/类型定义信息（感知层已把 Materials 合并为 Material 键）"""
    for key in ("Material", "material", "材料", "Grade", "grade", "TypeName", "ObjectType"):
        if props.get(key):
            return True
    return False


@_register_hard_rule("HR-001", "critical", "墙体厚度未定义或为零")
def _check_wall_thickness(elem: dict) -> bool:
    if elem.get("ifc_type") != "IFCWALL":
        return False
    # IFC 输入：墙有几何表示（任何类型）→ 厚度存在于模型几何，不算"未定义"
    if elem.get("has_geometry") and elem.get("source") == "ifc_parse":
        return False
    return _extract_thickness(elem.get("properties", {})) is None


@_register_hard_rule("HR-002", "warning", "楼板标高缺失")
def _check_slab_elevation(elem: dict) -> bool:
    if elem.get("ifc_type") != "IFCSLAB":
        return False
    # 建筑面层（地砖/环氧等地面做法）不参与结构规则（非受力构件）
    if elem.get("element_category") == "finishing":
        return False
    # IFC 输入：板有几何表示（B-rep/挤出）→ 标高在几何里（或所属楼层），不算"缺失"
    if elem.get("has_geometry") and elem.get("source") == "ifc_parse":
        return False
    props = elem.get("properties", {})
    elevation = props.get("Elevation") or props.get("Offset") or props.get("标高")
    return elevation is None


@_register_hard_rule("HR-005", "warning", "楼板厚度未定义或小于100mm")
def _check_slab_thickness(elem: dict) -> bool:
    """结构楼板必须有厚度且≥100mm（GB50010-2010 8.2.1 现浇钢筋混凝土板最小厚度）"""
    if elem.get("ifc_type") != "IFCSLAB":
        return False
    # 面层板不参与结构规则
    if elem.get("element_category") == "finishing":
        return False
    if elem.get("has_geometry") and elem.get("source") == "ifc_parse":
        return False
    t = _extract_thickness(elem.get("properties", {}))
    if t is None:
        return False   # 有几何的板厚度在几何里，仅对无几何构件判缺失
    return t < 100.0


@_register_hard_rule("HR-006", "warning", "柱截面尺寸未定义或异常")
def _check_column_section(elem: dict) -> bool:
    """柱必须有截面尺寸（GB50010 柱截面设计要求）"""
    if elem.get("ifc_type") != "IFCCOLUMN":
        return False
    if elem.get("has_geometry") and elem.get("source") == "ifc_parse":
        return False
    props = elem.get("properties", {})
    # 截面宽度/高度（Qto_ColumnBaseQuantities.Width/Height 或 Psets）
    w = props.get("Width") or props.get("宽度")
    h = props.get("Height") or props.get("高度")
    return w is None and h is None


@_register_hard_rule("HR-007", "warning", "梁截面尺寸未定义或异常")
def _check_beam_section(elem: dict) -> bool:
    """梁必须有截面尺寸（GB50010 梁截面设计要求）"""
    if elem.get("ifc_type") not in ("IFCBEAM", "IFCMEMBER"):
        return False
    # 非结构杆件（支撑/连接件）不参与
    if elem.get("element_category") == "non_structural":
        return False
    if elem.get("has_geometry") and elem.get("source") == "ifc_parse":
        return False
    props = elem.get("properties", {})
    w = props.get("Width") or props.get("宽度")
    h = props.get("Height") or props.get("高度")
    return w is None and h is None


@_register_hard_rule("HR-003", "warning", "构件未命名")
def _check_unnamed(elem: dict) -> bool:
    name = elem.get("name", "")
    return not name or name == "未命名"


@_register_hard_rule("HR-004", "warning", "关键属性集缺失")
def _check_missing_properties(elem: dict) -> bool:
    props = elem.get("properties", {})
    ifc_type = elem.get("ifc_type", "")
    # 墙/柱/梁必须有材料或类型信息（感知层已合并 Materials/Psets）
    if ifc_type in ("IFCWALL", "IFCCOLUMN", "IFCBEAM"):
        return not _has_material(props)
    return False


def hard_track_review(baseline: list[dict], source: str = "drawing") -> list[ComplianceViolation]:
    """硬轨审查：遍历黄金基准，逐条规则检查，返回确定性违规清单

    source: "ifc"（IFC 模型评审）/ "drawing"（DXF/PDF 图纸评审）
    规则错配防护：IFC 语义规则（HR-004 属性集/材料）仅对 IFC 输入启用——
    图纸是二维几何，无属性集概念，查材料属性属于误报（材料在 IFC 生成阶段保证）。
    """
    violations: list[ComplianceViolation] = []
    for elem in baseline:
        for rule_id, check_fn, severity, desc_template in _HARD_RULES:
            # 规则错配防护：HR-004（关键属性集缺失）仅 IFC 输入适用
            if rule_id == "HR-004" and source != "ifc":
                continue
            try:
                if check_fn(elem):
                    violations.append({
                        "rule_id": rule_id,
                        "severity": severity,
                        "track": "hard",
                        "element_id": elem.get("element_id", ""),
                        # ── 违规定位：构件名/楼层/类型（报告可直接看是哪里的构件）──
                        "element_name": elem.get("name", ""),
                        "element_floor": elem.get("floor") or elem.get("楼层") or "",
                        "element_type": IFC_TYPE_CN.get(elem.get("ifc_type", ""), elem.get("ifc_type", "")),
                        # ── 位置坐标（ObjectPlacement 全局 X/Y/Z，模型里可搜）──
                        "element_placement": elem.get("placement"),
                        # ── IFC 原始数据原样翻译（属性键值对，人可对照 IFC 文件）──
                        "element_props": _translate_props(elem.get("properties", {})),
                        "description": desc_template,
                        "confidence": 1.0,
                        "explanation": "",
                        "suggestion": f"请补充或修正该构件的相关属性",
                    })
            except Exception as e:
                logger.warning("compliance.hard_rule_error", rule=rule_id, error=str(e)[:80])
    return violations


# ── 软轨：LLM 语义审查 ────────────────────────────────────────────────────

async def soft_track_review(baseline: list[dict], regulation_context: str = "",
                            design_notes: str = "", memory_context: str = "") -> list[ComplianceViolation]:
    """软轨审查：LLM 语义级合规判断，返回带置信度的疑似问题清单"""
    from backend.agents.drawing2bim.prompts import SOFT_COMPLIANCE_REVIEW_PROMPT

    baseline_json = json.dumps(baseline[:50], ensure_ascii=False)  # 截断防超 token
    prompt = SOFT_COMPLIANCE_REVIEW_PROMPT.format(
        baseline_json=baseline_json,
        design_notes=design_notes or "（图纸未读取到设计说明文字）",
        regulation_context=regulation_context or "（无额外规范上下文）",
    )

    try:
        llm = get_llm("qa", temperature=0)
        from backend.application.agent_memory import memory_prompt
        resp = await llm.ainvoke([HumanMessage(content=memory_prompt({"memory_context": memory_context}, prompt))])
        items = parse_json_loose(msg_text(resp)) or []
    except Exception as e:
        logger.warning("compliance.soft_track_failed", error=str(e)[:120])
        return []

    if not isinstance(items, list):
        return []

    violations: list[ComplianceViolation] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        violations.append({
            "rule_id": item.get("rule_id", "llm_soft"),
            "severity": item.get("severity", "info"),
            "track": "soft",
            "element_id": item.get("element_id", ""),
            "description": item.get("description", ""),
            "confidence": float(item.get("confidence", 0.5)),
            "explanation": item.get("explanation", ""),
            "suggestion": item.get("suggestion", ""),
        })
    return violations


# ── LangGraph 节点入口 ────────────────────────────────────────────────────

# ── 构件类别筛选（LLM：面层板 vs 结构板 vs 楼梯平台）────────────────────────

_SLAB_CLASSIFY_PROMPT = """你是建筑构件分类专家。Revit 导出的 IFC 中，IfcSlab（楼板）混有三类构件：
- structural：结构受力板（结构楼板/基础底板，材料为混凝土，承重）
- finishing：建筑面层（地面做法：地砖/环氧/细石/耐磨/防水/找平等，不承重）
- landing：楼梯休息平台

请根据每块板的【名称】【材料】【承重标记】分类，输出 JSON 对象：{{"element_id": "类别"}}
只返回 JSON，不要附加说明。如果无法判断默认 structural。

板清单（JSON）：
{slabs_json}
"""

# 规则兜底关键词（LLM 失败时用）
_FINISHING_KEYWORDS = ("地砖", "环氧", "细石", "耐磨", "防水", "找平", "面层", "地坪", "贴砖", "地面")


def _classify_slab_rules(slab: dict) -> str:
    """规则兜底：名字/材料含面层关键词 → finishing；PredefinedType=LANDING → landing"""
    if slab.get("PredefinedType") == "LANDING":
        return "landing"
    text = f"{slab.get('name', '')} {slab.get('material', '')}"
    if any(k in text for k in _FINISHING_KEYWORDS):
        return "finishing"
    return "structural"


async def classify_slabs_llm(baseline: list[dict]) -> dict[str, str]:
    """LLM 批量分类楼板（structural/finishing/landing）；失败降级规则兜底"""
    slabs = [e for e in baseline if e.get("ifc_type") == "IFCSLAB"]
    result: dict[str, str] = {}
    if not slabs:
        return result
    # 规则兜底先行（保证必有值）
    for s in slabs:
        result[s["element_id"]] = _classify_slab_rules({
            "name": s.get("name", ""), "PredefinedType": s.get("PredefinedType"),
            "material": (s.get("properties") or {}).get("Material", ""),
        })
    try:
        from backend.core.llm_factory import get_llm
        from langchain_core.messages import HumanMessage
        payload = [{"element_id": s["element_id"], "name": s.get("name", ""),
                    "predefined_type": s.get("PredefinedType"),
                    "material": (s.get("properties") or {}).get("Material", ""),
                    "load_bearing": (s.get("properties") or {}).get("LoadBearing")}
                   for s in slabs[:200]]
        llm = get_llm("qa", temperature=0)
        resp = await llm.ainvoke([HumanMessage(content=_SLAB_CLASSIFY_PROMPT.format(
            slabs_json=json.dumps(payload, ensure_ascii=False)[:6000]))])
        parsed = parse_json_loose(resp.content if hasattr(resp, "content") else str(resp))
        if isinstance(parsed, dict):
            for eid, cat in parsed.items():
                if cat in ("structural", "finishing", "landing") and eid in result:
                    result[eid] = cat
        logger.info("compliance.slab_classified", total=len(slabs),
                    finishing=sum(1 for v in result.values() if v == "finishing"))
    except Exception as e:
        logger.warning("compliance.slab_classify_failed", error=str(e)[:100])
    return result


async def classify_members_llm(baseline: list[dict]) -> dict[str, str]:
    """LLM 批量分类梁/杆件（structural_beam 结构梁 / non_structural 支撑连接件）

    Revit 导出 IFCMEMBER 混有结构梁、支撑、连接件、幕墙龙骨等——
    仅结构梁参与梁截面规则。
    """
    members = [e for e in baseline if e.get("ifc_type") in ("IFCBEAM", "IFCMEMBER")]
    result: dict[str, str] = {}
    if not members:
        return result
    _NON_STRUCTURAL_KEYWORDS = ("支撑", "连接", "加固", "系杆", "拉条", "马镫", "龙骨", "角码", "预埋", "垫片")
    for m in members:
        text = f"{m.get('name', '')} {(m.get('properties') or {}).get('Material', '')}"
        result[m["element_id"]] = ("non_structural" if any(k in text for k in _NON_STRUCTURAL_KEYWORDS)
                                   else "structural_beam")
    try:
        from backend.core.llm_factory import get_llm
        from langchain_core.messages import HumanMessage
        payload = [{"element_id": m["element_id"], "name": m.get("name", ""),
                    "material": (m.get("properties") or {}).get("Material", "")}
                   for m in members[:200]]
        prompt = ("你是建筑构件分类专家。IFC 的梁/杆件混有两类："
                  "structural_beam=结构受力梁（框架梁/次梁，承重）；"
                  "non_structural=非结构杆件（支撑、连接件、加固件、龙骨、预埋件等）。"
                  "根据【名称】【材料】分类，输出 JSON：{{\"element_id\": \"类别\"}}，只返回 JSON。\n\n"
                  "杆件清单：\n" + json.dumps(payload, ensure_ascii=False)[:6000])
        llm = get_llm("qa", temperature=0)
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        parsed = parse_json_loose(resp.content if hasattr(resp, "content") else str(resp))
        if isinstance(parsed, dict):
            for eid, cat in parsed.items():
                if cat in ("structural_beam", "non_structural") and eid in result:
                    result[eid] = cat
        logger.info("compliance.member_classified", total=len(members),
                    non_structural=sum(1 for v in result.values() if v == "non_structural"))
    except Exception as e:
        logger.warning("compliance.member_classify_failed", error=str(e)[:100])
    return result


async def compliance_review_node(state: Drawing2BimState) -> dict:
    """双轨合规审查节点：并行执行硬轨+软轨，写入 state"""
    baseline = state.get("golden_baseline", [])
    if not baseline:
        logger.info("compliance.skip_empty_baseline")
        return {"hard_violations": [], "soft_violations": []}

    source = "ifc" if state.get("ifc_path") else "drawing"
    # LLM 构件分类（楼板：结构板/面层/楼梯平台 + 梁杆件：结构梁/非结构）——分类后仅结构构件参与结构规则
    slab_cats = await classify_slabs_llm(baseline)
    if slab_cats:
        for e in baseline:
            if e.get("ifc_type") == "IFCSLAB":
                e["element_category"] = slab_cats.get(e.get("element_id", ""), "structural")
    member_cats = await classify_members_llm(baseline)
    if member_cats:
        for e in baseline:
            if e.get("ifc_type") in ("IFCBEAM", "IFCMEMBER"):
                e["element_category"] = member_cats.get(e.get("element_id", ""), "structural_beam")
    hard = hard_track_review(baseline, source=source)
    # 软轨：注入图纸设计说明 + 国家规范要点库（核查依据）
    from backend.agents.drawing2bim.regulations import build_regulation_context
    regulation_context = build_regulation_context()
    soft = await soft_track_review(baseline,
                                   regulation_context=regulation_context,
                                   design_notes=state.get("drawing_notes") or "",
                                   memory_context=state.get("memory_context", ""))

    # 审查依据：规则执行统计（每条规则检查数/命中数）+ 构件类型分布
    rule_stats = {}
    for rule_id, check_fn, severity, desc in _HARD_RULES:
        checked = sum(1 for e in baseline if _rule_applies(check_fn, e))
        hit = sum(1 for v in hard if v["rule_id"] == rule_id)
        rule_stats[rule_id] = {"desc": desc, "severity": severity,
                               "checked": checked, "hit": hit}
    from collections import Counter
    element_stats = dict(Counter(e.get("ifc_type", "?") for e in baseline))
    element_stats = {IFC_TYPE_CN.get(k, k): v for k, v in element_stats.items()}
    slab_finishing = sum(1 for e in baseline if e.get("element_category") == "finishing")

    logger.info("compliance.review_done",
                hard_count=len(hard), soft_count=len(soft),
                slab_finishing=slab_finishing)
    return {
        "hard_violations": hard,
        "soft_violations": soft,
        "rule_stats": rule_stats,
        "element_stats": element_stats,
        "slab_finishing_count": slab_finishing,
        "slab_categories": {e.get("element_id"): e.get("element_category", "structural")
                            for e in baseline if e.get("ifc_type") == "IFCSLAB"},
    }
