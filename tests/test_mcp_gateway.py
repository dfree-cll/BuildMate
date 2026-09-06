"""MCP 工具网关单元测试：ACL 拦截 / 参数校验 / 超时 / 网络错误 / 成功解析"""
import httpx
import pytest

from backend.config import get_settings
from backend.core.exceptions import (
    MCPToolDenied, MCPInvalidParams, MCPToolTimeout, MCPNetworkError,
)
from backend.mcp import client as gw

_URL = "http://127.0.0.1:9"   # 仅作标识，httpx 已被 fake 替换


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    resp: _FakeResp | None = None
    exc: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        if _FakeClient.exc:
            raise _FakeClient.exc
        return _FakeClient.resp


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    _FakeClient.resp = None
    _FakeClient.exc = None
    monkeypatch.setattr(gw.httpx, "AsyncClient", _FakeClient)
    yield
    get_settings.cache_clear()


def _ok_resp():
    return _FakeResp({"result": {"content": [{"text": '{"a": 1}'}]}})


# ── ACL ────────────────────────────────────────────────────────────────────

async def test_acl_denied_for_user(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_ACL", '{"user": ["search_knowledge_base"]}')
    get_settings.cache_clear()
    with pytest.raises(MCPToolDenied):
        await gw.call_mcp_tool(_URL, "web_search", {"query": "x"}, role="user")


async def test_acl_admin_wildcard(monkeypatch):
    _FakeClient.resp = _ok_resp()
    monkeypatch.setenv("MCP_TOOL_ACL", '{"admin": ["*"]}')
    get_settings.cache_clear()
    result = await gw.call_mcp_tool(_URL, "any_tool", {"query": "x"}, role="admin")
    assert result == [{"a": 1}]


async def test_acl_none_role_passes():
    """role=None 视为系统内部调用，不查 ACL"""
    _FakeClient.resp = _ok_resp()
    result = await gw.call_mcp_tool(_URL, "web_search", {"query": "x"})
    assert result == [{"a": 1}]


async def test_default_acl_allows_user_web_search():
    """默认 ACL 表：user 可调用 web_search"""
    _FakeClient.resp = _ok_resp()
    result = await gw.call_mcp_tool(_URL, "web_search", {"query": "x"}, role="user")
    assert result == [{"a": 1}]


async def test_invalid_acl_config_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_ACL", "not-a-json")
    get_settings.cache_clear()
    _FakeClient.resp = _ok_resp()
    result = await gw.call_mcp_tool(_URL, "web_search", {"query": "x"}, role="user")
    assert result == [{"a": 1}]


# ── 参数校验 ────────────────────────────────────────────────────────────────

async def test_missing_required_param():
    with pytest.raises(MCPInvalidParams):
        await gw.call_mcp_tool(_URL, "web_search", {})


# ── 超时 / 网络 ─────────────────────────────────────────────────────────────

async def test_timeout_wrapped():
    _FakeClient.exc = httpx.ConnectTimeout("slow")
    with pytest.raises(MCPToolTimeout):
        await gw.call_mcp_tool(_URL, "web_search", {"query": "x"})


async def test_network_error_wrapped():
    _FakeClient.exc = httpx.ConnectError("unreachable")
    with pytest.raises(MCPNetworkError):
        await gw.call_mcp_tool(_URL, "web_search", {"query": "x"})


# ── 成功路径与超时解析 ──────────────────────────────────────────────────────

async def test_registry_timeout_used_when_not_specified(monkeypatch):
    captured = {}
    real_fake = _FakeClient

    class _CaptureClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            super().__init__(*args, **kwargs)

    _FakeClient.resp = _ok_resp()
    monkeypatch.setattr(gw.httpx, "AsyncClient", _CaptureClient)
    await gw.call_mcp_tool(_URL, "web_search", {"query": "x"})
    assert captured["timeout"] == gw.TOOL_REGISTRY["web_search"]["timeout"]
    monkeypatch.setattr(gw.httpx, "AsyncClient", real_fake)


# ── IFC 解析工具 ────────────────────────────────────────────────────────────

async def test_ifc_missing_required_param():
    with pytest.raises(MCPInvalidParams):
        await gw.call_mcp_tool(_URL, "parse_ifc_model", {})


async def test_ifc_default_acl_allows_user():
    """默认 ACL：user 可调用 parse_ifc_model"""
    _FakeClient.resp = _ok_resp()
    result = await gw.call_mcp_tool(_URL, "parse_ifc_model", {"ifc_path": "/tmp/x.ifc"}, role="user")
    assert result == [{"a": 1}]
