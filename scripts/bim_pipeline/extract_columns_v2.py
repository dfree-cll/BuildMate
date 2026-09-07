# -*- coding: utf-8 -*-
"""通用楼层柱提取器 v2 —— 不写死任何图纸参数
输入: DXF 路径 + 楼层名(可选)
输出: {楼层名}_columns.json / {楼层名}_grid.json

自动探测:
1. 柱块: 全图找含"墙柱"的块(或 S-COLU INSERT 最多的块), 自动读块内矩形中心
2. 旋转角: 从柱 INSERT 的 rotation 自动读(不写死 353.63)
3. 块内中心: 从块内 LWPOLYLINE 4点平均自动算
4. 扶正角: 从轴网线主方向自动算(已有逻辑)
5. 标高: 从块名括号自动解析 (-6.5~-0.1m) → base/top
6. 镜像柱: sx<0 自动识别, 块内中心 x 取反
"""
import ezdxf, math, json, re, sys, os
from ezdxf import bbox as ezbbox
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DXF = sys.argv[1] if len(sys.argv) > 1 else ''
OUT = os.environ.get('BIM_JSON_IN', os.path.join(
    PROJECT_ROOT, 'data', 'runtime', 'revit', 'json_in'))
if not DXF:
    raise SystemExit('请提供 DXF 路径：python extract_columns_v2.py <drawing.dxf>')

# ★ 可成长机制(四本账知识库): 查库命中 → 提取 → 坑位自查 → 画像入库
_kb = None
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cad_knowledge_kb import KnowledgeBase
    _kb = KnowledgeBase()
except Exception as _e:
    print('[KB] 知识库加载失败(不影响提取): %s' % str(_e)[:80])

doc = ezdxf.readfile(DXF)

# ★ 轴网线图层关键词: 从 KB layer_map 读('轴网线图层' 角色), 兜底写死
#   入库即生效: layer_map 加 S-ANNO-AXIS → 这里自动带上, 不用改代码(PIT-10)
GRID_LAYER_KW = ('S-ANNO-DOTE', 'A-GRID', 'GRID')
if _kb is not None:
    try:
        _gk = _kb.get_layer_kw('轴网线图层')
        if _gk:
            GRID_LAYER_KW = tuple(_gk)
            print('[KB] 轴网线图层关键词: %s' % list(GRID_LAYER_KW))
    except Exception:
        pass

# 查库: 画像命中 → 提示已知参数(同设计院第二次跑直接命中)
if _kb is not None:
    try:
        layer_names = [l.dxf.name for l in doc.layers]
        hit = _kb._match_profile(layer_names)
        if hit:
            print('[KB] 画像命中: %s | 上次参数: 轴网%s, 柱=%s' % (
                hit.get('id', '?'),
                (hit.get('解析结果') or {}).get('轴网', '?'),
                (hit.get('解析结果') or {}).get('柱', '?')))
        else:
            print('[KB] 新项目(无画像)——提取后自动入库')
    except Exception as _e:
        print('[KB] 查库失败: %s' % str(_e)[:80])

# ============ 1. 自动找柱块(图层名自适应) ============
# ★ 不写死 S-COLU: 按"实体特征"找——含最多 4顶点闭合矩形 INSERT 的块 = 柱块
# 图层名关键词只是辅助(COLU/COL/柱/COLUMN/STRUCT 等常见命名)
COL_LAYER_KW = ('COLU', 'COL', '柱', 'COLUMN', 'STRUCT-COL', 'C-COL', 'COLS')
COL_SHAPE_KW = ('S-COLU', 'COLU', '柱', 'COLUMN')  # 备选形状关键词

def find_col_block(doc):
    """全图找柱块: ①含'墙柱'的块 ②含最多 4顶点闭合矩形的 INSERT 的块 ③含最多柱类图层实体的块"""
    wall_cols = []          # 含"墙柱"的块
    rect_blocks = {}        # 块名 -> (矩形INSERT数, 矩形LWP数)
    layer_col_blocks = {}   # 块名 -> 柱类图层实体数

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
                            if len(pts) == 4:
                                edges = [math.hypot(pts[(i+1) % 4][0] - pts[i][0], pts[(i+1) % 4][1] - pts[i][1]) for i in range(4)]
                                if all(100 <= ed <= 3000 for ed in edges):
                                    n_rect_ins += 1
                                    break
            elif e.dxftype() == 'LWPOLYLINE':
                pts = list(e.get_points('xy'))
                if len(pts) == 4:
                    edges = [math.hypot(pts[(i+1) % 4][0] - pts[i][0], pts[(i+1) % 4][1] - pts[i][1]) for i in range(4)]
                    if all(100 <= ed <= 3000 for ed in edges):
                        n_rect_lwp += 1
        score = (n_rect_ins * 2 + n_rect_lwp, n_layer_col, n_rect_ins)
        if n_rect_ins > 0 or n_layer_col > 0:
            rect_blocks[name] = score
        if '墙柱' in name:
            wall_cols.append(name)

    if not rect_blocks:
        raise RuntimeError('未找到柱块(无矩形INSERT/柱图层)')
    # 优先: 含"墙柱"的块; 其次 矩形INSERT最多(得分最高)
    if wall_cols:
        best = max(wall_cols, key=lambda n: rect_blocks.get(n, (0, 0, 0)))
    else:
        best = max(rect_blocks, key=lambda n: rect_blocks[n])
    return best, rect_blocks[best]

