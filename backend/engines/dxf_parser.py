"""DXF 矢量解析器（第五期：DWG/DXF 原生矢量感知通道）

用 ezdxf 读取 CAD 文件的底层图形数据：图层表、线实体、块引用、文本标注。
输出结构化中间数据，供 dxf_extractor 做图层映射 + 几何启发式提取。

精度优势：矢量坐标精确，可直接计算长度/间距，不受出图清晰度影响。
短板（已知）：依赖画图规范；图层乱画时靠几何启发式兜底 + 置信度分级。
"""
import asyncio
import re

from backend.core.logger import get_logger

logger = get_logger(__name__)

# MTEXT 格式码清理：\f字体; \c颜色; \h高度; \b粗体; \i斜体; \p段落; {组码}
_MTEXT_CODE_RE = re.compile(r"\\[A-Za-z][^;]*;|\\[{}]|\\[pP]")
# 段落码替换为换行（先替换再清其余）
_MTEXT_PARAGRAPH_RE = re.compile(r"\\[pP]")


def _clean_mtext(raw) -> str:
    """清理 MTEXT/TEXT 的格式控制码，返回纯文本（轴线编号等短标注保留原样）"""
    if raw is None:
        return ""
    s = str(raw)
    s = _MTEXT_PARAGRAPH_RE.sub("\n", s)
    s = _MTEXT_CODE_RE.sub("", s)
    return s.strip()

# 单文件展开实体数上限（防恶意/异常块递归拖垮线程与内存）
MAX_PLACED_ENTITIES = 150000
# 分块提取：墙图层独立高上限（大图纸不截断墙线），0 图层杂线限制
WALL_LAYER_KEYS = ('WALL', '内板墙', '外墙板', 'I-WALL', 'A-WALL', '墙')
MAX_PER_LAYER_LINES = 100000   # 墙图层
MAX_ZERO_LAYER_LINES = 5000    # 0 图层（杂线）


def _iter_entities(doc):
    """遍历模型空间和块定义；门窗、文字等旧 DTO 保持兼容。"""
    msp = doc.modelspace()
    for e in msp:
        yield e
    for blk in doc.blocks:
        if blk.name.startswith("*"):  # 跳过匿名/图纸空间块
            continue
        try:
            for e in blk:
                yield e
        except Exception:
            continue  # 单块异常（嵌套过深/循环引用）跳过——不拖垮整体


def _placed_wall_lines(doc):
    """只为墙分支展开真实放置；不改变门窗、文本和轴网的既有 DTO。"""
    from backend.engines.geometry_first_clean import drawing_identity, walk_wall_evidence

    lines = []
    for entity, layer, provenance in walk_wall_evidence(
            doc.modelspace(), drawing_id=drawing_identity(doc)):
        if len(lines) >= MAX_PLACED_ENTITIES:
            break
        handle = str(provenance.get("placed_entity_id") or "")
        if entity.dxftype() == "LINE":
            lines.append({
                "handle": handle,
                "type": "LINE",
                "layer": layer,
                "start": (entity.dxf.start.x, entity.dxf.start.y),
                "end": (entity.dxf.end.x, entity.dxf.end.y),
            })
        elif entity.dxftype() == "LWPOLYLINE":
            points = [(point[0], point[1])
                      for point in entity.get_points(format="xy")]
            if len(points) >= 2:
                lines.append({
                    "handle": handle,
                    "type": "LWPOLYLINE",
                    "layer": layer,
                    "closed": entity.closed,
                    "points": points,
                })
    return lines


def _precise_wall_candidates(doc):
    """复用标准链的主平面选择、块放置和墙面配对；无主平面时返回兼容状态。"""
    from backend.engines.wall_geometry import (
        extract_precise_walls, wall_geometry_group)
    from backend.engines.geometry_first_clean import (
        drawing_identity, select_main_plan, walk_wall_evidence)

    try:
        selected_plan, selection = select_main_plan(doc)
    except ValueError as exc:
        reason = str(exc)
        blocked = "ambiguous" in reason.lower()
        return ([] if blocked else None), {
            "status": "BLOCKED" if blocked else "UNAVAILABLE",
            "reason": reason,
            "mode": ("blocked_ambiguous_plan" if blocked
                     else "legacy_placed_geometry"),
        }

    drawing_id = drawing_identity(doc)
    records = list(walk_wall_evidence([selected_plan], drawing_id=drawing_id))
    available_groups = {
        wall_geometry_group(layer) for _, layer, _ in records}
    if selection.get("selection_mode") == "structural_wall_column_block":
        wall_groups = (("S",) if "S" in available_groups else ("A",))
    else:
        # 建筑平面里的重复结构参照由独立结构图负责；审图墙数不混入其多次放置。
        wall_groups = (("A",) if "A" in available_groups else ("S",))
    candidates = extract_precise_walls(
        records, ox=0.0, oy=0.0, rot_deg=0.0, gcx=0.0, gcy=0.0,
        wall_groups=wall_groups)
    return candidates, {
        "status": "OK",
        "mode": "precise_placed_plan",
        "coordinate_unit": "m",
        "source_entity_count": len(records),
        "selected_group_source_count": sum(
            wall_geometry_group(layer) in wall_groups
            for _, layer, _ in records),
        "candidate_count": len(candidates),
        "wall_groups": list(wall_groups),
        "plan_selection": selection,
    }


