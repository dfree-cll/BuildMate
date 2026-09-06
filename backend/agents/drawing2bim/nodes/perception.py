"""drawing2bim 感知节点（第五期：DXF 矢量通道接入）

通道优先级（perception_mode=auto 时）：
  DXF 矢量（最高精度）→ 视觉 → 文本约定格式 → IFC → 注入基准 → 内置样例

关键原则（第五期确立）：
- 格式级错误（DWG/不支持的扩展名）→ perception_error 短路，
  下游输出明确指引，禁止静默回退内置样例（假数据）
- 传输级失败（网关不通）→ 保留既有优雅降级（demo 兜底，离线可跑）
"""
import os
import uuid

from backend.agents.drawing2bim.state import Drawing2BimState
from backend.agents.drawing2bim.nodes.change_detection import (
    compute_content_hash, detect_change, save_content_hash,
)
from backend.agents.drawing2bim.nodes.extractors import extract_baseline_from_text
from backend.agents.drawing2bim.nodes.version_merge import merge_baseline
from backend.core.logger import get_logger
from backend.engines.drawing_guidance import DWG_GUIDANCE

logger = get_logger(__name__)

# 视觉通道可接受的文件扩展名
_VISION_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# 内置最小样例基准（离线 demo / 测试兜底）
_DEMO_BASELINE = [
    {
        "element_id": "wall-001", "ifc_type": "IFCWALL", "name": "外墙-W1",
        "properties": {"Thickness": 240, "Material": "烧结多孔砖"},
        "confidence": 0.95, "source": "demo", "review_status": "pending",
    },
    {
        "element_id": "slab-001", "ifc_type": "IFCSLAB", "name": "楼板-B1",
        "properties": {"Thickness": 120, "Material": "C30 混凝土"},
        "confidence": 0.9, "source": "demo", "review_status": "pending",
    },
]

def _drawing_mcp_url() -> str:
    from backend.config import get_settings
    return get_settings().mcp_drawing_perception_url


def _ifc_mcp_url() -> str:
    from backend.config import get_settings
    return get_settings().mcp_ifc_parser_url


