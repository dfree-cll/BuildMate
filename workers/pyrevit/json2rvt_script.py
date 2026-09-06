# -*- coding: utf-8 -*-
"""JSON -> Revit model builder (pyRevit script, Revit 2020)

主副本在项目内：workers/pyrevit/json2rvt_script.py，经 scripts/sync_revit_ext.py
同步到 pyRevit 扩展目录后在 Revit 内执行。

Reads BIM_JSON_IN/model.json, creates walls/floors/beams/columns + views in
the active Revit project, and saves a copy to REVIT_OUTPUT_DIR.

建模质量修复：
1. 墙厚：按 IR thickness 匹配/复制 WallType 并写 Width，替代盲取默认厚度；
2. 楼板标高：outline Z 取构件 level 对应标高，删除 Z=0 硬编码；
3. 多层绑定：create_levels 返回 name→Level 映射，构件按 IR level 名绑定；
4. 吸附容差：轴线吸附偏移 >500mm 不吸附并记 notes；
5. Slab：映射为 Floor 创建，不再记 unknown；
6. join 容差 0.5ft 收紧至 50mm，join 失败明细进 notes；
7. validate 全量墙位置比对（上限 20），DirectShape 单独计数并加 DS_ 命名。
验收修复：
8. create_wall 改用 Wall.Create(doc, CurveLoop, wallTypeId, levelId, structural=False)
   重载——v3 的 6 参 Line 签名在 IronPython 被误解析为 curves 重载
   （expected IList[Curve], got Line），导致全量墙失败；
9. 零长度墙在 Line.CreateBound 之前跳过（CreateBound 对退化线直接抛异常）；
10. 中文视图名前缀改 \\u 转义（pyRevit 按非 UTF-8 读脚本，字面中文会乱码）；
11. validate 的 DirectShape 计数改 OfClass(DirectShape)，Category.Id 用 int() 比较。
"""
import clr, hashlib, json, math, os, sys, time, traceback
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
from Autodesk.Revit.DB import *
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.UI import TaskDialog
from System.Collections.Generic import List

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from wall_model_contract import normalize_wall_model_payload

MM = 304.8  # mm per foot
SNAP_TOL_MM = 500.0   # 轴线吸附容差（超过不吸附）
JOIN_TOL_FT = 50.0 / MM  # 墙 join 容差（50mm）

def _exchange_dir(env_name, relative_dir):
    """Resolve a project exchange directory from the Revit process environment."""
    configured = os.environ.get(env_name, "")
    if configured:
        return configured
    project_root = os.environ.get("BUILDMATE_PROJECT_ROOT", "")
    if project_root:
        candidate = os.path.join(project_root, relative_dir)
        if os.path.isdir(candidate):
            return candidate
    return ""


# 历史 Windows 部署已约定此交换目录。保留环境变量优先级，避免 Revit 在
# 设置变量前已启动时，Routes 调用静默返回而没有任何可追溯结果。
JSON_DIR = _exchange_dir("BIM_JSON_IN", os.path.join("data", "runtime", "revit", "json_in"))
RVT_OUT = _exchange_dir("REVIT_OUTPUT_DIR", os.path.join("data", "runtime", "revit", "rvt_out"))
WALL_MODEL_IN = os.path.join(JSON_DIR, "wall_model.json") if JSON_DIR else ""
LEGACY_MODEL_IN = os.path.join(JSON_DIR, "model.json") if JSON_DIR else ""
JSON_IN = (WALL_MODEL_IN if WALL_MODEL_IN and os.path.isfile(WALL_MODEL_IN)
           else LEGACY_MODEL_IN)

app = __revit__.Application
uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

NOTES = []  # 模块级 notes，create_* 内可记录
TXN = None  # 模块级事务句柄，崩溃时外层兜底回滚
BUILD_ID = "legacy"
CREATED_RECORDS = []
BOUNDARY_COLUMN_IDS = []
BOUNDARY_COLUMN_TARGETS = {}
BOUNDARY_COLUMN_CUTTER_IDS = []
BOUNDARY_COLUMN_CUTTER_TARGETS = {}
BOUNDARY_COLUMN_CUTTER_COLUMNS = {}
BOUNDARY_COLUMN_CUTTER_INPUTS = {}
AUTO_MARKER_PREFIX = "BUILDMATE_AUTO:"
PROJECT_ID = ""
FLOOR_CODE = ""


def note(msg):
    NOTES.append(msg)


def _element_id_value(el):
    try:
        return el.Id.IntegerValue
    except Exception:
        return None


def _auto_marker(project_id, floor_code, build_id, input_id):
    """Marker is deliberately parseable from Revit's built-in Comments field.

    A build may only replace objects with the same project and floor.  Old
    markers remain readable but are never selected by a scoped cleanup.
    """
    return "%sP=%s;F=%s;B=%s;I=%s" % (
        AUTO_MARKER_PREFIX, project_id, floor_code, build_id, input_id)


def mark_auto(el, input_id="", kind="", mode="native", scope_floor=None):
    """Mark and record an auto element with its project/floor ownership."""
    marker = _auto_marker(PROJECT_ID, scope_floor or FLOOR_CODE, BUILD_ID,
                          input_id or kind)
    try:
        p = el.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p is not None and not p.IsReadOnly:
            p.Set(marker)
    except Exception:
        pass
    CREATED_RECORDS.append({
        "input_id": input_id or "",
        "kind": kind or "",
        "revit_element_id": _element_id_value(el),
        "mode": mode,
        "project_id": PROJECT_ID,
        "floor_code": scope_floor or FLOOR_CODE,
    })
    return el


def is_buildmate_auto(el, project_id=None, floor_code=None):
    try:
        p = el.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        value = p.AsString() if p is not None else ""
        if not value or not value.startswith(AUTO_MARKER_PREFIX):
            return False
        if project_id is None and floor_code is None:
            return True
        # Legacy markers have no project/floor and must never be deleted by a
        # project-scoped update: they require a deliberate one-time migration.
        return ("P=%s;" % project_id in value and
                "F=%s;" % floor_code in value)
    except Exception:
        return False


def is_buildmate_boundary_family_instance(element):
    try:
        return Element.Name.GetValue(element.Symbol.Family).startswith(
            "BM_Boundary_")
    except Exception:
        return False


def first_of(collector):
    for e in collector:
        return e
    return None


def get_level(doc, name):
    coll = FilteredElementCollector(doc).OfClass(Level)
    for lvl in coll:
        if lvl.Name == name:
            return lvl
    return first_of(coll)


def resolve_level(doc, level_map, e, default):
    """按 IR level 名解析标高；找不到回退默认"""
    name = e.get("level")
    if name:
        lv = get_level(doc, name)
        if lv is not None:
            return lv
        lv = level_map.get(name)
        if lv is not None:
            return lv
    return default


def xyz(mm3):
    return XYZ(mm3[0] / MM, mm3[1] / MM, mm3[2] / MM)


_WALL_TYPE_CACHE = {}


def _dbg(msg):
    """仅在显式配置日志文件时记录墙诊断。"""
    import io
    path = os.environ.get("BIM_REVIT_DEBUG_LOG", "")
    if path:
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def _is_basic_wall(wt):
    """只认基本墙系统族——幕墙/叠层墙也有 Width 属性但语义不同(25mm 竖挺等),
    参与匹配会把厚墙错换成幕墙(实测 CW 102-50-140p/幕墙 被选中)"""
    try:
        return wt.FamilyName in ("Basic Wall", "基本墙")
    except Exception:
        return False


def _set_wall_type_width(doc, wall_type, target_mm):
    """通过 CompoundStructure 调整基本墙总厚度，WallType.Width 本身只读。"""
    try:
        cs = wall_type.GetCompoundStructure()
        if cs is None or cs.LayerCount <= 0:
            return False
        widths = [cs.GetLayerWidth(i) for i in range(cs.LayerCount)]
        # 调整最厚实体层，保留饰面层和其他层的相对构造。
        index = max(range(len(widths)), key=lambda i: widths[i])
        target_ft = float(target_mm) / MM
        new_width = widths[index] + target_ft - sum(widths)
        if new_width <= 0.001:
            return False
        cs.SetLayerWidth(index, new_width)
        wall_type.SetCompoundStructure(cs)
        doc.Regenerate()
        return abs(wall_type.Width * MM - float(target_mm)) < 5.0
    except Exception as ex:
        _dbg("  compound width set err: %s" % ex)
        return False


def get_wall_type(doc, thickness_mm):
    """按厚度匹配 WallType：先找现有类型，找不到复制一份改 Width。
    返回 (wall_type, how)，how ∈ matched/copied/fallback/default/none
    ★ 修复: 一律用 wt.Width 属性(类型宽度, 英尺 double)判定/设置——
    get_Parameter(WALL_ATTR_WIDTH_PARAM) 在部分版本取不到(p=None)导致
    matched 误跳 + Duplicate 后 Set 落空, 墙厚静默丢失(dump 实测全 200mm)"""
    if thickness_mm is None:
        _basic = [wt for wt in FilteredElementCollector(doc).OfClass(WallType) if _is_basic_wall(wt)]
        return (_basic[0] if _basic else first_of(FilteredElementCollector(doc).OfClass(WallType))), "default"
    key = round(float(thickness_mm))
    if key in _WALL_TYPE_CACHE:
        _dbg("  cached key=%s width=%s" % (key, round(_WALL_TYPE_CACHE[key].Width * MM)))
        return _WALL_TYPE_CACHE[key], "cached"
    coll = [wt for wt in FilteredElementCollector(doc).OfClass(WallType) if _is_basic_wall(wt)]
    # ★ 教训: 勿在此处遍历 wt.Name——部分墙类型(幕墙系统等)在 IronPython 下访问 .Name
    # 抛 RPC "Name" 异常, 曾一行诊断杀死全部 27 根墙(v10 轮 walls expected 27 actual 0)
    _dbg("get_wall_type key=%s basic_count=%d" % (key, len(coll)))
    for wt in coll:  # 1) 现有基本墙类型中找 Width 匹配(用属性, 不用参数)
        try:
            if abs(wt.Width * MM - key) < 5.0:
                _dbg("  matched %s width=%s" % (
                    _safe_type_label(wt), round(wt.Width * MM)))
                _WALL_TYPE_CACHE[key] = wt
                return wt, "matched"
        except Exception as _e:
            _dbg("  probe err %s: %s" % (_safe_type_label(wt), _e))
            continue
    if not coll:
        _all = list(FilteredElementCollector(doc).OfClass(WallType))
        _dbg("  NO basic walls, fallback all[0]")
        return (_all[0] if _all else None), "fallback"
    target_name = "BM_Wall_%dmm" % key
    # 重复运行时项目中可能已有早期创建但厚度错误的 BM 类型。先修复并复用，
    # 避免 Duplicate 因同名冲突而退回默认墙型。
    for existing in coll:
        if _safe_type_label(existing) == target_name:
            if _set_wall_type_width(doc, existing, key):
                _WALL_TYPE_CACHE[key] = existing
                return existing, "repaired"
            break
    try:  # 2) 复制第一个基本墙类型, 双路设宽(属性+参数)
        dup = coll[0].Duplicate(target_name)
        _w_set = _set_wall_type_width(doc, dup, key)
        _dbg("  duplicated BM_Wall_%d width_now=%s set_ok=%s" % (key, round(dup.Width * MM), _w_set))
        _WALL_TYPE_CACHE[key] = dup
        if not _w_set:
            return coll[0], "fallback"
        return dup, "copied"
    except Exception as _e:
        _dbg("  duplicate err: %s -> fallback %s" % (
            _e, _safe_type_label(coll[0])))
        _WALL_TYPE_CACHE[key] = coll[0]
        return coll[0], "fallback"


