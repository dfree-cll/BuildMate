"""drawing2bim Agent 状态定义

核心数据结构：黄金基准 JSON（GoldenBaseline）+ 字段级置信度标签。
整条流水线围绕此 State 流转：感知 → 审查 → 融合 → HITL → 输出。
"""
from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class GoldenBaselineElement(TypedDict, total=False):
    """单个构件的黄金基准记录"""
    element_id: str
    ifc_type: str              # IFCWALL / IFCSLAB / ...
    name: str
    properties: dict           # 属性键值对
    confidence: float          # 0.0~1.0，字段级置信度
    source: str                # "perception" / "manual" / "merged"
    review_status: str         # "pending" / "approved" / "rejected" / "auto_passed"


class ComplianceViolation(TypedDict, total=False):
    """合规违规条目"""
    rule_id: str               # 规则编号（硬轨）或 "llm_soft"（软轨）
    severity: str              # "critical" / "warning" / "info"
    track: str                 # "hard" / "soft"
    element_id: str
    description: str
    confidence: float          # 软轨必填，硬轨固定 1.0
    explanation: str           # 软轨 LLM 生成的解释
    suggestion: str            # 修正建议


from backend.application.agent_memory import MemoryState


class Drawing2BimState(MemoryState):
    # ── 会话基础 ──
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    tenant_id: str
    session_id: str
    role: str                  # 用户角色（MCP 网关工具 ACL 用）
    input_text: str            # 用户指令/上传描述

    # ── 感知阶段 ──
    drawing_source: str        # "drawing_text" / "drawing_vision" / "drawing_dxf" / "ifc_existing" / "manual" / "demo" / "unsupported"
    drawing_path: str          # 可选：图纸文件路径（DXF/PDF/图片，经 MCP 网关解析）
    drawing_review_key: str    # 图纸标识（变更检测的哈希 key，默认用 session_id）
    drawing_hash: str          # 本次图纸内容哈希
    change_status: str         # "first" / "changed" / "unchanged"（变更检测结果）
    perception_mode: str       # "text" / "vision" / "auto"（默认 auto：DXF 优先，其次视觉，失败回退文本）
    perception_error: str      # 感知错误（非空时下游短路，输出明确指引，禁止静默回退假数据）
    drawing_notes: str         # 图纸设计说明文字（TEXT/MTEXT 合并；审查/报告依据）
    ifc_path: str              # 可选：已有 IFC 文件路径（经 MCP 网关解析生成基准）
    golden_baseline: list[GoldenBaselineElement]   # 黄金基准 JSON
    baseline_version: int      # 版本号（增量合并时递增）

    # ── 几何质量闸门（第六期）──
    geometry_report: dict      # 闸门结果 {verdict, gate_blocked, checks, stats}
    gate_blocked: bool         # 闸门失败标志（失败 → HITL 冻结）
    dxf_grid: dict             # DXF 自适应轴网 {x_axes, y_axes}
    dxf_levels: list           # DXF 自适应标高
    dxf_adaptive_meta: dict    # 自适应提取元信息（柱实体数/旋转角等）

    # ── 合规审查阶段 ──
    hard_violations: list[ComplianceViolation]     # 硬轨：确定性违规
    soft_violations: list[ComplianceViolation]     # 软轨：LLM 疑似问题
    # ── 审查依据（报告证据区）──
    rule_stats: dict           # 每条规则检查数/命中数 {HR-001: {desc, severity, checked, hit}}
    element_stats: dict        # 构件类型分布 {IFCWALL: 1900, ...}
    slab_categories: dict      # 楼板分类 {element_id: structural/finishing/landing}
    slab_finishing_count: int  # 面层板数量（不参与结构规则）
    merged_report: dict                            # 融合后的合规报告

    # ── HITL 阶段 ──
    hitl_required: bool        # 是否需要人工确认
    hitl_items: list[dict]     # 待确认条目列表
    hitl_decision: str         # "approved" / "rejected" / "" (未决)

    # ── 输出 ──
    final_baseline: list[GoldenBaselineElement]    # 最终黄金基准（附审查痕迹）
    compliance_report: dict                        # 最终合规报告
    content: str                                   # 给用户的文本摘要
    fallback_used: bool
    structured_output: Optional[dict]

    # ── IFC 生成闭环（第三期）──
    generate_ifc: bool                             # 是否生成 IFC（默认 False）
    ifc_generation: dict                           # 生成结果：status/ifc_path/diffs/iterations
