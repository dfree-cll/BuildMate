"""MCP 工具网关客户端（MCP-Gateway）

统一封装对 stateless MCP Server 的工具调用：
- 可配超时：注册表按工具覆盖，默认取 settings.mcp_default_timeout
- 统一异常：超时/网络/HTTP/工具业务错误分型抛出（backend.core.exceptions.MCP*）
- 参数校验：注册表声明必填参数，缺失即拒（MCPInvalidParams）
- 访问控制：按角色查 TOOL_ACL（settings.mcp_tool_acl 覆盖，空用内置默认表）
"""
import json
from typing import Any

import httpx

from backend.config import get_settings
from backend.core.exceptions import (
    MCPToolTimeout, MCPNetworkError, MCPToolError, MCPToolDenied, MCPInvalidParams,
)
from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── 工具注册表：已知 MCP 工具的超时与必填参数（未注册工具仅做 ACL 与默认超时）──
TOOL_REGISTRY: dict[str, dict] = {
    "search_knowledge_base": {"timeout": 15.0, "required": ["query"]},
    "knowledge.search": {"timeout": 15.0, "required": ["query", "tenant_id"]},
    "web_search": {"timeout": 10.0, "required": ["query"]},
    "parse_ifc_model": {"timeout": 60.0, "required": ["ifc_path"]},
    "generate_ifc_model": {"timeout": 60.0, "required": ["baseline", "out_path"]},
    "parse_drawing": {"timeout": 30.0, "required": ["path"]},
    "parse_drawing_vision": {"timeout": 60.0, "required": ["path"]},
    "parse_drawing_dxf": {"timeout": 30.0, "required": ["path"]},
}

# ── 默认工具访问表（settings.mcp_tool_acl 非空时覆盖）；"*" = 该角色全放行 ──
# 角色集对齐 mock_users：admin/buyer/project/reviewer；
# 未在表中的角色回退到 user 基线权限（避免新增角色因漏配被整体卡死）
_ALL_TOOLS = ["search_knowledge_base", "knowledge.search", "web_search", "parse_ifc_model",
              "generate_ifc_model", "parse_drawing", "parse_drawing_vision",
              "parse_drawing_dxf"]

DEFAULT_TOOL_ACL: dict[str, list[str]] = {
    "user": list(_ALL_TOOLS),
    "buyer": list(_ALL_TOOLS),
    "project": list(_ALL_TOOLS),
    "reviewer": list(_ALL_TOOLS),
    "admin": ["*"],
}


def _resolve_acl() -> dict[str, list[str]]:
    raw = get_settings().mcp_tool_acl
    if not raw:
        return DEFAULT_TOOL_ACL
    try:
        acl = json.loads(raw)
        if isinstance(acl, dict):
            return acl
    except json.JSONDecodeError:
        pass
    logger.warning("mcp_gateway.acl_config_invalid_fallback_default", raw=raw[:80])
    return DEFAULT_TOOL_ACL


def check_tool_access(tool_name: str, role: str | None) -> None:
    """按角色校验工具访问权限；role 为 None 视为系统内部调用，放行。
    未在 ACL 表中的角色回退到 user 基线权限（防止新增角色因漏配被整体卡死）。"""
    if role is None:
        return
    acl = _resolve_acl()
    allowed = acl.get(role)
    if allowed is None:
        allowed = acl.get("user", [])
        logger.info("mcp_gateway.unknown_role_fallback_user", role=role)
    if "*" in allowed or tool_name in allowed:
        return
    logger.warning("mcp_gateway.tool_denied", tool=tool_name, role=role)
    raise MCPToolDenied(f"角色 {role} 无权调用工具 {tool_name}", details={"tool": tool_name, "role": role})


def _validate_params(tool_name: str, arguments: dict) -> None:
    spec = TOOL_REGISTRY.get(tool_name)
    if not spec:
        return
    missing = [k for k in spec.get("required", []) if k not in arguments]
    if missing:
        raise MCPInvalidParams(f"工具 {tool_name} 缺少必填参数: {', '.join(missing)}",
                               details={"tool": tool_name, "missing": missing})


def _resolve_timeout(tool_name: str, timeout: float | None) -> float:
    if timeout is not None:
        if timeout <= 0:
            raise MCPInvalidParams("MCP 调用 timeout 必须大于 0", details={"timeout": timeout})
        return timeout
    spec = TOOL_REGISTRY.get(tool_name) or {}
    resolved = spec.get("timeout") or get_settings().mcp_default_timeout
    if resolved <= 0:
        raise MCPInvalidParams("MCP 默认 timeout 必须大于 0", details={"timeout": resolved})
    return resolved


async def call_mcp_tool(server_url: str, tool_name: str, arguments: dict,
                        timeout: float | None = None, role: str | None = None,
                        auth_token: str | None = None) -> Any:
    """调用 stateless MCP Server 的单个工具（JSON-RPC tools/call）

    参数向后兼容：旧调用传 timeout= 仍生效；role 缺省 None（系统调用不查 ACL）。
    """
    check_tool_access(tool_name, role)
    _validate_params(tool_name, arguments)
    eff_timeout = _resolve_timeout(tool_name, timeout)

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    token = auth_token or get_settings().mcp_auth_token
    if token:
        headers["Authorization"] = "Bearer " + token

    try:
        async with httpx.AsyncClient(timeout=eff_timeout, trust_env=False) as client:
            resp = await client.post(server_url + "/mcp", json=payload, headers=headers)
            resp.raise_for_status()
    except httpx.TimeoutException as e:
        logger.warning("mcp_gateway.timeout", tool=tool_name, timeout=eff_timeout)
        raise MCPToolTimeout(f"MCP 工具 {tool_name} 调用超时（{eff_timeout}s）",
                             details={"tool": tool_name}) from e
    except httpx.HTTPStatusError as e:
        raise MCPToolError(f"MCP 工具 {tool_name} 服务端错误: HTTP {e.response.status_code}",
                           details={"tool": tool_name, "status": e.response.status_code}) from e
    except httpx.HTTPError as e:
        raise MCPNetworkError(f"MCP 工具 {tool_name} 网络错误: {type(e).__name__}",
                              details={"tool": tool_name}) from e

    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        raise MCPToolError(
            f"MCP 工具 {tool_name} 返回了无效 JSON",
            details={"tool": tool_name},
        ) from exc
    if not isinstance(data, dict):
        raise MCPToolError(
            f"MCP 工具 {tool_name} 返回了无效响应结构",
            details={"tool": tool_name},
        )
    if "error" in data:
        raise MCPToolError("MCP tool " + tool_name + " error: " + str(data["error"]),
                           details={"tool": tool_name, "rpc_error": data["error"]})
    result = data.get("result")
    if not isinstance(result, dict):
        raise MCPToolError(
            f"MCP 工具 {tool_name} 缺少 result 响应",
            details={"tool": tool_name},
        )
    content = result.get("content", [])
    if not isinstance(content, list):
        raise MCPToolError(
            f"MCP 工具 {tool_name} 返回了无效 content",
            details={"tool": tool_name},
        )
    items = []
    for item in content:
        if isinstance(item, dict) and item.get("text"):
            try:
                items.append(json.loads(item["text"]))
            except (json.JSONDecodeError, TypeError):
                items.append(item["text"])
    return items
