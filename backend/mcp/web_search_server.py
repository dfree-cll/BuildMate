"""联网搜索 MCP Server
工具：web_search(query, max_results) — DuckDuckGo 免费搜索（无需 key）
"""
import asyncio

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="BuildMate-WebSearch",
    stateless_http=True,
    json_response=True,
)


async def _search_duckduckgo(query: str, max_results: int) -> list[dict]:
    from duckduckgo_search import DDGS

    def _sync() -> list[dict]:
        results = []
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "snippet": r.get("body", "")[:300],
                    })
        except Exception:
            pass
        return results

    return await asyncio.to_thread(_sync)


@mcp.tool()
async def web_search(
    query: str,
    max_results: int = 5,
) -> list[dict]:
    """联网搜索最新信息（知识库未覆盖时兜底，对标行业范式）
    Args:
        query: 搜索关键词
        max_results: 返回条数
    """
    return await _search_duckduckgo(query, max_results)


if __name__ == "__main__":
    import uvicorn
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    port = int(os.getenv("WS_MCP_PORT", "8002"))
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=port)
