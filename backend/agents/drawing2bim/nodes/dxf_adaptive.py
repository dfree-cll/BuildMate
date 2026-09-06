"""drawing2bim 柱/轴网自适应提取器（第六期）——替换纯图层映射，实体特征驱动

来源: 项目内已验证的自适应柱、轴网与标高提取算法。
设计:
1. 柱块自动找(实体特征: 4顶点闭合矩形 INSERT 最多, 不依赖图层名)
2. 块内中心自动算(LWPOLYLINE 4点平均)
3. INSERT 旋转/缩放/镜像(sx<0)自动处理
4. 轴距自动探测(柱间距众数, 不写死 9000)
5. 轴网从柱反推(柱是最终基准) + 轴网线角度扶正
6. 标高从块名括号解析(-6.5~-0.1m)
输出: 黄金基准元素列表(IFCCOLUMN) + 轴网 + 标高, 全部带置信度
"""
import math
import re
from collections import Counter

try:
    import ezdxf
except ImportError:
    ezdxf = None

from backend.core.logger import get_logger

logger = get_logger(__name__)

CONFIDENCE_COLUMN = 0.92      # 柱: 矩形INSERT + 块内中心 → 高置信
CONFIDENCE_GRID = 0.85
CONFIDENCE_LEVEL = 0.80


def _xform(pt, ins, rot_deg, sx, sy):
    r = math.radians(rot_deg)
    c, s = math.cos(r), math.sin(r)
    return (pt[0] * sx * c - pt[1] * sy * s + ins[0],
            pt[0] * sx * s + pt[1] * sy * c + ins[1])


def _rect_center_edges(pts):
    """4顶点矩形: 返回 (中心, 边长列表, 长边方向) 或 None"""
    if len(pts) != 4:
        return None
    edges = [math.hypot(pts[(i + 1) % 4][0] - pts[i][0], pts[(i + 1) % 4][1] - pts[i][1])
             for i in range(4)]
    if not all(100 <= ed <= 3000 for ed in edges):
        return None
    cx = sum(p[0] for p in pts) / 4
    cy = sum(p[1] for p in pts) / 4
    # 长边方向(用于 width/depth 语义)
    long_dx = long_dy = 0
    for i in range(4):
        p1, p2 = pts[i], pts[(i + 1) % 4]
        if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) == edges[2]:
            long_dx, long_dy = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
    return (cx, cy, edges, (long_dx, long_dy))


def find_col_block(doc):
    """全图找柱块: ①含'墙柱'的块 ②4顶点矩形INSERT最多"""
    COL_LAYER_KW = ('COLU', 'COL', '柱', 'COLUMN', 'COLS')
    wall_cols = []
    rect_blocks = {}
    for br in doc.blocks.block_records:
        name = br.dxf.name
        if name.startswith('*') or 'AVE_' in name:
            continue
        blk = doc.blocks[name]
        n_rect_ins = n_rect_lwp = n_layer_col = 0
        for e in blk:
            lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
            lyr_short = lyr.split('$')[-1]
            if any(k in lyr_short.upper() for k in COL_LAYER_KW):
                n_layer_col += 1
            if e.dxftype() == 'INSERT':
                sub = doc.blocks.get(e.dxf.name)
                if sub is not None:
                    for el in sub:
                        if el.dxftype() == 'LWPOLYLINE':
                            pts = list(el.get_points('xy'))
                            if _rect_center_edges(pts):
                                n_rect_ins += 1
                                break
            elif e.dxftype() == 'LWPOLYLINE':
                pts = list(e.get_points('xy'))
                if _rect_center_edges(pts):
                    n_rect_lwp += 1
        score = (n_rect_ins * 2 + n_rect_lwp, n_layer_col, n_rect_ins)
        if n_rect_ins > 0 or n_layer_col > 0:
            rect_blocks[name] = score
        if '墙柱' in name:
            wall_cols.append(name)
    if not rect_blocks:
        return None, None
    if wall_cols:
        best = max(wall_cols, key=lambda n: rect_blocks.get(n, (0, 0, 0)))
    else:
        best = max(rect_blocks, key=lambda n: rect_blocks[n])
    return best, rect_blocks[best]