async def _perceive_dxf(state: Drawing2BimState) -> dict | None:
    """DXF 矢量通道（第五期，精度最高）：parse_drawing_dxf(MCP) → 版本合并
    ★ 第六期: MCP 网关不可用时本地 ezdxf 降级(不依赖 Drawing MCP 服务)
    """
    from backend.mcp.client import call_mcp_tool
    parsed = None
    try:
        results = await call_mcp_tool(
            _drawing_mcp_url(), "parse_drawing_dxf",
            {"path": state["drawing_path"]}, role=state.get("role"))
        if results and isinstance(results[0], dict) and "error" not in results[0]:
            parsed = results[0]
        else:
            logger.warning("perception.dxf_mcp_empty",
                           error=str((results or [{}])[0].get("error", "no data"))[:120])
    except Exception as e:
        logger.warning("perception.dxf_gateway_failed", error=str(e)[:120])

    # ★ 本地降级: MCP 不可用 → ezdxf 直接解析(墙双线配对 + 柱/轴网自适应)
    if parsed is None:
        try:
            from backend.engines.dxf_parser import parse_dxf
            raw = await parse_dxf(state["drawing_path"])
            # 补 baseline: 本地解析 → dxf_extractor 生成黄金基准(墙/门窗)
            from backend.agents.drawing2bim.nodes.dxf_extractor import extract_baseline_from_dxf
            baseline_local = extract_baseline_from_dxf(raw)
            parsed = {**raw, "baseline": baseline_local}
            logger.info("perception.dxf_local_fallback", baseline=len(baseline_local))
        except Exception as e:
            logger.warning("perception.dxf_local_parse_failed", error=str(e)[:120])

    if parsed is None:
        return None

    wall_extraction = parsed.get("wall_extraction") or {}
    if wall_extraction.get("status") == "BLOCKED":
        reason = str(wall_extraction.get("reason") or "主平面选择不唯一")
        logger.warning("perception.dxf_wall_extraction_blocked", reason=reason[:160])
        return {
            "perception_error": f"DXF 墙体提取已阻断：{reason}",
            "drawing_source": "unsupported",
        }

    baseline = parsed.get("baseline") or []
    if not baseline:
        # DXF 可解析但零构件（图层混乱/空图）→ 短路并指引，不用假数据
        logger.warning("perception.dxf_zero_elements",
                       layers=str(parsed.get("layers", []))[:120])
        return {"perception_error": ("DXF 文件已解析但未识别出任何构件。"
                                     "请确认图纸按图层规范绘制（墙/门窗图层），"
                                     "或改用 PDF 图片走视觉通道。"),
                "drawing_source": "unsupported"}

    existing = state.get("golden_baseline") or []
    merged = merge_baseline(existing, baseline) if existing else baseline

    # ★ 第六期: 自适应柱/轴网提取(实体特征驱动, 不依赖图层规范)——补充 MCP 通道漏掉的柱/轴网/标高
    state_extra = {}   # 提到 try 之前：try 内早期抛异常时，下方 out.update 不再 UnboundLocalError
    try:
        from backend.agents.drawing2bim.nodes.dxf_adaptive import extract_columns_from_dxf
        path = state.get("drawing_path") or ""
        if path and path.lower().endswith(".dxf"):
            adaptive = extract_columns_from_dxf(path)
            cols = adaptive.get("columns") or []
            if cols:
                merged = merge_baseline(merged, cols)
                logger.info("perception.dxf_adaptive_columns", added=len(cols))
            grid = adaptive.get("grid")
            if grid:
                state_extra["dxf_grid"] = grid
            levels = adaptive.get("levels")
            if levels:
                state_extra["dxf_levels"] = levels
            meta = adaptive.get("meta") or {}
            if meta:
                state_extra["dxf_adaptive_meta"] = meta
            if meta.get("reason"):
                logger.warning("perception.dxf_adaptive_skip", reason=meta["reason"])
    except Exception as e:
        logger.warning("perception.dxf_adaptive_failed", error=str(e)[:150])

    logger.info("perception.dxf_perceived", extracted=len(baseline), merged_total=len(merged))
    out = {
        "golden_baseline": merged,
        "baseline_version": state.get("baseline_version", 0) + 1,
        "drawing_source": "drawing_dxf",
        "change_status": "first",
        "drawing_notes": (parsed.get("design_notes") or ""),
    }
    out.update(state_extra)
    return out


async def _perceive_vision(state: Drawing2BimState) -> dict | None:
    """视觉通道：多模态 LLM 提取图纸构件（第四期）"""
    from backend.mcp.client import call_mcp_tool
    try:
        results = await call_mcp_tool(
            _drawing_mcp_url(), "parse_drawing_vision",
            {"path": state["drawing_path"]}, role=state.get("role"))
    except Exception as e:
        logger.warning("perception.vision_gateway_failed", error=str(e)[:120])
        return None

    if not results or not isinstance(results[0], dict):
        return None
    parsed = results[0]
    if "error" in parsed:
        logger.warning("perception.vision_error", error=str(parsed["error"])[:120])
        return None

    baseline = parsed.get("baseline") or []
    if not baseline:
        logger.info("perception.vision_empty_result")
        return None

    existing = state.get("golden_baseline") or []
    merged = merge_baseline(existing, baseline) if existing else baseline
    logger.info("perception.vision_perceived", extracted=len(baseline), merged_total=len(merged))
    return {
        "golden_baseline": merged,
        "baseline_version": state.get("baseline_version", 0) + 1,
        "drawing_source": "drawing_vision",
        "change_status": "first",
    }


