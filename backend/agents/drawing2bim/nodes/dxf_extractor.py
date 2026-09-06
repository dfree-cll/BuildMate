"""drawing2bim DXF 结构化提取器（第五期）

矢量数据 → 黄金基准元素，两条证据链：
1. 图层映射：图层名关键词 → 构件类型（置信度 0.85）
   - 门窗类图层上的 INSERT 块引用 → 门/窗
2. 几何启发式：墙层平行双线 → 墙体（间距=墙厚，置信度 0.7）
   - 设计师不按图层规范画图时的兜底

已知短板：依赖画图规范；提取结果走置信度分级 + 双轨审查 + HITL，
低置信条目会被人工卡点拦截，不会静默进入生成环节。
"""
import copy
import math
import json
import re
import uuid

from backend.core.logger import get_logger
from backend.engines.wall_geometry import wall_geometry_group

logger = get_logger(__name__)

# ── 图层映射表（关键词 → IFC 类型；项目级覆盖见 config.dxf_layer_map）──
DEFAULT_LAYER_MAP: dict[str, str] = {
    "墙": "IFCWALL", "wall": "IFCWALL",
    "柱": "IFCCOLUMN", "column": "IFCCOLUMN", "col": "IFCCOLUMN",
    "梁": "IFCBEAM", "beam": "IFCBEAM",
    "板": "IFCSLAB", "楼板": "IFCSLAB", "slab": "IFCSLAB",
    "门": "IFCDOOR", "door": "IFCDOOR",
    "窗": "IFCWINDOW", "window": "IFCWINDOW",
    "楼梯": "IFCSTAIR", "stair": "IFCSTAIR",
    "屋顶": "IFCROOF", "屋面": "IFCROOF", "roof": "IFCROOF",
}

CONFIDENCE_LAYER = 0.85      # 图层命中：确定性高
CONFIDENCE_GEOMETRY = 0.70   # 几何推断：需人工复核概率更高

# 墙厚合理范围（mm）与平行判定容差
WALL_THICKNESS_RANGE = (40.0, 600.0)
PARALLEL_ANGLE_TOL_DEG = 1.0
MAX_LINES_PER_GROUP = 200    # 单方向组上限，防 O(n²) 配对爆炸

# 门窗块名惯例：M1/C-2/ML3（M=门 C=窗）或含中文
_DOOR_BLOCK_RE = re.compile(r"^(M|门)", re.IGNORECASE)
_WINDOW_BLOCK_RE = re.compile(r"^(C|窗)", re.IGNORECASE)


def _match_layer_type(layer_name: str, layer_map: dict[str, str] | None = None) -> str | None:
    """图层名关键词匹配 → IFC 类型（长关键词优先，避免'门窗'先命中'门'）"""
    m = layer_map or DEFAULT_LAYER_MAP
    lowered = (layer_name or "").lower()
    hits = [(k, v) for k, v in m.items() if k.lower() in lowered]
    if not hits:
        return None
    hits.sort(key=lambda kv: len(kv[0]), reverse=True)
    return hits[0][1]


def _line_angle_deg(line: dict) -> float:
    (x1, y1), (x2, y2) = line["start"], line["end"]
    angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
    return angle


def _line_length(line: dict) -> float:
    (x1, y1), (x2, y2) = line["start"], line["end"]
    return math.hypot(x2 - x1, y2 - y1)


def _point(value) -> tuple[float, float] | None:
    try:
        x, y = float(value[0]), float(value[1])
    except (IndexError, TypeError, ValueError):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return x, y


def _wall_layer_names(parsed: dict,
                      layer_map: dict[str, str] | None = None) -> set[str]:
    """从完整图层表和实际线实体共同识别墙层，语义仅看嵌套层叶子。"""
    names = {
        str(layer.get("name") or "")
        for layer in parsed.get("layers", [])
        if isinstance(layer, dict)
    }
    names.update(
        str(line.get("layer") or "")
        for key in ("placed_wall_lines", "lines")
        for line in parsed.get(key, [])
        if isinstance(line, dict)
    )
    wall_layers = set()
    for name in names:
        leaf = name.rsplit("$0$", 1)[-1]
        if (wall_geometry_group(name) is not None or
                _match_layer_type(leaf, layer_map) == "IFCWALL"):
            wall_layers.add(name)
    return wall_layers


