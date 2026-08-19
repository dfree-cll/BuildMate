"""IFC 模型解析服务（BIM 图纸合规审查的数据层）
用 ifcopenshell 提取：构件清单/空间/属性，供合规规则引擎检查
"""
import asyncio

from backend.core.logger import get_logger

logger = get_logger(__name__)


def _sync_parse(ifc_path: str) -> dict:
    """同步解析 IFC 文件：提取构件/空间/属性数据"""
    import ifcopenshell
    model = ifcopenshell.open(ifc_path)

    element_types = {
        "IFCWALL": "墙", "IFCCOLUMN": "柱", "IFCBEAM": "梁", "IFCSLAB": "楼板",
        "IFCDOOR": "门", "IFCWINDOW": "窗", "IFCSTAIR": "楼梯", "IFCROOF": "屋顶",
    }
    elements = []
    for ifc_type, cn_name in element_types.items():
        for o in model.by_type(ifc_type)[:50]:
            elements.append({
                "type": cn_name,
                "name": getattr(o, "Name", None) or "未命名",
                "global_id": getattr(o, "GlobalId", ""),
                "ifc_type": ifc_type,
            })

    spaces = []
    for sp in model.by_type("IFCSPACE"):
        spaces.append({
            "name": getattr(sp, "Name", None) or "未命名",
            "long_name": getattr(sp, "LongName", None) or "",
        })

    building_info = {}
    for b in model.by_type("IFCBUILDING"):
        building_info = {"name": getattr(b, "Name", None) or "未命名"}
        break

    props = []
    for ps in model.by_type("IFCPROPERTYSET")[:30]:
        for pr in ps.HasProperties or []:
            val = getattr(pr, "NominalValue", None)
            v = val.wrappedValue if val else None
            props.append({"set": ps.Name or "", "name": getattr(pr, "Name", ""), "value": str(v) if v is not None else ""})

    return {
        "schema": model.schema,
        "building": building_info,
        "elements_count": {t: sum(1 for e in elements if e["type"] == t) for t in element_types.values()},
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
