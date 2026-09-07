"""IFC 模型解析服务（BIM 图纸合规审查的数据层）

v2（2026-08-21）：全量语义导出升级
- 不再 8 类白名单 + [:50] 截断：全类型扫描 IfcProduct，统计全量真实数量
- 每个构件携带完整语义：Container（楼层/空间）、Psets（属性集）、Quantities（工程量）、
  Materials（材料）、ObjectType/PredefinedType（对齐外部导出样本 extract_ifc_to_json）
- 保留 v1 兼容字段（type/ifc_type/name/global_id）与顶层统计（elements_count/total_*/spaces/properties），
  供 BIM 规则轨、前端展示、drawing2bim 感知通道无感使用
"""
import asyncio
from collections import Counter

from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── 类型中文映射（扩展：覆盖全量语义导出的常见类型）──
_TYPE_CN = {
    "IFCWALL": "墙", "IFCWALLSTANDARDCASE": "墙", "IFCCOLUMN": "柱", "IFCBEAM": "梁",
    "IFCSLAB": "楼板", "IFCDOOR": "门", "IFCWINDOW": "窗", "IFCSTAIR": "楼梯",
    "IFCSTAIRFLIGHT": "楼梯", "IFCROOF": "屋顶",
    "IFCMEMBER": "杆件", "IFCOPENINGELEMENT": "洞口", "IFCPLATE": "板件",
    "IFCCURTAINWALL": "幕墙", "IFCRAILING": "栏杆", "IFCBUILDINGELEMENTPROXY": "体量",
    "IFCCOVERING": "覆盖层", "IFCFURNISHINGELEMENT": "家具", "IFCPILE": "桩",
    "IFCFOOTING": "基础", "IFCGRID": "轴网", "IFCPROXY": "代理构件",
}

# 空间层级对象（过滤，不算构件）
_SPATIAL_CLASSES = ("IfcSpace", "IfcBuildingStorey", "IfcBuilding", "IfcSite", "IfcProject")


def _safe_get_attr(entity, attr_name: str):
    """安全读取 ifc 实体属性，属性不存在返回 None（兼容 IFC2X3）"""
    if entity is None:
        return None
    try:
        return getattr(entity, attr_name)
    except AttributeError:
        return None


def _extract_wall_thickness_geometry(prod) -> float | None:
    """从 IFC 几何表示提取墙体厚度（mm）

    Revit 导出 IFC2X3 墙厚常驻 SweptSolid 轮廓（IfcRectangleProfileDef.XDim），
    而 Qto/Psets 未必带厚度——几何是厚度最可靠来源。
    支持：IfcExtrudedAreaSolid / IfcBooleanClippingResult → RectangleProfile → XDim（mm）
    """
    try:
        import ifcopenshell.util.element as el_util
        reps = getattr(prod, "Representation", None)
        if not reps:
            return None
        for rep in reps.Representations or []:
            for item in rep.Items or []:
                thickness = _profile_thickness(item)
                if thickness:
                    return thickness
    except Exception:
        return None
    return None


def _profile_thickness(item) -> float | None:
    """递归查找 SweptSolid 轮廓的 XDim（墙厚）"""
    try:
        # 直接挤出体
        if item.is_a("IfcExtrudedAreaSolid"):
            prof = item.SweptArea
            if prof and prof.is_a("IfcRectangleProfileDef"):
                x = getattr(prof, "XDim", None)
                if x:
                    return float(x)   # 保留 IFC 原始单位（该文件为 mm）
        # 布尔裁剪结果（门窗洞的墙）→ 外层是 IfcBooleanClippingResult
        if item.is_a("IfcBooleanClippingResult"):
            op = getattr(item, "Operand", None)
            if op:
                return _profile_thickness(op)
        # 复合表示（IfcBooleanResult / 其他）递归
        for attr in ("FirstOperand", "SecondOperand", "Operand"):
            child = getattr(item, attr, None)
            if child and hasattr(child, "is_a"):
                r = _profile_thickness(child)
                if r:
                    return r
    except Exception:
        return None
    return None


