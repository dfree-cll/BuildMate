"""IFC 解析 MCP Server（对应架构图 H3：IFC解析-IfcOpenShell服务）

工具：parse_ifc_model(ifc_path) — 提取构件/空间/属性数据
文件通过共享存储路径引用（本地开发用项目 data/bim/，生产挂载卷）。
core/ifc_parser.py 保留本地直调作为回退（非 MCP 场景仍可 import 使用）。
"""
import asyncio
import os
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="BuildMate-IFCParser",
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def parse_ifc_model(ifc_path: str) -> dict:
    """解析 IFC 模型文件，提取构件清单、空间、属性数据

    Args:
        ifc_path: IFC 文件的绝对路径（需服务端可访问；
                  本地开发默认在 data/bim/ 下，生产环境挂载共享卷）
    """
    if not os.path.isfile(ifc_path):
        return {"error": f"IFC 文件不存在: {ifc_path}"}

    # 复用 core 层的同步解析逻辑，避免重复实现
    from backend.engines.ifc_parser import _sync_parse
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _sync_parse, ifc_path)
    except Exception as e:
        return {"error": f"IFC 解析失败: {type(e).__name__}: {str(e)[:200]}"}
    return result


@mcp.tool()
async def generate_ifc_model(baseline: list[dict], out_path: str) -> dict:
    """从黄金基准 JSON 生成 IFC4 模型文件（IFC生成Agent，架构图 R 节点）

    Args:
        baseline: 黄金基准元素列表（含 element_id/ifc_type/name/properties）
        out_path: 输出 IFC 文件的绝对路径（服务端需可写）
    """
    if not isinstance(baseline, list) or not baseline:
        return {"error": "baseline 不能为空"}

    from backend.engines.ifc_generator import generate_ifc_from_baseline
    try:
        result = await generate_ifc_from_baseline(baseline, out_path)
    except Exception as e:
        return {"error": f"IFC 生成失败: {type(e).__name__}: {str(e)[:200]}"}
    return result


if __name__ == "__main__":
    import uvicorn
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    port = int(os.getenv("IFC_MCP_PORT", "8003"))
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=port)
