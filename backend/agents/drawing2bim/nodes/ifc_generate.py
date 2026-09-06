"""drawing2bim IFC 生成闭环节点（架构图 R→S→T→U→V）

流程：生成(MCP) → 解析回读(MCP) → 差异比对 → （有差异）修正回退 → 重试
- 最多 MAX_CORRECTION_ITERATIONS 轮修正重试，不做"差异归零"死循环
- 任何 MCP 失败优雅降级为 skipped/error（不阻断审查主流程）
- 仅在审查通过（verdict pass/approved）时由图的条件边触发
"""
import os

from backend.agents.drawing2bim.state import Drawing2BimState
from backend.agents.drawing2bim.labels import IFC_TYPE_CN
from backend.agents.drawing2bim.nodes.ifc_diff import (
    compute_ifc_diffs, apply_corrections, MAX_CORRECTION_ITERATIONS,
    format_diff_report_natural,
)
from backend.core.logger import get_logger
from backend.engines.validator import Validator

logger = get_logger(__name__)

# 属性键 → 中文（报告"比对范围"展示，全中文）
_ATTR_CN = {
    "Thickness": "厚度", "Length": "长度", "Width": "宽度", "Height": "高度",
    "Elevation": "标高", "Offset": "偏移", "Area": "面积", "Volume": "体积",
    "Perimeter": "周长", "Depth": "深度", "Material": "材料", "Reference": "类型名称",
    "Name": "名称", "FireRating": "耐火等级", "IsExternal": "是否外墙",
    "LoadBearing": "是否承重", "NominalWidth": "名义宽度", "PitchAngle": "坡度角",
}

def _ifc_mcp_url() -> str:
    from backend.config import get_settings
    return get_settings().mcp_ifc_parser_url
# 生成产物落盘目录（项目根 data/generated/，与运行时产物约定一致）
_GENERATED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))),
    "data", "generated")


async def _mcp_generate(call_mcp_tool, baseline: list[dict], out_path: str, role) -> dict | None:
    try:
        results = await call_mcp_tool(_ifc_mcp_url(), "generate_ifc_model",
                                      {"baseline": baseline, "out_path": out_path}, role=role)
    except Exception as e:
        # ★ 传输级失败（MCP 服务未启动/网络错误/超时）→ 返回 None 走调用点降级分支，
        # 不阻断审查主流程（docstring 承诺的"任何 MCP 失败优雅降级"）
        logger.warning("ifc_generate.mcp_call_failed", error=str(e)[:120])
        return None
    if results and isinstance(results[0], dict) and "error" not in results[0]:
        return results[0]
    if results and isinstance(results[0], dict):
        logger.warning("ifc_generate.mcp_error", error=str(results[0].get("error"))[:120])
    return None


async def _mcp_parse(call_mcp_tool, ifc_path: str, role) -> dict | None:
    try:
        results = await call_mcp_tool(_ifc_mcp_url(), "parse_ifc_model",
                                      {"ifc_path": ifc_path}, role=role)
    except Exception as e:
        logger.warning("ifc_generate.mcp_parse_call_failed", error=str(e)[:120])
        return None
    if results and isinstance(results[0], dict) and "error" not in results[0]:
        return results[0]
    return None


# 顶层几何/物理属性 → properties（IFC Pset 来源），键名对齐 _ATTR_CN 以便中文展示
_TOP_PROP_KEYS = {"thickness": "Thickness", "height": "Height", "width": "Width",
                  "depth": "Depth", "base": "Elevation", "top": "Offset",
                  "level": "Level", "material": "Material"}
# 默认材料（IR 无材料信息时不静默留空，标记 material_source=default 可追溯）
_DEFAULT_MATERIAL = "混凝土"


def _wall_length_mm(e: dict) -> float | None:
    s, t = e.get("start"), e.get("end")
    if not (isinstance(s, (list, tuple)) and isinstance(t, (list, tuple))
            and len(s) >= 2 and len(t) >= 2):
        return None
    return ((s[0] - t[0]) ** 2 + (s[1] - t[1]) ** 2) ** 0.5


def _clean_baseline(baseline: list[dict]) -> tuple[list[dict], dict]:
    """生成前数据清洗：剔除零长度墙、顶层属性归一化进 properties、material 兜底。

    返回 (清洗后生成集, 统计信息)；不修改原 state 中的构件（逐件浅拷贝）。
    """
    dropped_zero_len = 0
    material_defaulted = 0
    cleaned: list[dict] = []
    for e in baseline:
        if e.get("review_status") == "rejected":
            continue  # HITL 驳回的不进生成集
        if (e.get("type") or "").lower() in ("wall",):
            L = _wall_length_mm(e)
            if L is not None and L < 10:
                dropped_zero_len += 1
                continue  # 零长度退化墙，建出来是坏构件
        ec = dict(e)
        props = dict(ec.get("properties") or {})
        for key, prop_key in _TOP_PROP_KEYS.items():
            if key in ec and prop_key not in props:
                props[prop_key] = ec[key]
        if "Material" not in props:
            props["Material"] = _DEFAULT_MATERIAL
            props["material_source"] = "default"
            material_defaulted += 1
        ec["properties"] = props
        cleaned.append(ec)
    return cleaned, {"dropped_zero_len_walls": dropped_zero_len,
                     "material_defaulted": material_defaulted}