def _iter_wall_segments(parsed: dict, wall_layers: set[str]):
    """将 LINE/POLYLINE 统一为可配对的线段，并保留 occurrence-aware handle。"""
    source_lines = parsed.get("placed_wall_lines") or parsed.get("lines", [])
    for record_index, line in enumerate(source_lines):
        if line.get("layer") not in wall_layers:
            continue
        base_handle = str(line.get("handle") or f"record-{record_index}")
        if line.get("type") == "LINE":
            start, end = _point(line.get("start")), _point(line.get("end"))
            if start and end and math.dist(start, end) > 1e-9:
                yield {**line, "handle": base_handle,
                       "start": start, "end": end}
            continue
        if line.get("type") not in ("LWPOLYLINE", "POLYLINE"):
            continue
        points = [_point(value) for value in line.get("points") or []]
        pairs = [(start, end) for start, end in zip(points, points[1:])
                 if start is not None and end is not None]
        if (line.get("closed") and len(points) > 2 and
                points[-1] is not None and points[0] is not None):
            pairs.append((points[-1], points[0]))
        for segment_index, (start, end) in enumerate(pairs):
            if math.dist(start, end) <= 1e-9:
                continue
            yield {
                "handle": f"{base_handle}:{segment_index}",
                "type": "LINE",
                "layer": line.get("layer"),
                "start": start,
                "end": end,
            }


def _aligned_pair_endpoints(line_a: dict, line_b: dict):
    """统一两条边线方向，避免反向端点平均后生成交叉/退化中心线。"""
    same = (math.dist(line_a["start"], line_b["start"]) +
            math.dist(line_a["end"], line_b["end"]))
    reversed_distance = (math.dist(line_a["start"], line_b["end"]) +
                         math.dist(line_a["end"], line_b["start"]))
    if reversed_distance < same:
        return line_b["end"], line_b["start"]
    return line_b["start"], line_b["end"]


def _perpendicular_distance(line_a: dict, line_b: dict) -> float:
    """两条平行线的垂直间距（以 line_a 方向为基准）"""
    (x1, y1), (x2, y2) = line_a["start"], line_a["end"]
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return float("inf")
    # 单位法向量
    nx, ny = -dy / length, dx / length
    bx, by = line_b["start"]
    return abs((bx - x1) * nx + (by - y1) * ny)


def _projection_overlap(line_a: dict, line_b: dict) -> bool:
    """两线沿走向的投影是否有重叠（重叠才算同一面墙的双线）"""
    (x1, y1), (x2, y2) = line_a["start"], line_a["end"]
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return False
    ux, uy = dx / length, dy / length

    def proj(p):
        return (p[0] - x1) * ux + (p[1] - y1) * uy

    a0, a1 = sorted((proj(line_a["start"]), proj(line_a["end"])))
    b0, b1 = sorted((proj(line_b["start"]), proj(line_b["end"])))
    # 重叠长度 > 较短线的 30% 视为同一墙段
    overlap = min(a1, b1) - max(a0, b0)
    return overlap > min(a1 - a0, b1 - b0) * 0.3


def extract_walls_from_double_lines(parsed: dict, wall_layers: set[str],
                                    layer_map: dict[str, str] | None = None) -> list[dict]:
    """几何启发式：墙层平行双线配对 → 墙体元素（含精确墙厚）"""
    walls: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    # 按（图层, 方向角）分组
    groups: dict[tuple[str, int], list[dict]] = {}
    for line in _iter_wall_segments(parsed, wall_layers):
        angle_bucket = round(_line_angle_deg(line) / PARALLEL_ANGLE_TOL_DEG)
        groups.setdefault((line["layer"], angle_bucket), []).append(line)

    for (layer, _bucket), group in groups.items():
        group = group[:MAX_LINES_PER_GROUP]
        for i, la in enumerate(group):
            for lb in group[i + 1:]:
                key = tuple(sorted((la["handle"], lb["handle"])))
                if key in seen_pairs:
                    continue
                dist = _perpendicular_distance(la, lb)
                if not (WALL_THICKNESS_RANGE[0] <= dist <= WALL_THICKNESS_RANGE[1]):
                    continue
                if not _projection_overlap(la, lb):
                    continue
                seen_pairs.add(key)
                lb_start, lb_end = _aligned_pair_endpoints(la, lb)
                walls.append({
                    "element_id": f"dxf-{uuid.uuid5(uuid.NAMESPACE_DNS, key[0] + key[1]).hex[:12]}",
                    "ifc_type": "IFCWALL",
                    "name": f"墙({layer})-{len(walls) + 1}",
                    "properties": {
                        "Thickness": round(dist, 1),
                        "Length": round(min(_line_length(la), _line_length(lb)), 1),
                    },
                    "geometry": {
                        # 双线中点 = 墙中心线（供建模链 DXF 直通使用）
                        "center_start": ((la["start"][0] + lb_start[0]) / 2.0,
                                         (la["start"][1] + lb_start[1]) / 2.0),
                        "center_end": ((la["end"][0] + lb_end[0]) / 2.0,
                                       (la["end"][1] + lb_end[1]) / 2.0),
                        "width": round(dist, 1),
                    },
                    "_source_handles": [la["handle"], lb["handle"]],
                    "confidence": CONFIDENCE_GEOMETRY,
                    "source": "dxf_geometry",
                    "review_status": "pending",
                })
    return walls


