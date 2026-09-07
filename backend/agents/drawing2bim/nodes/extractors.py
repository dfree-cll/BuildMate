"""drawing2bim 结构化提取器（确定性文本 → 黄金基准元素）

第一期：基于约定的"构件表"行格式做正则提取（纯函数，可单测）。
支持的行格式（中英文冒号均可）：
    [类型] 名称: <name>; 厚度: <thickness>; 标高: <elevation>; 材料: <material>

类型映射：墙/柱/梁/板(楼板)/门/窗/楼梯/屋顶 → IFC 类型。
多模态感知（OCR/图面理解）接入后，此提取器保留为文本层兜底通道。
"""
import re
import uuid

from backend.core.logger import get_logger

logger = get_logger(__name__)

_TYPE_MAP = {
    "墙": "IFCWALL", "墙体": "IFCWALL",
    "柱": "IFCCOLUMN", "柱子": "IFCCOLUMN",
    "梁": "IFCBEAM",
    "板": "IFCSLAB", "楼板": "IFCSLAB",
    "门": "IFCDOOR",
    "窗": "IFCWINDOW", "窗户": "IFCWINDOW",
    "楼梯": "IFCSTAIR",
    "屋顶": "IFCROOF", "屋面": "IFCROOF",
}

# 行格式：[类型] 名称: xx; 厚度: xx; 标高: xx; 材料: xx
# 值捕获组禁止跨行（[^;；\n]），避免贪婪吞掉下一行
_LINE_RE = re.compile(
    r"\[(?P<type>[^\]]+)\]\s*名称\s*[:：]\s*(?P<name>[^;；\n]+)"
    r"(?:\s*[;；]\s*厚度\s*[:：]\s*(?P<thickness>[^;；\n]+))?"
    r"(?:\s*[;；]\s*标高\s*[:：]\s*(?P<elevation>[^;；\n]+))?"
    r"(?:\s*[;；]\s*材料\s*[:：]\s*(?P<material>[^;；\n]+))?"
)


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except (ValueError, TypeError):
        return None


def extract_baseline_from_text(text: str) -> list[dict]:
    """从图纸文本中提取构件清单 → 黄金基准元素列表（确定性，无 LLM）"""
    elements: list[dict] = []
    for m in _LINE_RE.finditer(text or ""):
        raw_type = m.group("type").strip()
        ifc_type = _TYPE_MAP.get(raw_type)
        if not ifc_type:
            continue  # 未知类型跳过（不猜）

        props: dict = {}
        thickness = _to_float(m.group("thickness"))
        elevation = _to_float(m.group("elevation"))
        if thickness is not None:
            props["Thickness"] = thickness
        if elevation is not None:
            props["Elevation"] = elevation
        material = (m.group("material") or "").strip()
        if material:
            props["Material"] = material

        elements.append({
            "element_id": f"draw-{uuid.uuid5(uuid.NAMESPACE_DNS, m.group(0).strip()).hex[:12]}",
            "ifc_type": ifc_type,
            "name": m.group("name").strip(),
            "properties": props,
            "confidence": 0.85,   # 文本层确定性提取，置信度固定偏高
            "source": "drawing_text",
            "review_status": "pending",
        })
    return elements
