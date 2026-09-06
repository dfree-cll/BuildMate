"""RevitConnector：Revit 连接器（RVT 输出检测 / 引导状态 / 触发）

职责：
- 检测 rvt_out 目录新生成的 RVT（json2rvt 产物）
- 提供 Revit 建模引导状态（给前端展示）
- 后续 Journal 全自动触发（占位）
"""
from __future__ import annotations

import logging
import json
import os
import re
import time

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RVT_OUT_DIR = os.environ.get("REVIT_OUTPUT_DIR", os.path.join(
    _PROJECT_ROOT, "data", "runtime", "revit", "rvt_out"))
_DEFAULT_PYREVIT_SCRIPT = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "pyRevit", "Extensions",
    "IFC2RVT.extension", "IFC2RVT.tab", "IFC2RVT.panel",
    "json2rvt.pushbutton", "script.py")


class RevitConnector:
    """Revit 连接器——检测/引导/触发"""

    @staticmethod
    def detect_latest_rvt(since_ts: float | None = None) -> dict | None:
        """检测 rvt_out 最新 RVT（可选：since_ts 之后）——返回文件信息或 None"""
        if not os.path.isdir(RVT_OUT_DIR):
            return None
        candidates = []
        for name in os.listdir(RVT_OUT_DIR):
            if name.lower().endswith(".rvt"):
                p = os.path.join(RVT_OUT_DIR, name)
                st = os.stat(p)
                if since_ts is None or st.st_mtime > since_ts:
                    candidates.append({"name": name, "path": p,
                                       "size_mb": round(st.st_size / 1048576, 1),
                                       "mtime": st.st_mtime})
        if not candidates:
            return None
        candidates.sort(key=lambda c: c["mtime"], reverse=True)
        return candidates[0]

    @staticmethod
    def guide_steps() -> list[str]:
        """Revit 建模引导步骤（前端展示——傻瓜化）"""
        return [
            "打开 Revit（新建项目）",
            "顶部 pyRevit 面板 → IFC2RVT 选项卡 → 点 json2rvt 按钮",
            "等待自动建模（轴网→柱子→墙体→三维视图）",
            "完成弹窗显示 VALIDATE PASS",
            f"RVT 文件生成在 {RVT_OUT_DIR}",
        ]

    @staticmethod
    def trigger_model_build(json2rvt_script: str | None = None,
                            port: int = 48884, timeout: int = 240,
                            build_id: str = "") -> dict:
        """全自动触发：外部 HTTP 调 pyRevit Routes -> Revit 内执行 json2rvt 建模

        返回 {"status": "done"/"error", "message", "rvt": 最新 RVT 信息}
        """
        import json as _json
        import urllib.request
        safe_build_id = re.sub(r"[^A-Za-z0-9_-]", "_", build_id or "legacy")
        result_path = os.path.join(RVT_OUT_DIR, f"build_{safe_build_id}_result.json")
        # 同 build_id 理论上不复用；仍主动移除旧结果，杜绝历史结果冒充本次执行。
        if os.path.isfile(result_path):
            try:
                os.remove(result_path)
            except OSError as ex:
                return {"status": "error", "build_id": build_id,
                        "message": f"无法清理旧建模结果: {str(ex)[:120]}"}
        script = (json2rvt_script or os.environ.get("PYREVIT_JSON2RVT_SCRIPT")
                  or _DEFAULT_PYREVIT_SCRIPT)
        url = "http://localhost:%d/pyrevit-core/execute/" % port
        # json2rvt is also a pyRevit pushbutton and intentionally self-runs
        # when loaded.  Asking the route to call main again executes the full
        # cleanup/build/save pipeline twice.
        body = _json.dumps({"script_path": script, "call": ""}).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        started_at = time.time()

        def complete_from_result(detail: dict) -> dict:
            wait_deadline = started_at + timeout
            while (not os.path.isfile(result_path) and
                   time.time() < wait_deadline):
                time.sleep(0.25)
            if not os.path.isfile(result_path):
                return {"status": "error", "build_id": build_id,
                        "message": "Revit 调用结束，但未生成本次任务的结构化结果",
                        "detail": detail}
            try:
                with open(result_path, encoding="utf-8") as f:
                    build_result = json.load(f)
            except Exception as ex:
                return {"status": "error", "build_id": build_id,
                        "message": f"建模结果无法读取: {str(ex)[:120]}",
                        "detail": detail}
            if build_result.get("build_id") != build_id:
                return {"status": "error", "build_id": build_id,
                        "message": "Revit 返回结果与本次 build_id 不匹配",
                        "build_result": build_result}
            if build_result.get("status") != "done":
                return {"status": "error", "build_id": build_id,
                        "message": "Revit 构件创建或验证未通过",
                        "build_result": build_result}
            rvt_path = build_result.get("rvt_path") or ""
            if not rvt_path or not os.path.isfile(rvt_path):
                return {"status": "error", "build_id": build_id,
                        "message": "Revit 调用结束，但项目工作模型未保存",
                        "build_result": build_result, "detail": detail}
            st = os.stat(rvt_path)
            saved_model = {"name": os.path.basename(rvt_path), "path": rvt_path,
                           "size_mb": round(st.st_size / 1048576, 1),
                           "mtime": st.st_mtime}
            return {"status": "done", "message": "建模完成",
                    "build_id": build_id, "rvt": saved_model,
                    "build_result": build_result, "detail": detail}

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = resp.read().decode("utf-8", "replace")
            result = _json.loads(out) if out else {}
            if result.get("status") == "done":
                # The Routes handler can acknowledge queued UI-thread work
                # before Revit finishes Save/result serialization.  Wait only
                # for this build's result file instead of reporting a false
                # failure or triggering the same build again.
                return complete_from_result(result)
            return {"status": "error", "message": result.get("message", "未知错误"),
                    "detail": result}
        except Exception as ex:
            # Revit Routes can keep the HTTP request open after the UI-thread
            # build has already saved a valid result. Never re-trigger merely
            # because the transport timed out.
            if os.path.isfile(result_path):
                return complete_from_result({
                    "status": "done", "transport_error": str(ex)[:200]})
            return {"status": "error", "message": "触发失败: %s" % str(ex)[:200]}

    @staticmethod
    def trigger_journal(ifc_path: str, out_dir: str = RVT_OUT_DIR) -> dict:
        """（已废弃）Journal 方案——被 Routes 外部触发取代"""
        return {"status": "superseded", "note": "已用 pyRevit Routes 外部触发替代"}