def _parsed_to_baseline(parsed: dict) -> list[dict]:
    """IFC 解析结果 → 黄金基准元素列表

    v2（2026-08-21）：感知层不再丢属性——Psets/Quantities/Materials/楼层 全部
    合并进 properties，硬轨规则才有数据可查（旧实现 properties 恒空导致
    HR-001/HR-004 对全部构件 100% 命中，审查结果全是废话）。
    """
    baseline = []
    type_map = {"墙": "IFCWALL", "柱": "IFCCOLUMN", "梁": "IFCBEAM", "楼板": "IFCSLAB",
                "门": "IFCDOOR", "窗": "IFCWINDOW", "楼梯": "IFCSTAIR", "屋顶": "IFCROOF"}
    for elem in parsed.get("elements", []):
        props: dict = {}
        # ① 属性集 Psets 展开（跳过 ifcopenshell 的 id 元信息，保留业务键）
        for pset in (elem.get("Psets") or {}).values():
            if not isinstance(pset, dict):
                continue
            for k, v in pset.items():
                if k == "id" or v is None:
                    continue
                props.setdefault(k, v)   # 首见保留（属性集顺序优先）
        # ② 工程量 Quantities 展开（墙厚常驻 Qto_WallBaseQuantities.Width）
        for qto in (elem.get("Quantities") or {}).values():
            if not isinstance(qto, dict):
                continue
            for k, v in qto.items():
                if k == "id" or v is None:
                    continue
                props.setdefault(k, v)
        # ③ 材料（首个材料名 → Material）
        mats = elem.get("Materials") or []
        if mats:
            props.setdefault("Material", mats[0].get("Name"))

        # ③' 几何厚度（墙：SweptSolid 轮廓 XDim——最可靠厚度来源）
        geom_t = elem.get("geom_thickness")
        if geom_t:
            props.setdefault("Thickness", geom_t)
        # ③'' 几何标高（楼板：挤出位置 Z / 楼层 Elevation 兜底）
        geom_e = elem.get("geom_elevation")
        if geom_e:
            props.setdefault("Elevation", geom_e)

        # ④ 楼层归属（Container → floor，供违规定位/展示）
        container = elem.get("Container") or {}
        floor = None
        if container.get("IfcClass") == "IfcBuildingStorey":
            floor = container.get("Name")

        baseline.append({
            "element_id": elem.get("global_id") or elem.get("GlobalId")
                          or f"elem-{uuid.uuid4().hex[:8]}",
            "ifc_type": type_map.get(elem.get("type"), elem.get("ifc_type", "")),
            "name": elem.get("name") or elem.get("Name", ""),
            "properties": props,
            "floor": floor,
            "PredefinedType": elem.get("PredefinedType"),
            "placement": elem.get("placement"),
            "has_geometry": bool(elem.get("has_geometry")),
            "confidence": 0.8,
            "source": "ifc_parse",
            "review_status": "pending",
        })
    return baseline


async def _perceive_drawing(state: Drawing2BimState) -> dict | None:
    """图纸通道：解析 → 变更检测 → 提取 → 合并。失败返回 None（走回退）"""
    from backend.mcp.client import call_mcp_tool

    try:
        results = await call_mcp_tool(
            _drawing_mcp_url(), "parse_drawing",
            {"path": state["drawing_path"]}, role=state.get("role"))
    except Exception as e:
        logger.warning("perception.drawing_gateway_failed", error=str(e)[:120])
        return None

    if not results or not isinstance(results[0], dict):
        return None
    parsed = results[0]
    if "error" in parsed:
        logger.warning("perception.drawing_parse_error", error=str(parsed["error"])[:120])
        return None

    text = parsed.get("text", "")
    content_hash = compute_content_hash(text)
    review_key = state.get("drawing_review_key") or state.get("session_id") or "default"
    change_status = await detect_change(review_key, content_hash)
    existing_baseline = state.get("golden_baseline") or []

    # 未变更且已有基准 → 跳过重感知，直接复用
    if change_status == "unchanged" and existing_baseline:
        logger.info("perception.unchanged_skip", review_key=review_key)
        return {
            "golden_baseline": existing_baseline,
            "drawing_hash": content_hash,
            "change_status": "unchanged",
            "drawing_source": state.get("drawing_source") or "drawing_text",
        }

    # first/changed → 结构化提取 + 版本合并
    extracted = extract_baseline_from_text(text)
    merged = merge_baseline(existing_baseline, extracted) if existing_baseline else extracted
    await save_content_hash(review_key, content_hash)

    logger.info("perception.drawing_perceived",
                change_status=change_status, extracted=len(extracted), merged_total=len(merged))
    return {
        "golden_baseline": merged,
        "baseline_version": state.get("baseline_version", 0) + 1,
        "drawing_hash": content_hash,
        "change_status": change_status,
        "drawing_source": "drawing_text",
    }