col_block, col_score = find_col_block(doc)
print('[AUTO] 柱块: %s (得分=%s)' % (col_block, col_score))

# ============ 2. 解析标高(从块名括号) ============
m_elev = re.search(r'\((-?\d+\.?\d*)\s*m?\s*~\s*(-?\d+\.?\d*)\s*m\)', col_block)
if m_elev:
    base_m = float(m_elev.group(1))
    top_m = float(m_elev.group(2))
    print('[AUTO] 标高: %.1f ~ %.1f m' % (base_m, top_m))
else:
    base_m, top_m = 0.0, 3.0
    print('[AUTO] 块名无标高括号, 默认 0~3m')

# ============ 3. 柱块内实体 ============
b = doc.blocks[col_block]
ins_list = []
direct_lwps = []
inner_c = None

for e in b:
    lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
    if 'S-COLU' not in lyr:
        continue
    if e.dxftype() == 'INSERT':
        ins_list.append(e)
    elif e.dxftype() == 'LWPOLYLINE':
        pts = list(e.get_points('xy'))
        if len(pts) == 4:
            direct_lwps.append(pts)

# 块内矩形中心(从 INSERT 引用最多的子块算)
sub_counts = Counter(e.dxf.name for e in ins_list)
print('[AUTO] INSERT 子块分布:', dict(sub_counts.most_common(3)))
main_sub = sub_counts.most_common(1)[0][0]
if main_sub in doc.blocks:
    for el in doc.blocks[main_sub]:
        if el.dxftype() == 'LWPOLYLINE':
            pts = list(el.get_points('xy'))
            if len(pts) == 4:
                inner_c = (sum(p[0] for p in pts) / 4, sum(p[1] for p in pts) / 4)
                break
if inner_c is None and direct_lwps:
    pts = direct_lwps[0]
    inner_c = (sum(p[0] for p in pts) / 4, sum(p[1] for p in pts) / 4)
print('[AUTO] 块内中心: (%.4f, %.4f)' % inner_c)

# ============ 4. 柱心 = ins + 旋转(块内中心), 含镜像 ============
cols_world = []
direct_sizes = []
# ★ 块内截面缓存: 子块名 -> {'w':米,'h':米,'profile':None|{...}}
# 修复: INSERT 路径原默认 size=[0.8,0.8](195/204 根长方形被建成正方形的根因)。
# 块内闭合 LWPOLYLINE 4 顶点=矩形截面(读真实宽深); >4 顶点/CIRCLE=异形截面(提 profile)。
_sec_cache = {}
def _section_of(bname):
    """子块截面信息: 真实宽深(米)、异形 profile、块内截面中心(用于柱心定位)。
    ★ 修复: 此前所有 INSERT 统一用主块中心 inner_c 做偏移——子块用绝对坐标绘制时
    (CAD 拷贝常见)其他型号柱(如 fbz1 1200×600 扶壁柱)位置整体错位, 被网格过滤丢弃。"""
    if bname in _sec_cache:
        return _sec_cache[bname]
    w = h = None
    profile = None
    c = None
    try:
        sb = doc.blocks.get(bname)
        for el in sb:
            if el.dxftype() == 'LWPOLYLINE' and el.closed:
                pts = list(el.get_points('xy'))
                c = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
                if len(pts) == 4:
                    # ★ bbox 法保留朝向: w=块内X方向宽, h=块内Y方向深。
                    # 此前用 edges 排序(长边,短边)丢失横竖信息——fbz1 块内 600(X)×1200(Y)
                    # 竖放被输出成 [1200,600], 11 根横竖全反(rotation≈0 的全错, ≈90°的碰巧对)
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    w, h = round((max(xs) - min(xs)) / 1000, 3), round((max(ys) - min(ys)) / 1000, 3)
                elif len(pts) > 4:
                    # 异形多边形: 输出相对截面中心的偏移(mm)——块内常为绝对坐标, 直接用会错位
                    profile = {'kind': 'poly',
                               'points': [[round(p[0] - c[0], 1), round(p[1] - c[1], 1)] for p in pts]}
                break
            if el.dxftype() == 'CIRCLE':
                profile = {'kind': 'circle', 'r': round(el.dxf.radius, 1)}
                c = (el.dxf.center.x, el.dxf.center.y)
                break
    except Exception:
        pass
    _sec_cache[bname] = {'w': w, 'h': h, 'profile': profile, 'c': c}
    return _sec_cache[bname]

