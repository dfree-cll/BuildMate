"""DocumentEngine：文档解析统一入口（PDF / DXF / IFC / 图片——按扩展名路由）

薄封装现有核心：backend.engines.pdf_parser / dxf_parser / ifc_parser
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class DocumentEngine:
    """文档解析引擎——统一入口，按扩展名路由到对应解析器"""

    @staticmethod
    async def parse(path: str, filename: str | None = None) -> dict:
        """解析任意支持的文档（PDF/DXF/IFC/图片/docx）——返回结构化数据"""
        name = (filename or path).lower()
        if name.endswith(".dxf"):
            from backend.engines.dxf_parser import parse_dxf
            return await parse_dxf(path)
        if name.endswith(".ifc"):
            from backend.engines.ifc_parser import parse_ifc
            return await parse_ifc(path)
        if name.endswith((".pdf", ".png", ".jpg", ".jpeg", ".docx", ".doc")):
            from backend.engines.pdf_parser import _sync_parse_document
            return _sync_parse_document(path, name)
        raise ValueError(f"不支持的文档类型: {name}")

    @staticmethod
    async def parse_dxf(path: str) -> dict:
        from backend.engines.dxf_parser import parse_dxf
        return await parse_dxf(path)

    @staticmethod
    async def parse_ifc(path: str) -> dict:
        from backend.engines.ifc_parser import parse_ifc
        return await parse_ifc(path)

    @staticmethod
    async def extract_pdf_text(path: str) -> dict:
        from backend.engines.pdf_parser import extract_pdf_text
        return await extract_pdf_text(path)
