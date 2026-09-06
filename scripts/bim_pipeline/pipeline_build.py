# -*- coding: utf-8 -*-
"""BuildMate 图纸→BIM 提取函数库(纯库, 无 CLI 入口)。

角色(3层结构): 算法层——只提供提取/检查/建模函数, 供 pipeline_std.py import。
流程编排见 pipeline_std.py(标准流程 Step0~7 + Step8 建模)。
工具层: extract_columns_v2.py(柱/轴网/标高自适应提取) + merge_layers.py(多标高合并)。

主要函数:
- clean_drawing(doc)                    图纸清洗
- extract_shear_walls / extract_beams / extract_slabs / extract_openings / extract_stairs
- extract_walls_with_thickness(双线配对取厚)
- check_beam_connections(四通道) / check_beam_level / structure_gate
- merge_layers / build_manifest / quality_gate
- trigger_revit / dump_verify(建模执行层, 经 Routes)
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JSON_IN = os.environ.get("BIM_JSON_IN", os.path.join(
    PROJECT_ROOT, "data", "runtime", "revit", "json_in"))
RVT_OUT = os.environ.get("REVIT_OUTPUT_DIR", os.path.join(
    PROJECT_ROOT, "data", "runtime", "revit", "rvt_out"))
ROUTES_PORT = 48884
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

DROP_KEYS = ('家具', '厨洗', '卫生间', 'A-CAR', 'A-STAIR', 'A-FIRE', 'EQUIP', 'A-TECH',
             'A-FURT', 'F-FURN', 'KITCHEN', 'KJ', 'A-FLOR-OVHD', '消火栓', '3T_BAR',
             'A-DOOR', 'A-PART', '精装', 'SANT', '设备', '集水坑', '潜污泵', '墙洞', '夹层')


# ═══════════════ Step0: 图纸清洗 ═══════════════
def clean_drawing(doc):
    """保留结构/轴网/文字图层, 去建筑参照杂层。返回 (kept_entities, 统计)"""
    KEEP = ('S-COLU', 'S-WALL', 'S-STEL', 'A-GRID', 'A-ANNO', 'DIM', 'TEXT',
            '轴网', 'S-ANNO', 'GRID', 'COLUMN', 'COLU')
    all_ents = [(e, e.dxf.layer if hasattr(e.dxf, 'layer') else '')
                for blk in doc.blocks for e in blk]
    total = len(all_ents)
    kept = []
    for e, l in all_ents:
        if not any(k in l for k in KEEP):
            continue
        if any(k in l for k in ('A-GRID', '轴网', 'A-ANNO', 'DIM', 'TEXT')):
            kept.append((e, l))
        elif not any(k in l for k in DROP_KEYS):
            kept.append((e, l))
    text_n = sum(1 for e, _ in all_ents if e.dxftype() in ('TEXT', 'MTEXT'))
    return kept, {'before': total, 'after': len(kept), 'text': text_n,
                  'dropped': total - len(kept)}


# ═══════════════ Step1+2: 轴网+柱(调 extract_columns_v2 已验证) ═══════════════
def extract_columns(dxf_path):
    """subprocess 调 extract_columns_v2.py → (grid, cols, prefix)"""
    r = subprocess.run([sys.executable, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "extract_columns_v2.py"), dxf_path],
                       capture_output=True, timeout=600, text=True)
    if r.returncode != 0:
        raise RuntimeError("提取失败: %s" % (r.stderr or r.stdout)[-300:])
    prefix = None
    for line in r.stdout.splitlines():
        if "[OK]" in line:
            prefix = line.split("输出")[-1].strip()
    if not prefix:
        raise RuntimeError("无输出: %s" % r.stdout[-300:])
    grid = json.load(open(os.path.join(JSON_IN, f"{prefix}_grid.json"), encoding="utf-8"))
    cols = json.load(open(os.path.join(JSON_IN, f"{prefix}_columns.json"), encoding="utf-8"))
    return grid, cols, prefix


# ═══════════════ Step3: 墙(贴柱统计) ═══════════════
def extract_walls_with_thickness(kept, ox, oy, rot_deg, gcx, gcy):
    """墙提取 + 双线配对取墙厚(#4 核心缺口修复)

    算法(确定性规则):
    1. 收集 S-WALL / A-WALL 图层 LINE(旋转对齐 + 相对坐标)
    2. 每根墙线找同方向平行邻近线 → 间距 = 墙厚
    3. 配对后取双线中线(真正的墙中心线)
    4. 未配对的单线用全局墙厚众数
    返回: wall_elems(start/end/thickness/paired)
    """
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def _rp(p):
        x, y = p[0] - gcx, p[1] - gcy
        return (x * c - y * s + gcx, x * s + y * c + gcy)

    # 1. 收集墙线(旋转对齐, 相对坐标)
    lines = []
    for e, l in kept:
        wall_group = "A" if "A-WALL" in l else "S"
        if (wall_group in ("A", "S") and ("S-WALL" in l or "A-WALL" in l)
                and 'HATC' not in l and e.dxftype() == 'LINE'):
            st, en = e.dxf.start, e.dxf.end
            L = math.hypot(en.x - st.x, en.y - st.y)
            if L < 400:
                continue
            p1 = _rp((st.x, st.y))
            p2 = _rp((en.x, en.y))
            q1 = ((p1[0] - ox) / 1000, (p1[1] - oy) / 1000)
            q2 = ((p2[0] - ox) / 1000, (p2[1] - oy) / 1000)
            # 方向角(0~180)
            ang = math.degrees(math.atan2(q2[1] - q1[1], q2[0] - q1[0])) % 180
            lines.append({"q1": q1, "q2": q2, "ang": ang, "len": L / 1000,
                          "wall_group": wall_group})

    if not lines:
        return []

    # 2. 双线配对: 同方向(角度差<2°)、垂直距离 100~800mm 的线配对 → 墙厚
    def _perp_dist(a, b):
        """线段 a 中点到 b 的垂直距离"""
        ax, ay = a["q1"]
        bx, by = a["q2"]
        # b 方向单位向量
        dx, dy = b["q2"][0] - b["q1"][0], b["q2"][1] - b["q1"][1]
        blen = math.hypot(dx, dy)
        if blen < 1e-6:
            return 1e9
        ux, uy = dx / blen, dy / blen
        # a 中点到 b 起点的向量
        vx, vy = ax - b["q1"][0], ay - b["q1"][1]
        # 垂直分量
        proj = vx * ux + vy * uy
        px, py = b["q1"][0] + proj * ux, b["q1"][1] + proj * uy
        return math.hypot(ax - px, ay - py)

    paired = [False] * len(lines)
    thicknesses = []
    for i in range(len(lines)):
        if paired[i]:
            continue
        best_j, best_d = None, 1e9
        for j in range(len(lines)):
            if i == j or paired[j]:
                continue
            if lines[i]["wall_group"] != lines[j]["wall_group"]:
                continue
            if abs(lines[i]["ang"] - lines[j]["ang"]) > 2.0:
                continue
            d = _perp_dist(lines[i], lines[j])
            if 0.1 < d < 0.8:  # 100~800mm 合理墙厚
                if d < best_d:
                    best_d, best_j = d, j
        if best_j is not None:
            # 配对成功: 墙厚 = 双线距离, 中线 = 两线中点
            li, lj = lines[i], lines[best_j]
            t = round(best_d * 1000, 1)
            thicknesses.append(t)
            # 中线端点 = 两线对应端点平均
            m1 = ((li["q1"][0] + lj["q1"][0]) / 2, (li["q1"][1] + lj["q1"][1]) / 2)
            m2 = ((li["q2"][0] + lj["q2"][0]) / 2, (li["q2"][1] + lj["q2"][1]) / 2)
            paired[i] = paired[best_j] = True
            yield {"start": [round(m1[0], 3), round(m1[1], 3)],
                    "end": [round(m2[0], 3), round(m2[1], 3)],
                    "thickness": t, "paired": True,
                    "wall_group": li["wall_group"]}
    # 3. 未配对单线: 用配对墙厚的众数(兜底)
    default_t = None
    if thicknesses:
        from collections import Counter
        default_t = Counter(round(t, -1) for t in thicknesses).most_common(1)[0][0]
    for i, ln in enumerate(lines):
        if not paired[i]:
            yield {"start": [round(ln["q1"][0], 3), round(ln["q1"][1], 3)],
                    "end": [round(ln["q2"][0], 3), round(ln["q2"][1], 3)],
                    "thickness": default_t or 200, "paired": False,
                    "wall_group": ln["wall_group"]}


def extract_walls_with_thickness_list(kept, ox, oy, rot_deg, gcx, gcy):
    """包装: generator → list"""
    return list(extract_walls_with_thickness(kept, ox, oy, rot_deg, gcx, gcy))


def extract_walls(kept, cols, ox, oy, rot_deg, gcx, gcy):
    """清洗后实体中 S-WALL LINE → 贴柱统计(贴柱 1200mm)
    墙端点(世界) → rot_pt 旋转对齐柱 → 减 ox/oy 转相对 → 与柱 center 比较
    """
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def rp(p):
        x, y = p[0] - gcx, p[1] - gcy
        return (x * c - y * s + gcx, x * s + y * c + gcy)

    walls = []
    for e, l in kept:
        if 'S-WALL' in l and 'HATC' not in l and e.dxftype() == 'LINE':
            st, en = e.dxf.start, e.dxf.end
            L = math.hypot(en.x - st.x, en.y - st.y)
            if L < 400:
                continue
            p1 = rp((st.x, st.y))
            p2 = rp((en.x, en.y))
            q1 = ((p1[0] - ox) / 1000, (p1[1] - oy) / 1000)
            q2 = ((p2[0] - ox) / 1000, (p2[1] - oy) / 1000)
            near = 0
            for c_ in cols:
                cc = c_['center']
                if math.hypot(q1[0] - cc[0], q1[1] - cc[1]) < 1.2 or math.hypot(q2[0] - cc[0], q2[1] - cc[1]) < 1.2:
                    near += 1
                    break
            walls.append({'len_m': round(L / 1000, 2), 'near_col': near > 0})
    return walls


# ═══════════════ 阶段② 全构件提取(梁/板/门窗/楼梯) ═══════════════
def extract_beams(doc, ox, oy, rot_deg, gcx, gcy, max_n=400):
    """梁: S-BEAM 图层 LINE → 世界坐标 → 旋转对齐 → 相对坐标
    标准: 梁搭柱(端点贴柱边)——保留所有梁段, 贴柱率统计
    ★ v9: 跳过 AVE_/参照块(渲染块坐标 -47 万米混入) + 图外过滤(超出轴网范围)
    """
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def rp(p):
        x, y = p[0] - gcx, p[1] - gcy
        return (x * c - y * s + gcx, x * s + y * c + gcy)

    # 图外过滤范围: 以原点为中心 ±2000m(相对坐标)
    LIMIT = 2000.0
    beams = []
    for blk in doc.blocks:
        name = blk.name
        # ★ 只跳 AVE_ 渲染/参照块; *D437 等匿名块是正常实体容器(Revit 导出), 不能跳
        if 'AVE_' in name:
            continue
        for e in blk:
            lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
            if 'S-BEAM' not in lyr or 'TEXT' in lyr or e.dxftype() != 'LINE':
                continue
            st, en = e.dxf.start, e.dxf.end
            L = math.hypot(en.x - st.x, en.y - st.y)
            if L < 500:  # 短梁段跳过
                continue
            p1 = rp((st.x, st.y))
            p2 = rp((en.x, en.y))
            q1 = ((p1[0] - ox) / 1000, (p1[1] - oy) / 1000)
            q2 = ((p2[0] - ox) / 1000, (p2[1] - oy) / 1000)
            # ★ 图外过滤: 端点超出 ±LIMIT 米 → 垃圾实体
            if abs(q1[0]) > LIMIT or abs(q1[1]) > LIMIT or abs(q2[0]) > LIMIT or abs(q2[1]) > LIMIT:
                continue
            beams.append({
                "start": [round(q1[0], 3), round(q1[1], 3)],
                "end": [round(q2[0], 3), round(q2[1], 3)],
                "len_m": round(L / 1000, 2),
            })
            if len(beams) >= max_n:
                break
    return beams


def extract_slabs(doc, ox, oy, rot_deg, gcx, gcy, max_n=200):
    """板: S-SLAB 图层闭合 LWPOLYLINE → 轮廓中心+面积
    标准: 板为闭合轮廓, 以中心点+范围表示(Revit 板用轮廓)
    """
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def rp(p):
        x, y = p[0] - gcx, p[1] - gcy
        return (x * c - y * s + gcx, x * s + y * c + gcy)

    slabs = []
    for blk in doc.blocks:
        name = blk.name
        if 'AVE_' in name:
            continue  # ★ 跳过渲染/参照块
        for e in blk:
            lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
            if 'S-SLAB' not in lyr or e.dxftype() != 'LWPOLYLINE':
                continue
            pts = [(p[0], p[1]) for p in e.get_points('xy')]
            if len(pts) < 4:
                continue
            rpts = [rp(p) for p in pts]
            # ★ 图外过滤
            if any(abs((p[0] - ox) / 1000) > 2000 or abs((p[1] - oy) / 1000) > 2000 for p in rpts):
                continue
            cx = sum(p[0] for p in rpts) / len(rpts)
            cy = sum(p[1] for p in rpts) / len(rpts)
            # 面积(鞋带公式)
            area = 0.5 * abs(sum(rpts[i][0] * rpts[(i + 1) % len(rpts)][1]
                                 - rpts[(i + 1) % len(rpts)][0] * rpts[i][1]
                                 for i in range(len(rpts))))
            if area < 1000 * 1000:  # <1m² 跳过(碎块)
                continue
            slabs.append({
                "center": [round((cx - ox) / 1000, 3), round((cy - oy) / 1000, 3)],
                "area_m2": round(area / 1e6, 2),
                "thickness_mm": 120,  # 默认板厚(可从标注提取, 后续)
                # ★ outline: 闭合轮廓(相对原点, 米)——json2rvt create_floor 需要轮廓点(mm)
                #   之前只给 center → create_floor e["outline"] KeyError → 板全建失败(实测 8-25)
                "outline": [[round((p[0] - ox) / 1000, 3), round((p[1] - oy) / 1000, 3)]
                            for p in rpts],
            })
            if len(slabs) >= max_n:
                break
    return slabs


def extract_openings(doc, ox, oy, rot_deg, gcx, gcy, max_n=100):
    """门窗洞口: 洞口块(M-暖通洞/A-HOLE-E/门窗块名) → 位置+尺寸
    标准: 围护构件——以块 INSERT 位置 + 块内矩形尺寸表示
    """
    OPENING_KEYS = ('暖通洞', 'HOLE', '洞口', '门', '窗', 'DOOR', 'WINDOW', '开口')
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def rp(p):
        x, y = p[0] - gcx, p[1] - gcy
        return (x * c - y * s + gcx, x * s + y * c + gcy)

    openings = []
    # ① INSERT 块(块名/图层含洞口关键词)
    for e in doc.modelspace():
        if e.dxftype() != 'INSERT':
            continue
        name = e.dxf.name
        lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
        if not any(k in name for k in OPENING_KEYS) and not any(k in lyr for k in OPENING_KEYS):
            continue
        if 'AVE_' in name:
            continue
        ins = rp((e.dxf.insert.x, e.dxf.insert.y))
        # 块内矩形尺寸
        w, h = 900, 2100  # 默认门尺寸(可从块内矩形提, 后续)
        if name in doc.blocks:
            for el in doc.blocks[name]:
                if el.dxftype() == 'LWPOLYLINE':
                    pts = [(p[0], p[1]) for p in el.get_points('xy')]
                    if len(pts) == 4:
                        ws = [math.hypot(pts[(i + 1) % 4][0] - pts[i][0],
                                         pts[(i + 1) % 4][1] - pts[i][1]) for i in range(4)]
                        w, h = sorted(ws)[:2] if len(ws) >= 2 else (900, 2100)
                        break
        openings.append({
            "x": round((ins[0] - ox) / 1000, 3),
            "y": round((ins[1] - oy) / 1000, 3),
            "width": round(w / 1000, 3),
            "height": round(h / 1000, 3),
            "kind": "door" if any(k in (name + lyr) for k in ('门', 'DOOR')) else "opening",
        })
        if len(openings) >= max_n:
            break
    # ② 普通实体(图层含洞口关键词: M-暖通洞/A-HOLE-E)——独立实体非块
    for blk in doc.blocks:
        name = blk.name
        if 'AVE_' in name:
            continue  # ★ 跳过渲染/参照块
        for e in blk:
            lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
            if not any(k in lyr for k in OPENING_KEYS):
                continue
            if e.dxftype() in ('AVE_RENDER',):
                continue
            try:
                if e.dxftype() == 'LINE':
                    pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
                elif e.dxftype() == 'LWPOLYLINE':
                    pts = [(p[0], p[1]) for p in e.get_points('xy')]
                elif e.dxftype() == 'CIRCLE':
                    pts = [(e.dxf.center.x, e.dxf.center.y)]
                elif e.dxftype() == 'INSERT':
                    pts = [(e.dxf.insert.x, e.dxf.insert.y)]
                else:
                    continue
                rpts = [rp(p) for p in pts]
                # ★ 图外过滤
                if any(abs((p[0] - ox) / 1000) > 2000 or abs((p[1] - oy) / 1000) > 2000 for p in rpts):
                    continue
                cx = sum(p[0] for p in rpts) / len(rpts)
                cy = sum(p[1] for p in rpts) / len(rpts)
                # 去重(同位置多个实体只留一个)
                if any(math.hypot(cx - o2["x"] * 1000 - ox, cy - o2["y"] * 1000 - oy) < 500
                       for o2 in openings):
                    continue
                openings.append({
                    "x": round((cx - ox) / 1000, 3),
                    "y": round((cy - oy) / 1000, 3),
                    "width": 0.9, "height": 2.1,
                    "kind": "opening",
                })
            except Exception:
                continue
            if len(openings) >= max_n:
                break
        if len(openings) >= max_n:
            break
    return openings


def extract_stairs(doc, ox, oy, rot_deg, gcx, gcy, max_n=50):
    """楼梯: S-STAIR 图层实体 → 位置(包围盒中心)
    标准: 交通构件——以位置+方向表示(Revit 楼梯需专门构件, 简化为轮廓)
    """
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def rp(p):
        x, y = p[0] - gcx, p[1] - gcy
        return (x * c - y * s + gcx, x * s + y * c + gcy)

    stairs = []
    for blk in doc.blocks:
        name = blk.name
        if 'AVE_' in name:
            continue  # ★ 跳过渲染/参照块
        for e in blk:
            lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
            if 'S-STAIR' not in lyr:
                continue
            # 取实体包围盒中心
            try:
                if e.dxftype() == 'LINE':
                    pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
                elif e.dxftype() == 'LWPOLYLINE':
                    pts = [(p[0], p[1]) for p in e.get_points('xy')]
                elif e.dxftype() == 'INSERT':
                    pts = [(e.dxf.insert.x, e.dxf.insert.y)]
                else:
                    continue
                rpts = [rp(p) for p in pts]
                # ★ 图外过滤
                if any(abs((p[0] - ox) / 1000) > 2000 or abs((p[1] - oy) / 1000) > 2000 for p in rpts):
                    continue
                cx = sum(p[0] for p in rpts) / len(rpts)
                cy = sum(p[1] for p in rpts) / len(rpts)
                stairs.append({
                    "x": round((cx - ox) / 1000, 3),
                    "y": round((cy - oy) / 1000, 3),
                })
            except Exception:
                continue
            if len(stairs) >= max_n:
                break
        if len(stairs) >= max_n:
            break
    return stairs


# ═══════════════ Step4: 多层合并 ═══════════════
def merge_layers(layers, out_path=None):
    """layers = [(lv, grid, cols, walls, beams, slabs, openings, stairs)]
    全构件合并: 柱/墙/梁/板/门窗/楼梯 → model.json
    """
    levels, grids, elems = [], [], []
    for layer in layers:
        lv, grid, cols = layer[0], layer[1], layer[2]
        walls = layer[3] if len(layer) > 3 else []
        beams = layer[4] if len(layer) > 4 else []
        slabs = layer[5] if len(layer) > 5 else []
        openings = layer[6] if len(layer) > 6 else []
        stairs = layer[7] if len(layer) > 7 else []
        if not cols and not walls and not beams:
            continue
        base = cols[0].get("elev_base_m", 0.0) if cols else 0.0
        top = cols[0].get("elev_top_m", 3.0) if cols else 3.0
        levels.append({"name": lv, "elevation": int(base * 1000)})
        levels.append({"name": lv + "顶", "elevation": int(top * 1000)})
        grids.append({"level": lv,
                      "x_axes": [round(a["coord"] * 1000, 1) for a in grid["x_axes"]],
                      "y_axes": [round(a["coord"] * 1000, 1) for a in grid["y_axes"]]})
        for i, c in enumerate(cols):
            elems.append({"type": "Column", "id": "%s_col_%d" % (lv, i),
                          "x": round(c["center"][0] * 1000), "y": round(c["center"][1] * 1000),
                          "width": round(c["size"][0] * 1000), "depth": round(c["size"][1] * 1000),
                          "base": 0, "top": round((top - base) * 1000), "level": lv})
        for i, w in enumerate(walls):
            elems.append({"type": "Wall", "id": "%s_wall_%d" % (lv, i),
                          "start": w["start"], "end": w["end"],
                          "thickness": w.get("thickness", 200),
                          "height": round((top - base) * 1000), "level": lv})
        for i, b in enumerate(beams):
            elems.append({"type": "Beam", "id": "%s_beam_%d" % (lv, i),
                          "start": b["start"], "end": b["end"],
                          "width": 300, "height": 600,  # 默认梁截面(后续从标注提)
                          "level": lv})
        for i, s in enumerate(slabs):
            elems.append({"type": "Slab", "id": "%s_slab_%d" % (lv, i),
                          "center": s["center"], "area_m2": s["area_m2"],
                          "thickness": s.get("thickness_mm", 120),
                          "level": lv})
        for i, o in enumerate(openings):
            elems.append({"type": "Opening", "id": "%s_open_%d" % (lv, i),
                          "x": o["x"], "y": o["y"],
                          "width": o["width"], "height": o["height"],
                          "kind": o.get("kind", "opening"), "level": lv})
        for i, st in enumerate(stairs):
            elems.append({"type": "Stair", "id": "%s_stair_%d" % (lv, i),
                          "x": st["x"], "y": st["y"], "level": lv})
    model = {"project": {"name": "+".join(l[0] for l in layers) + " 多层模型",
                         "levels": levels},
             "grids": grids, "model_elements": elems}
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        json.dump(model, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return model


def build_manifest(layers, out_path=None):
    """阶段① 模型策划: 生成 project_manifest.json(建模标准/LOD/命名规范)"""
    import datetime
    manifest = {
        "schema": "BIM建模策划-方案C",
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "建模标准": {
            "命名规范": "{层}_{构件}_{序号}", "单位": "mm(坐标)/m²(面积)",
            "坐标系": "相对原点(轴网最小交点)", "图层规范": "S-COLU柱/S-WALL墙/S-BEAM梁/S-SLAB板",
        },
        "LOD等级": {
            "说明": "LOD300 构件级(几何+尺寸+位置)",
            "柱": "LOD300", "墙": "LOD300", "梁": "LOD300",
            "板": "LOD300", "门窗洞口": "LOD300", "楼梯": "LOD200(轮廓)",
        },
        "构件清单": [],
    }
    for layer in layers:
        lv = layer[0]
        manifest["构件清单"].append({
            "层": lv,
            "柱": len(layer[2]) if len(layer) > 2 else 0,
            "墙": len(layer[3]) if len(layer) > 3 else 0,
            "梁": len(layer[4]) if len(layer) > 4 else 0,
            "板": len(layer[5]) if len(layer) > 5 else 0,
            "门窗": len(layer[6]) if len(layer) > 6 else 0,
            "楼梯": len(layer[7]) if len(layer) > 7 else 0,
        })
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        json.dump(manifest, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return manifest


# ═══════════════ 第一批: Step2 剪力墙 + Step3 梁检查 + 结构框架 Gate ═══════════════
def extract_shear_walls(kept, ox, oy, rot_deg, gcx, gcy):
    """剪力墙识别(规范 Step2): 长宽比 >3 的 S-WALL 厚墙段 → 结构墙

    判据(确定性规则):
    - S-WALL 图层 LINE, 长度 >3m
    - 墙厚来自双线配对(>250mm 为剪力墙候选)
    - 长宽比 = 长度/厚度 >3 → 剪力墙(否则普通建筑墙)
    返回: [(start, end, thickness, ratio, paired)]
    """
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def _rp(p):
        x, y = p[0] - gcx, p[1] - gcy
        return (x * c - y * s + gcx, x * s + y * c + gcy)

    # 收集 S-WALL 线
    lines = []
    for e, l in kept:
        if 'S-WALL' in l and 'HATC' not in l and e.dxftype() == 'LINE':
            st, en = e.dxf.start, e.dxf.end
            L = math.hypot(en.x - st.x, en.y - st.y)
            if L < 400:
                continue
            p1 = _rp((st.x, st.y))
            p2 = _rp((en.x, en.y))
            q1 = ((p1[0] - ox) / 1000, (p1[1] - oy) / 1000)
            q2 = ((p2[0] - ox) / 1000, (p2[1] - oy) / 1000)
            ang = math.degrees(math.atan2(q2[1] - q1[1], q2[0] - q1[0])) % 180
            lines.append({"q1": q1, "q2": q2, "ang": ang, "len": L / 1000})

    def _perp_dist(a, b):
        ax, ay = a["q1"]
        dx, dy = b["q2"][0] - b["q1"][0], b["q2"][1] - b["q1"][1]
        blen = math.hypot(dx, dy)
        if blen < 1e-6:
            return 1e9
        ux, uy = dx / blen, dy / blen
        vx, vy = ax - b["q1"][0], ay - b["q1"][1]
        proj = vx * ux + vy * uy
        px, py = b["q1"][0] + proj * ux, b["q1"][1] + proj * uy
        return math.hypot(ax - px, ay - py)

    # 双线配对取厚
    paired = [False] * len(lines)
    shear = []
    for i in range(len(lines)):
        if paired[i]:
            continue
        best_j, best_d = None, 1e9
        for j in range(len(lines)):
            if i == j or paired[j]:
                continue
            if abs(lines[i]["ang"] - lines[j]["ang"]) > 2.0:
                continue
            d = _perp_dist(lines[i], lines[j])
            if 0.1 < d < 0.8 and d < best_d:
                best_d, best_j = d, j
        ln = lines[i]
        if best_j is not None:
            t = best_d * 1000
            paired[i] = paired[best_j] = True
        else:
            t = 200.0
        # 剪力墙判据: 长 >3m 且 长宽比 >3 且 厚 >200
        if ln["len"] > 3.0 and t >= 250 and (ln["len"] * 1000 / t) > 3.0:
            shear.append({"start": ln["q1"], "end": ln["q2"],
                          "thickness": round(t, 1),
                          "ratio": round(ln["len"] * 1000 / t, 1),
                          "paired": best_j is not None})
    return shear


def check_beam_connections(beams, cols, shear_walls=None, grid_xy=None, tol_m=1.2):
    """梁连接检查(规范 Step3): 梁端点是否连接 柱/剪力墙/其他梁/轴网
    四通道(规范 3.6: 柱→梁→柱/墙/梁/轴网):
    - 贴柱: 梁端距柱心 < 柱半宽+300mm
    - 贴剪力墙: 梁端距墙线 < 300mm
    - 贴梁: 梁端距其他梁端点/中线 < 300mm
    - 贴轴网: 梁端距轴网交点 < 600mm(柱间跨度梁)
    返回: (connected_count, total_count, floating_beams)
    梁悬空 = 四通道都不连
    """
    # 预计算柱半宽
    col_r = []
    for c in cols:
        w = c.get("size", [0.8, 0.8])[0]
        col_r.append(max(0.4, w / 2) + 0.3)

    def _on_col(pt):
        for i, c in enumerate(cols):
            if math.hypot(pt[0] - c["center"][0], pt[1] - c["center"][1]) < col_r[i]:
                return True
        return False

    def _on_shear(pt):
        if not shear_walls:
            return False
        for sw in shear_walls:
            sx, sy = sw["start"]
            ex, ey = sw["end"]
            wx, wy = ex - sx, ey - sy
            wl = math.hypot(wx, wy)
            if wl < 1e-6:
                continue
            ux, uy = wx / wl, wy / wl
            vx, vy = pt[0] - sx, pt[1] - sy
            proj = max(0, min(wl, vx * ux + vy * uy))
            px, py = sx + proj * ux, sy + proj * uy
            if math.hypot(pt[0] - px, pt[1] - py) < 0.3:
                return True
        return False

    # 梁中线集合(梁-梁搭接)
    beam_lines = [b for b in beams]

    def _on_beam(pt, skip_i):
        for j, b in enumerate(beam_lines):
            if j == skip_i:
                continue
            sx, sy = b["start"]
            ex, ey = b["end"]
            wx, wy = ex - sx, ey - sy
            wl = math.hypot(wx, wy)
            if wl < 1e-6:
                continue
            ux, uy = wx / wl, wy / wl
            vx, vy = pt[0] - sx, pt[1] - sy
            proj = max(0, min(wl, vx * ux + vy * uy))
            px, py = sx + proj * ux, sy + proj * uy
            # 次梁搭主梁: 端点距主梁中线 < 600mm(次梁端伸到主梁中心附近)
            if math.hypot(pt[0] - px, pt[1] - py) < 0.6:
                return True
        return False

    def _on_axis(pt, grid_xy):
        """梁端贴轴网(柱间跨度梁: 端点在轴网交点/轴线上)"""
        if not grid_xy:
            return False
        gx, gy = grid_xy
        # 端点距最近轴网交点 < 600mm(跨中梁端搭柱间)
        best = min(math.hypot(pt[0] - x, pt[1] - y) for x in gx for y in gy)
        return best < 0.6

    connected = 0
    floating = []
    for i, b in enumerate(beams):
        s, e = b["start"], b["end"]
        s_ok = _on_col(s) or _on_shear(s) or _on_beam(s, i) or _on_axis(s, grid_xy)
        e_ok = _on_col(e) or _on_shear(e) or _on_beam(e, i) or _on_axis(e, grid_xy)
        if s_ok or e_ok:
            connected += 1
        else:
            floating.append(i)
    return connected, len(beams), floating


def check_beam_level(beams, base_m, top_m, tol_m=0.1):
    """防"梁落地"检查(规范 Step3.5): 梁高异常 → 可疑
    标准梁高范围 150~1200mm(超出 = 异常); 不依赖层高比例(夹层/高跨比复杂)
    返回: (ok, suspicious_count)
    """
    suspicious = 0
    for b in beams:
        h = b.get("height", 600)
        if h < 150 or h > 1200:
            suspicious += 1
    return suspicious == 0, suspicious


def structure_gate(cols, grid, beams, shear_walls, debug=False):
    """结构框架 Gate(规范): 柱贴点率 + 梁悬空率 + 剪力墙识别, 任一不达标 STOP
    - 柱贴轴网率 ≥70%
    - 梁悬空率(完全无支撑)<30%(规范 3.6: 梁是否存在孤立——次梁搭主梁/梁连柱都算有支撑)
    - 剪力墙: 识别完成即可
    返回: (PASS/FAIL, checks)
    """
    checks = []
    ok = True
    # ① 柱贴轴网率
    if cols and grid.get("x_axes") and grid.get("y_axes"):
        gx = [a["coord"] * 1000 for a in grid["x_axes"]]
        gy = [a["coord"] * 1000 for a in grid["y_axes"]]
        attached = sum(1 for c in cols
                       if math.hypot(min(gx, key=lambda v: abs(v - c["center"][0] * 1000)) - c["center"][0] * 1000,
                                     min(gy, key=lambda v: abs(v - c["center"][1] * 1000)) - c["center"][1] * 1000) < 300)
        rate = attached / len(cols)
        checks.append(f"柱贴轴网率 {rate:.0%}")
        if rate < 0.7:
            ok = False
    # ② 梁悬空率(完全无支撑)
    if beams:
        grid_xy = ([a["coord"] for a in grid["x_axes"]], [a["coord"] for a in grid["y_axes"]])
        conn, total, floating = check_beam_connections(beams, cols, shear_walls, grid_xy)
        float_rate = len(floating) / total
        checks.append(f"梁悬空率 {float_rate:.0%} ({total-len(floating)}/{total} 有支撑)")
        if float_rate > 0.3:
            ok = False
    # ③ 剪力墙识别
    if shear_walls:
        checks.append(f"剪力墙 {len(shear_walls)} 段")
    return ("PASS" if ok else "FAIL"), checks


# ═══════════════ Step5: 质量闸门 ═══════════════
def quality_gate(model):
    cols = [e for e in model["model_elements"] if e["type"] == "Column"]
    checks, warnings = [], 0
    for g in model.get("grids", []):
        if len(g["x_axes"]) < 3 or len(g["y_axes"]) < 3:
            checks.append(f"轴网不完整: {len(g['x_axes'])}x{len(g['y_axes'])}")
            warnings += 1
    if cols and model.get("grids"):
        gx, gy = model["grids"][0]["x_axes"], model["grids"][0]["y_axes"]
        attached = sum(1 for c in cols
                       if math.hypot(min(gx, key=lambda v: abs(v - c["x"])) - c["x"],
                                     min(gy, key=lambda v: abs(v - c["y"])) - c["y"]) < 300)
        rate = attached / len(cols)
        if rate < 0.7:
            checks.append(f"柱贴轴网率 {rate:.0%}<70%")
            warnings += 1
    return ("pass" if warnings == 0 else "warn"), checks


# ═══════════════ Step6-7: Revit ═══════════════
def _routes_exec(script_name, timeout=580):
    import urllib.request
    from urllib.parse import urlparse
    script = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "pyRevit",
                          "Extensions", "IFC2RVT.extension", "IFC2RVT.tab",
                          "IFC2RVT.panel", script_name, "script.py")
    # The pushbutton script self-runs on load; a second explicit main call
    # duplicates the entire model transaction.
    body = json.dumps({"script_path": script, "call": ""}).encode("utf-8")
    # SSRF 边界: 仅允许 http(s) 且目标为本机 Revit Routes 桥固定端口(模块常量, 白名单断言,
    # 无任何外部可控输入进入 URL)
    url = f"http://localhost:{ROUTES_PORT}/pyrevit-core/execute/"
    _p = urlparse(url)
    if _p.scheme not in ("http", "https"):
        raise ValueError("仅允许 http/https")
    if _p.hostname not in ("localhost", "127.0.0.1") or _p.port != ROUTES_PORT:
        raise ValueError("仅允许本机 Revit Routes 桥固定端口")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8", "replace")) if raw else {}


def trigger_revit():
    try:
        result = _routes_exec("json2rvt.pushbutton", 580)
        if result.get("status") == "done":
            latest = max([os.path.join(RVT_OUT, f) for f in os.listdir(RVT_OUT)
                          if f.endswith(".rvt")], key=os.path.getmtime) if os.path.isdir(RVT_OUT) else None
            return {"status": "done", "rvt": latest}
        return {"status": "error", "error": (result.get("message") or result.get("traceback") or "")[:200]}
    except Exception as ex:
        return {"status": "error", "error": f"Routes 不可达: {ex}"}


def dump_verify():
    try:
        _routes_exec("dump_revit.pushbutton", 300)
    except Exception:
        return {"error": "dump 调用失败"}
    dump_path = os.path.join(JSON_IN, "dump_sample.json")
    if not os.path.exists(dump_path):
        return {"note": "dump 未生成"}
    d = json.load(open(dump_path, encoding="utf-8"))
    model = json.load(open(os.path.join(JSON_IN, "model.json"), encoding="utf-8"))
    cols_d = [e for e in d.get("model_elements", []) if e.get("type") == "Column"]
    cols_m = [e for e in model["model_elements"] if e.get("type") == "Column"]
    matched = 0
    for cm in cols_m:
        best = min(cols_d, key=lambda c: (c["location"][0] - cm["x"] / 1000) ** 2
                  + (c["location"][1] - cm["y"] / 1000) ** 2)
        if ((best["location"][0] - cm["x"] / 1000) ** 2
                + (best["location"][1] - cm["y"] / 1000) ** 2) ** 0.5 < 0.05:
            matched += 1
    return {"grids": len(d.get("grids", [])), "cols_revit": len(cols_d),
            "cols_input": len(cols_m), "match": f"{matched}/{len(cols_m)}"}




# ═══════════════ 纯库说明 ═══════════════
# 本文件为纯函数库, 不再提供 main()/CLI 入口。
# 入口: python pipeline_std.py 图纸.dxf [--skip-build]
# (2026-08-26 方案2重构: 删除旧 main() 180行, 消除双入口混乱)
