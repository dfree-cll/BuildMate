"""MCP Client"""
import json
from typing import Any

import httpx

from backend.core.logger import get_logger

logger = get_logger(__name__)


async def call_mcp_tool(server_url: str, tool_name: str, arguments: dict,
                        timeout: float = 30.0) -> Any:
    """调用 stateless MCP Server 的单个工具（JSON-RPC tools/call）"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(server_url + "/mcp", json=payload, headers=headers)
        resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise ValueError("MCP tool " + tool_name + " error: " + str(data["error"]))
    content = data.get("result", {}).get("content", [])
    items = []
    for item in content:
        if isinstance(item, dict) and item.get("text"):
            try:
                items.append(json.loads(item["text"]))
            except (json.JSONDecodeError, TypeError):
                items.append(item["text"])
    return items
