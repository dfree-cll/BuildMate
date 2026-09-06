"""Revit MCP 客户端（Python 版）——连接 Revit 的 mcp-server-for-revit-python

前置条件：
1. Revit 已装 mcp-servers-for-revit 插件或 pyRevit + revit-mcp-python 扩展
2. pyRevit Settings → Routes Server 已启用（localhost:48884）
3. REVIT_MCP_PYTHON（FastMCP 环境）+ REVIT_MCP_MAIN（MCP 入口脚本）

实现说明：
- 用同步 subprocess.Popen 跑 main.py（stdio MCP 传输，不占端口）
- async 包装走 asyncio.to_thread —— 兼容项目 WindowsSelectorEventLoopPolicy
  （Windows Selector 事件循环不支持 asyncio subprocess，同步实现无此限制）

用法：
    from backend.mcp.revit_client import RevitMCPClient
    async with RevitMCPClient() as revit:
        await revit.test_connection()
        out = await revit.convert_ifc_to_rvt(ifc_path, out_dir)
"""
import json
import logging
import os
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

# Python 版 Revit MCP（pyRevit Routes + FastMCP）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MCP_PYTHON = os.environ.get("REVIT_MCP_PYTHON", sys.executable)
_MCP_MAIN = os.environ.get(
    "REVIT_MCP_MAIN", os.path.join(_PROJECT_ROOT, "workers", "revit_mcp", "main.py"))


class RevitMCPError(Exception):
    """Revit MCP 通信/执行错误"""


class RevitMCPClient:
    """同步 subprocess 核心 + async 包装（兼容 Selector 事件循环）"""

    def __init__(self, timeout: float = 90.0):
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._next_id = 1
        self._read_lock = threading.Lock()

    # ── async 包装 ──────────────────────────────────────────────
    async def __aenter__(self) -> "RevitMCPClient":
        import asyncio
        await asyncio.to_thread(self._start_sync)
        return self

    async def __aexit__(self, *exc) -> None:
        import asyncio
        await asyncio.to_thread(self._stop_sync)

    async def list_tools(self) -> list[str]:
        import asyncio
        return await asyncio.to_thread(self._list_tools_sync)

    async def call_tool(self, name: str, arguments: dict) -> dict:
        import asyncio
        return await asyncio.to_thread(self._call_tool_sync, name, arguments)

    # ── 同步核心（在线程中运行，规避 Selector loop 不支持 subprocess）──
    def _start_sync(self) -> None:
        if not os.path.isfile(_MCP_PYTHON):
            raise RevitMCPError("REVIT_MCP_PYTHON 不可用: %s" % _MCP_PYTHON)
        if not os.path.isfile(_MCP_MAIN):
            raise RevitMCPError("项目内 Revit MCP 入口不存在: %s" % _MCP_MAIN)
        # 清 PYTHONPATH：系统注入的 hermes venv 路径会污染子进程包解析
        env = {**os.environ}
        env["PYTHONPATH"] = ""
        self._proc = subprocess.Popen(
            [_MCP_PYTHON, _MCP_MAIN],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.dirname(_MCP_MAIN),
            bufsize=0,  # 二进制模式（text 模式在 Windows 管道下缓冲异常）
            env=env,
        )
        self._rpc_sync({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05",
                                   "capabilities": {}, "clientInfo": {"name": "buildmate", "version": "1.0"}}})
        # initialized 是通知（无响应）——fire-and-forget，不能等响应
        self._notify_sync("notifications/initialized")

    def _notify_sync(self, method: str, params: dict | None = None) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _stop_sync(self) -> None:
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def _rpc_sync(self, msg: dict) -> dict:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RevitMCPError("Revit MCP 进程未启动")
        msg = dict(msg)
        msg["id"] = msg.get("id", self._next_id)
        self._next_id += 1
        req_id = msg["id"]
        with self._read_lock:
            self._proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
            while True:
                raw = self._proc.stdout.readline()
                if not raw:
                    raise RevitMCPError("Revit MCP 进程已退出")
                line = raw.decode("utf-8", errors="replace")
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("id") == req_id:
                    return data
                # 忽略通知/其他请求

    def _list_tools_sync(self) -> list[str]:
        data = self._rpc_sync({"jsonrpc": "2.0", "method": "tools/list", "params": {}})
        return [t.get("name", "") for t in (data.get("result", {}).get("tools") or [])]

    def _call_tool_sync(self, name: str, arguments: dict) -> dict:
        data = self._rpc_sync({"jsonrpc": "2.0", "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})
        result = data.get("result") or {}
        texts = []
        for item in (result.get("content") or []):
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
        if result.get("isError"):
            raise RevitMCPError(" / ".join(texts) or "Revit 工具执行失败")
        return {"text": "\n".join(texts), "raw": result}

    # ── 业务工具 ────────────────────────────────────────────────
    async def test_connection(self) -> str:
        return await self.call_tool("say_hello", {})

    async def convert_ifc_to_rvt(self, ifc_path: str, out_dir: str) -> str:
        """全自动 IFC→RVT：Revit 打开 IFC → 另存 RVT（code_execution 工具执行 C#）"""
        import asyncio
        return await asyncio.to_thread(self._convert_sync, ifc_path, out_dir)

    def _convert_sync(self, ifc_path: str, out_dir: str) -> str:
        ifc_path = os.path.abspath(ifc_path)
        if not os.path.exists(ifc_path):
            raise RevitMCPError(f"IFC 文件不存在: {ifc_path}")
        os.makedirs(out_dir, exist_ok=True)
        # ASCII 化输出文件名：Revit SaveAs 对中文路径/文件名支持不佳（编码问题）
        base = os.path.splitext(os.path.basename(ifc_path))[0]
        base_ascii = "".join(ch for ch in base if ord(ch) < 128).strip() or "converted"
        out_path = os.path.join(out_dir, base_ascii + ".rvt")
        if os.path.exists(out_path):
            os.remove(out_path)
        code = f'''
import clr, os
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIIFC")
from Autodesk.Revit.DB.IFC import IFCImportOptions
app = doc.Application
ifc_path = r"{ifc_path}"
out_path = r"{out_path}"
try:
    if os.path.exists(out_path):
        os.remove(out_path)
    opts = IFCImportOptions()
    ifc_doc = app.OpenIFCDocument(ifc_path, opts)
    if ifc_doc is None:
        print("FAIL: open IFC returned None")
    else:
        ifc_doc.SaveAs(out_path)
        print("OK: " + out_path)
        ifc_doc.Close(False)
except Exception as ex:
    print("FAIL: " + str(ex))
'''
        try:
            result = self._call_tool_sync("execute_revit_code", {"code": code, "description": "IFC to RVT"})
        except RevitMCPError as e:
            raise RevitMCPError(f"Revit 执行失败: {str(e)[:300]}")
        text = result.get("text", "")
        if "OK:" in text:
            return out_path
        raise RevitMCPError(text[:300] or "转换失败（Revit 无返回）")
