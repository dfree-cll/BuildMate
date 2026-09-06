"""多模态图纸视觉提取器（第四期：替代文本约定格式提取）

核心能力：将图纸图片（PNG/JPG/PDF 首页截图）送入多模态 LLM，
输出结构化构件列表 JSON → 转为黄金基准元素。

离线兼容：Mock 模式下 LLM 不识别图片内容，回退到文本层提取器兜底；
生产环境配置真实多模态模型（如 Qwen-VL / GPT-4o）后自动启用视觉通道。
"""
import base64
import json
import os
from pathlib import Path

from backend.core.llm_factory import get_llm
from backend.core.llm_text import msg_text, parse_json_loose
from backend.core.logger import get_logger
from langchain_core.messages import HumanMessage

logger = get_logger(__name__)

VISION_EXTRACTION_PROMPT = """\
你是建筑行业图纸识别专家。请分析以下建筑图纸图片，提取所有可见的建筑构件信息。

【输出要求】
以 JSON 数组返回构件列表，每项包含：
- type: 构件类型（墙/柱/梁/板/门/窗/楼梯/屋顶，中文）
- name: 构件名称或编号（如图中标注）
- thickness: 厚度数值（mm，仅墙/板有则填，否则省略）
- elevation: 标高数值（m，仅板/梁有则填，否则省略）
- material: 材料名称（如图中标注，无则省略）

如果图中无法识别任何构件，返回空数组 []。
只返回 JSON，不要附加说明文字。
"""

# 支持的图片格式
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _encode_image_base64(image_path: str) -> str:
    """读取图片文件并编码为 base64 data URI"""
    ext = Path(image_path).suffix.lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp"}.get(ext.lstrip("."), "image/png")
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _vision_result_to_baseline(items: list[dict]) -> list[dict]:
    """LLM 视觉提取结果 → 黄金基准元素列表"""
    import uuid
    from backend.agents.drawing2bim.nodes.extractors import _TYPE_MAP

    elements: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_type = (item.get("type") or "").strip()
        ifc_type = _TYPE_MAP.get(raw_type)
        if not ifc_type:
            continue

        props: dict = {}
        thickness = item.get("thickness")
        elevation = item.get("elevation")
        material = (item.get("material") or "").strip()
        if thickness is not None:
            try:
                props["Thickness"] = float(thickness)
            except (ValueError, TypeError):
                pass
        if elevation is not None:
            try:
                props["Elevation"] = float(elevation)
            except (ValueError, TypeError):
                pass
        if material:
            props["Material"] = material

        name = (item.get("name") or "").strip() or "未命名"
        elements.append({
            "element_id": f"vis-{uuid.uuid5(uuid.NAMESPACE_DNS, json.dumps(item, sort_keys=True)).hex[:12]}",
            "ifc_type": ifc_type,
            "name": name,
            "properties": props,
            "confidence": 0.75,   # 视觉提取置信度低于文本确定性提取
            "source": "drawing_vision",
            "review_status": "pending",
        })
    return elements


async def extract_baseline_from_image(image_path: str) -> list[dict]:
    """多模态视觉提取入口

    Returns:
        黄金基准元素列表；Mock 模式或失败时返回空列表（调用方回退文本提取器）
    """
    if not os.path.isfile(image_path):
        logger.warning("vision.file_not_found", path=image_path)
        return []

    ext = Path(image_path).suffix.lower()
    if ext not in _IMAGE_EXTS:
        logger.warning("vision.unsupported_format", ext=ext)
        return []

    image_uri = _encode_image_base64(image_path)
    message = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": image_uri}},
        {"type": "text", "text": VISION_EXTRACTION_PROMPT},
    ])

    try:
        llm = get_llm("drawing2bim", temperature=0)
        resp = await llm.ainvoke([message])
        raw = msg_text(resp)
    except Exception as e:
        logger.warning("vision.llm_failed", error=str(e)[:120])
        return []

    items = parse_json_loose(raw)
    if not isinstance(items, list):
        logger.info("vision.no_structured_output", raw_preview=raw[:100])
        return []

    baseline = _vision_result_to_baseline(items)
    logger.info("vision.extracted", elements=len(baseline))
    return baseline
