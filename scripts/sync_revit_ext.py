"""同步 json2rvt 主副本到 pyRevit 扩展目录（保留上一版备份便于回退）

用法：.venv\Scripts\python.exe scripts\sync_revit_ext.py
目标目录：用户 pyRevit 扩展下的 IFC2RVT json2rvt pushbutton 目录。
行为：主副本 → 目标 script.py；若目标已存在且内容不同，先备份为 script.py.bak_<时间戳>。
"""
import os
import ast
import json
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "workers", "pyrevit", "json2rvt_script.py")
CONTRACT_SRC = os.path.join(ROOT, "workers", "pyrevit", "wall_model_contract.py")
MANIFEST_SRC = os.path.join(ROOT, "workers", "pyrevit", "manifest.json")
DST = os.environ.get("PYREVIT_JSON2RVT_SCRIPT") or os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "pyRevit", "Extensions",
    "IFC2RVT.extension", "IFC2RVT.tab", "IFC2RVT.panel",
    "json2rvt.pushbutton", "script.py")


def _validate_manifest() -> dict:
    """Validate the source bundle before touching a user's pyRevit folder."""

    try:
        with open(MANIFEST_SRC, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"pyRevit manifest 无法读取: {exc}") from exc
    if manifest.get("schema_version") != "buildmate.pyrevit/1":
        raise RuntimeError("pyRevit manifest schema_version 不受支持")
    entrypoint = manifest.get("entrypoint") or {}
    source_name = str(entrypoint.get("source") or "")
    if source_name != os.path.basename(SRC):
        raise RuntimeError("pyRevit manifest entrypoint.source 与主脚本不一致")
    support_files = manifest.get("support_files") or []
    required = [source_name, *[str(item) for item in support_files]]
    for name in required:
        path = os.path.join(os.path.dirname(MANIFEST_SRC), name)
        if not os.path.isfile(path):
            raise RuntimeError(f"pyRevit manifest 文件不存在: {path}")
        try:
            source = open(path, "r", encoding="utf-8").read()
            ast.parse(source, filename=path, mode="exec")
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise RuntimeError(f"pyRevit 文件校验失败: {path}: {exc}") from exc
    return manifest


def main() -> int:
    try:
        manifest = _validate_manifest()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        return 1
    if not os.path.exists(SRC):
        print(f"[ERROR] 主副本不存在: {SRC}")
        return 1
    if not os.path.isdir(os.path.dirname(DST)):
        print(f"[ERROR] 目标目录不存在（pyRevit 扩展未安装？）: {os.path.dirname(DST)}")
        return 1
    contract_dst = os.path.join(os.path.dirname(DST), "wall_model_contract.py")
    shutil.copy2(CONTRACT_SRC, contract_dst)
    # pyRevit Routes 在 Revit 2020 的 IronPython 执行器中会把 CR 字符当作
    # 非法 token；部署副本统一使用 UTF-8 + LF，避免 Windows checkout 的
    # CRLF 在 exec(code, ...) 时触发 ``unexpected token '\\r'``。
    with open(SRC, "rb") as f:
        new = f.read().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if os.path.exists(DST):
        with open(DST, "rb") as f:
            old = f.read()
        if old == new:
            print("= 目标与主副本一致，无需同步")
            print(f"[OK] 已同步审核合同: {CONTRACT_SRC} -> {contract_dst}")
            return 0
        bak = DST + ".bak_" + time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(DST, bak)
        print(f"[BACKUP] 已备份旧版: {bak}")
    with open(DST, "wb") as f:
        f.write(new)
    print(f"[OK] 已同步: {SRC} -> {DST}")
    print(f"[OK] 已同步审核合同: {CONTRACT_SRC} -> {contract_dst}")
    print(f"[OK] manifest: {manifest['schema_version']} / Revit {','.join(manifest.get('revit_versions', []))}")
    print("  注意：在 Revit 内重新点击 json2rvt 按钮即生效（pyRevit 每次执行重新加载）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
