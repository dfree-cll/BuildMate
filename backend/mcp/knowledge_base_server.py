"""知识库检索 MCP Server（对标 EduAgent 5.11 H-1）
工具：search_knowledge_base(query, tenant_id, top_k) — 混合检索 + Reranker 精排
"""
import asyncio

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="BuildMate-KnowledgeBase",
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def search_knowledge_base(
    query: str,
    tenant_id: str = "tenant_default",
    top_k: int = 3,
) -> list[dict]:
    """在建筑知识库中做混合检索 + 精排（对标 EduAgent 5.11）
    Args:
        query: 查询文本
        tenant_id: 租户 ID（默认 tenant_default）
        top_k: 返回条数
    """
    from backend.services.vector_store import search
    from backend.core.reranker import rerank_results
    # 召回 8 条 → 精排 top_k
    candidates = await search(query, tenant_id=tenant_id, top_k=8)
    try:
        ranked, confidence = await rerank_results(query, candidates, top_k=top_k)
    except Exception:
        ranked, confidence = candidates[:top_k], (candidates[0]["score"] if candidates else 0)
    return [
        {
            "content": c["content"],
            "source_name": c["metadata"].get("source_name", ""),
            "score": c["score"],
            "confidence": round(confidence, 4),
        }
        for c in ranked
    ]


if __name__ == "__main__":
    import uvicorn
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    port = int(os.getenv("KB_MCP_PORT", "8001"))
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=port)