def _auto_pitch(vals):
    """轴距自动探测: 柱间距众数(3~15m 范围)"""
    ds = []
    sv = sorted(set(round(v, 1) for v in vals))
    for i in range(1, len(sv)):
        d = sv[i] - sv[i - 1]
        if 3000 < d < 15000:
            ds.append(round(d, 0))
    if not ds:
        return 9000.0
    return Counter(ds).most_common(1)[0][0]


def _mode_rem(vals, pitch):
    cnt = Counter(round(v % pitch, 0) for v in vals)
    return cnt.most_common(1)[0][0]


def extract_columns_from_dxf(dxf_path: str) -> dict:
    """DXF → {columns: [黄金基准元素], grid: {x_axes, y_axes}, levels: [...], meta: {...}}

    纯函数(输入路径, 输出结构化)。无柱块/非混凝土图 → 返回 columns=[] + meta.reason
    """
    if ezdxf is None:
        return {"columns": [], "grid": None, "levels": [], "meta": {"error": "ezdxf missing"}}
    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as ex:
        return {"columns": [], "grid": None, "levels": [],
                "meta": {"error": "dxf read failed: %s" % ex}}

    col_block, score = find_col_block(doc)
    if not col_block:
        return {"columns": [], "grid": None, "levels": [],
                "meta": {"reason": "no_col_block", "detail": "未找到柱块(非混凝土平面图?)"}}

    b = doc.blocks[col_block]

    # 标高: 块名括号解析
    m_elev = re.search(r'\((-?\d+\.?\d*)\s*m?\s*~\s*(-?\d+\.?\d*)\s*m\)', col_block)
    base_m = float(m_elev.group(1)) if m_elev else 0.0
    top_m = float(m_elev.group(2)) if m_elev else 3.0

    # 柱块内实体
    ins_list = []
    direct_lwps = []
    inner_c = None
    for e in b:
        lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
        if 'S-COLU' not in lyr and not any(k in lyr.split('$')[-1].upper() for k in ('COLU', 'COL', '柱')):
            continue
        if e.dxftype() == 'INSERT':
            ins_list.append(e)
        elif e.dxftype() == 'LWPOLYLINE':
            pts = list(e.get_points('xy'))
            if _rect_center_edges(pts):
                direct_lwps.append(pts)

    if not ins_list and not direct_lwps:
        return {"columns": [], "grid": None, "levels": [],
                "meta": {"reason": "no_col_entities", "col_block": col_block}}

    # 块内中心(从子块 LWPOLYLINE)
    sub_counts = Counter(e.dxf.name for e in ins_list)
    if sub_counts:
        main_sub = sub_counts.most_common(1)[0][0]
        if main_sub in doc.blocks:
            for el in doc.blocks[main_sub]:
                if el.dxftype() == 'LWPOLYLINE':
                    pts = list(el.get_points('xy'))
                    rc = _rect_center_edges(pts)
                    if rc:
                        inner_c = (rc[0], rc[1])
                        break
    if inner_c is None and direct_lwps:
        rc = _rect_center_edges(direct_lwps[0])
        if rc:
            inner_c = (rc[0], rc[1])

    # 柱心 = ins + 旋转(块内中心), 含镜像
    cols_world = []
    direct_sizes = []
    for e in ins_list:
        ix, iy = e.dxf.insert.x, e.dxf.insert.y
        sx = e.dxf.xscale if e.dxf.hasattr('xscale') else 1
        sy = e.dxf.yscale if e.dxf.hasattr('yscale') else 1
        rot = math.radians(e.dxf.rotation if e.dxf.hasattr('rotation') else 0)
        c, s = math.cos(rot), math.sin(rot)
        icx, icy = inner_c[0] * sx, inner_c[1] * sy
        cols_world.append((ix + icx * c - icy * s, iy + icx * s + icy * c))

    n_direct = len(direct_lwps)
    for pts in direct_lwps:
        rc = _rect_center_edges(pts)
        if not rc:
            continue
        cx, cy, edges, (ldx, ldy) = rc
        if ldx >= ldy:
            direct_sizes.append([round(edges[2] / 1000, 3), round(edges[0] / 1000, 3)])
        else:
            direct_sizes.append([round(edges[0] / 1000, 3), round(edges[2] / 1000, 3)])
        cols_world.append((cx, cy))

    # 旋转中心 = 柱 ins 中位数; 扶正角: 从柱主方向算(两正交主方向)
    ins_pts = [(e.dxf.insert.x, e.dxf.insert.y) for e in ins_list]
    gcx = sorted(p[0] for p in ins_pts)[len(ins_pts) // 2]
    gcy = sorted(p[1] for p in ins_pts)[len(ins_pts) // 2]

    # 扶正角: 轴网线主方向众数(线是全局基准; 柱矩形方向受 INSERT 旋转影响不可靠)
    grid_lines = []

    def walk_lines(container, p_ins, p_rot, p_sx, p_sy, _d=0):
        if _d > 6:
            return
        for e in container:
            if e.dxftype() in ('LINE', 'LWPOLYLINE'):
                lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
                if ('S-ANNO-DOTE' in lyr or 'A-GRID' in lyr or 'GRID' in lyr
                        or 'AXIS' in lyr) and '夹层' not in lyr:
                    if e.dxftype() == 'LINE':
                        p1 = _xform((e.dxf.start.x, e.dxf.start.y), p_ins, p_rot, p_sx, p_sy)
                        p2 = _xform((e.dxf.end.x, e.dxf.end.y), p_ins, p_rot, p_sx, p_sy)
                    else:
                        pts = list(e.get_points('xy'))
                        if len(pts) != 2:
                            continue
                        p1 = _xform(pts[0], p_ins, p_rot, p_sx, p_sy)
                        p2 = _xform(pts[1], p_ins, p_rot, p_sx, p_sy)
                    if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) > 30000:
                        grid_lines.append((p1, p2))
            elif e.dxftype() == 'INSERT':
                lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
                rot = e.dxf.rotation if e.dxf.hasattr('rotation') else 0
                sx = e.dxf.xscale if e.dxf.hasattr('xscale') else 1
                sy = e.dxf.yscale if e.dxf.hasattr('yscale') else 1
                ins = _xform((e.dxf.insert.x, e.dxf.insert.y), p_ins, p_rot, p_sx, p_sy)
                name = e.dxf.name
                if '夹层' in name or 'AVE_' in name:
                    continue
                if name in doc.blocks:
                    walk_lines(doc.blocks[name], ins, p_rot + rot, sx * p_sx, sy * p_sy, _d + 1)

    walk_lines(doc.modelspace(), (0, 0), 0, 1, 1)
    angs = []
    for (s, t) in grid_lines:
        a = abs(math.degrees(math.atan2(t[1] - s[1], t[0] - s[0]))) % 180
        angs.append(a if a <= 90 else 180 - a)
    rot = 0.0
    if angs:
        bins = {}
        for a in angs:
            bkey = int(a // 5) * 5
            bins.setdefault(bkey, []).append(a)
        peak = max(bins, key=lambda k: len(bins[k]))
        med = sum(bins[peak]) / len(bins[peak])
        rot = (90 - med) if med > 45 else -med

    rad = math.radians(rot)
    c_r, s_r = math.cos(rad), math.sin(rad)

    def rot_pt(p):
        x, y = p[0] - gcx, p[1] - gcy
        return (x * c_r - y * s_r + gcx, x * s_r + y * c_r + gcy)

    cols_rot = [rot_pt(p) for p in cols_world]

    # 轴距自动探测 + 网格过滤
    pitch_x = _auto_pitch([p[0] for p in cols_rot])
    pitch_y = _auto_pitch([p[1] for p in cols_rot])
    mxr = _mode_rem([p[0] for p in cols_rot], pitch_x)
    myr = _mode_rem([p[1] for p in cols_rot], pitch_y)
    grid_cols = [(wx, wy) for (wx, wy) in cols_rot
                 if min(abs((wx % pitch_x) - mxr), pitch_x - abs((wx % pitch_x) - mxr)) < 300
                 and min(abs((wy % pitch_y) - myr), pitch_y - abs((wy % pitch_y) - myr)) < 300]

    def cluster(vals, tol=100.0):
        out = []
        for v in sorted(vals):
            if out and abs(v - out[-1]) < tol:
                out[-1] = (out[-1] + v) / 2
            else:
                out.append(v)
        return out

    gx = cluster(sorted(p[0] for p in grid_cols), 100.0)
    gy = cluster(sorted(p[1] for p in grid_cols), 100.0)
    ox, oy = min(gx), min(gy)

    # 输出黄金基准元素
    n_ins = len(ins_list)
    columns = []
    for i, (wx, wy) in enumerate(cols_rot):
        if not (min(p[0] for p in cols_rot) - 5000 < wx < max(p[0] for p in cols_rot) + 5000
                and min(p[1] for p in cols_rot) - 5000 < wy < max(p[1] for p in cols_rot) + 5000):
            continue
        if i < n_ins:
            dxr = abs((wx % pitch_x) - mxr)
            dyr = abs((wy % pitch_y) - myr)
            if min(dxr, pitch_x - dxr) > 2000 or min(dyr, pitch_y - dyr) > 2000:
                continue
            w_m, d_m = 0.8, 0.8
        else:
            w_m, d_m = direct_sizes[i - n_ins]
        columns.append({
            "element_id": "col_%d" % i,
            "ifc_type": "IFCCOLUMN",
            "name": "柱-%d" % (i + 1),
            "location": [float(round((wx - ox) / 1000, 3)), float(round((wy - oy) / 1000, 3)), 0.0],
            "width": float(w_m), "depth": float(d_m),
            "base": float(base_m), "top": float(top_m),
            "confidence": float(CONFIDENCE_COLUMN),
            "source": "dxf_adaptive",
            "grid_ref": None,
        })

    # grid_ref 标注
    if gx and gy:
        for c in columns:
            bx = min(gx, key=lambda v: abs(v - (c["location"][0] * 1000 + ox) + ox - ox))
            # 最近交点
            best = min(((gxv, gyv) for gxv in gx for gyv in gy),
                       key=lambda g: (g[0] - (c["location"][0] * 1000 + ox)) ** 2
                                     + (g[1] - (c["location"][1] * 1000 + oy)) ** 2)
            d = math.hypot(best[0] - (c["location"][0] * 1000 + ox),
                           best[1] - (c["location"][1] * 1000 + oy))
            if d < 5000:
                c["grid_ref"] = "%d-%s" % (gx.index(best[0]) + 1, chr(65 + gy.index(best[1])))
            c["dist_to_grid_mm"] = float(round(d, 1))

    grid = {"x_axes": [float(round((v - ox) / 1000, 2)) for v in gx],
            "y_axes": [float(round((v - oy) / 1000, 2)) for v in gy]}
    levels = [{"name": "层", "elevation_m": float(base_m)}]

    logger.info("dxf_adaptive.done", cols=len(columns), gx=len(gx), gy=len(gy),
                base=base_m, top=top_m, rot=round(rot, 2))
    return {"columns": columns, "grid": grid, "levels": levels,
            "meta": {"col_block": col_block, "rot_deg": float(round(rot, 2)),
                     "pitch_mm": [float(pitch_x), float(pitch_y)], "n_ins": n_ins,
                     "n_direct": n_direct, "elev_m": [float(base_m), float(top_m)],
                     "rotation_center_world_mm": [float(gcx), float(gcy)],
                     "origin_world_mm": [float(ox), float(oy)]}}
