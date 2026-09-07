"""IFC 生成器：黄金基准 JSON → IFC4 模型（IfcOpenShell）

对应架构图 R 节点（IFC生成Agent）：
- 结构树：IfcProject → IfcSite → IfcBuilding → IfcBuildingStorey
- 构件：按 ifc_type 创建 Ifc* 实体，GlobalId = element_id（差异比对可追溯）
- 属性：properties → IfcPropertySet（Pset_<IFC类型>），IfcRelDefinesByProperties 关联

设计约定：
- 生成是确定性的（同基准 → 同结构），供差异比对闭环使用
- 几何放置（坐标/尺寸）第一期不实现，仅生成语义结构
"""
import asyncio
import os

from backend.core.logger import get_logger

logger = get_logger(__name__)

# 基准 ifc_type → IFC 实体类名（大写 IFCWALL → IfcWall）
_VALID_TYPES = {
    "IFCWALL", "IFCCOLUMN", "IFCBEAM", "IFCSLAB",
    "IFCDOOR", "IFCWINDOW", "IFCSTAIR", "IFCROOF",
}

# IR type → ifc_type 兜底（json2rvt 写出的 model.json 只有 type 无 ifc_type）
_IR_TYPE_TO_IFC = {"Wall": "IFCWALL", "Column": "IFCCOLUMN", "Beam": "IFCBEAM",
                   "Floor": "IFCSLAB", "Slab": "IFCSLAB"}


def _sync_generate(baseline: list[dict], out_path: str) -> dict:
    """同步生成 IFC 文件（线程池运行）"""
    import ifcopenshell

    model = ifcopenshell.file(schema="IFC4")

    # ── 结构树 ──
    project = model.create_entity("IfcProject", Name="BuildMate 生成项目")
    site = model.create_entity("IfcSite", Name="Site-1",
                               CompositionType="ELEMENT")
    building = model.create_entity("IfcBuilding", Name="Building-1",
                                   CompositionType="ELEMENT")
    storey = model.create_entity("IfcBuildingStorey", Name="Level-1",
                                 CompositionType="ELEMENT")

    model.create_entity("IfcRelAggregates", RelatingObject=project,
                        RelatedObjects=[site])
    model.create_entity("IfcRelAggregates", RelatingObject=site,
                        RelatedObjects=[building])
    model.create_entity("IfcRelAggregates", RelatingObject=building,
                        RelatedObjects=[storey])

    # ── 构件 ──
    written = 0
    skipped = 0
    skipped_types: dict[str, int] = {}
    contained = []
    for elem in baseline:
        ifc_type = (elem.get("ifc_type") or "").upper()
        if not ifc_type:
            # 兜底：IR 只有 type（Wall/Column/...）→ 映射 ifc_type
            ifc_type = _IR_TYPE_TO_IFC.get(elem.get("type") or "", "")
        if ifc_type not in _VALID_TYPES:
            skipped += 1
            key = elem.get("ifc_type") or elem.get("type") or "(none)"
            skipped_types[key] = skipped_types.get(key, 0) + 1
            continue
        # IFCWALL → IfcWall（前缀 Ifc + 类型名首字母大写）
        entity_cls = "Ifc" + ifc_type[3:].capitalize()
        element = model.create_entity(
            entity_cls,
            GlobalId=elem.get("element_id") or ifcopenshell.guid.new(),
            Name=elem.get("name") or "未命名",
        )
        contained.append(element)

        # ── 属性集 ──
        props = elem.get("properties") or {}
        if props:
            properties = [
                # IFC 简单类型（IfcLabel 等）用位置参数传值
                model.create_entity(
                    "IfcPropertySingleValue", Name=str(k),
                    NominalValue=model.create_entity("IfcLabel", str(v)))
                for k, v in props.items()
            ]
            pset = model.create_entity(
                "IfcPropertySet", Name=f"Pset_{ifc_type}",
                HasProperties=properties)
            model.create_entity("IfcRelDefinesByProperties",
                                RelatedObjects=[element],
                                RelatingPropertyDefinition=pset)
        written += 1

    model.create_entity("IfcRelContainedInSpatialStructure",
                        RelatingStructure=storey, RelatedElements=contained)

    model.write(out_path)
    logger.info("ifc_generator.written", path=out_path, elements=written,
                skipped=skipped, skipped_types=skipped_types or None)
    return {"ifc_path": out_path, "elements_written": written,
            "elements_skipped": skipped, "skipped_types": skipped_types}


async def generate_ifc_from_baseline(baseline: list[dict], out_path: str) -> dict:
    """异步生成入口（线程池，不阻塞事件循环）"""
    return await asyncio.to_thread(_sync_generate, baseline, out_path)