def _extract_slab_elevation(prod) -> float | None:
    """从 IFC 几何/楼层提取楼板标高（保留 IFC 原始单位）

    Revit 导出楼板：挤出体 Position.Z（常规板）/ FacetedBrep 顶点 Z（异形板）；
    几何缺失时用所属楼层 IfcBuildingStorey.Elevation 兜底（ContainedIn 关联）。
    """
    try:
        reps = getattr(prod, "Representation", None)
        if reps:
            for rep in reps.Representations or []:
                for item in rep.Items or []:
                    z = _solid_position_z(item)
                    if z is not None:
                        return z
                    z2 = _brep_min_z(item)
                    if z2 is not None:
                        return z2
        # 兜底：所属楼层标高（IFC2X3 用 IfcRelContainedInSpatialStructure）
        storey = None
        for rel in getattr(prod, "ContainedIn", None) or []:
            if rel.is_a("IfcRelContainedInSpatialStructure"):
                storey = rel.RelatingStructure
                break
        if storey and storey.is_a("IfcBuildingStorey"):
            el = getattr(storey, "Elevation", None)
            if el is not None:
                return float(el)
    except Exception:
        return None
    return None


def _brep_min_z(item) -> float | None:
    """IfcFacetedBrep 顶点 Z（首个面首个循环首顶点，异形板标高）

    IFC2X3 用 Outer（单个 ClosedShell），IFC4 用 CfsFaces（列表）——兼容两者
    """
    try:
        if item.is_a("IfcFacetedBrep"):
            shells = getattr(item, "CfsFaces", None)
            if not shells:
                outer = getattr(item, "Outer", None)
                shells = [outer] if outer else []
            for shell in shells or []:
                faces = getattr(shell, "CfsFaces", None) or []
                for face in faces:
                    bounds = getattr(face, "Bounds", None) or []
                    for bound in bounds:
                        loop = getattr(bound, "Bound", None)
                        poly = getattr(loop, "Polygon", None) if loop else None
                        if poly:
                            for pt in poly:
                                coords = getattr(pt, "Coordinates", None) or []
                                if len(coords) > 2:
                                    return float(coords[2])
    except Exception:
        return None
    return None


def _solid_position_z(item) -> float | None:
    """递归找挤出体 Position.Location.Z（m → mm）"""
    try:
        if item.is_a("IfcExtrudedAreaSolid"):
            pos = getattr(item, "Position", None)
            if pos:
                loc = getattr(pos, "Location", None)
                if loc is not None:
                    # IfcCartesianPoint 坐标在 Coordinates 列表（非 .Z 属性）
                    coords = getattr(loc, "Coordinates", None) or []
                    if len(coords) > 2 and coords[2] is not None:
                        return float(coords[2])   # 保留 IFC 原始单位（该文件为 mm）
        # 布尔/复合表示递归
        for attr in ("Operand", "FirstOperand", "SecondOperand"):
            child = getattr(item, attr, None)
            if child and hasattr(child, "is_a"):
                r = _solid_position_z(child)
                if r is not None:
                    return r
    except Exception:
        return None
    return None


def _extract_placement(prod) -> dict | None:
    """提取构件放置坐标（ObjectPlacement → Location.X/Y/Z，IFC 原始单位）

    递归追溯相对定位链（IfcLocalPlacement.PlacementRelTo），近似求全局坐标。
    """
    try:
        def _loc_coords(placement) -> tuple | None:
            rel = getattr(placement, "RelativePlacement", None)
            if not rel:
                return None
            loc = getattr(rel, "Location", None)
            if loc is None:
                return None
            coords = getattr(loc, "Coordinates", None) or []
            if len(coords) < 3:
                return None
            return (float(coords[0]), float(coords[1]), float(coords[2]))

        total = None
        placement = getattr(prod, "ObjectPlacement", None)
        visited = 0
        while placement is not None and visited < 10:
            c = _loc_coords(placement)
            if c is None:
                break
            total = (c[0] + (total[0] if total else 0.0),
                     c[1] + (total[1] if total else 0.0),
                     c[2] + (total[2] if total else 0.0))
            placement = getattr(placement, "PlacementRelTo", None)
            visited += 1
        if total is None:
            return None
        return {"x": round(total[0], 2), "y": round(total[1], 2), "z": round(total[2], 2)}
    except Exception:
        return None


def _cn_name(ifc_class: str) -> str:
    """IfcClass → 中文类型名（未映射用 Ifc 前缀剥离后的短名）"""
    return _TYPE_CN.get(ifc_class.upper(), ifc_class.removeprefix("Ifc"))