def find_type_by_name(doc, cls, name):
    """按 IR type_name/Reference 匹配类型；找不到返回 None"""
    if not name:
        return None
    for t in FilteredElementCollector(doc).OfClass(cls):
        try:
            # Revit 2020/Python.NET 下 FamilySymbol、WallType 的实例 .Name
            # 偶发抛 RPC "Name"；静态 API 可稳定读取，也能避免重复创建同名类型。
            if Element.Name.GetValue(t) == name:
                return t
        except Exception:
            continue
    return None


def create_wall(doc, e, level):
    _dbg("create_wall enter id=%s thickness=%s" % (e.get("id"), e.get("thickness")))
    ref = (e.get("properties") or {}).get("Reference") or e.get("type_name")
    _dbg("  ref=%s" % ref)
    wall_type = find_type_by_name(doc, WallType, ref)
    _dbg("  find_type_by_name -> %s" % (
        _safe_type_label(wall_type) if wall_type is not None else "None"))
    how = "named" if wall_type else None
    if wall_type is None:
        wall_type, how = get_wall_type(doc, e.get("thickness"))
    if wall_type is None:
        _dbg("  exit: no WallType id=%s" % e.get("id"))
        return "no WallType"
    if _is_degenerate_wall(e):
        _dbg("  exit: degenerate id=%s" % e.get("id"))
        return "skip degenerate wall"
    source_start = e["start"]
    source_end = e["end"]
    # Wall point Z is local to its assigned level in model.json. Revit curves
    # require project-absolute Z, otherwise a B1 wall is created at elevation 0.
    level_z_mm = level.Elevation * MM
    line = Line.CreateBound(
        xyz([source_start[0], source_start[1], level_z_mm + source_start[2]]),
        xyz([source_end[0], source_end[1], level_z_mm + source_end[2]]),
    )
    if line.Length < 0.02:  # skip degenerate wall (zero-length -> revit errors)
        return "skip degenerate wall"
    # 注意：Wall.Create(doc, line, ...) 6 参签名在 IronPython 会被误解析为
    # curves 重载（"expected IList[Curve], got Line"）。改用 CurveLoop 重载。
    cl = CurveLoop()
    cl.Append(line)
    h = e.get("height", 3000)
    # Revit 2020/IronPython 对 Wall.Create 的带类型重载解析不稳定；先用稳定的
    # 4 参数重载创建，再显式 ChangeTypeId，避免静默退回默认墙厚。
    wall = Wall.Create(doc, line, level.Id, False)
    # Revit joins new wall ends automatically during regeneration.  Disable
    # both ends immediately so the source centre-line remains unchanged.
    WallUtils.DisallowWallJoinAtEnd(wall, 0)
    WallUtils.DisallowWallJoinAtEnd(wall, 1)
    wall.Location.Curve = line
    if wall.GetTypeId() != wall_type.Id:
        wall.ChangeTypeId(wall_type.Id)
    wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM).Set(h / MM)
    # ★ 建后硬校验纠偏: 不管上面哪条路径出错, 最终以实例类型 Width 为准——
    # 不等于输入厚度就强制换到正确类型(实例 ChangeTypeId 可行), 墙厚不再可能静默丢失
    try:
        _in = round(float(e.get("thickness") or 0))
        _got = round(wall.WallType.Width * MM)
        if _in and abs(_got - _in) > 5:
            wt2, _h2 = get_wall_type(doc, _in)
            if wt2 is not None:
                wall.ChangeTypeId(wt2.Id)
    except Exception as _ex:
        note("wall fixup err %s: %s" % (e.get("id", "?"), _ex))
    if how in ("copied", "fallback"):
        note("wall %s thickness=%s type=%s" % (e.get("id", "?"), e.get("thickness"), how))
    _dbg("  exit OK id=%s type=%s width=%s" % (
        e.get("id"), _safe_type_label(wall.WallType),
        round(wall.WallType.Width * MM)))
    mark_auto(wall, e.get("id", ""), "Wall", "native")
    return wall.Id


def wall_line_at_level(e, level):
    """Convert a wall's level-local JSON points to an absolute Revit line."""
    level_z_mm = level.Elevation * MM
    start = list(e["start"])
    end = list(e["end"])
    start[2] += level_z_mm
    end[2] += level_z_mm
    return Line.CreateBound(xyz(start), xyz(end))


def create_floor(doc, e, level):
    ref = (e.get("properties") or {}).get("Reference") or e.get("type_name")
    floor_type = find_type_by_name(doc, FloorType, ref) or \
        first_of(FilteredElementCollector(doc).OfClass(FloorType))
    if floor_type is None:
        return "no FloorType"
    z = level.Elevation  # ★ 楼板标高取 level 标高（v3 修复 Z=0 硬编码）
    pts = [XYZ(p[0] / MM, p[1] / MM, z) for p in e["outline"]]
    loop = CurveLoop()
    for i in range(len(pts)):
        loop.Append(Line.CreateBound(pts[i], pts[(i + 1) % len(pts)]))
    loops = List[CurveLoop]()
    loops.Add(loop)
    floor = Floor.Create(doc, loops, floor_type.Id, level.Id)
    t = e.get("thickness", 150)
    floor.get_Parameter(BuiltInParameter.FLOOR_ATTR_THICKNESS_PARAM).Set(t / MM)
    mark_auto(floor, e.get("id", ""), e.get("type", "Floor"), "native")
    return None


def find_family_symbol_for_file(doc, family_path, type_name=None):
    """只在指定 RFA 对应的族内查符号，避免结构柱类别内误选钢柱族。"""
    family_stem = os.path.splitext(os.path.basename(family_path or ""))[0].strip().lower()
    if not family_stem:
        return None
    fallback = None
    for sym in FilteredElementCollector(doc).OfClass(FamilySymbol):
        try:
            fam_name = Element.Name.GetValue(sym.Family).strip().lower()
            if fam_name != family_stem:
                continue
            if fallback is None:
                fallback = sym
            if type_name and Element.Name.GetValue(sym) == type_name:
                return sym
        except Exception:
            continue
    return None if type_name else fallback


def _safe_type_label(element_type):
    """Revit 2020 某些系统类型访问 .Name 会抛 RPC Name 异常。"""
    try:
        return Element.Name.GetValue(element_type)
    except Exception:
        try:
            return str(element_type.Id.IntegerValue)
        except Exception:
            return "?"


def _set_symbol_dimension(sym, names, value_mm):
    """设置族类型尺寸；兼容中英文族参数名。"""
    for name in names:
        try:
            p = sym.LookupParameter(name)
            if p is not None and not p.IsReadOnly:
                p.Set(float(value_mm) / MM)
                return True
        except Exception:
            continue
    return False


def resolve_sized_family_symbol(doc, e, cat_id, width_key, depth_key):
    """按 type_name 查找；不存在时加载 RFA、复制类型并写入截面尺寸。"""
    type_name = e.get("type_name")
    family_path = e.get("family_path") or ""
    # type_name 在不同族内允许重名；必须同时匹配指定 RFA，不能命中历史钢柱类型。
    sym = find_family_symbol_for_file(doc, family_path, type_name)
    if sym is None:
        if not family_path or not os.path.isfile(family_path):
            return None, "family file missing: " + family_path
        try:
            # Revit 2020 的 Python.NET 对 LoadFamily(path) 只返回 bool；
            # Family 本身是 out 参数，必须显式用 clr.Reference 获取。
            family_ref = clr.Reference[Family]()
            loaded = doc.LoadFamily(family_path, family_ref)
            family = family_ref.Value if loaded else None
            base = None
            if family is not None:
                for sid in family.GetFamilySymbolIds():
                    base = doc.GetElement(sid)
                    break
            # 族已加载时 LoadFamily 返回 False 且 out Family 为空；必须按 RFA 族名
            # 找母型。旧逻辑按类别随便取首个结构柱，可能取到钢柱族。
            if base is None:
                base = find_family_symbol_for_file(doc, family_path)
            if base is None:
                return None, "loaded family has no symbol"
            existing = find_family_symbol_for_file(doc, family_path, type_name)
            sym = existing or base.Duplicate(type_name)
        except Exception as ex:
            return None, "family load/duplicate failed: " + str(ex)[:100]
    try:
        if sym.Category is None or sym.Category.Id.IntegerValue != int(cat_id):
            return None, "family category mismatch"
    except Exception:
        return None, "family category unavailable"
    width_ok = _set_symbol_dimension(sym, ("b", "B", "宽度", "Width"), e.get(width_key, 0))
    depth_ok = _set_symbol_dimension(sym, ("h", "H", "高度", "深度", "Height", "Depth"),
                                     e.get(depth_key, 0))
    if not width_ok or not depth_ok:
        return None, "family width/depth parameters not writable"
    try:
        if not sym.IsActive:
            sym.Activate()
            doc.Regenerate()
    except Exception as ex:
        return None, "family activation failed: " + str(ex)[:80]
    return sym, None


def find_family_symbol(doc, cat_id):
    coll = FilteredElementCollector(doc).OfClass(FamilySymbol)
    for sym in coll:
        if sym.Category is not None and sym.Category.Id.IntegerValue == cat_id:
            return sym
    return None


def make_box_shape(doc, p0, p1):
    """DirectShape box between two XYZ corners (mm units already converted)."""
    profile = CurveLoop()
    profile.Append(Line.CreateBound(XYZ(p0.X, p0.Y, p0.Z), XYZ(p1.X, p0.Y, p0.Z)))
    profile.Append(Line.CreateBound(XYZ(p1.X, p0.Y, p0.Z), XYZ(p1.X, p1.Y, p0.Z)))
    profile.Append(Line.CreateBound(XYZ(p1.X, p1.Y, p0.Z), XYZ(p0.X, p1.Y, p0.Z)))
    profile.Append(Line.CreateBound(XYZ(p0.X, p1.Y, p0.Z), XYZ(p0.X, p0.Y, p0.Z)))
    solid = GeometryCreationUtilities.CreateExtrusionGeometry(
        [profile], XYZ(0, 0, 1), p1.Z - p0.Z)
    return solid