def extract_doors_windows_from_inserts(parsed: dict,
                                       layer_map: dict[str, str] | None = None) -> list[dict]:
    """块引用识别：块名 M*/门* → 门，C*/窗* → 窗（门窗图层上的块优先）"""
    elements: list[dict] = []
    for ins in parsed.get("inserts", []):
        block_name = (ins.get("block_name") or "").strip()
        ifc_type = None
        if _DOOR_BLOCK_RE.search(block_name):
            ifc_type = "IFCDOOR"
        elif _WINDOW_BLOCK_RE.search(block_name):
            ifc_type = "IFCWINDOW"
        if ifc_type is None:
            continue
        elements.append({
            "element_id": f"dxf-{uuid.uuid5(uuid.NAMESPACE_DNS, ins['handle']).hex[:12]}",
            "ifc_type": ifc_type,
            "name": block_name,
            "properties": {},
            "confidence": CONFIDENCE_LAYER,
            "source": "dxf_block",
            "review_status": "pending",
        })
    return elements


def extract_grid_from_dxf(parsed: dict, tol: float = 200.0, path: str | None = None) -> dict | None:
    """DXF 轴网图层（*GRID）→ 真实轴网 {x_axes, y_axes}

    图纸设计院已画好轴网（S-GRID 图层）——直接读坐标——不聚类瞎算。
    path 提供时用 ezdxf 直读（parse_dxf 可能漏掉 GRID 图层的线实体）。
    """
    try:
        xs: list[float] = []
        ys: list[float] = []
        if path:
            import ezdxf
            from backend.engines.dxf_parser import _iter_entities
            doc = ezdxf.readfile(path)
            for e in _iter_entities(doc):
                if not str(getattr(e.dxf, "layer", "")).upper().endswith("GRID"):
                    continue
                if e.dxftype() == "LINE":
                    s, t = e.dxf.start, e.dxf.end
                    x1, y1, x2, y2 = s.x, s.y, t.x, t.y
                elif e.dxftype() in ("LWPOLYLINE", "POLYLINE"):
                    pts = list(e.get_points())
                    if len(pts) < 2:
                        continue
                    x1, y1 = pts[0][0], pts[0][1]
                    x2, y2 = pts[-1][0], pts[-1][1]
                else:
                    continue
                dx, dy = abs(x2 - x1), abs(y2 - y1)
                if dx > dy:      # 水平轴网线 → y 轴位置
                    ys.append((y1 + y2) / 2.0)
                elif dy > dx:    # 垂直轴网线 → x 轴位置
                    xs.append((x1 + x2) / 2.0)
        else:
            grid_layers = {l["name"] for l in parsed.get("layers", [])
                           if str(l.get("name", "")).upper().endswith("GRID")}
            if not grid_layers:
                return None
            for ln in parsed.get("lines", []):
                if ln.get("layer") not in grid_layers:
                    continue
                (x1, y1), (x2, y2) = ln["start"], ln["end"]
                dx, dy = abs(x2 - x1), abs(y2 - y1)
                if dx > dy:      # 水平轴网线 → y 轴位置
                    ys.append((y1 + y2) / 2.0)
                elif dy > dx:    # 垂直轴网线 → x 轴位置
                    xs.append((x1 + x2) / 2.0)
        if not xs and not ys:
            return None

        def _dedup(vals: list[float], t: float) -> list[float]:
            out: list[float] = []
            for v in sorted(set(vals)):
                if not out or v - out[-1] > t:
                    out.append(v)
            return out
        return {"x_axes": _dedup(xs, tol), "y_axes": _dedup(ys, tol)}
    except Exception:
        return None


