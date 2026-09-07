"""ModelingEngine：建模引擎（构件提取 / 轴网检测 / 柱墙生成——LLM 决策+规则计算）

薄封装 backend.engines.ifc_to_json（LLM 决策层 + 规则执行层）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ModelingEngine:
    """建模引擎——统一入口：IFC -> model.json（LLM 决策 + 规则计算）"""

    @staticmethod
    def build(ifc_path: str, max_walls: int = 300, max_cols: int = 60,
              strategy: dict | None = None) -> dict:
        """同步版（strategy 可空——默认参数）"""
        from backend.engines.ifc_to_json import build_model_json
        return build_model_json(ifc_path, max_walls=max_walls, max_cols=max_cols,
                                strategy=strategy)

    @staticmethod
    async def build_async(ifc_path: str, max_walls: int = 300,
                          max_cols: int = 60) -> dict:
        """async 版（LLM 决策——自适应任意项目）"""
        from backend.engines.ifc_to_json import build_model_json_async
        return await build_model_json_async(ifc_path, max_walls=max_walls,
                                            max_cols=max_cols)

    @staticmethod
    def extract_summary(ifc_path: str) -> dict:
        """只提取统计摘要（喂 LLM 决策用——轻量）"""
        import ifcopenshell
        import ifcopenshell.geom
        from backend.engines.ifc_to_json import (_extract_project, _extract_all_walls,
                                              _build_summary)
        f = ifcopenshell.open(ifc_path)
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        proj = _extract_project(f)
        kept = _extract_all_walls(f, settings, proj["mm_per_unit"])
        return _build_summary(kept, proj)
