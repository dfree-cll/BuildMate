"""图纸解析 MCP Server（drawing2bim 感知通道入口，端口 8004）

工具：
- parse_drawing(path)        从图纸文件提取文本层（PDF）
- parse_drawing_vision(path) 多模态视觉提取（图片；需真实多模态 LLM）
- parse_drawing_dxf(path)    DXF 原生矢量解析（第五期，精度最高）

DWG 为私有格式：不直接解析，返回引导用户在 CAD 中另存为 DXF 的提示
（DXF 为公开文档化格式，ezdxf 可可靠解析）。

返回：{"kind": ..., ...} 或 {"error": ...}
变更检测由调用方基于内容做哈希比对。
"""
import asyncio
import os
import sys

from mcp.server.fastmcp import FastMCP
from backend.engines.drawing_guidance import DWG_GUIDANCE

mcp = FastMCP(
    name="BuildMate-DrawingPerception",
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def parse_drawing(path: str) -> dict:
    """解析图纸文件，提取文本层内容

    Args:
        path: 图纸文件的绝对路径（PDF；DWG/DXF 请用专用工具）
    """
    if not os.path.isfile(path):
        return {"error": f"图纸文件不存在: {path}"}

    ext = os.path.splitext(path)[1].lower()
    if ext == ".dwg":
        return {"error": DWG_GUIDANCE, "guidance": "export_dxf"}
    if ext == ".dxf":
        return {"error": "DXF 文件请使用 parse_drawing_dxf 矢量解析工具", "guidance": "use_dxf_tool"}
    if ext != ".pdf":
        return {"error": f"不支持的图纸格式: {ext}（支持 PDF/DXF）"}

    from backend.engines.pdf_parser import _sync_extract_pdf
    try:
        result = await asyncio.to_thread(_sync_extract_pdf, path)
    except Exception as e:
        return {"error": f"图纸解析失败: {type(e).__name__}: {str(e)[:200]}"}
    return {"kind": "pdf", "text": result["raw_text"], "page_count": result["page_count"]}


@mcp.tool()
async def parse_drawing_dxf(path: str) -> dict:
    """DXF 原生矢量解析：读取图层/线实体/块引用，提取构件（第五期，精度最高）

    不转 PDF、不截图，直接读取 CAD 底层矢量数据；
    提取走图层映射 + 平行双线几何推断（置信度分级）。
    Args:
        path: DXF 文件的绝对路径
    """
    if not os.path.isfile(path):
        return {"error": f"图纸文件不存在: {path}"}

    ext = os.path.splitext(path)[1].lower()
    if ext == ".dwg":
        return {"error": DWG_GUIDANCE, "guidance": "export_dxf"}
    if ext != ".dxf":
        return {"error": f"DXF 工具仅支持 .dxf 文件，收到: {ext}"}

    from backend.engines.dxf_parser import parse_dxf
    from backend.agents.drawing2bim.nodes.dxf_extractor import extract_baseline_from_dxf
    try:
        parsed = await parse_dxf(path)
        baseline = extract_baseline_from_dxf(parsed)
        # 设计说明文字：MTEXT 大段优先 + TEXT 标注合并（供规范核查/报告依据）
        texts = parsed.get("texts", [])
        notes_parts = []
        for t in texts:
            txt = (t.get("text") or "").strip()
            if len(txt) >= 8:   # 短标注（标高/编号）不视为设计说明
                notes_parts.append(f"[{t.get('layer','')}] {txt}")
        design_notes = "\n".join(notes_parts)[:6000]
    except Exception as e:
        return {"error": f"DXF 解析失败: {type(e).__name__}: {str(e)[:200]}"}
    return {"kind": "dxf", "baseline": baseline, "elements_count": len(baseline),
            "layers": [l["name"] for l in parsed.get("layers", [])],
            "texts_count": len(texts), "design_notes": design_notes,
            "wall_extraction": parsed.get("wall_extraction") or {}}


@mcp.tool()
async def parse_drawing_vision(path: str) -> dict:
    """多模态视觉提取：从图纸图片中提取结构化构件信息（第四期）

    支持 PNG/JPG/WEBP；需配置真实多模态 LLM（Mock 模式返回空结果）。
    Args:
        path: 图纸图片文件的绝对路径
    """
    if not os.path.isfile(path):
        return {"error": f"图纸图片不存在: {path}"}

    from backend.core.drawing_vision import extract_baseline_from_image
    try:
        baseline = await extract_baseline_from_image(path)
    except Exception as e:
        return {"error": f"视觉提取失败: {type(e).__name__}: {str(e)[:200]}"}
    return {"kind": "vision", "baseline": baseline, "elements_count": len(baseline)}


if __name__ == "__main__":
    import uvicorn
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    port = int(os.getenv("DRAWING_MCP_PORT", "8004"))
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=port)
