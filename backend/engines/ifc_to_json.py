"""IFC -> Revit 建模 JSON（人工建模流程 v9 标准版）

流程: 信息提取(构件) -> 轴网(长墙聚类) -> 柱(轴网交点推断) -> 墙(吸附主轴网)
LLM 决策层: 装饰阈值/轴网策略/柱策略（LLM 只出参数——坐标计算归规则）

输出 model.json 数据:
  project(Project Context) / workflow_steps / grid / model_elements / views
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

MM = 1000.0            # 米 -> 毫米（默认；实际单位从 IFC 读取）
MIN_THICK = 80         # 跳过装饰面板 (<80mm)
MIN_LEN = 8000         # 长墙阈值 (>8m 才贡献主轴)
CLUSTER_TOL = 1500     # 轴线聚类容差 (mm)


def baseline_to_kept(baseline: list[dict], height_mm: float = 3000.0) -> list[tuple]:
    """DXF 基线（extract_baseline_from_dxf）→ kept 元组——DXF 直通建模（复用 _generate）

    架构：DXF → 解析 → 基线 → model.json（不依赖 IFC 中转）
    """
    kept: list[tuple] = []
    for e in baseline:
        if e.get("ifc_type") != "IFCWALL":
            continue
        g = e.get("geometry") or {}
        cs, ce = g.get("center_start"), g.get("center_end")
        if not cs or not ce:
            continue
        x1, y1 = float(cs[0]), float(cs[1])
        x2, y2 = float(ce[0]), float(ce[1])
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        if dx < 1 and dy < 1:  # 退化墙（零长度）
            continue
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        thick = float(g.get("width") or (e.get("properties") or {}).get("Thickness") or 200.0)
        name = e.get("name") or "墙"
        guid = e.get("element_id") or ""
        kept.append((cx, cy, dx, dy, thick, name, 0.0, height_mm, guid))
    return kept


def _extract_project(f: Any) -> dict:
    """从 IFC 提取项目上下文（Project Context）"""
    mm_per_unit = 1000.0
    units_label = "mm"
    proj = None
    try:
        plist = f.by_type("IfcProject")
        if plist:
            proj = plist[0]
    except Exception:
        pass
    # 单位（IfcSIUnit LENGTHUNIT）
    try:
        for u in (proj.UnitsInContext.Units if proj and proj.UnitsInContext else []):
            if u.is_a("IfcSIUnit") and u.UnitType == "LENGTHUNIT":
                if u.Name == "METRE":
                    mm_per_unit = 1000.0
                    units_label = "m"
                elif u.Name == "MILLIMETRE":
                    mm_per_unit = 1.0
                    units_label = "mm"
                elif u.Name == "CENTIMETRE":
                    mm_per_unit = 10.0
                    units_label = "cm"
    except Exception:
        pass
    # 标高（IfcBuildingStorey——Elevation 值可能是毫米（Revit 导出）或米）
    levels = []
    try:
        for s in f.by_type("IfcBuildingStorey"):
            elev = s.Elevation if s.Elevation is not None else 0.0
            # 启发式：|值|>100 视为毫米直接保留；否则按米换算
            if abs(elev) > 100:
                elev_mm = float(elev)
            else:
                elev_mm = float(elev) * mm_per_unit
            levels.append({"name": s.Name or "Level", "elevation": round(elev_mm, 1)})
    except Exception:
        pass
    levels.sort(key=lambda lv: lv["elevation"])
    # 专业推断（构件构成：墙多=建筑，管线多=机电）
    try:
        wall_n = len(f.by_type("IfcWall")) + len(f.by_type("IfcSlab"))
        flow_n = len(f.by_type("IfcFlowSegment")) + len(f.by_type("IfcFlowFitting")) \
            + len(f.by_type("IfcFlowTerminal"))
        discipline = "architectural" if wall_n >= flow_n else "mep"
    except Exception:
        discipline = "architectural"
    return {
        "project_id": proj.GlobalId if proj else "",
        "name": proj.Name if proj else "",
        "discipline": discipline,
        "building_type": "",
        "units": units_label,
        "mm_per_unit": mm_per_unit,
        "levels": levels,
        "design_rules": ["HR-001", "HR-002", "HR-003", "HR-004",
                         "HR-005", "HR-006", "HR-007"],
    }


_DECIDE_PROMPT = """你是 BIM 建模规划器。根据项目统计摘要，输出建模策略（只输出 JSON，不要其他文字）。

