"""Validator：验证引擎

分工：
- IR 校验（生成前）：validate_ir——model.json 数据完整性（Python 侧）
- 读回校验（生成后）：json2rvt 脚本内（Revit API 读回——数量/位置对比 JSON 预期）
本模块提供 IR 校验封装 + 生成结果对比的 Python 侧工具。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from backend.core.logger import get_logger
from backend.engines.model_ir import validate_ir

logger = get_logger(__name__)


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) else None


def _elem_xy(e: dict) -> tuple[float | None, float | None]:
    """构件坐标提取：兼容 x/y 标量与 start/end=[x,y] 数组两种形态"""
    x, y = _num(e.get("x")), _num(e.get("y"))
    pl = e.get("placement")
    if isinstance(pl, dict):
        x = x if x is not None else _num(pl.get("x"))
        y = y if y is not None else _num(pl.get("y"))
    for key in ("start", "end"):
        v = e.get(key)
        if isinstance(v, dict):
            x = x if x is not None else _num(v.get("x"))
            y = y if y is not None else _num(v.get("y"))
        elif isinstance(v, (list, tuple)) and len(v) >= 2:
            x = x if x is not None else _num(v[0])
            y = y if y is not None else _num(v[1])
    return x, y


class Validator:
    """验证引擎——IR 校验 + 预期/实际对比"""

    @staticmethod
    def check_ir(data: dict) -> dict:
        """生成前校验：model.json 完整性——返回 {valid, issues, summary}"""
        issues = validate_ir(data)
        elems = data.get("model_elements") or []
        return {
            "valid": not issues,
            "issues": issues,
            "expected": {
                "walls": sum(1 for e in elems if e.get("type") == "Wall"),
                "columns": sum(1 for e in elems if e.get("type") == "Column"),
                "beams": sum(1 for e in elems if e.get("type") == "Beam"),
                "slabs": sum(1 for e in elems if e.get("type") == "Floor"),
            },
        }

    @staticmethod
    def compare(expected: dict, actual: dict, tol_mm: float = 100.0,
                max_pos_diffs: int = 50) -> dict:
        """预期 vs 实际对比（读回结果）——{pass, diffs}；全量构件比对"""
        diffs = []
        for key in ("walls", "columns", "beams", "slabs"):
            ev = expected.get(key, 0)
            av = actual.get(key, 0)
            if ev != av:
                diffs.append(f"{key}: expected {ev} actual {av}")
        # 全量位置比对（按名称配对，避免索引错位误报；双方都有坐标才比）
        act_by_name = {ae.get("name"): ae for ae in actual.get("elements", [])}
        pos_diffs = 0
        pos_skipped = not act_by_name
        for ee in expected.get("elements", []):
            ae = act_by_name.get(ee.get("name"))
            if ae is None:
                diffs.append(f"missing {ee.get('id', ee.get('name'))}")
                continue
            for axis in ("x", "y"):
                ev, av = _num(ee.get(axis)), _num(ae.get(axis))
                if ev is not None and av is not None and abs(ev - av) > tol_mm:
                    pos_diffs += 1
                    if pos_diffs <= max_pos_diffs:
                        diffs.append(
                            f"pos {ee.get('id', ee.get('name'))} {axis}: "
                            f"expect {ev} actual {av}")
        if pos_diffs > max_pos_diffs:
            diffs.append(f"... {pos_diffs - max_pos_diffs} more position diffs")
        result = {"pass": not diffs, "diffs": diffs,
                  "pos_compared": not pos_skipped}
        logger.info("validator.compare", passed=result["pass"],
                    diff_count=len(diffs), pos_compared=not pos_skipped)
        return result

    @staticmethod
    def compare_ifc(baseline: list[dict], parsed: dict, session_id: str = "default",
                    tol_mm: float = 100.0, out_dir: str | None = None) -> dict:
        """IR 基准 vs IFC 回读对比（主链路闭环校验），报告落盘并记日志。

        按 name 配对；数量按类型统计（Wall/Column/Beam、Floor+Slab 归 slabs）。
        """
        type_key = {"Wall": "walls", "Column": "columns", "Beam": "beams",
                    "Floor": "slabs", "Slab": "slabs"}
        expected = {k: 0 for k in type_key.values()}
        exp_elems = []
        for e in baseline:
            k = type_key.get(e.get("type"))
            if not k:
                continue
            expected[k] += 1
            x, y = _elem_xy(e)
            if x is None and y is None:
                continue
            exp_elems.append({"id": e.get("element_id") or e.get("name"),
                              "name": e.get("name"), "x": x, "y": y})
        actual = {"walls": 0, "columns": 0, "beams": 0, "slabs": 0}
        act_elems = []
        ifc_type_key = {"IFCWALL": "walls", "IFCWALLSTANDARDCASE": "walls",
                        "IFCCOLUMN": "columns", "IFCBEAM": "beams",
                        "IFCMEMBER": "beams", "IFCSLAB": "slabs"}
        for ae in parsed.get("elements", []):
            k = ifc_type_key.get((ae.get("ifc_type") or "").upper())
            if not k:
                continue
            actual[k] += 1
            x, y = _elem_xy(ae)
            item = {"name": ae.get("name"), "x": x, "y": y}
            for key in ("x", "y"):
                if item[key] is None:
                    del item[key]
            act_elems.append(item)
        result = Validator.compare(
            {"walls": expected["walls"], "columns": expected["columns"],
             "beams": expected["beams"], "slabs": expected["slabs"],
             "elements": exp_elems},
            {"walls": actual["walls"], "columns": actual["columns"],
             "beams": actual["beams"], "slabs": actual["slabs"],
             "elements": act_elems}, tol_mm=tol_mm)

        # 属性级比对（厚度/高度容差 1mm，材料按字符串）
        def _fnum(v) -> float | None:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return float(v)
            try:
                return float(str(v))
            except ValueError:
                return None

        def _ir_attrs(e: dict) -> dict:
            p = e.get("properties") or {}
            return {"Thickness": _fnum(p.get("Thickness")) or _fnum(e.get("thickness")),
                    "Height": _fnum(p.get("Height")) or _fnum(e.get("height")),
                    "Material": p.get("Material") or e.get("material")}

        def _ifc_attrs(ae: dict) -> dict:
            vals: dict = {}
            for pset in (ae.get("Psets") or {}).values():
                if not isinstance(pset, dict):
                    continue
                for k in ("Thickness", "Height", "Material"):
                    if k not in vals and pset.get(k) is not None:
                        vals[k] = pset.get(k)
            return {"Thickness": _fnum(vals.get("Thickness")),
                    "Height": _fnum(vals.get("Height")),
                    "Material": vals.get("Material")}

        act_by_name = {a.get("name"): a for a in parsed.get("elements", [])}
        attr_diffs: list[str] = []
        for e in baseline:
            if e.get("type") not in type_key:
                continue
            ae = act_by_name.get(e.get("name"))
            if ae is None:
                continue
            ea, aa = _ir_attrs(e), _ifc_attrs(ae)
            for k in ("Thickness", "Height"):
                if ea[k] is not None and aa.get(k) is not None \
                        and abs(ea[k] - aa[k]) > 1.0:
                    if len(attr_diffs) < 20:
                        attr_diffs.append(
                            f"attr {e.get('name')} {k}: expect {ea[k]} actual {aa[k]}")
            if ea["Material"] and aa["Material"] and str(ea["Material"]) != str(aa["Material"]):
                if len(attr_diffs) < 20:
                    attr_diffs.append(
                        f"attr {e.get('name')} Material: expect {ea['Material']} actual {aa['Material']}")
        result["diffs"] = result["diffs"] + attr_diffs
        result["pass"] = not result["diffs"]
        result["attr_diffs"] = len(attr_diffs)
        report = {"session_id": session_id, "tol_mm": tol_mm,
                  "expected_counts": expected, "actual_counts": actual,
                  **result, "checked_at": datetime.now().isoformat(timespec="seconds")}
        if out_dir is None:
            # validator.py 在 backend/engines/ 下，向上三层即项目根
            out_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)))), "data", "generated")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"validate_{session_id}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            report["report_path"] = path
        except OSError as e:
            logger.warning("validator.report_write_failed", error=str(e)[:120])
        logger.info("validator.compare_ifc", session_id=session_id,
                    passed=result["pass"], diff_count=len(result["diffs"]))
        return report