ins_sizes = []     # 与 ins_list 对齐: 每根 INSERT 柱的世界系 [w,h](米)
ins_profiles = []  # 与 ins_list 对齐: 异形轮廓(块内坐标, 米)
for e in ins_list:
    ix, iy = e.dxf.insert.x, e.dxf.insert.y
    sx = e.dxf.xscale if e.dxf.hasattr('xscale') else 1
    sy = e.dxf.yscale if e.dxf.hasattr('yscale') else 1
    rot = math.radians(e.dxf.rotation if e.dxf.hasattr('rotation') else 0)
    c, s = math.cos(rot), math.sin(rot)
    sec = _section_of(e.dxf.name)
    # 柱心偏移用各自子块的截面中心(主块回退全局 inner_c)
    _ic = sec['c'] if sec['c'] is not None else inner_c
    icx, icy = _ic[0] * sx, _ic[1] * sy
    cols_world.append((ix + icx * c - icy * s, iy + icx * s + icy * c))
    if sec['w'] is not None:
        ww, hh = sec['w'] * abs(sx), sec['h'] * abs(sy)
        # 旋转≈90°/270° 时宽深在世界系互换
        rot_deg = math.degrees(rot) % 180
        if 45 < rot_deg < 135:
            ww, hh = hh, ww
        ins_sizes.append([round(ww, 3), round(hh, 3)])
    else:
        ins_sizes.append(None)   # None=未知, 输出时回退默认
    ins_profiles.append(sec['profile'])

# 直接 LWPOLYLINE 柱(尺寸+方向自动)
n_direct = len(direct_lwps)
for pts in direct_lwps:
    cx = sum(p[0] for p in pts) / 4
    cy = sum(p[1] for p in pts) / 4
    edges = sorted([math.hypot(pts[(i + 1) % 4][0] - pts[i][0], pts[(i + 1) % 4][1] - pts[i][1]) for i in range(4)])
    long_dx = long_dy = 0
    for i in range(4):
        p1, p2 = pts[i], pts[(i + 1) % 4]
        if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) == edges[2]:
            long_dx, long_dy = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
    if long_dx >= long_dy:
        direct_sizes.append([round(edges[2] / 1000, 3), round(edges[0] / 1000, 3)])
    else:
        direct_sizes.append([round(edges[0] / 1000, 3), round(edges[2] / 1000, 3)])
    cols_world.append((cx, cy))

