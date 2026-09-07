"""drawing2bim 感知错误短路节点（第五期）

当 perception_error 非空时（如 DWG 格式、DXF 零构件识别），
图跳过审查链路直达本节点：输出明确错误指引，禁止静默回退假数据。
"""
from backend.agents.drawing2bim.state import Drawing2BimState
from backend.core.logger import get_logger

logger = get_logger(__name__)


async def error_report_node(state: Drawing2BimState) -> dict:
    """感知错误收尾：verdict=error，content 携带可操作的指引"""
    err = state.get("perception_error", "图纸感知失败")
    logger.info("perception.error_report", error=err[:120])
    return {
        "compliance_report": {"verdict": "error", "error": err},
        "content": f"图纸感知失败：{err}",
        "fallback_used": False,
        "final_baseline": [],
        "structured_output": {"verdict": "error", "perception_error": err},
    }