async def ifc_generate_node(state: Drawing2BimState) -> dict:
    """IFC 生成闭环节点"""
    from backend.mcp.client import call_mcp_tool

    if not state.get("generate_ifc"):
        return {"ifc_generation": {"status": "skipped", "reason": "未请求生成"}}

    baseline = state.get("final_baseline") or state.get("golden_baseline") or []
    if not baseline:
        return {"ifc_generation": {"status": "skipped", "reason": "无可用基准"}}

    os.makedirs(_GENERATED_DIR, exist_ok=True)
    session_id = state.get("session_id") or "default"
    out_path = os.path.join(_GENERATED_DIR, f"generated_{session_id}.ifc")
    role = state.get("role")

    # 生成前清洗：剔除 rejected 与零长度墙、属性归一化、material 兜底
    gen_set, clean_stats = _clean_baseline(baseline)
    logger.info("ifc_generate.start", session_id=session_id,
                baseline_count=len(baseline),
                rejected_count=len(baseline) - len(gen_set) - clean_stats["dropped_zero_len_walls"],
                dropped_zero_len_walls=clean_stats["dropped_zero_len_walls"],
                material_defaulted=clean_stats["material_defaulted"],
                to_generate=len(gen_set), out_path=out_path)

    iterations = 0
    corrections_applied: list[str] = []
    diffs: list[dict] = []
    last_parsed = None

    def _evidence(parsed_elems: list[dict]) -> dict:
        """核查证据链：数量闭环 + 比对范围 + 构件清单（人可复现）"""
        written = len(gen_set)
        parsed_count = len(parsed_elems)
        # 比对范围：提取构件实际携带的属性键（并集）
        attr_keys: list[str] = []
        for e in gen_set:
            for k in (e.get("properties") or {}).keys():
                if k not in attr_keys:
                    attr_keys.append(k)
        # 构件清单（截断前 30，人可对照图纸抽查；全中文）
        samples = []
        for e in gen_set[:30]:
            props = {_ATTR_CN.get(k, k): v for k, v in (e.get("properties") or {}).items()}
            samples.append({"element_id": e.get("element_id", ""),
                            "ifc_type": IFC_TYPE_CN.get(e.get("ifc_type", ""), e.get("ifc_type", "")),
                            "name": e.get("name", ""),
                            "properties": props})
        return {"extracted_count": written, "written_count": written,
                "parsed_count": parsed_count,
                "compared_attributes": [_ATTR_CN.get(k, k) for k in (attr_keys or [])] or ["（无属性，仅存在性比对）"],
                "sample_elements": samples,
                "ifc_path": out_path}

    while iterations <= MAX_CORRECTION_ITERATIONS:
        gen_result = await _mcp_generate(call_mcp_tool, gen_set, out_path, role)
        if gen_result is None:
            return {"ifc_generation": {"status": "error",
                                       "reason": "IFC 生成工具调用失败",
                                       "iterations": iterations}}
        iterations += 1

        # 回读比对（第一轮差异比对，架构图 T）
        parsed = await _mcp_parse(call_mcp_tool, out_path, role)
        if parsed is None:
            return {"ifc_generation": {"status": "error",
                                       "reason": "IFC 回读失败，无法完成差异比对",
                                       "ifc_path": out_path, "iterations": iterations}}
        last_parsed = parsed
        # 闭环校验（IR 基准 vs IFC 回读），报告落盘 data/generated/validate_{session}.json
        validate_report = Validator.compare_ifc(gen_set, parsed, session_id=session_id)
        diffs = compute_ifc_diffs(gen_set, parsed)
        if not diffs:
            logger.info("ifc_generate.verified", iterations=iterations)
            return {"ifc_generation": {
                "status": "verified", "ifc_path": out_path,
                "elements": len(gen_set), "diffs": [],
                "natural_report": "图纸与生成 IFC 完全一致，无差异。",
                "evidence": _evidence(parsed.get("elements", [])),
                "validation": validate_report,
                "iterations": iterations, "corrections_applied": corrections_applied,
            }}

        # 有差异 → 修正回退（架构图 U），无修正可做则终止循环
        gen_set, applied = apply_corrections(gen_set, diffs)
        if not applied:
            break
        corrections_applied.extend(applied)

    logger.info("ifc_generate.diffs_found", iterations=iterations, diffs=len(diffs))
    # 自然语言差异报告（位置 + 构件 + 差异，LLM 生成；失败降级模板）
    natural_report = await format_diff_report_natural(diffs, out_path)
    return {"ifc_generation": {
        "status": "diffs_found", "ifc_path": out_path,
        "elements": len(gen_set), "diffs": diffs,
        "natural_report": natural_report,
        "evidence": _evidence((last_parsed or {}).get("elements", [])),
        "validation": validate_report,
        "iterations": iterations, "corrections_applied": corrections_applied,
    }}