async def _perceive_ifc(state: Drawing2BimState) -> dict | None:
    """IFC 通道（第一期保留）"""
    from backend.mcp.client import call_mcp_tool
    try:
        results = await call_mcp_tool(
            _ifc_mcp_url(), "parse_ifc_model",
            {"ifc_path": state["ifc_path"]}, role=state.get("role"))
    except Exception as e:
        logger.warning("perception.ifc_gateway_failed", error=str(e)[:120])
        return None

    if results and isinstance(results[0], dict) and "error" not in results[0]:
        baseline = _parsed_to_baseline(results[0])
        logger.info("perception.ifc_parsed", elements=len(baseline))
        return {
            "golden_baseline": baseline,
            "baseline_version": state.get("baseline_version", 0) + 1,
            "drawing_source": "ifc_existing",
        }
    logger.warning("perception.ifc_parse_error",
                   error=str(results[0].get("error", ""))[:120] if results else "empty")
    return None


async def perception_node(state: Drawing2BimState) -> dict:
    """感知入口：按 perception_mode 调度通道优先级（第五期）

    auto（默认）: DXF 矢量 → 视觉 → 文本，按文件扩展名智能路由
    vision: 仅视觉；text: 仅文本
    之后依次尝试 IFC 通道 → 注入基准 → 内置样例

    格式级错误（DWG 等）→ 返回 perception_error，图短路到 error_report，
    禁止静默回退内置样例。
    """
    mode = state.get("perception_mode") or "auto"
    drawing_path = state.get("drawing_path") or ""
    ext = os.path.splitext(drawing_path)[1].lower() if drawing_path else ""

    # ① 图纸通道（按扩展名 + 模式调度）
    if drawing_path:
        # DWG：格式级短路（明确指引，不静默降级）
        if ext == ".dwg":
            logger.warning("perception.dwg_rejected")
            return {"perception_error": DWG_GUIDANCE, "drawing_source": "unsupported"}

        # DXF：矢量通道（精度最高，auto/text/vision 模式均优先）
        if ext == ".dxf":
            result = await _perceive_dxf(state)
            if result:
                return result
            # DXF 解析工具报错（文件损坏等）→ 同样短路，不用假数据
            return {"perception_error": "DXF 文件解析失败，请确认文件有效后重试。",
                    "drawing_source": "unsupported"}

        # 图片：视觉通道
        if ext in _VISION_EXTS:
            result = await _perceive_vision(state)
            if result:
                return result
            return {"perception_error": "图片视觉提取失败或无结果（Mock 模式/未配置多模态模型）。",
                    "drawing_source": "unsupported"}

        # PDF：视觉优先（auto/vision），文本兜底（auto/text）
        if mode in ("vision", "auto"):
            result = await _perceive_vision(state)
            if result:
                return result
            if mode == "vision":
                logger.warning("perception.vision_only_failed")
        if mode in ("text", "auto"):
            result = await _perceive_drawing(state)
            if result:
                return result
        logger.warning("perception.drawing_fallback_to_next")

    # ② IFC 通道
    if state.get("ifc_path"):
        result = await _perceive_ifc(state)
        if result:
            return result

    # ③ 注入基准 / 内置样例
    baseline = state.get("golden_baseline") or []
    if not baseline:
        baseline = _DEMO_BASELINE
        logger.info("perception.demo_baseline_used")
    return {
        "golden_baseline": baseline,
        "baseline_version": state.get("baseline_version", 0) + 1,
        "drawing_source": state.get("drawing_source") or "demo",
        "change_status": state.get("change_status") or "first",
    }
