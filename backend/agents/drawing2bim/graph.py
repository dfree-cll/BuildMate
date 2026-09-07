"""drawing2bim Agent 图装配

perception → compliance_review（双轨）→ report_merge → hitl_review → finalize
  → (条件：generate_ifc 且审查通过) → ifc_generate → END

- hitl_review 在 verdict != pass 时 interrupt 冻结，等待 Command(resume=decision)
- ifc_generate 仅在请求生成（generate_ifc=True）且 verdict ∈ {pass, approved} 时触发
- checkpointer 使用独立命名空间 "drawing2bim"
"""
from langgraph.graph import StateGraph, START, END

from backend.agents.drawing2bim.state import Drawing2BimState
from backend.agents.drawing2bim.nodes.perception import perception_node
from backend.agents.drawing2bim.nodes.geometry_gate import geometry_gate_node
from backend.agents.drawing2bim.nodes.compliance import compliance_review_node
from backend.agents.drawing2bim.nodes.report_merge import report_merge_node
from backend.agents.drawing2bim.nodes.hitl_review import hitl_review_node
from backend.agents.drawing2bim.nodes.finalize import finalize_node
from backend.agents.drawing2bim.nodes.ifc_generate import ifc_generate_node
from backend.agents.drawing2bim.nodes.error_report import error_report_node
from backend.core.memory import get_memory_saver
from backend.application.agent_memory import memory_nodes


def _route_after_perception(state: Drawing2BimState) -> str:
    """感知错误短路：perception_error 非空 → 直达错误收尾（禁止静默回退假数据）"""
    return "error_report" if state.get("perception_error") else "geometry_gate"


def _route_after_gate(state: Drawing2BimState) -> str:
    """质量闸门：失败 → HITL 冻结等人工（禁止带病进合规/建模）"""
    if state.get("gate_blocked"):
        return "hitl_review"
    return "compliance_review"


def _route_after_finalize(state: Drawing2BimState) -> str:
    """条件路由：请求生成 IFC 且审查通过 → 进生成闭环；否则直接结束"""
    if not state.get("generate_ifc"):
        return "end"
    verdict = (state.get("compliance_report") or {}).get("verdict", "")
    if verdict in ("pass", "approved"):
        return "ifc_generate"
    return "end"


def build_drawing2bim_graph():
    builder = StateGraph(Drawing2BimState)
    load_memory, save_memory = memory_nodes("drawing2bim")
    builder.add_node("load_memory", load_memory)
    builder.add_node("save_memory", save_memory)
    builder.add_node("perception", perception_node)
    builder.add_node("error_report", error_report_node)
    builder.add_node("geometry_gate", geometry_gate_node)
    builder.add_node("compliance_review", compliance_review_node)
    builder.add_node("report_merge", report_merge_node)
    builder.add_node("hitl_review", hitl_review_node)
    builder.add_node("finalize", finalize_node)
    builder.add_node("ifc_generate", ifc_generate_node)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "perception")
    builder.add_conditional_edges(
        "perception", _route_after_perception,
        {"geometry_gate": "geometry_gate", "error_report": "error_report"},
    )
    builder.add_conditional_edges(
        "geometry_gate", _route_after_gate,
        {"compliance_review": "compliance_review", "hitl_review": "hitl_review"},
    )
    builder.add_edge("error_report", "save_memory")
    builder.add_edge("compliance_review", "report_merge")
    builder.add_edge("report_merge", "hitl_review")
    builder.add_edge("hitl_review", "finalize")
    builder.add_conditional_edges(
        "finalize", _route_after_finalize,
        {"ifc_generate": "ifc_generate", "end": "save_memory"},
    )
    builder.add_edge("ifc_generate", "save_memory")
    builder.add_edge("save_memory", END)

    checkpointer = get_memory_saver("drawing2bim")
    return builder.compile(checkpointer=checkpointer)