def _baseline_walls_from_precise(parsed: dict) -> list[dict] | None:
    """把标准链米制墙候选适配为旧感知契约的毫米制 IFC baseline。"""
    if "precise_wall_candidates" not in parsed:
        return None
    elements = []
    for index, candidate in enumerate(parsed.get("precise_wall_candidates") or []):
        start_m, end_m = _point(candidate.get("start")), _point(candidate.get("end"))
        if not start_m or not end_m:
            continue
        start = (start_m[0] * 1000.0, start_m[1] * 1000.0)
        end = (end_m[0] * 1000.0, end_m[1] * 1000.0)
        length = math.dist(start, end)
        if length < 1e-6:
            continue
        try:
            thickness = float(candidate.get("thickness"))
        except (TypeError, ValueError):
            thickness = 0.0
        if not math.isfinite(thickness) or thickness <= 0:
            continue
        source_ids = [str(value) for value in
                      candidate.get("source_segment_ids") or [] if value]
        source_refs = copy.deepcopy(
            candidate.get("source_segment_refs") or [])
        identity_seed = json.dumps({
            "source_segment_ids": sorted(source_ids),
            "start_mm": [round(start[0], 3), round(start[1], 3)],
            "end_mm": [round(end[0], 3), round(end[1], 3)],
            "thickness_mm": round(thickness, 3),
            "wall_group": candidate.get("wall_group"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        element_id = "dxf-precise-" + uuid.uuid5(
            uuid.NAMESPACE_DNS, identity_seed).hex[:16]
        paired = bool(candidate.get("paired"))
        properties = {
            "Length": round(length, 1),
            "WallGroup": candidate.get("wall_group"),
        }
        if paired:
            properties["Thickness"] = round(thickness, 1)
        else:
            # 单线厚度是同图配对墙的推测值，不伪装成已实测属性。
            properties["ProposedThickness"] = round(thickness, 1)
        group_name = "剪力墙" if candidate.get("wall_group") == "S" else "建筑墙"
        elements.append({
            "element_id": element_id,
            "ifc_type": "IFCWALL",
            "name": f"{group_name}-{index + 1}",
            "properties": properties,
            "geometry": {
                "center_start": start,
                "center_end": end,
                "width": round(thickness, 1),
            },
            "start": [start[0], start[1], 0.0],
            "end": [end[0], end[1], 0.0],
            "thickness": round(thickness, 1),
            "confidence": CONFIDENCE_LAYER if paired else CONFIDENCE_GEOMETRY,
            "source": "dxf_precise_geometry",
            "review_status": "pending",
            "paired": paired,
            "wall_group": candidate.get("wall_group"),
            "geometry_source": candidate.get("geometry_source"),
            "source_layers": list(candidate.get("source_layers") or []),
            "source_segment_ids": source_ids,
            "source_segment_refs": source_refs,
        })
    return elements


def extract_baseline_from_dxf(parsed: dict,
                              layer_map: dict[str, str] | None = None) -> list[dict]:
    """DXF 矢量数据 → 黄金基准元素列表（纯函数，可单测）"""
    # ① 图层分类
    wall_layers = _wall_layer_names(parsed, layer_map)

    # ② 几何启发式：墙层双线配对
    precise_walls = _baseline_walls_from_precise(parsed)
    elements = (precise_walls if precise_walls is not None else
                extract_walls_from_double_lines(parsed, wall_layers, layer_map))

    # ②b 单线兜底：墙层未配对长线（单线画法图纸——双线配对可能漏）
    if precise_walls is None:
        paired_handles = {
            handle
            for element in elements
            for handle in element.get("_source_handles") or []
        }
        seen_single_geometries = set()
        for line in _iter_wall_segments(parsed, wall_layers):
            if line["handle"] in paired_handles:
                continue
            start, end = line["start"], line["end"]
            length = math.dist(start, end)
            if length < 500.0:
                continue
            forward = tuple(round(value, 3) for point in (start, end) for value in point)
            reverse = tuple(round(value, 3) for point in (end, start) for value in point)
            geometry_key = (str(line.get("layer") or ""),) + min(forward, reverse)
            if geometry_key in seen_single_geometries:
                continue
            seen_single_geometries.add(geometry_key)
            seed = f"{line['handle']}:{geometry_key}"
            elements.append({
                "element_id": "wall-single-" + uuid.uuid5(
                    uuid.NAMESPACE_DNS, seed).hex[:16],
                "ifc_type": "IFCWALL",
                "name": "单线墙",
                "properties": {"Length": round(length, 1)},
                "geometry": {
                    "center_start": start,
                    "center_end": end,
                    "width": 200.0,
                },
                "start": [start[0], start[1], 0.0],
                "end": [end[0], end[1], 0.0],
                "thickness": 200.0,
                "confidence": CONFIDENCE_LAYER,
                "source": "dxf_single_line",
                "review_status": "pending",
            })

    for element in elements:
        element.pop("_source_handles", None)

    # ③ 块引用：门窗
    elements.extend(extract_doors_windows_from_inserts(parsed, layer_map))

    logger.info("dxf_extractor.done", walls=sum(1 for e in elements if e["ifc_type"] == "IFCWALL"),
                doors=sum(1 for e in elements if e["ifc_type"] == "IFCDOOR"),
                windows=sum(1 for e in elements if e["ifc_type"] == "IFCWINDOW"))
    return elements

# ── 语义补全：从图名/说明文字提取标高 ──
def extract_levels_from_dxf(parsed: dict, path: str | None = None,
                            default_height_m: float = 3.6) -> list[dict]:
    """DXF 图名 + 说明文字 → 标高列表 [{name, elevation_m}]

    信息源（图纸自带，非默认）：
      1. 图名："楼层平面-标高1" → 标高1(0m)；"二层..." → 二层(层高推断)
      2. 说明文字（PUB_TEXT/A-ANNO）："hh+5.7m（为零点）参照" → 参照标高 5.7m
      3. 找不到 → 单层默认 标高1(0m) + default_height_m（可配置）
    """
    import os
    import re

    texts = []
    for t in parsed.get("texts", []):
        layer = t.get("layer", "")
        if any(k in layer for k in ("PUB", "ANNO", "TEXT", "TITLE", "ELEV")):
            texts.append(t.get("text", "") or "")

    name = os.path.basename(path or "")

    # ① 图名 → 楼层线索
    floor_no = None
    name_elev = None
    m = re.search(r"标高\s*([0-9.]+)", name)
    if m:
        name_elev = float(m.group(1))
    m = re.search(r"([一二三四五六七八九十]+|[0-9]+)层|层", name)
    if m:
        cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        g = m.group(1)
        if g and g.isdigit():
            floor_no = int(g)
        elif g in cn:
            floor_no = cn[g]
    # 楼层名模式（"2F-AR" → floor 2）
    m = re.search(r"([0-9]+)F[-_]?AR", name, re.I)
    if m:
        floor_no = int(m.group(1))

    # ② 说明文字 → 参照标高（如 hh+5.7m / ±0.000 / 5.700）
    ref_elev = None
    for t in texts:
        mm = re.search(r"([+-]?\s*[0-9]+(?:\.[0-9]+)?)\s*m", t)
        if mm and ("标高" in t or "参照" in t or "hh" in t.lower() or "零点" in t):
            ref_elev = float(mm.group(1).replace(" ", ""))
            break

    # ③ 组合输出
    if ref_elev is not None:
        # 有参照标高：主标高 = ref_elev；若图名有楼层号，补一层(0m)
        levels = [{"name": "标高1", "elevation_m": 0.0}]
        if ref_elev != 0.0:
            levels.append({"name": "标高%d" % (floor_no or 2), "elevation_m": ref_elev})
        return levels
    if name_elev is not None:
        # 图名"标高N"：N 是编号不是高程——主标高 0m，编号进名称
        return [{"name": "标高%d" % int(name_elev), "elevation_m": 0.0}]
    if floor_no is not None:
        # 图名楼层号（"2F-AR"/"二层"）：返回 {n}F-AR，高程默认 0（多图纸时汇总）
        return [{"name": "%dF-AR" % floor_no, "elevation_m": 0.0}]
    return [{"name": "标高1", "elevation_m": 0.0}]
