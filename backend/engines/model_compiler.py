"""ModelCompiler：模型编译器（model.json -> Revit 建模指令描述）

职责：把中间表示（ModelIR）翻译成 Revit 可执行的结构化指令——
json2rvt（Revit 内脚本）按指令分步执行（轴网 -> 柱 -> 梁 -> 墙 -> 视图 -> 保存）。
Python 侧负责"编译"（指令生成 + 顺序编排），Revit 侧负责"执行"（API 调用）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ModelCompiler:
    """模型编译器——统一入口：model.json -> Revit 指令"""

    @staticmethod
    def compile(data: dict) -> dict:
        """编译中间表示 -> Revit 建模指令（顺序：轴网->柱->梁->墙->视图）"""
        grid = data.get("grid") or {}
        elems = data.get("model_elements") or []
        views = data.get("views") or []
        # 按人工建模顺序分组
        ordered = []
        for t in ("Column", "Beam", "Wall", "Floor"):
            ordered.extend([e for e in elems if e.get("type") == t])
        instructions = [
            {"step": "grid", "params": grid},
            {"step": "elements", "order": ["Column", "Beam", "Wall", "Floor"],
             "elements": ordered},
            {"step": "join", "params": {"tolerance_ft": 0.5}},
            {"step": "views", "views": views},
            {"step": "validate", "params": {"position_tol_mm": 100}},
            {"step": "save", "params": {"dir": "rvt_out", "prefix": "json_model_"}},
        ]
        return {
            "compiler_version": "1.0",
            "workflow": instructions,
            "element_count": len(ordered),
            "grid": "%dx%d" % (len(grid.get("x_axes", [])), len(grid.get("y_axes", []))),
        }

    @staticmethod
    def describe(data: dict) -> str:
        """人类可读的建模计划（给前端/演示用）"""
        c = ModelCompiler.compile(data)
        lines = [
            "自动建模计划（人工建模流程）:",
            "  1. 轴网: %s" % c["grid"],
            "  2. 构件: %d 个（柱->梁->墙->板顺序）" % c["element_count"],
            "  3. 连接: 相邻墙自动 join（容差 152mm）",
            "  4. 视图: 三维预览 + 平面",
            "  5. 验证: 生成后读回（数量/位置——PASS/FAIL）",
            "  6. 保存: RVT（时间戳命名）",
        ]
        return "\n".join(lines)