def _name_ds(ds, name):
    try:
        ds.Name = name
    except Exception:
        pass


def _boundary_family_template():
    candidates = [
        os.path.join(app.FamilyTemplatePath, u"公制常规模型.rft"),
        os.path.join(app.FamilyTemplatePath, "Metric Generic Model.rft"),
        os.path.join(app.FamilyTemplatePath, u"Chinese", u"公制常规模型.rft"),
        os.path.join(app.FamilyTemplatePath, u"English", "Metric Generic Model.rft"),
        os.path.join(app.FamilyTemplatePath, u"公制结构柱.rft"),
        os.path.join(app.FamilyTemplatePath, "Metric Structural Column.rft"),
        os.path.join(app.FamilyTemplatePath, u"Chinese", u"公制结构柱.rft"),
        os.path.join(app.FamilyTemplatePath, u"English", "Metric Structural Column.rft"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _boundary_family_key(e):
    payload = {
        # v3 fixes the FamilyItemFactory.NewExtrusion isSolid polarity and
        # deliberately invalidates the earlier reversed solid/void cache.
        "family_schema": "generic-one-level-v3",
        "profile": (e.get("profile") or {}).get("points") or [],
        "height": round(float(e.get("top", 0)) - float(e.get("base", 0)), 3),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _write_boundary_profile_family(path, template, points, height,
                                   is_void=False):
    """Write one exact-profile solid column or invisible loaded void cutter."""
    family_doc = None
    family_tx = None
    try:
        family_doc = app.NewFamilyDocument(template)
        family_tx = Transaction(
            family_doc, "BuildMate boundary column profile")
        family_tx.Start()
        family_options = family_tx.GetFailureHandlingOptions()
        family_options.SetFailuresPreprocessor(
            _DeleteNonFatalFamilyWarnings())
        family_tx.SetFailureHandlingOptions(family_options)
        category = (BuiltInCategory.OST_GenericModel if is_void else
                    BuiltInCategory.OST_StructuralColumns)
        family_doc.OwnerFamily.FamilyCategory = \
            family_doc.Settings.Categories.get_Item(category)
        if is_void:
            # In Revit 2020 this family setting is exposed on OwnerFamily,
            # not as a FamilyManager type parameter.
            allow_cut = family_doc.OwnerFamily.get_Parameter(
                BuiltInParameter.FAMILY_ALLOW_CUT_WITH_VOIDS)
            if allow_cut is None or allow_cut.IsReadOnly:
                raise ValueError("Cut with Voids When Loaded parameter missing")
            allow_cut.Set(1)
        plane = Plane.CreateByNormalAndOrigin(XYZ.BasisZ, XYZ.Zero)
        sketch = SketchPlane.Create(family_doc, plane)
        curve_array = CurveArray()
        for index in range(len(points)):
            p0 = points[index]
            p1 = points[(index + 1) % len(points)]
            a = XYZ(float(p0[0]) / MM, float(p0[1]) / MM, 0)
            b = XYZ(float(p1[0]) / MM, float(p1[1]) / MM, 0)
            if a.DistanceTo(b) > 1e-9:
                curve_array.Append(Line.CreateBound(a, b))
        profiles = CurveArrArray()
        profiles.Append(curve_array)
        family_doc.FamilyCreate.NewExtrusion(
            not bool(is_void), profiles, sketch, height / MM)
        family_tx.Commit()
        save_options = SaveAsOptions()
        save_options.OverwriteExistingFile = True
        family_doc.SaveAs(path, save_options)
    except Exception:
        try:
            if family_tx is not None and \
                    family_tx.GetStatus() == TransactionStatus.Started:
                family_tx.RollBack()
        except Exception:
            pass
        raise
    finally:
        try:
            if family_doc is not None:
                family_doc.Close(False)
        except Exception:
            pass


def _cluster_axis_values(values, tolerance_mm=5.0):
    indexed = sorted((float(value), index) for index, value in enumerate(values))
    replacements = [0.0] * len(values)
    cluster = []
    for value, index in indexed:
        if cluster and value - cluster[0][0] > tolerance_mm:
            target = sum(item[0] for item in cluster) / len(cluster)
            for _, cluster_index in cluster:
                replacements[cluster_index] = target
            cluster = []
        cluster.append((value, index))
    if cluster:
        target = sum(item[0] for item in cluster) / len(cluster)
        for _, cluster_index in cluster:
            replacements[cluster_index] = target
    return replacements


def _orthogonal_profile_points(points):
    """Collapse sub-5mm drafting noise onto shared X/Y axes."""
    xs = _cluster_axis_values([point[0] for point in points])
    ys = _cluster_axis_values([point[1] for point in points])
    corrected = [[xs[index], ys[index]] for index in range(len(points))]
    for index in range(len(corrected)):
        current = corrected[index]
        following = corrected[(index + 1) % len(corrected)]
        dx = abs(following[0] - current[0])
        dy = abs(following[1] - current[1])
        if dx > 0 and dy > 0:
            if dy <= 5.0:
                following[1] = current[1]
            elif dx <= 5.0:
                following[0] = current[0]
    return corrected


class _DeleteNonFatalFamilyWarnings(IFailuresPreprocessor):
    def PreprocessFailures(self, accessor):
        for message in accessor.GetFailureMessages():
            try:
                if message.GetSeverity() == FailureSeverity.Warning:
                    accessor.DeleteWarning(message)
            except Exception:
                pass
        return FailureProcessingResult.Continue


def prepare_boundary_column_families(elements):
    """Prepare exact-profile loadable families before the project transaction."""
    profile_columns = [e for e in elements
                       if e.get("type") == "Column" and e.get("profile")]
    if not profile_columns:
        return 0, []
    template = _boundary_family_template()
    if not template:
        return 0, ["boundary column family template missing"]
    family_dir = os.path.join(RVT_OUT, "boundary_column_families")
    if not os.path.isdir(family_dir):
        os.makedirs(family_dir)
    generated = 0
    errors = []
    paths = {}
    cutter_paths = {}
    for e in profile_columns:
        key = _boundary_family_key(e)
        path = paths.get(key) or os.path.join(
            family_dir, "BM_Boundary_%s.rfa" % key)
        cutter_path = cutter_paths.get(key) or os.path.join(
            family_dir, "BM_Boundary_Cut_%s.rfa" % key)
        paths[key] = path
        cutter_paths[key] = cutter_path
        e["_profile_family_path"] = path
        e["_profile_cutter_family_path"] = cutter_path
        if os.path.isfile(path) and os.path.isfile(cutter_path):
            continue
        try:
            points = _orthogonal_profile_points(
                (e.get("profile") or {}).get("points") or [])
            height = float(e.get("top", 0)) - float(e.get("base", 0))
            if len(points) < 3 or height <= 0:
                raise ValueError("invalid profile or height")
            if not os.path.isfile(path):
                _write_boundary_profile_family(
                    path, template, points, height, False)
                generated += 1
            if not os.path.isfile(cutter_path):
                _write_boundary_profile_family(
                    cutter_path, template, points, height, True)
                generated += 1
        except Exception as ex:
            errors.append("%s:%s" % (e.get("id", "?"), str(ex)[:120]))
    return generated, errors


def _load_profile_family_symbol(doc, family_path):
    symbol = find_family_symbol_for_file(doc, family_path)
    if symbol is None:
        family_ref = clr.Reference[Family]()
        loaded = doc.LoadFamily(family_path, family_ref)
        family = family_ref.Value if loaded else None
        if family is not None:
            for symbol_id in family.GetFamilySymbolIds():
                symbol = doc.GetElement(symbol_id)
                break
        if symbol is None:
            symbol = find_family_symbol_for_file(doc, family_path)
    if symbol is None:
        raise ValueError("loaded profile family has no symbol")
    if not symbol.IsActive:
        symbol.Activate()
        doc.Regenerate()
    return symbol


def create_beam(doc, e, level):
    sym, sym_err = resolve_sized_family_symbol(
        doc, e, BuiltInCategory.OST_StructuralFraming, "width", "height")
    if sym is not None:
        line = Line.CreateBound(xyz(e["start"]), xyz(e["end"]))
        beam = doc.Create.NewFamilyInstance(line, sym, level, StructuralType.Beam)
        mark_auto(beam, e.get("id", ""), "Beam", "native")
        return None
    return sym_err or "no matching structural beam FamilySymbol/type_name"


def create_column(doc, e, level):
    if e.get("profile"):
        try:
            base_mm = float(e.get("base", level.Elevation * MM))
            top_mm = float(e.get("top", base_mm + 3000.0))
            if top_mm <= base_mm:
                return "profile column top must be above base"
            family_path = e.get("_profile_family_path") or ""
            if not family_path or not os.path.isfile(family_path):
                return "profile column physical family missing"
            symbol = _load_profile_family_symbol(doc, family_path)
            point = XYZ(float(e["x"]) / MM, float(e["y"]) / MM,
                        base_mm / MM)
            column = doc.Create.NewFamilyInstance(
                point, symbol, level, StructuralType.Column)
            try:
                offset = column.get_Parameter(
                    BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)
                if offset is not None and not offset.IsReadOnly:
                    offset.Set(base_mm / MM - level.Elevation)
            except Exception:
                pass
            # Structural-column templates may carry their own reference-level
            # transform.  Parameters can report the requested level while the
            # actual solid is still displaced vertically.  Align from the
            # committed geometry envelope and reject any residual error.
            doc.Regenerate()
            box = column.get_BoundingBox(None)
            if box is None:
                doc.Delete(column.Id)
                return "profile column has no physical bounding box"
            z_delta = base_mm / MM - box.Min.Z
            if abs(z_delta) > 0.1 / MM:
                ElementTransformUtils.MoveElement(
                    doc, column.Id, XYZ(0, 0, z_delta))
                doc.Regenerate()
                box = column.get_BoundingBox(None)
            if box is None or abs(box.Min.Z * MM - base_mm) > 1.0:
                doc.Delete(column.Id)
                return "profile column base geometry misaligned"
            cutter_path = e.get("_profile_cutter_family_path") or ""
            if not cutter_path or not os.path.isfile(cutter_path):
                doc.Delete(column.Id)
                return "profile column void cutter family missing"
            cutter_symbol = _load_profile_family_symbol(doc, cutter_path)
            mark_auto(column, e.get("id", ""), "BoundaryColumn",
                      "profile-family")
            BOUNDARY_COLUMN_IDS.append(column.Id)
            targets = [str(item) for item in e.get("cut_wall_ids") or []]
            BOUNDARY_COLUMN_TARGETS[column.Id.IntegerValue] = targets
            # One loaded-void instance per wall/column pair. Revit may remove
            # an entire multi-wall cutter when one relation is invalid at
            # commit, taking otherwise valid cuts with it.
            for wall_input_id in targets:
                cutter = doc.Create.NewFamilyInstance(
                    column.Location.Point, cutter_symbol, level,
                    StructuralType.NonStructural)
                doc.Regenerate()
                if not InstanceVoidCutUtils.IsVoidInstanceCuttingElement(cutter):
                    doc.Delete(cutter.Id)
                    doc.Delete(column.Id)
                    return "profile column cutter is not a loaded cutting void"
                cutter_value = cutter.Id.IntegerValue
                BOUNDARY_COLUMN_CUTTER_IDS.append(cutter.Id)
                BOUNDARY_COLUMN_CUTTER_TARGETS[cutter_value] = [wall_input_id]
                BOUNDARY_COLUMN_CUTTER_COLUMNS[cutter_value] = column.Id
                BOUNDARY_COLUMN_CUTTER_INPUTS[cutter_value] = e.get("id", "")
            return None
        except Exception as ex:
            return "profile column create failed: %s" % str(ex)[:160]
    sym, sym_err = resolve_sized_family_symbol(
        doc, e, BuiltInCategory.OST_StructuralColumns, "width", "depth")
    if sym is not None and not e.get("profile"):
        pt = XYZ(e["x"] / MM, e["y"] / MM, e.get("base", 0) / MM)
        col = doc.Create.NewFamilyInstance(pt, sym, level, StructuralType.Column)
        rotation_deg = float(e.get("rotation_deg") or 0.0)
        if abs(rotation_deg) > 1e-6:
            axis = Line.CreateBound(pt, XYZ(pt.X, pt.Y, pt.Z + 1.0))
            ElementTransformUtils.RotateElement(
                doc, col.Id, axis, math.radians(rotation_deg))
        top = e.get("top", 3000)
        col.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM).Set(top / MM)
        mark_auto(col, e.get("id", ""), "Column", "native")
        return None
    return sym_err or "no matching structural column FamilySymbol/type_name"


def _profile_solid(profile, cx, cy, z0, z1):
    """异形截面拉伸体: poly=折线轮廓(相对柱心 mm), circle=圆(32 段折线近似)"""
    import math as _m
    kind = profile.get("kind")
    if kind == "circle":
        r = profile["r"] / MM
        pts = [XYZ(cx + r * _m.cos(2 * _m.pi * i / 32.0), cy + r * _m.sin(2 * _m.pi * i / 32.0), z0)
               for i in range(32)]
    else:
        pts = [XYZ(cx + px / MM, cy + py / MM, z0) for px, py in profile["points"]]
    loop = CurveLoop()
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        if a.DistanceTo(b) > 1e-9:
            loop.Append(Line.CreateBound(a, b))
    return GeometryCreationUtilities.CreateExtrusionGeometry([loop], XYZ.BasisZ, z1 - z0)


def create_views(doc, views_json, level):
    created = []
    used_names = set()
    for v in views_json:
        try:
            vt = v.get("view_type")
            vname = v.get("view_name", "View")
            if vname in used_names:
                vname = vname + "_" + str(len(created))
            if vt == "ThreeD":
                vft = None
                for vft_item in FilteredElementCollector(doc).OfClass(ViewFamilyType):
                    if vft_item.ViewFamily == ViewFamily.ThreeDimensional:
                        vft = vft_item
                        break
                if vft is None:
                    created.append("3D:no type")
                    continue
                # delete same-name 3d view from previous run (avoid dup name)
                for old in FilteredElementCollector(doc).OfClass(View3D):
                    try:
                        if (old.Name or "").strip() == vname.strip():
                            doc.Delete(old.Id)
                    except Exception:
                        pass
                v3d = View3D.CreateIsometric(doc, vft.Id)
                v3d.Name = vname
                try:
                    v3d.DisplayStyle = DisplayStyle.ShadingWithEdges
                except Exception:
                    pass
                used_names.add(vname)
                created.append(v3d.Name)
            elif vt == "FloorPlan":
                vft = None
                for vft_item in FilteredElementCollector(doc).OfClass(ViewFamilyType):
                    if vft_item.ViewFamily == ViewFamily.FloorPlan:
                        vft = vft_item
                        break
                if vft is None:
                    created.append("Plan:no type")
                    continue
                plan = ViewPlan.Create(doc, vft.Id, level.Id)
                plan.Name = vname
                used_names.add(vname)
                created.append(plan.Name)
        except Exception as ex:
            created.append(v.get("view_id", "?") + ":err " + str(ex)[:50])
    return created


def _view_by_name(doc, name):
    for view in FilteredElementCollector(doc).OfClass(View):
        try:
            if view.Name == name:
                return view
        except Exception:
            continue
    return None


def _floor_plan_type(doc):
    for item in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if item.ViewFamily == ViewFamily.FloorPlan:
            return item
    return None


def _apply_named_template(doc, view, template_name):
    template = _view_by_name(doc, template_name)
    try:
        if template is not None and template.IsTemplate:
            view.ViewTemplateId = template.Id
            return True
    except Exception:
        pass
    return False


def ensure_standard_views(doc, level, floor_code):
    """Create floor views once and reuse the template-owned names thereafter."""
    created = []
    view_type = _floor_plan_type(doc)
    if view_type is None:
        return ["standard views:no floor plan type"]
    # Revit 2020 的 Routes/IronPython 执行器会把 unicode escape 当作非法视图名；
    # 用 ASCII 保证批量建模稳定，语义由固定前缀表达。
    specs = (("AR-%s-PLAN" % floor_code, "VT-AR-PLAN"),
             ("ST-%s-PLAN" % floor_code, "VT-ST-PLAN"),
             ("QA-%s-CHECK" % floor_code, "VT-QA-CHECK"))
    for name, template in specs:
        view = _view_by_name(doc, name)
        if view is None:
            view = ViewPlan.Create(doc, view_type.Id, level.Id)
            view.Name = name
            created.append(name)
        _apply_named_template(doc, view, template)
    overview = "3D-AST-OVERVIEW"
    if _view_by_name(doc, overview) is None:
        for item in FilteredElementCollector(doc).OfClass(ViewFamilyType):
            if item.ViewFamily == ViewFamily.ThreeDimensional:
                view3d = View3D.CreateIsometric(doc, item.Id)
                view3d.Name = overview
                _apply_named_template(doc, view3d, "VT-3D-OVERVIEW")
                created.append(overview)
                break
    return created


def export_actual_plan_view(doc, level, output_dir, build_id):
    """Export a real Revit plan view for independent source/model audit."""
    candidates = []
    try:
        active = doc.ActiveView
        if (active is not None and not active.IsTemplate and
                isinstance(active, ViewPlan)):
            candidates.append(active)
    except Exception:
        pass
    try:
        for view in FilteredElementCollector(doc).OfClass(ViewPlan):
            if view.IsTemplate:
                continue
            if level is not None and view.GenLevel is not None and \
                    view.GenLevel.Id != level.Id:
                continue
            if all(existing.Id != view.Id for existing in candidates):
                candidates.append(view)
    except Exception:
        pass
    if not candidates:
        return None, "no non-template plan view available for actual-view audit"
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    prefix_name = "build_%s_actual_view" % build_id
    prefix = os.path.join(output_dir, prefix_name)
    try:
        options = ImageExportOptions()
        options.ExportRange = ExportRange.SetOfViews
        view_ids = List[ElementId]()
        view_ids.Add(candidates[0].Id)
        options.SetViewsAndSheets(view_ids)
        options.FilePath = prefix
        options.HLRandWFViewsFileType = ImageFileType.PNG
        options.ImageResolution = ImageResolution.DPI_150
        options.ZoomType = ZoomFitType.FitToPage
        options.PixelSize = 2400
        before = set(os.listdir(output_dir))
        doc.ExportImage(options)
        after = [name for name in os.listdir(output_dir)
                 if name not in before and name.lower().endswith(".png")]
        matching = [name for name in after if name.startswith(prefix_name)]
        if not matching:
            matching = [name for name in os.listdir(output_dir)
                        if name.startswith(prefix_name) and
                        name.lower().endswith(".png")]
        if not matching:
            return None, "Revit ExportImage returned no PNG"
        matching.sort(key=lambda name: os.path.getmtime(
            os.path.join(output_dir, name)), reverse=True)
        return os.path.join(output_dir, matching[0]), None
    except Exception as ex:
        return None, "actual Revit view export failed: " + str(ex)[:200]


def dist_point_seg(p, a, b):
    """3D point-to-segment distance."""
    import math
    abx, aby, abz = b.X - a.X, b.Y - a.Y, b.Z - a.Z
    apx, apy, apz = p.X - a.X, p.Y - a.Y, p.Z - a.Z
    L2 = abx * abx + aby * aby + abz * abz
    if L2 == 0:
        return p.DistanceTo(a)
    t = max(0.0, min(1.0, (apx * abx + apy * aby + apz * abz) / L2))
    qx, qy, qz = a.X + t * abx, a.Y + t * aby, a.Z + t * abz
    return math.sqrt((p.X - qx) ** 2 + (p.Y - qy) ** 2 + (p.Z - qz) ** 2)


def join_nearby_walls(doc, wall_ids, tol_ft=JOIN_TOL_FT):
    """Join wall pairs whose endpoints touch or whose endpoints touch the
    other wall's line (T-joint). Returns (joined_count, error_list)."""
    joined = 0
    errors = []
    curves = []
    walls = []
    for wid in wall_ids:
        w = doc.GetElement(wid)
        walls.append(w)
        loc = w.Location
        if loc is None or not hasattr(loc, "Curve"):
            curves.append(None)
            continue
        curves.append(loc.Curve)
    for i in range(len(curves)):
        ci = curves[i]
        if ci is None:
            continue
        p0, p1 = ci.GetEndPoint(0), ci.GetEndPoint(1)
        idir = (p1 - p0).Normalize()
        for j in range(i + 1, len(curves)):
            cj = curves[j]
            if cj is None:
                continue
            q0, q1 = cj.GetEndPoint(0), cj.GetEndPoint(1)
            jdir = (q1 - q0).Normalize()
            direction_dot = abs(idir.DotProduct(jdir))
            if direction_dot > 0.9998:
                # Never join merely adjacent parallel walls. Revit creates a
                # diagonal transition between their offset centre-lines,
                # which is especially visible on short walls. Collinear runs
                # may join; tolerate only 2 mm of transverse numeric noise.
                delta = q0 - p0
                transverse_offset = delta.CrossProduct(idir).GetLength()
                if transverse_offset > 2.0 / MM:
                    continue
            elif direction_dot > 0.0175:
                # The source contract is orthogonal. Do not manufacture a
                # cleanup between walls that are neither parallel nor square.
                continue
            # Extracted centre-lines commonly stop at the physical face of the
            # crossing wall. Their centre-line gap is therefore about half a
            # wall width even though the wall solids already touch.
            pair_tol = tol_ft
            try:
                pair_tol = max(pair_tol,
                               (walls[i].Width + walls[j].Width) / 2.0 +
                               20.0 / MM)
            except Exception:
                pass
            hit = min(p0.DistanceTo(q0), p0.DistanceTo(q1),
                      p1.DistanceTo(q0), p1.DistanceTo(q1)) <= pair_tol
            if not hit:
                for pp in (p0, p1):
                    if dist_point_seg(pp, q0, q1) <= pair_tol:
                        hit = True
                        break
            if not hit:
                for qq in (q0, q1):
                    if dist_point_seg(qq, p0, p1) <= pair_tol:
                        hit = True
                        break
            if hit:
                try:
                    if not JoinGeometryUtils.AreElementsJoined(
                            doc, walls[i], walls[j]):
                        JoinGeometryUtils.JoinGeometry(doc, walls[i], walls[j])
                    joined += 1
                except Exception as ex:
                    msg = str(ex)
                    if msg not in errors:
                        errors.append(msg[:60])
    return joined, errors


def _element_solids(element):
    solids = []
    def collect(geometry):
        for item in geometry or []:
            if isinstance(item, Solid) and item.Volume > 1e-10:
                solids.append(item)
            elif isinstance(item, GeometryInstance):
                collect(item.GetInstanceGeometry())
    collect(element.get_Geometry(Options()))
    return solids


def _elements_have_positive_solid_overlap(first, second):
    for first_solid in _element_solids(first):
        for second_solid in _element_solids(second):
            try:
                intersection = BooleanOperationsUtils.ExecuteBooleanOperation(
                    first_solid, second_solid,
                    BooleanOperationsType.Intersect)
                if intersection.Volume > 1e-7:
                    return True
            except Exception:
                pass
    return False


def cut_boundary_columns_from_walls(doc, wall_records, cutter_ids):
    """Subtract exact column overlap from walls using persistent loaded voids."""
    walls_by_input = {
        str(source.get("id")): doc.GetElement(wall_id)
        for wall_id, source in wall_records
    }
    cutters = [doc.GetElement(item) for item in cutter_ids]
    cutters = [item for item in cutters if item is not None]
    cut_count = 0
    candidate_pairs = 0
    source_only_pairs = 0
    errors = []
    for cutter in cutters:
        targets = BOUNDARY_COLUMN_CUTTER_TARGETS.get(
            cutter.Id.IntegerValue, [])
        for wall_input_id in targets:
            wall = walls_by_input.get(wall_input_id)
            if wall is None:
                errors.append("cutter %d missing source wall %s" % (
                    cutter.Id.IntegerValue, wall_input_id))
                continue
            try:
                column = doc.GetElement(BOUNDARY_COLUMN_CUTTER_COLUMNS.get(
                    cutter.Id.IntegerValue))
                if column is None:
                    raise ValueError("physical column missing")
                if not _elements_have_positive_solid_overlap(column, wall):
                    source_only_pairs += 1
                    doc.Delete(cutter.Id)
                    continue
                candidate_pairs += 1
                if not InstanceVoidCutUtils.CanBeCutWithVoid(wall):
                    raise ValueError("wall cannot be cut with loaded void")
                if not InstanceVoidCutUtils.InstanceVoidCutExists(
                        wall, cutter):
                    InstanceVoidCutUtils.AddInstanceVoidCut(
                        doc, wall, cutter)
                if not InstanceVoidCutUtils.InstanceVoidCutExists(
                        wall, cutter):
                    raise ValueError("void cut was not established")
                mark_auto(cutter, BOUNDARY_COLUMN_CUTTER_INPUTS.get(
                    cutter.Id.IntegerValue, ""), "BoundaryColumnCutter",
                    "void-cutter")
                cut_count += 1
            except Exception as ex:
                errors.append("cutter %d / wall %d: %s" % (
                    cutter.Id.IntegerValue, wall.Id.IntegerValue,
                    str(ex)[:100]))
    return cut_count, candidate_pairs, source_only_pairs, errors


def count_persisted_boundary_wall_cuts(doc, wall_records, cutter_ids):
    walls_by_input = {
        str(source.get("id")): doc.GetElement(wall_id)
        for wall_id, source in wall_records
    }
    persisted = 0
    for cutter_id in cutter_ids:
        cutter = doc.GetElement(cutter_id)
        if cutter is None:
            continue
        for wall_input_id in BOUNDARY_COLUMN_CUTTER_TARGETS.get(
                cutter.Id.IntegerValue, []):
            wall = walls_by_input.get(wall_input_id)
            if wall is None:
                continue
            if InstanceVoidCutUtils.InstanceVoidCutExists(wall, cutter):
                persisted += 1
    return persisted


def create_levels(doc, data, default_level):
    """Create Level elements from project.levels (mm -> ft).
    v3: 返回 name→Level 映射（含已有与新建），供构件按 IR level 名绑定。"""
    level_map = {}
    for el in FilteredElementCollector(doc).OfClass(Level):
        try:
            level_map[el.Name] = el
        except Exception:
            pass
    created, errors = 0, []
    try:
        levels = (data.get("project") or {}).get("levels") or []
        for lv in levels:
            name = lv.get("name") or "Level"
            elev_mm = float(lv.get("elevation", 0))
            if name in level_map:
                actual_mm = level_map[name].Elevation * MM
                if abs(actual_mm - elev_mm) > 1.0:
                    errors.append("level conflict %s actual=%.0f expect=%.0f" %
                                  (name, actual_mm, elev_mm))
                continue
            try:
                new_lv = Level.Create(doc, elev_mm / MM)  # mm -> ft
                new_lv.Name = name
                mark_auto(new_lv, name, "Level", "native", "__PROJECT__")
                level_map[name] = new_lv
                created += 1
            except Exception:
                pass
    except Exception:
        pass
    return level_map, created, errors


def _grid_exists(doc, axis, coordinate_mm):
    """Reuse an existing project grid at the same coordinate (1mm tolerance)."""
    for grid in FilteredElementCollector(doc).OfClass(Grid):
        try:
            curve = grid.Curve
            p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
            if axis == "x" and abs(p0.X * MM - coordinate_mm) <= 1.0 and \
                    abs(p1.X * MM - coordinate_mm) <= 1.0:
                return True
            if axis == "y" and abs(p0.Y * MM - coordinate_mm) <= 1.0 and \
                    abs(p1.Y * MM - coordinate_mm) <= 1.0:
                return True
        except Exception:
            continue
    return False


def _coerce_xy_offset(value):
    """Return a finite project XY offset in mm, or None for missing input."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        dx, dy = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    # Revit's internal coordinates are feet; reject accidental JSON garbage
    # before it can create a document-wide displacement.
    if abs(dx) > 1.0e9 or abs(dy) > 1.0e9:
        return None
    return [dx, dy]


def _grid_axis_items(grid_data, axis):
    """Return [(source_label, local_coordinate_mm)] for one grid direction."""
    key = "x_axes" if axis == "x" else "y_axes"
    label_key = "x_axis_labels" if axis == "x" else "y_axis_labels"
    values = grid_data.get(key, []) or []
    labels = grid_data.get(label_key, []) or []
    result = []
    for index, value in enumerate(values):
        label = labels[index] if index < len(labels) else None
        if label is None and isinstance(value, dict):
            label = value.get("source_label", value.get("label"))
            value = value.get("coord", 0.0)
        if label is None:
            continue
        try:
            result.append((str(label).strip(), float(value)))
        except (TypeError, ValueError):
            continue
    return result


def _existing_grid_coordinates(doc, prefix):
    """Index named existing grids as {(axis, suffix): [coordinate_mm]}.

    Auto anchoring is deliberately limited to an explicit grid prefix.  This
    prevents an unrelated grid system in the host project from silently
    becoming the coordinate reference.
    """
    result = {}
    marker = str(prefix or "").strip()
    if not marker:
        return result
    marker = marker + "-"
    for grid in FilteredElementCollector(doc).OfClass(Grid):
        try:
            name = Element.Name.GetValue(grid)
            if not name or not str(name).startswith(marker):
                continue
            curve = grid.Curve
            p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
            dx, dy = abs(p1.X - p0.X), abs(p1.Y - p0.Y)
            if dx <= dy:
                axis, coordinate = "x", p0.X * MM
            else:
                axis, coordinate = "y", p0.Y * MM
            suffix = str(name)[len(marker):]
            result.setdefault((axis, suffix), []).append(float(coordinate))
        except Exception:
            continue
    return result


def _derive_named_grid_offset(doc, grid_data):
    """Derive a common local→project translation from named host grids.

    At least three labels in each direction and a 300 mm residual cap are
    required.  A revision with changed bay dimensions therefore fails closed
    instead of being hidden by a best-fit translation.
    """
    prefix = grid_data.get("grid_prefix")
    if not prefix:
        return None
    existing = _existing_grid_coordinates(doc, prefix)
    offsets = {"x": [], "y": []}
    for axis in ("x", "y"):
        for label, local in _grid_axis_items(grid_data, axis):
            values = existing.get((axis, str(label)), [])
            if len(values) != 1:
                continue
            offsets[axis].append(values[0] - local)
    if len(offsets["x"]) < 3 or len(offsets["y"]) < 3:
        return None
    result = []
    for axis in ("x", "y"):
        values = sorted(offsets[axis])
        median = values[len(values) // 2]
        if any(abs(value - median) > 300.0 for value in values):
            return None
        result.append(median)
    return result


def _revit_project_base_point_mm(doc):
    """Return the Project Base Point position in Revit internal XY space.

    Revit 2020 does not provide the newer BasePoint convenience lookup used by
    later releases, so resolve the singleton through its built-in category.
    Position is the physical point used for element placement.  The built-in
    parameters are only a compatibility fallback for older API behavior.
    """
    try:
        base_point = first_of(
            FilteredElementCollector(doc)
            .OfCategory(BuiltInCategory.OST_ProjectBasePoint)
            .WhereElementIsNotElementType())
    except Exception:
        base_point = None
    if base_point is None:
        return None

    try:
        point = base_point.Position
        if point is not None:
            return [float(point.X) * MM, float(point.Y) * MM]
    except Exception:
        pass

    try:
        east_west = base_point.get_Parameter(
            BuiltInParameter.BASEPOINT_EASTWEST_PARAM)
        north_south = base_point.get_Parameter(
            BuiltInParameter.BASEPOINT_NORTHSOUTH_PARAM)
        if east_west is not None and north_south is not None:
            return [float(east_west.AsDouble()) * MM,
                    float(north_south.AsDouble()) * MM]
    except Exception:
        pass
    return None


def _project_offset_mm(doc, data, grid_data):
    """Resolve the local-to-project XY translation.

    New and legacy payloads both default to the active Revit Project Base
    Point.  Named-grid anchoring remains available only when an older payload
    explicitly requests that compatibility policy.
    """
    coordinate_system = data.get("coordinate_system") or {}
    explicit = _coerce_xy_offset(coordinate_system.get("project_offset_mm"))
    policy = str(coordinate_system.get("offset_policy") or "").strip()
    if not policy:
        policy = "revit_project_base_point"
    if policy == "revit_project_base_point":
        candidate = _revit_project_base_point_mm(doc)
        if candidate is None:
            raise RuntimeError(
                "cannot resolve active Revit Project Base Point")
        return candidate, "revit_project_base_point"
    if explicit is not None and any(abs(value) > 1.0e-9 for value in explicit):
        return explicit, "explicit"
    if policy == "auto_if_named_grid_consistent":
        candidate = _derive_named_grid_offset(doc, grid_data)
        if candidate is not None:
            return candidate, "named_grid_anchor"
    return explicit or [0.0, 0.0], "local_origin"


def _translate_model_elements(elements, offset_mm):
    """Translate level-local element XY coordinates to project coordinates."""
    dx, dy = offset_mm
    if abs(dx) <= 1.0e-9 and abs(dy) <= 1.0e-9:
        return
    for element in elements:
        kind = element.get("type")
        if kind in ("Wall", "Beam"):
            for key in ("start", "end"):
                point = element.get(key)
                if isinstance(point, list) and len(point) >= 2:
                    point[0] = float(point[0]) + dx
                    point[1] = float(point[1]) + dy
        elif kind in ("Column", "Opening", "Stair"):
            if "x" in element:
                element["x"] = float(element["x"]) + dx
            if "y" in element:
                element["y"] = float(element["y"]) + dy
        elif kind in ("Floor", "Slab"):
            outline = element.get("outline")
            if isinstance(outline, list):
                for point in outline:
                    if isinstance(point, list) and len(point) >= 2:
                        point[0] = float(point[0]) + dx
                        point[1] = float(point[1]) + dy
            center = element.get("center")
            if isinstance(center, list) and len(center) >= 2:
                center[0] = float(center[0]) + dx
                center[1] = float(center[1]) + dy


def create_grids(doc, grid_data, level, level_map=None, project_offset_mm=None):
    """Create Grid lines from x_axes/y_axes (mm).
    v4: 支持多套轴网(每层一套)——grid_data 可为 {'level': name, 'x_axes': [], 'y_axes': []}
    或 data['grids'] = [ {level, x_axes, y_axes}, ... ] 列表。
"""
    if not grid_data:
        return 0
    # 多套轴网: grids = [{level, x_axes, y_axes}, ...]
    if isinstance(grid_data, list):
        total = 0
        for g in grid_data:
            total += create_grids(doc, g, level, level_map, project_offset_mm)
        return total
    lv = level
    if level_map and grid_data.get("level"):
        lv = level_map.get(grid_data["level"]) or lv
    xa = grid_data.get("x_axes", []) or []
    ya = grid_data.get("y_axes", []) or []
    x_labels = grid_data.get("x_axis_labels", []) or []
    y_labels = grid_data.get("y_axis_labels", []) or []
    if not xa and not ya:
        return 0
    local_offset = _coerce_xy_offset(grid_data.get("project_offset_mm"))
    if local_offset is None:
        local_offset = project_offset_mm or [0.0, 0.0]
    dx, dy = local_offset
    xs = xa or [0]
    ys = ya or [0]
    xmin, xmax = min(xs) - 1000, max(xs) + 1000
    ymin, ymax = min(ys) - 1000, max(ys) + 1000
    n = 0
    try:
        for index, x in enumerate(xa):
            coordinate = float(x) + dx
            if _grid_exists(doc, "x", coordinate):
                continue
            line = Line.CreateBound(XYZ(coordinate / MM, (ymin + dy) / MM, 0),
                                    XYZ(coordinate / MM, (ymax + dy) / MM, 0))
            created_grid = Grid.Create(doc, line)
            if index < len(x_labels) and x_labels[index]:
                try:
                    created_grid.Name = str(x_labels[index])
                except Exception:
                    pass
            mark_auto(created_grid, "grid_x_%s" % x, "Grid", "native",
                      "__PROJECT__")
            n += 1
        for index, y in enumerate(ya):
            coordinate = float(y) + dy
            if _grid_exists(doc, "y", coordinate):
                continue
            line = Line.CreateBound(XYZ((xmin + dx) / MM, coordinate / MM, 0),
                                    XYZ((xmax + dx) / MM, coordinate / MM, 0))
            created_grid = Grid.Create(doc, line)
            if index < len(y_labels) and y_labels[index]:
                try:
                    created_grid.Name = str(y_labels[index])
                except Exception:
                    pass
            mark_auto(created_grid, "grid_y_%s" % y, "Grid", "native",
                      "__PROJECT__")
            n += 1
    except Exception:
        pass
    return n


def snap_axis(v, axes, tol_mm=SNAP_TOL_MM):
    """轴线吸附：偏移 ≤ tol 才吸附；返回 (value, offset)，offset 为负表示未吸附"""
    if not axes:
        return v, 0.0
    best = min(axes, key=lambda a: abs(a - v))
    d = best - v
    if abs(d) <= tol_mm:
        return best, abs(d)
    return v, -abs(d)


def _is_degenerate_wall(e):
    s, t = e.get("start"), e.get("end")
    if not (isinstance(s, (list, tuple)) and isinstance(t, (list, tuple))
            and len(s) >= 2 and len(t) >= 2):
        return True
    return ((s[0] - t[0]) ** 2 + (s[1] - t[1]) ** 2) ** 0.5 < 10.0


def validate_model(doc, data, wall_records):
    """BIM Validator: read back after build - counts + positions vs JSON."""
    notes = []
    exp_walls = [e for e in data.get("model_elements", [])
                 if e.get("type") == "Wall" and not _is_degenerate_wall(e)]
    exp_cols = sum(1 for e in data.get("model_elements", []) if e.get("type") == "Column")
    act_walls = len(wall_records)
    act_cols = 0
    act_ds = 0
    col_iv = int(BuiltInCategory.OST_StructuralColumns)
    try:
        for e in FilteredElementCollector(doc).OfClass(FamilyInstance):
            if e.Category is not None and e.Category.Id.IntegerValue == col_iv:
                act_cols += 1
        # ★ v4: DirectShape 柱按名称 DS_col_ 识别(GenericModel 类别)
        for e in FilteredElementCollector(doc).OfClass(DirectShape):
            _dsn = e.Name if e.Name else ""
            if _dsn.startswith("DS_col_"):
                act_cols += 1
                act_ds += 1
            elif _dsn.startswith("DS_beam_"):
                act_ds += 1
    except Exception:
        pass
    if exp_walls and len(exp_walls) != act_walls:
        notes.append("VALIDATE FAIL: walls expected %d actual %d" % (len(exp_walls), act_walls))
    if exp_cols > 0 and exp_cols != act_cols:
        notes.append("VALIDATE WARN: cols expected %d actual %d (skipped)" % (exp_cols, act_cols))
    if act_ds:
        notes.append("DirectShape count: %d (no structural family in template)" % act_ds)
    # 全量墙厚比对；墙位置差异明细最多输出 20 条（tol 100mm）。
    pos_bad = 0
    thickness_bad = 0
    skew_bad = 0
    for wid, source_wall in wall_records:
        try:
            w = doc.GetElement(wid)
            c = w.Location.Curve
            p0 = c.GetEndPoint(0)  # feet
            p1 = c.GetEndPoint(1)
            exp0 = source_wall["start"]
            dx = abs(p0.X * MM - exp0[0])
            dy = abs(p0.Y * MM - exp0[1])
            if (dx > 100 or dy > 100):
                pos_bad += 1
                if pos_bad <= 10:
                    notes.append("VALIDATE POS %s: actual(%.0f,%.0f) expect(%s,%s) d=%.0f" % (
                        source_wall["id"], p0.X * MM, p0.Y * MM,
                        exp0[0], exp0[1], max(dx, dy)))
            source_dx = source_wall["end"][0] - source_wall["start"][0]
            source_dy = source_wall["end"][1] - source_wall["start"][1]
            actual_dx = (p1.X - p0.X) * MM
            actual_dy = (p1.Y - p0.Y) * MM
            cross = abs(source_dx * actual_dy - source_dy * actual_dx)
            lengths = ((source_dx ** 2 + source_dy ** 2) ** 0.5 *
                       (actual_dx ** 2 + actual_dy ** 2) ** 0.5)
            if lengths and cross / lengths > 0.001745:  # sin(0.1 degrees)
                skew_bad += 1
                if skew_bad <= 10:
                    notes.append("VALIDATE SKEW %s: direction changed" %
                                 source_wall.get("id", "?"))
            expected_width = float(source_wall.get("thickness") or 0)
            actual_width = w.WallType.Width * MM
            if expected_width and abs(actual_width - expected_width) > 5.0:
                thickness_bad += 1
                notes.append("VALIDATE THICKNESS %s: actual=%.0f expect=%.0f" % (
                    source_wall["id"], actual_width, expected_width))
        except Exception:
            pass
    if pos_bad > 10:
        notes.append("... %d more position diffs" % (pos_bad - 10))
    if thickness_bad:
        notes.append("VALIDATE FAIL: wall thickness mismatches %d" % thickness_bad)
    if skew_bad:
        notes.append("VALIDATE FAIL: wall direction mismatches %d" % skew_bad)
    if not notes:
        notes.append("VALIDATE PASS: walls %d cols %d" % (act_walls, act_cols))
    return notes


def main():
    _dbg("=== main v12 start, cache cleared ===")
    _WALL_TYPE_CACHE.clear()
    del CREATED_RECORDS[:]
    del BOUNDARY_COLUMN_IDS[:]
    BOUNDARY_COLUMN_TARGETS.clear()
    del BOUNDARY_COLUMN_CUTTER_IDS[:]
    BOUNDARY_COLUMN_CUTTER_TARGETS.clear()
    BOUNDARY_COLUMN_CUTTER_COLUMNS.clear()
    BOUNDARY_COLUMN_CUTTER_INPUTS.clear()
    notes = NOTES
    if not JSON_IN or not RVT_OUT:
        TaskDialog.Show("JSON2RVT", "请配置 BIM_JSON_IN 和 REVIT_OUTPUT_DIR")
        return
    if not os.path.exists(JSON_IN):
        TaskDialog.Show("JSON2RVT", "JSON not found: " + JSON_IN)
        return
    import codecs
    with codecs.open(JSON_IN, "r", encoding="utf-8") as f:
        data = json.load(f)
    wall_model_input = data.get("artifact_type") == "wall_model"
    try:
        data = normalize_wall_model_payload(data)
    except Exception as ex:
        TaskDialog.Show("JSON2RVT", "WallModel 审核门禁拒绝执行：" + str(ex))
        return
    revit_write_approval = None
    if wall_model_input:
        gate_path = os.path.join(JSON_DIR, "revit_result.json")
        try:
            with codecs.open(gate_path, "r", encoding="utf-8") as gate_stream:
                revit_gate = json.load(gate_stream)
            expected_hash = (data.get("build") or {}).get("wall_model_sha256")
            if revit_gate.get("wall_model_sha256") != expected_hash:
                raise ValueError("Dry-run 结果与当前 WallModel 不一致")
            if revit_gate.get("status") != "write_approved":
                raise ValueError("必须先通过 Dry-run 并单独批准 Revit 写入")
            revit_write_approval = revit_gate.get("approval") or {}
            if (revit_write_approval.get("status") != "approved" or
                    not revit_write_approval.get("actor_id")):
                raise ValueError("Revit 写入审批记录不完整")
        except Exception as ex:
            TaskDialog.Show("JSON2RVT", "Revit 写入门禁拒绝执行：" + str(ex))
            return
    global BUILD_ID, PROJECT_ID, FLOOR_CODE
    BUILD_ID = ((data.get("build") or {}).get("build_id") or "legacy")
    PROJECT_ID = ((data.get("project") or {}).get("project_id") or
                  (data.get("build") or {}).get("project_id") or "")
    FLOOR_CODE = ((data.get("build") or {}).get("floor_code") or "")
    if not PROJECT_ID or not FLOOR_CODE:
        TaskDialog.Show("JSON2RVT", "缺少 project_id 或 floor_code，拒绝执行非项目级建模")
        return
    target_model_path = ((data.get("build") or {}).get("target_model_path") or "")
    if not target_model_path:
        TaskDialog.Show("JSON2RVT", "项目未绑定 RVT 工作模型，拒绝写入当前文档")
        return
    try:
        if os.path.normcase(os.path.abspath(doc.PathName)) != \
                os.path.normcase(os.path.abspath(target_model_path)):
            TaskDialog.Show("JSON2RVT", "请先在 Revit 中打开项目工作模型：" + target_model_path)
            return
    except Exception:
        TaskDialog.Show("JSON2RVT", "无法确认当前 Revit 文档是否为项目工作模型")
        return
    elements = data.get("model_elements", [])
    views = data.get("views", [])
    grid_data = data.get("grids") or data.get("grid", {})
    grid_anchor_data = (grid_data[0] if isinstance(grid_data, list) and grid_data
                        else grid_data)
    try:
        project_offset_mm, offset_source = _project_offset_mm(
            doc, data,
            grid_anchor_data if isinstance(grid_anchor_data, dict) else {})
    except Exception as ex:
        TaskDialog.Show(
            "JSON2RVT",
            "无法读取 Revit 项目基点，已停止建模：" + str(ex))
        return
    _translate_model_elements(elements, project_offset_mm)
    if (offset_source != "local_origin" or
            any(abs(value) > 1.0e-9 for value in project_offset_mm)):
        notes.append("project XY offset mm: %.1f, %.1f (%s)" % (
            project_offset_mm[0], project_offset_mm[1], offset_source))
    level = get_level(doc, data.get("level", "Level 1"))
    family_count, family_errors = prepare_boundary_column_families(elements)
    if family_count:
        notes.append("boundary column families generated: %d" % family_count)
    if family_errors:
        notes.append("boundary column family failures: %d (%s)" % (
            len(family_errors), "; ".join(family_errors[:3])))

    # --- direction-preserving endpoint snap (150 mm) ---
    walls_data = []
    for e in elements:
        if e.get("type") == "Wall":
            # 墙端点补 z 维度(旧模型只有 [x,y])——端点合并逻辑需要 3 维
            for _k in ("start", "end"):
                _v = e.get(_k)
                if _v is None:
                    e[_k] = [0.0, 0.0, 0.0]
                elif len(_v) == 2:
                    e[_k] = [float(_v[0]), float(_v[1]), 0.0]
                elif len(_v) >= 3:
                    e[_k] = [float(_v[0]), float(_v[1]), float(_v[2])]
            walls_data.append(e)
    for i in range(len(walls_data)):
        wi = walls_data[i]
        for j in range(i + 1, len(walls_data)):
            wj = walls_data[j]
            wi_dx = wi["end"][0] - wi["start"][0]
            wi_dy = wi["end"][1] - wi["start"][1]
            wj_dx = wj["end"][0] - wj["start"][0]
            wj_dy = wj["end"][1] - wj["start"][1]
            wi_axis = "H" if abs(wi_dx) >= abs(wi_dy) else "V"
            wj_axis = "H" if abs(wj_dx) >= abs(wj_dy) else "V"
            for a_key in ("start", "end"):
                for b_key in ("start", "end"):
                    a = wi[a_key]; b = wj[b_key]
                    d = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5
                    if d < 150.0:
                        z = (a[2] + b[2]) / 2.0
                        if wi_axis != wj_axis:
                            # Move each endpoint only along its own wall axis.
                            # This creates the H/V intersection without ever
                            # rotating either wall.
                            horizontal = wi if wi_axis == "H" else wj
                            vertical = wi if wi_axis == "V" else wj
                            h_point = a if wi_axis == "H" else b
                            v_point = a if wi_axis == "V" else b
                            joint = [v_point[0], h_point[1], z]
                            wi[a_key] = list(joint)
                            wj[b_key] = list(joint)
                        elif wi_axis == "H" and abs(a[1] - b[1]) <= 2.0:
                            joint = [(a[0] + b[0]) / 2.0,
                                     (a[1] + b[1]) / 2.0, z]
                            wi[a_key] = list(joint)
                            wj[b_key] = list(joint)
                        elif wi_axis == "V" and abs(a[0] - b[0]) <= 2.0:
                            joint = [(a[0] + b[0]) / 2.0,
                                     (a[1] + b[1]) / 2.0, z]
                            wi[a_key] = list(joint)
                            wj[b_key] = list(joint)
    ok, fail = [], []
    wall_ids = []
    # Keep each ElementId bound to the exact source wall. A failed creation
    # must not shift the following ids onto the wrong source curve.
    wall_records = []
    # suppress revit error dialogs (join/delete failures) via FailureHandlingOptions
    # so walls CAN be joined without popping "cannot keep elements joined"
    _fho = None
    try:
        class _Suppress(IFailuresPreprocessor):
            def PreprocessFailures(self, accessor):
                return FailureProcessingResult.Continue
        _fho = doc.Application.FailureHandlingOptions
        _fho.SetFailuresPreprocessor(_Suppress())
    except Exception:
        _fho = None
    global TXN
    # v3.2: 清理上次崩溃遗留的未闭合事务/子事务，避免 "Starting a new
    # transaction is not permitted" 连锁失败
    try:
        if doc.IsModifiable:
            try:
                doc.Regenerate()
            except Exception:
                pass
    except Exception:
        pass
    t = Transaction(doc, "JSON build")
    if _fho is not None:
        try:
            t.SetFailureHandlingOptions(_fho)
        except Exception:
            pass
    t.Start()
    TXN = t
    # 幂等清理：只删除明确带 BUILDMATE_AUTO 标记的旧自动构件。
    # 未标记的用户模型一律不碰；旧版遗留的无标记自动构件需人工确认后清理。
    del_fail = 0
    deleted_auto = 0
    try:
        # FamilyInstance 包含上一轮自动生成的结构柱和梁。旧版只清墙、板、轴网，
        # 多轮试建会让柱梁持续叠加，造成 KZ8 在错误区域重复显示。
        # 标高和轴网是项目资产，绝不可在更新一个楼层时清理。构件只删除
        # 同一项目、同一楼层的自动对象；其他楼层与用户对象保持不动。
        for et in (Wall, Floor, FamilyInstance):
            for el in list(FilteredElementCollector(doc).OfClass(et)):
                if not is_buildmate_auto(el, PROJECT_ID, FLOOR_CODE):
                    continue
                for _attempt in (0, 1):  # retry once
                    try:
                        doc.Delete(el.Id)
                        deleted_auto += 1
                        break
                    except Exception:
                        del_fail += 1
        for fi in list(FilteredElementCollector(doc).OfClass(FamilyInstance)):
            if not is_buildmate_auto(fi, PROJECT_ID, FLOOR_CODE):
                continue
            for _attempt in (0, 1):  # retry once
                try:
                    doc.Delete(fi.Id)
                    deleted_auto += 1
                    break
                except Exception:
                    del_fail += 1
        # A failed profile-column validation can occur before mark_auto.  The
        # generated BM_Boundary family name is an ownership marker in itself;
        # remove these orphan instances so a retry cannot accumulate solids.
        for fi in list(FilteredElementCollector(doc).OfClass(FamilyInstance)):
            if not is_buildmate_boundary_family_instance(fi):
                continue
            for _attempt in (0, 1):
                try:
                    doc.Delete(fi.Id)
                    deleted_auto += 1
                    break
                except Exception:
                    del_fail += 1
        # v3.3: 清 DirectShape 自动构件（无结构族时柱/梁走 DirectShape 兜底——之前漏删导致重跑重复）
        # ★ v4: 只删名称 DS_ 前缀的(柱/梁自动构件), 不误删用户自己的 DirectShape
        for ds in list(FilteredElementCollector(doc).OfClass(DirectShape)):
            if not is_buildmate_auto(ds, PROJECT_ID, FLOOR_CODE):
                continue
            for _attempt in (0, 1):
                try:
                    doc.Delete(ds.Id)
                    deleted_auto += 1
                    break
                except Exception:
                    del_fail += 1
    except Exception:
        pass
    if del_fail:
        notes.append("cleanup partial: %d delete failed" % del_fail)
    if deleted_auto:
        notes.append("cleanup auto: %d" % deleted_auto)
    level_map, ln, level_errors = create_levels(doc, data, level)
    if level_errors:
        notes.extend(level_errors)
        t.RollBack()
        TaskDialog.Show("JSON2RVT", "项目标高冲突：" + "; ".join(level_errors[:5]))
        return
    if ln:
        notes.append("levels created: %d" % ln)
    gn = create_grids(doc, grid_data, level, level_map, project_offset_mm)
    if gn:
        notes.append("grids created: %d" % gn)
    _gd0 = grid_data[0] if isinstance(grid_data, list) and grid_data else grid_data
    xa = _gd0.get("x_axes", []) if isinstance(_gd0, dict) else []
    ya = _gd0.get("y_axes", []) if isinstance(_gd0, dict) else []
    # Beam snap uses the same project frame as the translated elements.
    xa = [float(value) + project_offset_mm[0] for value in xa]
    ya = [float(value) + project_offset_mm[1] for value in ya]

    # manual build order: Column -> Beam -> Wall -> Floor/Slab (v3: Slab 并入 Floor)
    for et in ("Column", "Beam", "Wall", "Floor", "Slab"):
        for e in elements:
            if e.get("type") != et:
                continue
            try:
                err = None
                created = None
                lv = resolve_level(doc, level_map, e, level)
                if et == "Wall":
                    created = create_wall(doc, e, lv)
                elif et in ("Floor", "Slab"):
                    err = create_floor(doc, e, lv)
                elif et == "Beam":
                    # snap beam ends to axes (v3: 容差内才吸附)
                    e2 = dict(e)
                    sx, dxo = snap_axis(e["start"][0], xa)
                    sy, dyo = snap_axis(e["start"][1], ya)
                    ex2, dxe = snap_axis(e["end"][0], xa)
                    ey2, dye = snap_axis(e["end"][1], ya)
                    e2["start"] = [sx, sy, e["start"][2]]
                    e2["end"] = [ex2, ey2, e["end"][2]]
                    for lab, off in (("start.x", dxo), ("start.y", dyo),
                                     ("end.x", dxe), ("end.y", dye)):
                        if off < 0:
                            notes.append("no-snap %s %s off=%.0fmm" % (
                                e.get("id", "?"), lab, -off))
                    err = create_beam(doc, e2, lv)
                elif et == "Column":
                    # ★ 柱不做轴网吸附(v4)——图纸柱子可能本来就不在精确交点(实测 fbz1 大柱偏 100mm)，
                    # 吸附到交点=破坏真实位置(用户对照原图发现偏移)。柱位置以 JSON 为准。
                    err = create_column(doc, e, lv)
                if isinstance(created, str):
                    err = created
                if err:
                    notes.append(e.get("id", "?") + ":" + err)
                    fail.append(e.get("id", "?") + ":" + err)
                    continue
                # ★ 只收 ElementId(create_wall 返回字符串=错误说明, 不能进 wall_ids——join 时 GetElement 返回 None 崩溃)
                if created is not None and not isinstance(created, str):
                    wall_ids.append(created)
                    wall_records.append((created, e))
                ok.append(e.get("id", "?"))
            except Exception as ex:
                _dbg("EX %s %s: %s" % (et, e.get("id"), str(ex)[:200]))
                fail.append(e.get("id", "?") + ":" + str(ex)[:80])
    # Creating later walls can still reshape earlier walls during Revit's
    # regeneration. After every wall exists, restore all source curves in one
    # final pass with joins disabled, then regenerate once.
    for wid, source_wall in wall_records:
        try:
            built_wall = doc.GetElement(wid)
            WallUtils.DisallowWallJoinAtEnd(built_wall, 0)
            WallUtils.DisallowWallJoinAtEnd(built_wall, 1)
            built_wall.Location.Curve = wall_line_at_level(
                source_wall, doc.GetElement(built_wall.LevelId))
        except Exception as ex:
            notes.append("wall final lock %s: %s" % (
                source_wall.get("id", "?"), str(ex)[:80]))
    doc.Regenerate()
    notes.append("wall curves restored: %d" % len(wall_records))
    # Project views are retained across floor updates. create_views replaces
    # only a same-name view explicitly requested by this build.
    target_level = level_map.get("\u6807\u9ad8 " + FLOOR_CODE) or level
    view_names = ensure_standard_views(doc, target_level, FLOOR_CODE)
    view_names.extend(create_views(doc, views, target_level))
    commit_ok = True
    try:
        t.Commit()
    except Exception as ex:
        commit_ok = False
        notes.append("COMMIT FAIL: " + str(ex)[:80])
        try:
            t.RollBack()
        except Exception:
            pass
    # Native wall-end cleanup extends centre-lines to the crossing wall's axis
    # (typically by half a wall width), which changes the authoritative source
    # geometry and can produce visibly skewed cleanup. Keep ends locked, then
    # connect only actually touching wall solids with JoinGeometry instead.
    if commit_ok and wall_records:
        column_cut = 0
        column_candidates = 0
        topology_tx = Transaction(doc, "BuildMate Wall Topology")
        try:
            topology_tx.Start()
            for wid, source_wall in wall_records:
                built_wall = doc.GetElement(wid)
                WallUtils.DisallowWallJoinAtEnd(built_wall, 0)
                WallUtils.DisallowWallJoinAtEnd(built_wall, 1)
                built_wall.Location.Curve = wall_line_at_level(
                    source_wall, doc.GetElement(built_wall.LevelId))
            doc.Regenerate()
            joined_count, join_errors = join_nearby_walls(doc, wall_ids)
            column_cut, column_candidates, source_only_pairs, column_cut_errors = \
                cut_boundary_columns_from_walls(
                    doc, wall_records, BOUNDARY_COLUMN_CUTTER_IDS)
            topology_tx.Commit()
            notes.append("wall geometry joined: %d" % joined_count)
            if join_errors:
                notes.append("wall geometry join skipped: %d (%s)" % (
                    len(join_errors), "; ".join(join_errors[:3])))
            notes.append("boundary column-wall void cuts: %d/%d" % (
                column_cut, column_candidates))
            if source_only_pairs:
                notes.append("boundary source-only contact pairs skipped: %d" %
                             source_only_pairs)
            if column_cut_errors:
                notes.append("boundary column-wall void cut failed: %d (%s)" % (
                    len(column_cut_errors), "; ".join(column_cut_errors[:3])))
                fail.extend("physical_cut:" + item
                            for item in column_cut_errors)
        except Exception as ex:
            notes.append("VALIDATE FAIL: wall topology transaction: " +
                         str(ex)[:120])
            try:
                if topology_tx.GetStatus() == TransactionStatus.Started:
                    topology_tx.RollBack()
            except Exception:
                pass
        if BOUNDARY_COLUMN_CUTTER_IDS:
            persisted_cuts = count_persisted_boundary_wall_cuts(
                doc, wall_records, BOUNDARY_COLUMN_CUTTER_IDS)
            notes.append("boundary column-wall persisted void cuts: %d" %
                         persisted_cuts)
            if (column_candidates <= 0 or column_cut != column_candidates or
                    persisted_cuts != column_candidates):
                fail.append("physical_cut_post_commit:%d/%d/%d" % (
                    column_cut, column_candidates, persisted_cuts))
    # Validate the committed wall against its own source record. This catches
    # any direction drift while wall solids remain explicitly joined.
    for vn in validate_model(doc, data, wall_records):
        notes.append(vn)
    # Project mode persists into the bound working model.  SaveAs would detach
    # the next floor from the model that contains the earlier floors.
    if not os.path.isdir(RVT_OUT):
        os.makedirs(RVT_OUT)
    save_ok = True
    try:
        doc.Save()
        saved = target_model_path
    except Exception as ex:
        save_ok = False
        saved = "SAVE FAIL: " + str(ex)[:80]
    actual_view_path = None
    actual_view_sha256 = None
    actual_view_error = None
    if wall_model_input and commit_ok and save_ok:
        actual_view_path, actual_view_error = export_actual_plan_view(
            doc, level, RVT_OUT, BUILD_ID)
        if actual_view_path:
            try:
                digest = hashlib.sha256()
                with open(actual_view_path, "rb") as image_stream:
                    while True:
                        chunk = image_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                actual_view_sha256 = digest.hexdigest()
            except Exception as ex:
                actual_view_error = "actual view hash failed: " + str(ex)[:160]
        if actual_view_error:
            notes.append(actual_view_error)
    # v3: notes 同步落盘，便于后端/调试侧读取
    try:
        with open(os.path.join(RVT_OUT, "schema.log"), "a") as lf:
            lf.write(time.strftime("%Y-%m-%d %H:%M:%S") + " v3 | " +
                     " | ".join(notes) + "\n")
    except Exception:
        pass
    # 机器可读结果是后端判断成功的唯一依据；弹窗/目录中存在 RVT 都不再等于成功。
    try:
        validation_fail = any(str(n).startswith("VALIDATE FAIL") or
                              str(n).startswith("VALIDATE POS") for n in notes)
        direct_shape_count = sum(1 for r in CREATED_RECORDS
                                 if r.get("mode") == "direct_shape")
        result_status = ("done" if commit_ok and save_ok and not fail and
                         not validation_fail and direct_shape_count == 0 else "error")
        result = {
            "schema_version": "1.0",
            "build_id": BUILD_ID,
            "status": result_status,
            "rvt_path": saved if save_ok else "",
            "expected_count": len(elements),
            "created_count": len([r for r in CREATED_RECORDS
                                  if r.get("kind") not in (
                                      "Grid", "Level", "BoundaryColumnCutter")]),
            "created": CREATED_RECORDS,
            "failures": fail,
            "validation_failed": validation_fail,
            "direct_shape_count": direct_shape_count,
            "notes": [str(n)[:300] for n in notes[-200:]],
        }
        result_path = os.path.join(RVT_OUT, "build_%s_result.json" % BUILD_ID)
        with open(result_path, "w") as rf:
            json.dump(result, rf, ensure_ascii=True, indent=1)
        if wall_model_input and JSON_DIR:
            new_errors = list(fail)
            if actual_view_error:
                new_errors.append(actual_view_error)
            wall_result = {
                "schema_version": "buildmate.revit-result/1.0",
                "artifact_type": "revit_result",
                "tenant_id": (data.get("project") or {}).get("tenant_id"),
                "project_id": PROJECT_ID,
                "wall_model_sha256": (data.get("build") or {}).get(
                    "wall_model_sha256"),
                "status": ("succeeded" if result_status == "done" else "failed"),
                "transaction_id": BUILD_ID,
                "created_element_ids": [
                    str(record.get("revit_element_id"))
                    for record in CREATED_RECORDS
                    if record.get("kind") == "Wall" and
                    record.get("revit_element_id") is not None],
                "readback": {
                    "expected_count": len(elements),
                    "created_count": result["created_count"],
                    "wall_count": len(wall_records),
                    "validation_failed": validation_fail,
                    "rvt_path": saved if save_ok else "",
                },
                "actual_view_path": actual_view_path,
                "actual_view_sha256": actual_view_sha256,
                "approval": revit_write_approval,
                "errors": new_errors,
            }
            with open(os.path.join(JSON_DIR, "revit_result.json"), "w") as rf:
                json.dump(wall_result, rf, ensure_ascii=True, indent=1)
    except Exception as ex:
        note("RESULT WRITE FAIL: " + str(ex)[:80])
    # Automated builds report through build_<id>_result.json.  A modal success
    # dialog blocks the HTTP route and looks like an endless warning loop.
    return result


try:
    main()
except Exception:
    # v3.2: 崩溃兜底——回滚未提交事务，避免文档遗留开事务锁死后续运行
    try:
        if TXN is not None and TXN.GetStatus() == TransactionStatus.Started:
            TXN.RollBack()
    except Exception:
        pass
    TaskDialog.Show("JSON2RVT Error", traceback.format_exc()[:1500])