def _sync_parse_dxf(dxf_path: str) -> dict:
    """同步解析 DXF：提取图层/线实体/块引用/文本（线程池运行）"""
    import ezdxf

    doc = ezdxf.readfile(dxf_path)
    # ── 图层表 ──
    layers = [{
            "name": layer.dxf.name,
            "color": layer.dxf.color,
        } for layer in doc.layers]

    # ── 旧 DTO 保持不变；墙分支另行携带放置后的精确证据 ──
    lines = []
    layer_line_count = {}
    for e in _iter_entities(doc):
        if len(lines) >= MAX_PLACED_ENTITIES:
            break
        try:
            kind = e.dxftype()
            layer = str(e.dxf.layer or "")
            handle = str(getattr(e.dxf, "handle", "") or "")
            if kind in ("LINE", "LWPOLYLINE", "POLYLINE"):
                if "hatch" in layer.lower():
                    continue  # 填充轮廓不是结构实体——过滤避免占满上限
                cap = (MAX_PER_LAYER_LINES if any(k in layer for k in WALL_LAYER_KEYS)
                       else (MAX_ZERO_LAYER_LINES if layer == "0"
                             else MAX_PLACED_ENTITIES))
                if layer_line_count.get(layer, 0) >= cap:
                    continue
                layer_line_count[layer] = layer_line_count.get(layer, 0) + 1

            if kind == "LINE":
                lines.append({
                    "handle": handle,
                    "type": "LINE",
                    "layer": layer,
                    "start": (e.dxf.start.x, e.dxf.start.y),
                    "end": (e.dxf.end.x, e.dxf.end.y),
                })
            elif kind == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points(format="xy")]
                if len(pts) >= 2:
                    lines.append({
                        "handle": handle,
                        "type": "LWPOLYLINE",
                        "layer": layer,
                        "closed": e.closed,
                        "points": pts,
                    })
            elif kind == "POLYLINE":
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
                if len(pts) >= 2:
                    lines.append({
                        "handle": handle,
                        "type": "POLYLINE",
                        "layer": layer,
                        "closed": e.is_closed,
                        "points": pts,
                    })
        except (AttributeError, TypeError, ValueError):
            continue

    inserts = []
    for e in _iter_entities(doc):
        if len(inserts) >= MAX_PLACED_ENTITIES:
            break
        if e.dxftype() == "INSERT":
            try:
                inserts.append({
                    "handle": e.dxf.handle,
                    "layer": e.dxf.layer,
                    "block_name": e.dxf.name,
                    "insert_point": (e.dxf.insert.x, e.dxf.insert.y),
                })
            except (AttributeError, TypeError, ValueError):
                continue

    texts = []
    for e in _iter_entities(doc):
        if len(texts) >= MAX_PLACED_ENTITIES:
            break
        if e.dxftype() in ("TEXT", "MTEXT"):
            try:
                raw = e.dxf.text if e.dxftype() == "TEXT" else e.text
                txt = _clean_mtext(raw)
                if txt:
                    texts.append({
                        "handle": e.dxf.handle,
                        "layer": e.dxf.layer,
                        "text": txt[:200],
                        "insert_point": (e.dxf.insert.x, e.dxf.insert.y),
                    })
            except (AttributeError, TypeError, ValueError):
                continue

    try:
        placed_wall_lines = _placed_wall_lines(doc)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        placed_wall_lines = []
        logger.warning("dxf_parser.placed_wall_failed", error=str(exc))

    try:
        precise_walls, wall_extraction = _precise_wall_candidates(doc)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        precise_walls = []
        wall_extraction = {
            "status": "BLOCKED",
            "reason": str(exc),
            "mode": "blocked_precise_error",
        }
        logger.warning("dxf_parser.precise_wall_failed", error=str(exc))

    logger.info("dxf_parser.parsed", layers=len(layers), lines=len(lines),
                inserts=len(inserts), texts=len(texts),
                wall_mode=wall_extraction["mode"],
                precise_walls=len(precise_walls or []),
                placed_wall_lines=len(placed_wall_lines))
    result = {
        "layers": layers,
        "lines": lines,
        "inserts": inserts,
        "texts": texts,
        "wall_extraction": wall_extraction,
    }
    if placed_wall_lines:
        result["placed_wall_lines"] = placed_wall_lines
    if precise_walls is not None:
        result["precise_wall_candidates"] = precise_walls
    return result


async def parse_dxf(dxf_path: str) -> dict:
    """异步解析入口（线程池，不阻塞事件循环）"""
    return await asyncio.to_thread(_sync_parse_dxf, dxf_path)
