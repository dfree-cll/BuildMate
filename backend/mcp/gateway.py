"""工具层（MCP-Gateway）——统一工具注册表 + 统一调用入口

职责：
  - 注册现有能力为统一工具（IFC/DXF/PDF 解析、RAG 检索、Revit 触发、建材价格、IFC 生成）
  - call(name, **params)：任意 Agent/前端通过统一入口调用工具
  - list()：列出可用工具（供前端展示/调试）
说明：工具直接调 backend.core 函数（同步/线程池）——不依赖 8003/8004 进程；
      8003/8004 保留（现有 Agent 的 MCP 通道）——网关是统一层（可选接入）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

ToolFn = Callable[..., Any] | Callable[..., Coroutine[Any, Any, Any]]


class ToolGateway:
    """统一工具网关——注册表 + 调用"""

    def __init__(self):
        self._tools: dict[str, dict] = {}

    def register(self, name: str, fn: ToolFn, desc: str = "",
                 is_async: bool = False, category: str = "parse") -> None:
        self._tools[name] = {"fn": fn, "desc": desc, "is_async": is_async,
                             "category": category}
        logger.info("tool_gateway.register: %s (%s)", name, category)

    def list(self) -> list[dict]:
        return [{"name": n, "desc": t["desc"], "category": t["category"]}
                for n, t in sorted(self._tools.items())]

    def has(self, name: str) -> bool:
        return name in self._tools

    async def call(self, name: str, **params) -> dict:
        tool = self._tools.get(name)
        if not tool:
            return {"status": "error", "error": f"未知工具: {name}",
                    "available": [t["name"] for t in self.list()]}
        try:
            if tool["is_async"]:
                result = await tool["fn"](**params)
            else:
                result = await asyncio.to_thread(tool["fn"], **params)
            return {"status": "ok", "tool": name, "result": result}
        except Exception as ex:
            logger.warning("tool_gateway.call_failed: %s %s", name, str(ex)[:150])
            return {"status": "error", "tool": name, "error": f"{type(ex).__name__}: {str(ex)[:200]}"}


# 全局单例
gateway = ToolGateway()


def _register_builtins():
    """注册现有能力为统一工具（幂等——重复注册跳过）"""
    if gateway.has("parse_ifc"):
        return

    # 文档解析
    async def parse_ifc(path: str) -> dict:
        from backend.engines.ifc_parser import parse_ifc
        return await parse_ifc(path)

    async def parse_dxf(path: str) -> dict:
        from backend.engines.dxf_parser import parse_dxf
        return await parse_dxf(path)

    async def parse_pdf(path: str) -> dict:
        from backend.engines.pdf_parser import extract_pdf_text
        return await extract_pdf_text(path)

    # RAG 检索
    async def rag_search(
        query: str,
        tenant_id: str = "tenant_default",
        project_id: str | None = None,
        scope: str = "tenant",
        top_k: int = 5,
    ) -> dict:
        """Evidence-first RAG lookup through the same v2 service used by APIs.

        This uses the shared v2 RAG service and keeps tool calls tenant-scoped
        even for internal Agent invocations.
        """
        import uuid

        from backend.domain.contracts import RequestContext
        from backend.rag.contracts import KnowledgeScope, KnowledgeSearchRequest
        from backend.rag.service import get_rag_service

        trace_id = uuid.uuid4().hex
        context = RequestContext(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id="system:mcp-gateway",
            role="system",
            trace_id=trace_id,
            correlation_id=trace_id,
        )
        request = KnowledgeSearchRequest(
            query=query,
            tenant_id=tenant_id,
            project_id=project_id,
            scope=KnowledgeScope(scope),
            top_k=top_k,
        )
        hits, retrieval_run_id = await get_rag_service().search(context, request)
        return {
            "retrieval_run_id": retrieval_run_id,
            "hits": [hit.model_dump(mode="json") for hit in hits],
        }

    # Revit 写入必须经过 v2 BuildRun 的静态校验、Dry-run 和人工批准。
    # 保留旧工具名仅为兼容已有调用方，但绝不再直接触发 Revit。
    def trigger_revit(**_params: Any) -> dict:
        return {
            "status": "error",
            "code": "approval_required",
            "message": "Revit 写入已迁移到 /api/v2/builds/{build_id}/approve；请先完成静态校验、Dry-run 和人工审批。",
        }

    # 建材价格查询
    async def material_price(material: str, market_hint: str = "") -> dict:
        from backend.core.material_prices import query_prices
        rows = await query_prices(material, market_hint)
        return {"material": material, "rows": rows[:5]}

    # IFC 生成（图纸审查产物 → IFC）
    async def generate_ifc(baseline: list[dict], out_path: str) -> dict:
        from backend.engines.ifc_generator import generate_ifc_from_baseline
        return await generate_ifc_from_baseline(baseline, out_path)

    gateway.register("parse_ifc", parse_ifc, "解析 IFC 文件（构件/属性/坐标）", is_async=True, category="parse")
    gateway.register("parse_dxf", parse_dxf, "解析 DXF 图纸（图层/文字/实体）", is_async=True, category="parse")
    gateway.register("parse_pdf", parse_pdf, "解析 PDF（文本/OCR）", is_async=True, category="parse")
    gateway.register("rag_search", rag_search, "知识库检索（兼容名）", is_async=True, category="rag")
    gateway.register("knowledge.search", rag_search, "证据型知识检索", is_async=True, category="rag")
    gateway.register("trigger_revit", trigger_revit, "Revit 写入（仅供 v2 审批后的内部调用）", category="revit")
    gateway.register("material_price", material_price, "建材价格查询", is_async=True, category="data")
    gateway.register("generate_ifc", generate_ifc, "从图纸基线生成 IFC", is_async=True, category="generate")


_register_builtins()