规则：
- min_thick_mm：装饰面板厚度阈值（小于它的墙视为装饰层，跳过建模）。看厚度分布和名称判断——例如 10-20mm 的铝板/饰面板通常是装饰层，100mm+ 是主体墙。
- min_len_mm：主轴网候选墙的最小长度（长墙才贡献主轴）。看项目尺寸判断——大项目用大值，小项目用小值。
- axis_tol_mm：轴线聚类容差（相近轴线合并的距离）。
- column_size_mm：柱截面尺寸（宽×深，正方形给一个数）。
- column_strategy："grid-intersection"（轴网交点立柱，有墙经过的）或 "none"（无柱）。
- 如果项目无足够信息，用合理默认值。

统计摘要：
{summary}

输出 JSON 格式：
{{"min_thick_mm": 80, "min_len_mm": 8000, "axis_tol_mm": 1500, "column_size_mm": 400, "column_strategy": "grid-intersection"}}"""

_STRATEGY_KEYS = ("min_thick_mm", "min_len_mm", "axis_tol_mm",
                  "column_size_mm", "column_strategy")


async def _llm_decide(summary: dict) -> dict:
    """LLM 决策：装饰阈值/轴网策略/柱策略——输出策略参数（LLM 不给坐标）"""
    try:
        from backend.core.llm_factory import get_llm
        from backend.core.llm_text import parse_json_loose
        from langchain_core.messages import HumanMessage
        llm = get_llm("qa", temperature=0)
        resp = await llm.ainvoke([HumanMessage(
            content=_DECIDE_PROMPT.format(summary=json.dumps(summary, ensure_ascii=False)))])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        strategy = parse_json_loose(raw) or {}
        return {k: v for k, v in strategy.items() if k in _STRATEGY_KEYS}
    except Exception as ex:
        logger.warning("llm_decide fallback: %s", str(ex)[:100])
        return {}


def _build_summary(kept: list[tuple], proj: dict) -> dict:
    """统计摘要（喂给 LLM 决策——不是全量数据）"""
    thick_hist: dict[int, int] = {}
    for b in kept:
        thick_hist[int(b[4])] = thick_hist.get(int(b[4]), 0) + 1
    lens = sorted([max(b[2], b[3]) for b in kept], reverse=True)[:20]
    xs = [b[0] for b in kept]
    ys = [b[1] for b in kept]
    return {
        "project": proj.get("name", ""),
        "discipline": proj.get("discipline", ""),
        "wall_count": len(kept),
        "thickness_distribution": {str(k): v for k, v in sorted(thick_hist.items())},
        "top20_wall_lengths_mm": [round(l, 0) for l in lens],
        "x_range_mm": [round(min(xs), 0), round(max(xs), 0)] if xs else [],
        "y_range_mm": [round(min(ys), 0), round(max(ys), 0)] if ys else [],
    }


def _box_to_wall(w: Any, verts: list[float], mm_per_unit: float = 1000.0) -> tuple:
    """构件三角网格包围盒 -> (cx, cy, dx, dy, thick, name, minz, h, guid) 单位 mm"""
    xs = verts[0::3]
    ys = verts[1::3]
    zs = verts[2::3]
    minx, maxx = min(xs) * mm_per_unit, max(xs) * mm_per_unit
    miny, maxy = min(ys) * mm_per_unit, max(ys) * mm_per_unit
    minz, maxz = min(zs) * mm_per_unit, max(zs) * mm_per_unit
    h = maxz - minz
    dx, dy = maxx - minx, maxy - miny
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    thick = dy if dx >= dy else dx
    guid = ""
    try:
        guid = w.GlobalId or ""
    except Exception:
        pass
    return (cx, cy, dx, dy, thick, w.Name or "", minz, h, guid)


def _extract_ifc_grid(f: Any, mm: float) -> dict | None:
    """读取 IFC 真实轴网（IfcGrid——设计院定义）——返回 {x_axes, y_axes} 或 None（无轴网）"""
    try:
        grids = f.by_type("IfcGrid")
        if not grids:
            return None
        x_axes: list[float] = []
        y_axes: list[float] = []
        for g in grids:
            for axis in (getattr(g, "UAxes", None) or []):
                pnt = getattr(getattr(axis, "AxisCurve", None), "Pnt", None)
                if pnt is not None:
                    try:
                        x_axes.append(round(float(pnt[0]) * mm, 1))
                    except Exception:
                        pass
            for axis in (getattr(g, "VAxes", None) or []):
                pnt = getattr(getattr(axis, "AxisCurve", None), "Pnt", None)
                if pnt is not None:
                    try:
                        y_axes.append(round(float(pnt[1]) * mm, 1))
                    except Exception:
                        pass
        if not x_axes and not y_axes:
            return None
        # 去重排序——并过滤退化轴（间距过近）
        def _dedup(vals: list[float], tol: float = 300.0) -> list[float]:
            out: list[float] = []
            for v in sorted(set(vals)):
                if not out or v - out[-1] > tol:
                    out.append(v)
            return out
        return {"x_axes": _dedup(x_axes), "y_axes": _dedup(y_axes)}
    except Exception:
        return None


def _cluster(values: list[float], tol: float = CLUSTER_TOL) -> list[float]:
    """一维聚类（相近值归组取均值）"""
    groups: list[list[float]] = []
    for v in sorted(values):
        placed = False
        for g in groups:
            if abs(v - g[0]) <= tol:
                g[0] = (g[0] * g[1] + v) / (g[1] + 1)
                g[1] += 1
                placed = True
                break
        if not placed:
            groups.append([v, 1.0])
    return [round(g[0], 1) for g in groups]


def _nearest(v: float, axes: list[float]) -> float:
    if not axes:
        return v
    return min(axes, key=lambda a: abs(a - v))


def _extract_all_walls(f: Any, settings: Any, mm_per_unit: float) -> list[tuple]:
    """全量提取墙构件包围盒（去重——不过滤装饰厚度——供 LLM 决策看分布）"""
    import ifcopenshell.geom
    kept: list[tuple] = []
    for w in f.by_type("IfcWall"):
        try:
            shape = ifcopenshell.geom.create_shape(settings, w)
            verts = shape.geometry.verts
            if not verts:
                continue
            b = _box_to_wall(w, verts, mm_per_unit)
            dup = False
            for k in kept:
                dist = ((b[0] - k[0]) ** 2 + (b[1] - k[1]) ** 2) ** 0.5
                if dist < 300 and abs(b[2] - k[2]) < 300 and abs(b[3] - k[3]) < 300:
                    dup = True
                    break
            if dup:
                continue
            kept.append(b)
        except Exception:
            logger.debug("wall geom fail", exc_info=True)
    return kept


def _generate(kept: list[tuple], proj: dict, strategy: dict | None = None,
              max_walls: int | None = None, max_cols: int | None = None,
              grid_override: dict | None = None, snap: bool = True) -> dict:
    """按策略生成轴网/墙/柱（规则计算——LLM 只提供参数）

    max_walls/max_cols: None = 全量（大模型不丢构件）；给数 = 截断（演示降量）
    grid_override: IFC 真实轴网（IfcGrid——有则优先——source=ifc_grid；无则墙线推断——source=inferred）
    """
    st = strategy or {}
    min_thick = float(st.get("min_thick_mm", MIN_THICK))
    min_len = float(st.get("min_len_mm", MIN_LEN))
    axis_tol = float(st.get("axis_tol_mm", CLUSTER_TOL))
    col_size = float(st.get("column_size_mm", 400))
    col_strategy = st.get("column_strategy", "grid-intersection")

    # 按 LLM 阈值过滤装饰面板
    kept_main = [b for b in kept if b[4] >= min_thick]

    # 轴网（>min_len 长墙中心线聚类）
    long_x = [b[0] for b in kept_main if b[2] < b[3] and b[3] >= min_len]
    long_y = [b[1] for b in kept_main if b[2] >= b[3] and b[2] >= min_len]
    if grid_override and grid_override.get("x_axes") and grid_override.get("y_axes"):
        # IFC 真实轴网（设计院定义——优先）
        x_axes, y_axes = grid_override["x_axes"], grid_override["y_axes"]
        grid_source = "ifc_grid"
        long_x, long_y = [], []
    else:
        # 无真实轴网——从长墙中心线聚类推断（明确标注 inferred——前端提示用户）
        x_axes = _cluster(long_x, axis_tol)
        y_axes = _cluster(long_y, axis_tol)
        grid_source = "inferred"

    # 墙（吸附主轴网——snap=False 时保持图纸原始坐标，如 DXF 直通场景）
    elems: list[dict] = []
    for i, b in enumerate(kept_main[:max_walls]):
        cx, cy, dx, dy, thick, name, minz, h, guid = b
        if snap:
            if dx >= dy:  # 沿 x
                cy2 = _nearest(cy, y_axes)
                s = _nearest(cx - dx / 2 + thick / 2, x_axes)
                e = _nearest(cx + dx / 2 - thick / 2, x_axes)
                start = [s, cy2, minz]
                end = [e, cy2, minz]
            else:  # 沿 y
                cx2 = _nearest(cx, x_axes)
                s = _nearest(cy - dy / 2 + thick / 2, y_axes)
                e = _nearest(cy + dy / 2 - thick / 2, y_axes)
                start = [cx2, s, minz]
                end = [cx2, e, minz]
        else:
            # 原始图纸坐标（DXF 直通——不吸附，保持真实位置）
            if dx >= dy:
                start = [cx - dx / 2, cy, minz]
                end = [cx + dx / 2, cy, minz]
            else:
                start = [cx, cy - dy / 2, minz]
                end = [cx, cy + dy / 2, minz]
        elem = {
            "id": "wall_%d" % i, "type": "Wall", "name": name,
            "start": [round(v, 1) for v in start],
            "end": [round(v, 1) for v in end],
            "height": round(h, 1), "thickness": round(thick, 1),
            "level": "标高1",
        }
        if guid:
            elem["ifc_guid"] = guid
        elems.append(elem)

    # 墙端点精确相接（消除微缝——Revit 才能成功连接墙）
    # 相邻墙端点距离 < 60mm → 合并为同一坐标（真实建筑墙本就应相接）
    _EP_TOL = 60.0
    _eps: list[tuple] = []  # (x, y) 端点簇中心
    for e in elems:
        if e["type"] != "Wall":
            continue
        for pt in (e["start"], e["end"]):
            x, y = pt[0], pt[1]
            for cx, cy in _eps:
                if abs(x - cx) <= _EP_TOL and abs(y - cy) <= _EP_TOL:
                    break
            else:
                _eps.append((x, y))
    if _eps:
        for e in elems:
            if e["type"] != "Wall":
                continue
            for key in ("start", "end"):
                x, y = e[key][0], e[key][1]
                best, bd = None, _EP_TOL
                for cx, cy in _eps:
                    d = abs(x - cx) + abs(y - cy)
                    if d <= bd:
                        best, bd = (cx, cy), d
                if best is not None:
                    e[key][0], e[key][1] = best[0], best[1]

    # 柱（轴网交点 + 有墙经过 -> 推断柱）
    cols: list[dict] = []
    ci = 0
    if col_strategy != "none":
        def wall_passes(x: float, y: float, tol: float = 400) -> bool:
            for b in kept_main:
                cx, cy, dx, dy = b[0], b[1], b[2], b[3]
                if dx >= dy:
                    if abs(cy - y) <= tol and cx - dx / 2 - tol <= x <= cx + dx / 2 + tol:
                        return True
                else:
                    if abs(cx - x) <= tol and cy - dy / 2 - tol <= y <= cy + dy / 2 + tol:
                        return True
            return False

        for x in x_axes:
            for y in y_axes:
                if wall_passes(x, y):
                    cols.append({
                        "id": "col_%d" % ci, "type": "Column", "name": "C%d" % (ci + 1),
                        "x": x, "y": y, "base": 0, "top": 8000,
                        "width": col_size, "depth": col_size, "level": "标高1",
                    })
                    ci += 1
        # 均匀采样：交点柱全算——超上限则按网格均匀取（避免集中在前半；None=全量）
        if max_cols and len(cols) > max_cols:
            stride = len(cols) / float(max_cols)
            cols = [cols[int(i * stride)] for i in range(max_cols)]

    return {
        "project": proj,
        "workflow_steps": ["project-setup", "levels", "grid", "column",
                           "beam", "wall", "slab", "openings",
                           "views", "validate", "save"],
        "level": "标高1",
        "grid": {"x_axes": x_axes, "y_axes": y_axes, "source": grid_source},
        "model_elements": cols + elems,
        "views": [{"view_id": "v3d", "view_name": "三维预览", "view_type": "ThreeD"}],
    }


def build_model_json(ifc_path: str, max_walls: int | None = None,
                     max_cols: int | None = None,
                     strategy: dict | None = None) -> dict:
    """IFC -> 建模 JSON（同步版——strategy 可空用默认；max_* None=全量）"""
    import ifcopenshell
    import ifcopenshell.geom

    t0 = time.time()
    f = ifcopenshell.open(ifc_path)
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    proj = _extract_project(f)
    kept = _extract_all_walls(f, settings, proj["mm_per_unit"])
    grid_override = _extract_ifc_grid(f, proj["mm_per_unit"])
    data = _generate(kept, proj, strategy, max_walls, max_cols, grid_override)
    logger.info("ifc_to_json: walls=%d grid=%dx%d %.1fs",
                len([e for e in data["model_elements"] if e["type"] == "Wall"]),
                len(data["grid"]["x_axes"]), len(data["grid"]["y_axes"]),
                time.time() - t0)
    return data


async def build_model_json_async(ifc_path: str, max_walls: int | None = None,
                                 max_cols: int | None = None) -> dict:
    """IFC -> 建模 JSON（async 版：LLM 决策 + 规则执行——自适应任意项目；max_* None=全量）"""
    import ifcopenshell
    import ifcopenshell.geom

    t0 = time.time()
    f = ifcopenshell.open(ifc_path)
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    proj = _extract_project(f)
    kept = _extract_all_walls(f, settings, proj["mm_per_unit"])

    # LLM 决策（失败 fallback 空策略——用默认参数）
    strategy = await _llm_decide(_build_summary(kept, proj))
    data = _generate(kept, proj, strategy, max_walls, max_cols)
    logger.info("ifc_to_json.llm: strategy=%s walls=%d grid=%dx%d %.1fs",
                {k: strategy.get(k) for k in _STRATEGY_KEYS if k in strategy},
                len([e for e in data["model_elements"] if e["type"] == "Wall"]),
                len(data["grid"]["x_axes"]), len(data["grid"]["y_axes"]),
                time.time() - t0)
    return data