# ============ 5. 旋转中心 = 柱 ins 中位数 + 扶正 ============
ins_pts = [(e.dxf.insert.x, e.dxf.insert.y) for e in ins_list]
gcx = sorted(p[0] for p in ins_pts)[len(ins_pts) // 2]
gcy = sorted(p[1] for p in ins_pts)[len(ins_pts) // 2]

# 扶正角: 从轴网线主方向自动算(长线 >30m)
grid_lines = []
def xform(pt, ins, rot_deg, sx, sy):
    r = math.radians(rot_deg); c, s = math.cos(r), math.sin(r)
    return (pt[0] * sx * c - pt[1] * sy * s + ins[0], pt[0] * sx * s + pt[1] * sy * c + ins[1])

def walk_lines(container, p_ins, p_rot, p_sx, p_sy, _d=0):
    if _d > 6: return
    for e in container:
        if e.dxftype() in ('LINE', 'LWPOLYLINE'):
            lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
            # ★ 轴网线图层关键词从 KB layer_map 读(入库即生效, PIT-10 修复)
            #   写死版: S-ANNO-DOTE/A-GRID/GRID —— 漏 S-ANNO-AXIS(主轴线)/S-AXIS
            if any(k in lyr for k in GRID_LAYER_KW) and '夹层' not in lyr:
                if e.dxftype() == 'LINE':
                    p1 = xform((e.dxf.start.x, e.dxf.start.y), p_ins, p_rot, p_sx, p_sy)
                    p2 = xform((e.dxf.end.x, e.dxf.end.y), p_ins, p_rot, p_sx, p_sy)
                else:
                    pts = list(e.get_points('xy'))
                    if len(pts) != 2: continue
                    p1 = xform(pts[0], p_ins, p_rot, p_sx, p_sy)
                    p2 = xform(pts[1], p_ins, p_rot, p_sx, p_sy)
                if math.hypot(p2[0]-p1[0], p2[1]-p1[1]) > 30000:
                    grid_lines.append((p1, p2))
        elif e.dxftype() == 'INSERT':
            lyr = e.dxf.layer if hasattr(e.dxf, 'layer') else ''
            rot = e.dxf.rotation if e.dxf.hasattr('rotation') else 0
            sx = e.dxf.xscale if e.dxf.hasattr('xscale') else 1
            sy = e.dxf.yscale if e.dxf.hasattr('yscale') else 1
            ins = xform((e.dxf.insert.x, e.dxf.insert.y), p_ins, p_rot, p_sx, p_sy)
            name = e.dxf.name
            if '夹层' in name or 'AVE_' in name: continue
            if name in doc.blocks:
                walk_lines(doc.blocks[name], ins, p_rot + rot, sx * p_sx, sy * p_sy, _d + 1)
walk_lines(doc.modelspace(), (0, 0), 0, 1, 1)

angs = []
for (s, t) in grid_lines:
    ang = abs(math.degrees(math.atan2(t[1] - s[1], t[0] - s[0]))) % 180
    angs.append(ang if ang <= 90 else 180 - ang)
if angs:
    bins = {}
    for a in angs:
        bkey = int(a // 5) * 5
        bins.setdefault(bkey, []).append(a)
    peak = max(bins, key=lambda k: len(bins[k]))
    med = sum(bins[peak]) / len(bins[peak])
    rot = (90 - med) if med > 45 else -med
else:
    rot = 0.0
print('[AUTO] 轴网线 %d 条, 扶正角 %+.2f°' % (len(grid_lines), rot))

rad = math.radians(rot)
c_r, s_r = math.cos(rad), math.sin(rad)
def rot_pt(p):
    x, y = p[0] - gcx, p[1] - gcy
    return (x * c_r - y * s_r + gcx, x * s_r + y * c_r + gcy)

cols_rot = [rot_pt(p) for p in cols_world]

# 柱编号语义：保留图纸中的 KZ/FBZ 等编号，并按扶正后的最近柱心关联。
# 旧流程完全忽略 TEXT，导致 KZ8 到 JSON 后只剩截面尺寸，Revit 无法区分柱型。
_column_marks = []
for _te in doc.modelspace():
    if _te.dxftype() not in ('TEXT', 'MTEXT'):
        continue
    try:
        _txt = (_te.dxf.text if _te.dxftype() == 'TEXT' else _te.plain_text()).strip()
        # 结构柱编号不只有 KZ：剪力墙边缘构件常用 YBZ/GBZ/GGZ/AZ/QZ。
        # 这些文字位于 S-COLU-TEXT 或 S-W-INER-CO-NUMS 等柱编号图层。
        _mm = re.search(
            r'\b((?:KZ|FBZ|XZ|YBZ|GBZ|GGZ|AZ|QZ)\s*-?\s*\d+[A-Z]?)\b',
            _txt, re.I)
        if not _mm:
            continue
        _tp = rot_pt((_te.dxf.insert.x, _te.dxf.insert.y))
        _mark = re.sub(r'\s+', '', _mm.group(1)).upper()
        _layer = _te.dxf.layer if hasattr(_te.dxf, 'layer') else ''
        _priority = 0 if ('COLU-TEXT' in _layer or 'INER-CO-NUMS' in _layer) else 1
        # 文字插入点不等于可见文字边界。取完整包围框并转换到扶正坐标，
        # 后续计算“文字边缘到柱轮廓边缘”的距离。
        _tb = ezbbox.extents([_te])
        if _tb.has_data:
            _corners = [
                rot_pt((_tb.extmin.x, _tb.extmin.y)),
                rot_pt((_tb.extmin.x, _tb.extmax.y)),
                rot_pt((_tb.extmax.x, _tb.extmin.y)),
                rot_pt((_tb.extmax.x, _tb.extmax.y)),
            ]
            _tminx = min(p[0] for p in _corners); _tmaxx = max(p[0] for p in _corners)
            _tminy = min(p[1] for p in _corners); _tmaxy = max(p[1] for p in _corners)
        else:
            _tminx = _tmaxx = _tp[0]; _tminy = _tmaxy = _tp[1]
        _column_marks.append(
            (_mark, _tp[0], _tp[1], _priority, _tminx, _tminy, _tmaxx, _tmaxy))
    except Exception:
        pass
print('[AUTO] 柱编号标注: %d 个' % len(_column_marks))

# 文字与柱一对一匹配：一个文字不能被多个邻柱重复使用。旧版逐柱各自找最近文字，
# 会把同一个 KZ 标注复制给周边多根柱，造成型号数量膨胀。
_mark_by_col_index = {}
_pairs = []
for _ci, (_cx, _cy) in enumerate(cols_rot):
    if _ci < len(ins_list):
        _cs = ins_sizes[_ci] or [0.8, 0.8]
    else:
        _cs = direct_sizes[_ci - len(ins_list)]
    _hw, _hh = _cs[0] * 500.0, _cs[1] * 500.0
    for _mi, _mk in enumerate(_column_marks):
        _gapx = max(_mk[4] - (_cx + _hw), (_cx - _hw) - _mk[6], 0.0)
        _gapy = max(_mk[5] - (_cy + _hh), (_cy - _hh) - _mk[7], 0.0)
        _md = math.hypot(_gapx, _gapy)
        # 近距离层只接受文字边缘距柱轮廓不超过 1.5m；更远的交给引线层。
        if _md <= 1500:
            _pairs.append((_md + _mk[3] * 500.0, _md, _ci, _mi))
_used_cols, _used_marks = set(), set()
for _score, _md, _ci, _mi in sorted(_pairs):
    if _ci in _used_cols or _mi in _used_marks:
        continue
    _mark_by_col_index[_ci] = (_column_marks[_mi][0], _md)
    _used_cols.add(_ci)
    _used_marks.add(_mi)

# ============ 6. 轴网聚类 + 网格过滤 ============
def cluster(vals, tol=100.0):
    out = []
    for v in sorted(vals):
        if out and abs(v - out[-1]) < tol:
            out[-1] = (out[-1] + v) / 2
        else:
            out.append(v)
    return out

def _mode_rem(vals, pitch):
    """余数基准: 用动态轴距 pitch(不写死 9000)"""
    cnt = Counter(round(v % pitch, 0) for v in vals)
    return cnt.most_common(1)[0][0]

# ★ 轴距自动探测: 从柱坐标间距的众数算(9m/8m/7.5m 自动识别)
def _auto_pitch(vals):
    ds = []
    sv = sorted(set(round(v, 1) for v in vals))
    for i in range(1, len(sv)):
        d = sv[i] - sv[i - 1]
        if 3000 < d < 15000:
            ds.append(round(d, 0))
    if not ds:
        return 9000.0
    return Counter(ds).most_common(1)[0][0]

_pitch_x = _auto_pitch([p[0] for p in cols_rot])
_pitch_y = _auto_pitch([p[1] for p in cols_rot])
print('[AUTO] 轴距: x=%.0fmm y=%.0fmm' % (_pitch_x, _pitch_y))

# ★★ 线定位优先(修复 G50 柱反推丢无柱轴): 轴网坐标从轴线聚类
#    柱反推只保留"有柱的轴" → 无柱轴(G-21~G-25 延伸轴)丢失
#    轴线(38条 LWPOLYLINE)包含全部轴 → 聚类得真实轴网
#    线定位成功(竖≥20 横≥5)用线, 否则回退柱反推
#    ★ 旋转中心必须用柱 ins 中位数(gcx/gcy)——柱和轴网同旋转中心才不错位
#    (实测: 用线自身中心 → 柱偏 1.28m; 用柱中心 → 完全对齐)
_gx_line, _gy_line = [], []
try:
    _rad = math.radians(rot)
    _cc, _ss = math.cos(_rad), math.sin(_rad)
    def _rp_line(p):
        x, y = float(p[0]) - gcx, float(p[1]) - gcy
        return (x * _cc - y * _ss + gcx, x * _ss + y * _cc + gcy)
    _h_y, _v_x = [], []
    for (_s, _t) in grid_lines:
        _L = math.hypot(_t[0] - _s[0], _t[1] - _s[1])
        if _L < 50000:  # 长度过滤: <50m 构造线不算正式轴
            continue
        _s2, _t2 = _rp_line(_s), _rp_line(_t)
        _dx, _dy = abs(_s2[0] - _t2[0]), abs(_s2[1] - _t2[1])
        if _dy <= _dx * 0.35:      # 横线 → y
            _h_y.append((_s2[1] + _t2[1]) / 2)
        elif _dx <= _dy * 0.35:    # 竖线 → x
            _v_x.append((_s2[0] + _t2[0]) / 2)
    _gx_line = cluster(sorted(_v_x), 400.0)
    _gy_line = cluster(sorted(_h_y), 400.0)
    print('[AUTO] 线定位: 竖 %d / 横 %d (轴网线 %d 条, ≥50m)' % (
        len(_gx_line), len(_gy_line), len(grid_lines)))
except Exception as _e:
    print('[WARN] 线定位失败: %s' % str(_e)[:80])

# 轴网坐标: 线定位优先, 柱反推兜底
_axis_items2 = []
if len(_gx_line) >= 20 and len(_gy_line) >= 5:
    gx, gy = _gx_line, _gy_line
    # ★ 轴号标注(不砍轴!): 27竖10横全是真实轴线(主区+参照区)
    #   之前"按轴号数砍轴"是死代码——图里 27竖10横 含参照轴(1/G-1/4~8),
    #   砍掉 = 丢真实轴线。现在全保留, 轴号只做标注映射
    try:
        import re as _re
        def _walk_axis2(container, p_ins, p_rot, p_sx, p_sy):
            for _e in container:
                if _e.dxftype() == 'INSERT':
                    if 'AVE_' in _e.dxf.name: continue
                    _r2 = _e.dxf.rotation if _e.dxf.hasattr('rotation') else 0
                    _s2 = _e.dxf.xscale if _e.dxf.hasattr('xscale') else 1
                    _t2 = _e.dxf.yscale if _e.dxf.hasattr('yscale') else 1
                    _i2 = xform((_e.dxf.insert.x, _e.dxf.insert.y), p_ins, p_rot, p_sx, p_sy)
                    if '_AXISO' in _e.dxf.name:
                        try:
                            for _att in _e.attribs:
                                _lab = _att.dxf.text.strip()
                                if _lab: _axis_items2.append((_lab, _i2[0], _i2[1]))
                        except Exception: pass
                    if _e.dxf.name in doc.blocks:
                        _walk_axis2(doc.blocks[_e.dxf.name], _i2, p_rot + _r2, _s2 * p_sx, _t2 * p_sy)
        for _e in doc.modelspace():
            if _e.dxftype() == 'INSERT' and 'AVE_' not in _e.dxf.name:
                if _e.dxf.name in doc.blocks:
                    _r2 = _e.dxf.rotation if _e.dxf.hasattr('rotation') else 0
                    _s2 = _e.dxf.xscale if _e.dxf.hasattr('xscale') else 1
                    _t2 = _e.dxf.yscale if _e.dxf.hasattr('yscale') else 1
                    _walk_axis2(doc.blocks[_e.dxf.name], (_e.dxf.insert.x, _e.dxf.insert.y), _r2, _s2, _t2)
        # 全保留, 仅统计轴号数供日志
        _n_vert = len({_lab for _lab, _, _ in _axis_items2 if _re.fullmatch(r'G-[0-9]+', _lab)})
        _n_horiz = len({_lab for _lab, _, _ in _axis_items2 if _re.fullmatch(r'G-[A-Z]{1,2}', _lab)})
        print('[AUTO] 轴号: 竖 G-N %d / 横 G-A %d (轴线全保留 %dx%d)' % (
            _n_vert, _n_horiz, len(gx), len(gy)))
    except Exception as _e:
        print('[WARN] 轴号统计失败: %s' % str(_e)[:80])
    ox, oy = min(gx), min(gy)
    # 夹层过滤仍需余数基准(第 7 段用)
    _mxr = _mode_rem([p[0] for p in cols_rot], _pitch_x)
    _myr = _mode_rem([p[1] for p in cols_rot], _pitch_y)
    print('[AUTO] 用线定位轴网(保留无柱轴)')
else:
    # 柱反推(旧逻辑)
    _mxr = _mode_rem([p[0] for p in cols_rot], _pitch_x)
    _myr = _mode_rem([p[1] for p in cols_rot], _pitch_y)
    _grid_cols = [(wx, wy) for (wx, wy) in cols_rot
                  if min(abs((wx % _pitch_x) - _mxr), _pitch_x - abs((wx % _pitch_x) - _mxr)) < 300
                  and min(abs((wy % _pitch_y) - _myr), _pitch_y - abs((wy % _pitch_y) - _myr)) < 300]
    gx = cluster(sorted(p[0] for p in _grid_cols), 100.0)
    gy = cluster(sorted(p[1] for p in _grid_cols), 100.0)
    ox, oy = min(gx), min(gy)
    print('[AUTO] 用柱反推轴网(线定位不可用)')


def _map_source_axis_labels(axis_values, axis_index, pattern):
    """Map real G-* labels to detected axes without inventing numbers.

    A drawing may contain repeated/secondary grid labels.  Choose the
    contiguous label run with the strongest support, rather than assigning a
    label from the sorted axis index (which made an unlabeled extension axis
    look like G-1 in this drawing).
    """
    by_label = {}
    display = {}
    for raw_label, x, y in _axis_items2:
        match = pattern.fullmatch(str(raw_label).strip().upper())
        if not match:
            continue
        point = rot_pt((float(x), float(y)))
        coord = point[axis_index]
        index = min(range(len(axis_values)),
                    key=lambda i: abs(float(axis_values[i]) - coord))
        distance = abs(float(axis_values[index]) - coord)
        if distance > 2000.0:
            continue
        suffix = str(match.group(1))
        ordinal = int(suffix) if suffix.isdigit() else ord(suffix[0]) - 65
        by_label.setdefault(ordinal, Counter())[index] += 1
        display.setdefault(ordinal, suffix)
    if not by_label or not axis_values:
        return [None] * len(axis_values)

    min_ordinal = min(by_label)
    best_base = 0
    best_score = None
    for base in range(len(axis_values)):
        score = 0
        covered = 0
        for ordinal, counts in by_label.items():
            index = base + ordinal - min_ordinal
            if 0 <= index < len(axis_values):
                score += counts.get(index, 0)
                covered += counts.get(index, 0) > 0
        # Prefer the strongest contiguous run; the lower base is only a
        # deterministic tie-breaker when repeated reference labels match.
        rank = (score, covered, -base)
        if best_score is None or rank > best_score:
            best_score, best_base = rank, base

    labels = [None] * len(axis_values)
    for ordinal, counts in by_label.items():
        index = best_base + ordinal - min_ordinal
        if 0 <= index < len(axis_values) and counts.get(index, 0) > 0:
            labels[index] = display[ordinal]
    return labels


_x_source_labels = _map_source_axis_labels(
    gx, 0, re.compile(r'G-(\d+)', re.I))
_y_source_labels = _map_source_axis_labels(
    gy, 1, re.compile(r'G-([A-Z])', re.I))
print('[AUTO] 真实轴号映射: 竖 %d/%d | 横 %d/%d (未标注轴不伪造编号)' % (
    sum(label is not None for label in _x_source_labels), len(gx),
    sum(label is not None for label in _y_source_labels), len(gy)))

# 坐标原点优先锚定图纸真实的 1/A 轴。仅取 min(gx)/min(gy) 会把未标号的
# 延长/参照轴误当成 1/A；本图正好会造成 19.5m 的整体 X 偏移。
_origin_mode = 'minimum_axis_fallback'
if '1' in _x_source_labels:
    ox = gx[_x_source_labels.index('1')]
    _origin_mode = 'labeled_1_A' if 'A' in _y_source_labels else 'labeled_1'
if 'A' in _y_source_labels:
    oy = gy[_y_source_labels.index('A')]
    if _origin_mode == 'minimum_axis_fallback':
        _origin_mode = 'labeled_A'
print('[AUTO] 坐标原点: %s (%.1f, %.1f)' % (_origin_mode, ox, oy))

# ============ 7. 输出 ============
n_ins = len(ins_list)
cols = []
for i, (wx, wy) in enumerate(cols_rot):
    if not (min(p[0] for p in cols_rot) - 5000 < wx < max(p[0] for p in cols_rot) + 5000
            and min(p[1] for p in cols_rot) - 5000 < wy < max(p[1] for p in cols_rot) + 5000):
        continue
    if i < n_ins:
        _dxr = abs((wx % _pitch_x) - _mxr)
        _dyr = abs((wy % _pitch_y) - _myr)
        # 当前实体已经来自自动选中的专用柱块，并且位于 S-COLU 语义图层。
        # 主区轴网只用于定位和置信度，不能继续充当删除条件：同一墙柱块可能
        # 包含第二套轴网/扩展区，按最近主轴距离过滤会静默丢掉真实柱。
        size = ins_sizes[i] or [0.8, 0.8]
        profile = ins_profiles[i]
    else:
        size = direct_sizes[i - n_ins]
        profile = None
    best = min(((gxv, gyv) for gxv in gx for gyv in gy),
               key=lambda g: (g[0] - wx) ** 2 + (g[1] - wy) ** 2)
    d = math.hypot(best[0] - wx, best[1] - wy)
    rx, ry = (wx - ox) / 1000, (wy - oy) / 1000
    _best_x_index = gx.index(best[0])
    _best_y_index = gy.index(best[1])
    _x_label = _x_source_labels[_best_x_index]
    _y_label = _y_source_labels[_best_y_index]
    _col = {'center': [round(rx, 3), round(ry, 3)],
            'size': size,
            'orient': 'WxH' if size[0] >= size[1] else 'HxW',
            'dist_to_grid_mm': round(d, 1),
            'grid_ref': '%s-%s' % (_x_label, _y_label)
                        if d < 5000 and _x_label and _y_label else None,
            'grid_ref_source': 'DXF_AXIS_LABEL'
                               if _x_label and _y_label else None,
            'elev_base_m': base_m, 'elev_top_m': top_m}
    if i in _mark_by_col_index:
        _mark, _md = _mark_by_col_index[i]
        _col['mark'] = _mark
        _col['mark_distance_mm'] = round(_md, 1)
    if profile:
        _col['profile'] = profile   # 异形截面(L/T/圆)——json2rvt 用 DirectShape 建真实轮廓
    cols.append(_col)

# 无柱编号不能证明整排柱是图框：酒店 B1 的主轴网柱也可能没有文字标注。
# 曾按“连续稠密且未标注”删除整排，结果把主区域 182 根真实柱清掉。图框应在
# 上游按图层/图幅识别；柱提取阶段只保留轴网邻近性筛选，不再凭空删除构件。

# 型号只采用原图邻近文字，不做同轴、对称或成组传播。目标是忠实复刻原图，
# 图纸未标注就明确输出 UNRESOLVED，由质量报告提示，不能替图纸推断。
_explicit = [c for c in cols if c.get('mark')]
for _c in cols:
    if _c.get('mark'):
        _c['mark_source'] = 'drawing_text'

_unresolved_n = sum(1 for _c in cols if not _c.get('mark'))
print('[AUTO] 柱型号: 图纸直接标注 %d / 图纸未标注 %d / 完整率 %.1f%%' % (
    len(_explicit), _unresolved_n,
    100.0 * (len(cols) - _unresolved_n) / max(1, len(cols))))


grid_out = {
    'x_axes': [{'coord': round((v - ox) / 1000, 2),
                'label': _x_source_labels[i],
                'source_label': _x_source_labels[i]}
               for i, v in enumerate(gx)],
    'y_axes': [{'coord': round((v - oy) / 1000, 2),
                'label': _y_source_labels[i],
                'source_label': _y_source_labels[i]}
               for i, v in enumerate(gy)],
    'grid_prefix': 'G' if any(_x_source_labels) or any(_y_source_labels) else None,
    'origin_mm': [ox, oy],
    'meta': {'rot_deg': float(round(rot, 2)), 'gcx': float(gcx), 'gcy': float(gcy),
             'pitch_mm': [float(_pitch_x), float(_pitch_y)],
             'origin_mode': _origin_mode},
}

name = re.sub(r'[^\w-]', '_', col_block)[:40]
name = re.sub(r'_+', '_', name).strip('_') or 'cols'
os.makedirs(OUT, exist_ok=True)
json.dump(grid_out, open(os.path.join(OUT, '%s_grid.json' % name), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
json.dump(cols, open(os.path.join(OUT, '%s_columns.json' % name), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('[OK] 柱 %d 个 | 轴网 %dx%d | 标高 %.1f~%.1f | 输出 %s' % (len(cols), len(gx), len(gy), base_m, top_m, name))

# ============ 8. 坑位自查 + 画像入库(可成长闭环) ============
if _kb is not None:
    try:
        report = {
            '柱数': len(cols),
            '柱尺寸': [c['size'] for c in cols],
            'grid_ref': [c.get('grid_ref') for c in cols],
            '轴网': '%dx%d' % (len(gx), len(gy)),
            '扶正角': rot,
            '柱块': col_block,
            '标高': [base_m, top_m],
        }
        issues = _kb.check_pitfalls(report)
        if issues:
            print('[KB] 坑位自查 %d 项: %s' % (len(issues), '; '.join(issues)))
        else:
            print('[KB] 坑位自查: 全部通过')
        # 画像入库(同设计院下次直接命中)
        profile = {
            'id': name,
            '名称': os.path.basename(DXF),
            '图层映射': {
                '柱': col_block.split('$')[-1],
                '轴网': 'S-ANNO-DOTE/A-GRID(自适应)',
            },
            '参数': {
                '轴距mm': [int(_pitch_x), int(_pitch_y)],
                '扶正角': round(rot, 2),
                '柱矩形边长范围': [150, 2500],
            },
            '解析结果': {
                '轴网': '%dx%d' % (len(gx), len(gy)),
                '柱': len(cols),
                '标高': '%s~%s' % (base_m, top_m),
            },
        }
        _kb.save_profile(profile)
        print('[KB] 画像已入库: %s' % name)
    except Exception as _e:
        print('[KB] 自查/入库失败: %s' % str(_e)[:80])