def _sync_parse(ifc_path: str) -> dict:
    """同步解析 IFC 文件：全类型构件语义提取 + 空间/属性（线程池中运行）"""
    import ifcopenshell
    import ifcopenshell.util.element
    model = ifcopenshell.open(ifc_path)

    # ── 项目信息 ──
    project_info = {}
    projects = model.by_type("IfcProject")
    if projects:
        proj = projects[0]
        project_info = {
            "Name": _safe_get_attr(proj, "Name"),
            "Description": _safe_get_attr(proj, "Description"),
            "GlobalId": _safe_get_attr(proj, "GlobalId"),
        }

    # ── 全类型构件扫描（过滤空间层级对象）──
    elements = []
    for prod in model.by_type("IfcProduct"):
        ifc_class = prod.is_a()
        if ifc_class in _SPATIAL_CLASSES:
            continue

        name = _safe_get_attr(prod, "Name") or "未命名"
        global_id = _safe_get_attr(prod, "GlobalId") or ""

        # 所属楼层/空间（Container）
        container = None
        try:
            c = ifcopenshell.util.element.get_container(prod)
            if c is not None:
                container = {
                    "IfcClass": c.is_a(),
                    "Name": _safe_get_attr(c, "Name"),
                    "GlobalId": _safe_get_attr(c, "GlobalId"),
                }
        except Exception:
            pass

        # 属性集 Pset + 工程量 Qto
        psets, qtos = {}, {}
        try:
            psets = ifcopenshell.util.element.get_psets(prod, psets_only=True) or {}
        except Exception:
            pass
        try:
            qtos = ifcopenshell.util.element.get_psets(prod, qtos_only=True) or {}
        except Exception:
            pass

        # 材料
        materials = []
        try:
            for m in ifcopenshell.util.element.get_materials(prod) or []:
                materials.append({
                    "Name": _safe_get_attr(m, "Name"),
                    "Description": _safe_get_attr(m, "Description"),
                })
        except Exception:
            pass

        elements.append({
            # ── 全量语义字段（对齐 extract_ifc_to_json 样本）──
            "GlobalId": global_id,
            "IfcClass": ifc_class,
            "Name": name,
            "Description": _safe_get_attr(prod, "Description"),
            "ObjectType": _safe_get_attr(prod, "ObjectType"),
            "PredefinedType": _safe_get_attr(prod, "PredefinedType"),
            "Container": container,
            # ── 放置坐标（违规定位用：X/Y/Z 全局坐标）──
            "placement": _extract_placement(prod),
            "Psets": psets,
            "Quantities": qtos,
            "Materials": materials,
            # ── 几何厚度（墙）：Revit 导出 IFC 墙厚常驻 SweptSolid 轮廓 ──
            "geom_thickness": (_extract_wall_thickness_geometry(prod)
                               if ifc_class.upper() in ("IFCWALL", "IFCWALLSTANDARDCASE") else None),
            # ── 几何标高（楼板）：SweptSolid 挤出位置 Z / 楼层 Elevation 兜底 ──
            "geom_elevation": (_extract_slab_elevation(prod)
                               if ifc_class.upper() == "IFCSLAB" else None),
            # ── 是否有几何表示（有几何 → 信息在模型里，规则不应判"未定义"）──
            "has_geometry": bool(getattr(prod, "Representation", None)),
            # ── 兼容字段（下游：BIM 规则轨 / drawing2bim 感知 / 前端展示）──
            "type": _cn_name(ifc_class),
            "ifc_type": ifc_class.upper(),
            "name": name,
            "global_id": global_id,
        })

    # ── 空间 ──
    spaces = []
    for sp in model.by_type("IFCSPACE"):
        spaces.append({
            "name": _safe_get_attr(sp, "Name") or "未命名",
            "long_name": _safe_get_attr(sp, "LongName") or "",
        })

    # ── 建筑信息 ──
    building_info = {}
    for b in model.by_type("IFCBUILDING"):
        building_info = {"name": _safe_get_attr(b, "Name") or "未命名"}
        break

    # ── 全局属性样本（BIM 规则轨"无属性集"检查用；全量统计）──
    props = []
    for ps in model.by_type("IFCPROPERTYSET")[:100]:
        for pr in ps.HasProperties or []:
            val = getattr(pr, "NominalValue", None)
            v = val.wrappedValue if val else None
            props.append({"set": ps.Name or "", "name": getattr(pr, "Name", ""),
                          "value": str(v) if v is not None else ""})

    # ── 全量统计（不再 [:50] 截断）──
    elements_count = dict(Counter(e["type"] for e in elements))

    logger.info("ifc_parser.parsed", path=ifc_path, schema=model.schema,
                elements=len(elements), spaces=len(spaces))
    return {
        "ifc_version": model.schema,
        "project_info": project_info,
        "schema": model.schema,
        "building": building_info,
        "elements_count": elements_count,
        "elements": elements,
        "spaces": spaces,
        "properties": props[:50],
        "total_elements": len(elements),
        "total_spaces": len(spaces),
    }


async def parse_ifc(ifc_path: str) -> dict:
    """异步解析 IFC（线程池）"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_parse, ifc_path)
