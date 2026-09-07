"""AEC 领域引擎层（架构图落地——统一封装，薄封装现有核心，不重写逻辑）

引擎清单:
  DocumentEngine  文档解析（PDF/DXF/IFC/图片——按扩展名路由）
  ReviewEngine    审查引擎（HR-001~007 规则 + LLM 审查）
  ModelingEngine  建模引擎（构件提取/轴网/柱墙生成——LLM 决策+规则计算）
  ModelIR         中间表示（model.json 数据结构 + 完整性校验）
  ModelCompiler   模型编译器（model.json -> Revit 建模指令描述）
  RevitConnector  Revit 连接器（RVT 输出检测/引导状态）
  Validator       验证引擎（IR 校验——生成前；读回校验在 Revit 内）
"""
from backend.engines.document_engine import DocumentEngine
from backend.engines.model_ir import ModelIR, validate_ir
from backend.engines.validator import Validator

__all__ = [
    "DocumentEngine",
    "ModelIR",
    "validate_ir",
    "Validator",
]
