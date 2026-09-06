"""Wall observe-build-verify loop.

Pure functions only: expected IR + Revit element mapping + Revit dump become a
per-wall state manifest and a bounded repair list.  No model is mutated here.
"""
from __future__ import annotations

import math
from typing import Any


def _point_m(point_mm: list[float]) -> tuple[float, float]:
    return float(point_mm[0]) / 1000.0, float(point_mm[1]) / 1000.0


def _endpoint_error_m(expected: dict, actual: dict) -> float:
    exp_a, exp_b = _point_m(expected["start"]), _point_m(expected["end"])
    act_a = tuple(map(float, actual["start"][:2]))
    act_b = tuple(map(float, actual["end"][:2]))
    direct = max(math.dist(exp_a, act_a), math.dist(exp_b, act_b))
    reverse = max(math.dist(exp_a, act_b), math.dist(exp_b, act_a))
    return min(direct, reverse)


def _axis_deviation_deg(element: dict) -> float:
    start, end = element["start"], element["end"]
    angle = abs(math.degrees(math.atan2(
        float(end[1]) - float(start[1]), float(end[0]) - float(start[0])))) % 90.0
    return min(angle, 90.0 - angle)


def inspect_wall_iteration(model: dict[str, Any], build_result: dict[str, Any],
                           revit_dump: dict[str, Any], *,
                           position_tolerance_mm: float = 50.0,
                           angle_tolerance_deg: float = 0.1,
                           thickness_tolerance_mm: float = 10.0,
                           max_repairs: int = 200) -> dict[str, Any]:
    """Return candidate states and finite repair actions for structural walls."""
    expected = {str(e["id"]): e for e in model.get("model_elements", [])
                if e.get("type") == "Wall" and e.get("id")}
    mapping = {
        str(item.get("input_id")): str(item.get("revit_element_id"))
        for item in build_result.get("created", [])
        if item.get("kind") == "Wall" and item.get("input_id")
    }
    actual = {
        str(e.get("id", "")).removeprefix("wall_"): e
        for e in revit_dump.get("model_elements", []) if e.get("type") == "Wall"
    }
    resolved_mapping = {wall_id: revit_id for wall_id, revit_id in mapping.items()
                        if revit_id in actual}
    used_actual = set(resolved_mapping.values())
    # A saved copy or repeated build changes Revit ElementIds. Fall back to a
    # bounded one-to-one geometry match instead of reporting every wall missing.
    unmatched = [item for item in expected.items() if item[0] not in resolved_mapping]
    unmatched.sort(key=lambda item: math.dist(_point_m(item[1]["start"]),
                                               _point_m(item[1]["end"])), reverse=True)
    for wall_id, source in unmatched:
        candidates = []
        expected_thickness = float(source.get("thickness") or 0.0)
        for revit_id, built in actual.items():
            if revit_id in used_actual:
                continue
            thickness = float(built.get("thickness_m") or 0.0) * 1000.0
            if expected_thickness and abs(thickness - expected_thickness) > 25.0:
                continue
            error = _endpoint_error_m(source, built)
            if error <= 2.0:
                candidates.append((error, revit_id))
        if candidates:
            _, revit_id = min(candidates)
            resolved_mapping[wall_id] = revit_id
            used_actual.add(revit_id)
    states, repairs = [], []
    for wall_id, source in expected.items():
        revit_id = resolved_mapping.get(wall_id)
        built = actual.get(revit_id or "")
        evidence = {
            "source": source.get("source") or ["drawing"],
            "confidence": float(source.get("confidence", 1.0)),
            "expected_start_mm": source.get("start"),
            "expected_end_mm": source.get("end"),
            "expected_thickness_mm": source.get("thickness"),
            "revit_element_id": revit_id,
            "mapping_source": ("element_id" if mapping.get(wall_id) == revit_id
                               else "geometry" if revit_id else None),
        }
        issues = []
        metrics = None
        if not revit_id or built is None:
            status = "missing"
            issues.append("revit_element_missing")
            repairs.append({"action": "create", "element_id": wall_id,
                            "element": source, "reason": issues})
        else:
            position_error_mm = _endpoint_error_m(source, built) * 1000.0
            angle_error = _axis_deviation_deg(built)
            actual_thickness_mm = float(built.get("thickness_m") or 0.0) * 1000.0
            thickness_error_mm = abs(
                actual_thickness_mm - float(source.get("thickness") or 0.0))
            metrics = {"position_error_mm": round(position_error_mm, 2),
                       "axis_deviation_deg": round(angle_error, 4),
                       "thickness_error_mm": round(thickness_error_mm, 2)}
            if position_error_mm > position_tolerance_mm:
                issues.append("position_mismatch")
            if angle_error > angle_tolerance_deg:
                issues.append("angle_mismatch")
            if thickness_error_mm > thickness_tolerance_mm:
                issues.append("thickness_mismatch")
            status = "built" if not issues else "mismatch"
            if issues:
                repairs.append({"action": "update", "element_id": wall_id,
                                "revit_element_id": revit_id,
                                "fields": {"start": source["start"],
                                           "end": source["end"],
                                           "thickness": source.get("thickness")},
                                "reason": issues})
        states.append({"element_id": wall_id, "type": "Wall", "status": status,
                       "evidence": evidence, "metrics": metrics, "issues": issues})

    mapped_revit_ids = set(resolved_mapping.values())
    for revit_id, wall in actual.items():
        if revit_id not in mapped_revit_ids:
            states.append({"element_id": None, "type": "Wall", "status": "extra",
                           "evidence": {"revit_element_id": revit_id},
                           "metrics": None, "issues": ["unexpected_revit_wall"]})
            repairs.append({"action": "review_delete", "revit_element_id": revit_id,
                            "reason": ["unexpected_revit_wall"]})

    if len(repairs) > max_repairs:
        repairs = repairs[:max_repairs]
        truncated = True
    else:
        truncated = False
    counts: dict[str, int] = {}
    for state in states:
        counts[state["status"]] = counts.get(state["status"], 0) + 1
    return {"schema_version": "1.0", "element_type": "Wall",
            "counts": counts, "states": states, "repairs": repairs,
            "repair_count": len(repairs), "repairs_truncated": truncated,
            "ready": not repairs}
