"""Compare DXF-derived wall centerlines with a Revit wall dump.

The Revit dump uses metres in project coordinates.  The standard DXF review
artifact uses metres in a grid-local frame.  This script reports both an
absolute comparison anchored at matching grid 1/A and a diagnostic piecewise
normalization through every identically named grid.  A detected centerline can
cover multiple Revit wall instances (for example, when Revit splits a wall at
doors), so one-to-one counts are reported separately from geometric coverage.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


Point = tuple[float, float]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _point(value: Iterable[Any]) -> Point:
    values = list(value)
    return float(values[0]), float(values[1])


def _length(wall: dict[str, Any]) -> float:
    return math.dist(_point(wall["start"]), _point(wall["end"]))


def _wall_type_thickness_mm(wall: dict[str, Any]) -> float | None:
    value = wall.get("thickness_m")
    if value is not None:
        return float(value) * 1000.0
    value = wall.get("thickness")
    return float(value) if value is not None else None


def _revit_walls(dump: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(item.get("id") or ""),
            "start": list(map(float, item["start"][:2])),
            "end": list(map(float, item["end"][:2])),
            "thickness_m": float(item.get("thickness_m") or 0.0),
            "level": str(item.get("level_name") or ""),
            "wall_type": str(item.get("wall_type_name") or "unknown"),
        }
        for item in dump.get("model_elements", [])
        if item.get("type") == "Wall" and item.get("start") and item.get("end")
    ]


def _review_walls(review: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(item.get("id") or ""),
            "start": list(map(float, item["start"][:2])),
            "end": list(map(float, item["end"][:2])),
            "thickness": float(item.get("thickness") or 0.0),
            "paired": bool(item.get("paired")),
            "wall_group": item.get("wall_group"),
            "source": item.get("source"),
            "geometry_source": item.get("geometry_source"),
            "source_layers": list(item.get("source_layers") or []),
        }
        for item in review.get("geometry", {}).get("architectural_walls", [])
        if item.get("start") and item.get("end")
    ]


def _filter_walls_by_source_layers(
    walls: list[dict[str, Any]], *, include_patterns: list[str],
    exclude_patterns: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply explicit, report-visible source filters for calibration runs."""
    include = [value.casefold() for value in include_patterns if value]
    exclude = [value.casefold() for value in exclude_patterns if value]
    kept = []
    rejected = []
    for wall in walls:
        layers = [str(value).casefold()
                  for value in wall.get("source_layers") or []]
        included = not include or any(
            pattern in layer for pattern in include for layer in layers)
        excluded = any(
            pattern in layer for pattern in exclude for layer in layers)
        (kept if included and not excluded else rejected).append(wall)
    return kept, {
        "include_patterns": include_patterns,
        "exclude_patterns": exclude_patterns,
        "input_candidate_count": len(walls),
        "kept_candidate_count": len(kept),
        "rejected_candidate_count": len(rejected),
        "mode": "explicit_calibration_filter",
    }


def _grid_direction(grid: dict[str, Any]) -> tuple[str, float] | None:
    start, end = _point(grid["start"]), _point(grid["end"])
    dx, dy = abs(end[0] - start[0]), abs(end[1] - start[1])
    if dx <= max(0.01, dy * 0.02):
        return "x", (start[0] + end[0]) / 2.0
    if dy <= max(0.01, dx * 0.02):
        return "y", (start[1] + end[1]) / 2.0
    return None


def derive_grid_translation(
    revit_dump: dict[str, Any], grid_model: dict[str, Any]
) -> dict[str, Any]:
    local_x = {str(item["label"]): float(item["coord"])
               for item in grid_model.get("x_axes", [])}
    local_y = {str(item["label"]): float(item["coord"])
               for item in grid_model.get("y_axes", [])}
    by_prefix: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"x": [], "y": []})
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for grid in revit_dump.get("grids", []):
        name = str(grid.get("name") or "")
        if "-" not in name:
            continue
        prefix, label = name.rsplit("-", 1)
        direction = _grid_direction(grid)
        if direction is None:
            continue
        axis, coordinate = direction
        local = local_x.get(label) if axis == "x" else local_y.get(label)
        if local is None:
            continue
        offset = coordinate - local
        by_prefix[prefix][axis].append(offset)
        matches[prefix].append({
            "name": name, "axis": axis, "local": local,
            "revit": coordinate, "offset": offset,
        })

    eligible = [
        prefix for prefix, axes in by_prefix.items()
        if len(axes["x"]) >= 2 and len(axes["y"]) >= 2
    ]
    if not eligible:
        raise ValueError("no Revit grid prefix matches both DXF grid axes")
    prefix = max(eligible, key=lambda value: len(matches[value]))
    # The review grid's (0, 0) is explicitly defined by axes 1/A.  Use those
    # named origin axes when present; later bays can legitimately differ across
    # drawing/model revisions and must be reported as residuals, not allowed to
    # move the coordinate origin by their median discrepancy.
    origin_x = [item["offset"] for item in matches[prefix]
                if item["axis"] == "x" and abs(item["local"]) <= 1e-9]
    origin_y = [item["offset"] for item in matches[prefix]
                if item["axis"] == "y" and abs(item["local"]) <= 1e-9]
    offset_x = (statistics.median(origin_x) if origin_x else
                statistics.median(by_prefix[prefix]["x"]))
    offset_y = (statistics.median(origin_y) if origin_y else
                statistics.median(by_prefix[prefix]["y"]))
    residuals = [
        abs(item["offset"] - (offset_x if item["axis"] == "x" else offset_y))
        for item in matches[prefix]
    ]
    return {
        "method": "matching_named_grids_translation",
        "origin_anchor": "grid_1_A" if origin_x and origin_y else "median_named_grids",
        "revit_grid_prefix": prefix,
        "offset_m": [round(offset_x, 6), round(offset_y, 6)],
        "match_count": len(matches[prefix]),
        "x_match_count": len(by_prefix[prefix]["x"]),
        "y_match_count": len(by_prefix[prefix]["y"]),
        "median_residual_mm": round(statistics.median(residuals) * 1000.0, 3),
        "max_residual_mm": round(max(residuals, default=0.0) * 1000.0, 3),
        "matches": matches[prefix],
    }


def derive_grid_normalization(translation: dict[str, Any]) -> dict[str, Any]:
    """Build a diagnostic piecewise map through identically named grids.

    Absolute project coordinates remain the authoritative comparison.  This map
    deliberately removes bay-width differences between the drawing and model,
    which makes it useful for separating recognition misses from revision or
    registration differences.  It must therefore be reported as a second,
    explicitly normalized comparison rather than used as the coordinate truth.
    """

    axes: dict[str, list[dict[str, Any]]] = {"x": [], "y": []}
    for axis in axes:
        items = [item for item in translation["matches"] if item["axis"] == axis]
        by_local: dict[float, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_local[float(item["local"])].append(item)
        for local, duplicates in sorted(by_local.items()):
            axes[axis].append({
                "label": str(duplicates[0]["name"]).rsplit("-", 1)[-1],
                "local": local,
                "revit": statistics.median(float(item["revit"]) for item in duplicates),
            })
        if len(axes[axis]) < 2:
            raise ValueError(f"at least two matching {axis}-axis grids are required")

    def segments(control_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for first, second in zip(control_points, control_points[1:]):
            local_span = second["local"] - first["local"]
            if local_span <= 1e-9:
                continue
            revit_span = second["revit"] - first["revit"]
            result.append({
                "from": first["label"],
                "to": second["label"],
                "local_span_m": round(local_span, 6),
                "revit_span_m": round(revit_span, 6),
                "scale": round(revit_span / local_span, 9),
            })
        return result

    x_segments, y_segments = segments(axes["x"]), segments(axes["y"])
    return {
        "method": "matching_named_grids_piecewise_linear",
        "purpose": "diagnostic_layout_comparison_not_absolute_coordinate_validation",
        "revit_grid_prefix": translation["revit_grid_prefix"],
        "control_points": axes,
        "segments": {"x": x_segments, "y": y_segments},
        "x_scale_range": [
            min(item["scale"] for item in x_segments),
            max(item["scale"] for item in x_segments),
        ],
        "y_scale_range": [
            min(item["scale"] for item in y_segments),
            max(item["scale"] for item in y_segments),
        ],
    }


def _piecewise_coordinate(value: float, controls: list[dict[str, Any]]) -> float:
    locals_ = [float(item["local"]) for item in controls]
    index = bisect.bisect_right(locals_, value) - 1
    index = min(max(index, 0), len(controls) - 2)
    first, second = controls[index], controls[index + 1]
    local_span = float(second["local"]) - float(first["local"])
    if local_span <= 1e-9:
        raise ValueError("grid normalization control points must be strictly ordered")
    fraction = (value - float(first["local"])) / local_span
    return float(first["revit"]) + fraction * (
        float(second["revit"]) - float(first["revit"])
    )


def _world_mm_to_local_m(point: Iterable[Any], grid_model: dict[str, Any]) -> Point:
    x, y = _point(point)
    origin_x, origin_y = map(float, grid_model["origin_mm"])
    meta = grid_model["meta"]
    center_x, center_y = float(meta["gcx"]), float(meta["gcy"])
    radians = math.radians(float(meta["rot_deg"]))
    cosine, sine = math.cos(radians), math.sin(radians)
    dx, dy = x - center_x, y - center_y
    rotated_x = dx * cosine - dy * sine + center_x
    rotated_y = dx * sine + dy * cosine + center_y
    return (rotated_x - origin_x) / 1000.0, (rotated_y - origin_y) / 1000.0


async def _current_dxf_walls(
    dxf_path: Path, grid_model: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from backend.agents.drawing2bim.nodes.dxf_extractor import (
        extract_baseline_from_dxf,
    )
    from backend.engines.dxf_parser import parse_dxf

    parsed = await parse_dxf(str(dxf_path))
    baseline = extract_baseline_from_dxf(parsed)
    walls = []
    for item in baseline:
        if item.get("ifc_type") != "IFCWALL":
            continue
        walls.append({
            "id": str(item.get("element_id") or ""),
            "start": list(_world_mm_to_local_m(item["start"], grid_model)),
            "end": list(_world_mm_to_local_m(item["end"], grid_model)),
            "thickness": float(item.get("thickness") or 0.0),
            "paired": bool(item.get("paired")),
            "wall_group": item.get("wall_group"),
            "source": item.get("source"),
            "geometry_source": item.get("geometry_source"),
            "source_layers": list(item.get("source_layers") or []),
            "source_segment_ids": list(
                item.get("source_segment_ids") or []),
            "source_segment_refs": list(
                item.get("source_segment_refs") or []),
        })
    return walls, parsed.get("wall_extraction") or {}


def _diagnostic_group_walls(
    dxf_path: Path, grid_model: dict[str, Any], wall_groups: tuple[str, ...]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract an explicitly requested wall group without changing production routing."""
    import ezdxf

    from backend.engines.wall_geometry import extract_precise_walls
    from scripts.geometry_first_clean import (
        drawing_identity, select_main_plan, walk_wall_evidence,
    )

    doc = ezdxf.readfile(dxf_path)
    selected_plan, selection = select_main_plan(doc)
    records = list(walk_wall_evidence(
        [selected_plan], drawing_id=drawing_identity(doc)))
    candidates = extract_precise_walls(
        records, ox=0.0, oy=0.0, rot_deg=0.0, gcx=0.0, gcy=0.0,
        wall_groups=wall_groups)
    walls = []
    for index, item in enumerate(candidates):
        start_m, end_m = _point(item["start"]), _point(item["end"])
        walls.append({
            "id": f"diagnostic-{'-'.join(wall_groups)}-{index + 1}",
            "start": list(_world_mm_to_local_m(
                (start_m[0] * 1000.0, start_m[1] * 1000.0), grid_model)),
            "end": list(_world_mm_to_local_m(
                (end_m[0] * 1000.0, end_m[1] * 1000.0), grid_model)),
            "thickness": float(item.get("thickness") or 0.0),
            "paired": bool(item.get("paired")),
            "wall_group": item.get("wall_group"),
            "source": "dxf_precise_geometry_diagnostic",
            "geometry_source": item.get("geometry_source"),
        })
    return walls, {
        "wall_groups": list(wall_groups),
        "source_entity_count": len(records),
        "candidate_count": len(walls),
        "paired_count": sum(bool(wall.get("paired")) for wall in walls),
        "unpaired_count": sum(not bool(wall.get("paired")) for wall in walls),
        "plan_selection": selection,
        "production_parser_unchanged": True,
    }


def _translated(walls: list[dict[str, Any]], offset: list[float]) -> list[dict[str, Any]]:
    dx, dy = map(float, offset)
    result = []
    for wall in walls:
        copy = dict(wall)
        copy["start"] = [float(wall["start"][0]) + dx,
                         float(wall["start"][1]) + dy]
        copy["end"] = [float(wall["end"][0]) + dx,
                       float(wall["end"][1]) + dy]
        result.append(copy)
    return result


def _grid_normalized(
    walls: list[dict[str, Any]], normalization: dict[str, Any]
) -> list[dict[str, Any]]:
    controls = normalization["control_points"]
    result = []
    for wall in walls:
        copy = dict(wall)
        copy["start"] = [
            _piecewise_coordinate(float(wall["start"][0]), controls["x"]),
            _piecewise_coordinate(float(wall["start"][1]), controls["y"]),
        ]
        copy["end"] = [
            _piecewise_coordinate(float(wall["end"][0]), controls["x"]),
            _piecewise_coordinate(float(wall["end"][1]), controls["y"]),
        ]
        result.append(copy)
    return result


def _normalization_extent_stats(
    walls: list[dict[str, Any]], normalization: dict[str, Any]
) -> dict[str, int]:
    controls = normalization["control_points"]
    x_min, x_max = controls["x"][0]["local"], controls["x"][-1]["local"]
    y_min, y_max = controls["y"][0]["local"], controls["y"][-1]["local"]
    outside = 0
    for wall in walls:
        points = (_point(wall["start"]), _point(wall["end"]))
        if any(x < x_min or x > x_max or y < y_min or y > y_max
               for x, y in points):
            outside += 1
    return {
        "wall_count": len(walls),
        "walls_with_endpoint_outside_control_grid_extent": outside,
    }


def _angle_difference_deg(first: dict[str, Any], second: dict[str, Any]) -> float:
    a0, a1 = _point(first["start"]), _point(first["end"])
    b0, b1 = _point(second["start"]), _point(second["end"])
    av = (a1[0] - a0[0], a1[1] - a0[1])
    bv = (b1[0] - b0[0], b1[1] - b0[1])
    al, bl = math.hypot(*av), math.hypot(*bv)
    if al <= 1e-9 or bl <= 1e-9:
        return 90.0
    cosine = max(-1.0, min(1.0, abs((av[0] * bv[0] + av[1] * bv[1]) / (al * bl))))
    return math.degrees(math.acos(cosine))


def _point_line_distance(point: Point, wall: dict[str, Any]) -> float:
    start, end = _point(wall["start"]), _point(wall["end"])
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return float("inf")
    return abs((point[0] - start[0]) * dy - (point[1] - start[1]) * dx) / length


def _candidate_interval(
    source: dict[str, Any], candidate: dict[str, Any], *,
    angle_tolerance_deg: float, lateral_tolerance_m: float,
) -> tuple[float, float] | None:
    if _angle_difference_deg(source, candidate) > angle_tolerance_deg:
        return None
    start, end = _point(source["start"]), _point(source["end"])
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return None
    ux, uy = dx / length, dy / length

    def project(point: Point) -> float:
        return (point[0] - start[0]) * ux + (point[1] - start[1]) * uy

    raw_lo, raw_hi = sorted((project(_point(candidate["start"])),
                             project(_point(candidate["end"]))))
    lo, hi = max(0.0, raw_lo), min(length, raw_hi)
    if hi - lo <= 0.05:
        return None
    samples = (lo, (lo + hi) / 2.0, hi)
    if max(_point_line_distance((start[0] + value * ux,
                                 start[1] + value * uy), candidate)
           for value in samples) > lateral_tolerance_m:
        return None
    return lo, hi


def _coverage(
    source: dict[str, Any], candidates: list[dict[str, Any]], *,
    angle_tolerance_deg: float, lateral_tolerance_m: float,
) -> dict[str, Any]:
    length = _length(source)
    intervals = []
    matched_ids = []
    thickness_errors = []
    source_thickness = _wall_type_thickness_mm(source)
    for candidate in candidates:
        interval = _candidate_interval(
            source, candidate, angle_tolerance_deg=angle_tolerance_deg,
            lateral_tolerance_m=lateral_tolerance_m)
        if interval is None:
            continue
        intervals.append(interval)
        matched_ids.append(candidate.get("id"))
        candidate_thickness = _wall_type_thickness_mm(candidate)
        if source_thickness and candidate_thickness:
            thickness_errors.append(abs(source_thickness - candidate_thickness))
    intervals.sort()
    merged: list[list[float]] = []
    for lo, hi in intervals:
        if not merged or lo > merged[-1][1] + 1e-9:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    covered = sum(hi - lo for lo, hi in merged)
    return {
        "length_m": length,
        "covered_length_m": min(length, covered),
        "coverage": min(1.0, covered / length) if length > 1e-9 else 0.0,
        "matched_ids": list(dict.fromkeys(matched_ids)),
        "best_thickness_error_mm": min(thickness_errors) if thickness_errors else None,
    }


def _maximum_matching(edges: dict[int, list[int]], prediction_count: int) -> int:
    matched_actual: dict[int, int] = {}

    def augment(prediction: int, seen: set[int]) -> bool:
        for actual in edges.get(prediction, []):
            if actual in seen:
                continue
            seen.add(actual)
            if actual not in matched_actual or augment(matched_actual[actual], seen):
                matched_actual[actual] = prediction
                return True
        return False

    return sum(augment(prediction, set()) for prediction in range(prediction_count))


def _pair_is_majority_match(
    first: dict[str, Any], second: dict[str, Any], *,
    angle_tolerance_deg: float, lateral_tolerance_m: float,
) -> bool:
    first_interval = _candidate_interval(
        first, second, angle_tolerance_deg=angle_tolerance_deg,
        lateral_tolerance_m=lateral_tolerance_m)
    second_interval = _candidate_interval(
        second, first, angle_tolerance_deg=angle_tolerance_deg,
        lateral_tolerance_m=lateral_tolerance_m)
    if first_interval is None or second_interval is None:
        return False
    first_coverage = (first_interval[1] - first_interval[0]) / _length(first)
    second_coverage = (second_interval[1] - second_interval[0]) / _length(second)
    return first_coverage >= 0.5 and second_coverage >= 0.5


def compare(
    actual: list[dict[str, Any]], predicted: list[dict[str, Any]], *,
    angle_tolerance_deg: float, lateral_tolerance_m: float,
) -> dict[str, Any]:
    actual_rows = [
        {"id": wall["id"], **_coverage(
            wall, predicted, angle_tolerance_deg=angle_tolerance_deg,
            lateral_tolerance_m=lateral_tolerance_m)}
        for wall in actual
    ]
    predicted_rows = [
        {"id": wall["id"], "paired": wall.get("paired"),
         "wall_group": wall.get("wall_group"),
         "geometry_source": wall.get("geometry_source"),
         "thickness_mm": _wall_type_thickness_mm(wall),
         "source_layers": wall.get("source_layers") or [], **_coverage(
            wall, actual, angle_tolerance_deg=angle_tolerance_deg,
            lateral_tolerance_m=lateral_tolerance_m)}
        for wall in predicted
    ]
    actual_indexes_by_id: dict[str, list[int]] = defaultdict(list)
    for actual_index, actual_wall in enumerate(actual):
        actual_indexes_by_id[str(actual_wall.get("id") or "")].append(actual_index)
    edges = {}
    for prediction_index, row in enumerate(predicted_rows):
        candidate_indexes = {
            actual_index
            for actual_id in row["matched_ids"]
            for actual_index in actual_indexes_by_id.get(str(actual_id or ""), [])
        }
        edges[prediction_index] = [
            actual_index for actual_index in candidate_indexes
            if _pair_is_majority_match(
                predicted[prediction_index], actual[actual_index],
                angle_tolerance_deg=angle_tolerance_deg,
                lateral_tolerance_m=lateral_tolerance_m)
        ]
    one_to_one = _maximum_matching(edges, len(predicted))
    actual_length = sum(row["length_m"] for row in actual_rows)
    predicted_length = sum(row["length_m"] for row in predicted_rows)
    actual_covered_length = sum(row["covered_length_m"] for row in actual_rows)
    predicted_covered_length = sum(row["covered_length_m"] for row in predicted_rows)
    thickness_rows = [row for row in actual_rows
                      if row["best_thickness_error_mm"] is not None and row["coverage"] >= 0.5]

    def count_at(rows: list[dict[str, Any]], threshold: float) -> int:
        return sum(row["coverage"] >= threshold for row in rows)

    return {
        "actual_count": len(actual),
        "predicted_count": len(predicted),
        "predicted_paired_count": sum(bool(item.get("paired")) for item in predicted),
        "predicted_unpaired_count": sum(not bool(item.get("paired")) for item in predicted),
        "naive_count_ratio_not_recall": round(len(predicted) / len(actual), 6) if actual else 0.0,
        "actual_total_length_m": round(actual_length, 3),
        "predicted_total_length_m": round(predicted_length, 3),
        "actual_covered_length_m": round(actual_covered_length, 3),
        "predicted_covered_length_m": round(predicted_covered_length, 3),
        "actual_instance_coverage": {
            "any_count": sum(row["covered_length_m"] >= 0.1 for row in actual_rows),
            "at_least_50pct_count": count_at(actual_rows, 0.5),
            "at_least_80pct_count": count_at(actual_rows, 0.8),
            "at_least_50pct_rate": round(count_at(actual_rows, 0.5) / len(actual), 6) if actual else 0.0,
            "at_least_80pct_rate": round(count_at(actual_rows, 0.8) / len(actual), 6) if actual else 0.0,
        },
        "prediction_coverage": {
            "any_count": sum(row["covered_length_m"] >= 0.1 for row in predicted_rows),
            "at_least_50pct_count": count_at(predicted_rows, 0.5),
            "at_least_80pct_count": count_at(predicted_rows, 0.8),
            "at_least_50pct_rate": round(count_at(predicted_rows, 0.5) / len(predicted), 6) if predicted else 0.0,
            "at_least_80pct_rate": round(count_at(predicted_rows, 0.8) / len(predicted), 6) if predicted else 0.0,
        },
        "length_weighted_recall": round(
            actual_covered_length / actual_length, 6
        ) if actual_length else 0.0,
        "length_weighted_precision": round(
            predicted_covered_length / predicted_length, 6
        ) if predicted_length else 0.0,
        "maximum_one_to_one_match_count": one_to_one,
        "maximum_one_to_one_recall": round(one_to_one / len(actual), 6) if actual else 0.0,
        "maximum_one_to_one_precision": round(one_to_one / len(predicted), 6) if predicted else 0.0,
        "thickness_agreement": {
            "evaluated_actual_count": len(thickness_rows),
            "within_25mm_count": sum(row["best_thickness_error_mm"] <= 25.0 for row in thickness_rows),
            "within_50mm_count": sum(row["best_thickness_error_mm"] <= 50.0 for row in thickness_rows),
        },
        "actual_rows": actual_rows,
        "predicted_rows": predicted_rows,
    }


def summarize_causes(
    actual: list[dict[str, Any]], comparison: dict[str, Any]
) -> dict[str, Any]:
    rows = {row["id"]: row for row in comparison["actual_rows"]}
    by_type: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "covered_50_count": 0, "total_length_m": 0.0,
                 "covered_length_m": 0.0})
    length_bins = {
        "lt_0.5m": [0, 0], "0.5_to_1m": [0, 0], "1_to_3m": [0, 0],
        "3_to_6m": [0, 0], "ge_6m": [0, 0],
    }
    for wall in actual:
        row = rows[wall["id"]]
        bucket = by_type[wall["wall_type"]]
        bucket["count"] += 1
        bucket["covered_50_count"] += row["coverage"] >= 0.5
        bucket["total_length_m"] += row["length_m"]
        bucket["covered_length_m"] += row["covered_length_m"]
        length = row["length_m"]
        name = ("lt_0.5m" if length < 0.5 else "0.5_to_1m" if length < 1.0
                else "1_to_3m" if length < 3.0 else "3_to_6m" if length < 6.0
                else "ge_6m")
        length_bins[name][0] += 1
        length_bins[name][1] += row["coverage"] >= 0.5
    for value in by_type.values():
        value["total_length_m"] = round(value["total_length_m"], 3)
        value["covered_length_m"] = round(value["covered_length_m"], 3)
        value["recall_50"] = round(value["covered_50_count"] / value["count"], 6)
        value["length_recall"] = round(
            value["covered_length_m"] / value["total_length_m"], 6
        ) if value["total_length_m"] else 0.0
    return {
        "by_wall_type": dict(sorted(by_type.items(),
                                    key=lambda item: item[1]["count"], reverse=True)),
        "by_length_bin": {
            name: {"count": counts[0], "covered_50_count": counts[1],
                   "recall_50": round(counts[1] / counts[0], 6) if counts[0] else 0.0}
            for name, counts in length_bins.items()
        },
    }


def summarize_thickness_counts(walls: list[dict[str, Any]]) -> dict[str, int]:
    thickness_counts: dict[str, int] = defaultdict(int)
    for wall in walls:
        thickness = _wall_type_thickness_mm(wall)
        label = "unknown" if thickness is None else f"{round(thickness, 1):g}"
        thickness_counts[label] += 1
    return dict(sorted(
        thickness_counts.items(), key=lambda item: (-item[1], item[0])))


def summarize_candidate_set(walls: list[dict[str, Any]]) -> dict[str, Any]:
    paired = [wall for wall in walls if wall.get("paired")]
    unpaired = [wall for wall in walls if not wall.get("paired")]
    return {
        "count": len(walls),
        "paired_count": len(paired),
        "unpaired_count": len(unpaired),
        "total_length_m": round(sum(_length(wall) for wall in walls), 3),
        "paired_length_m": round(sum(_length(wall) for wall in paired), 3),
        "unpaired_length_m": round(sum(_length(wall) for wall in unpaired), 3),
        "thickness_mm_counts": summarize_thickness_counts(walls),
    }


def summarize_candidate_change(
    legacy_local: list[dict[str, Any]], current_local: list[dict[str, Any]], *,
    angle_tolerance_deg: float, lateral_tolerance_m: float,
) -> dict[str, Any]:
    legacy = summarize_candidate_set(legacy_local)
    current = summarize_candidate_set(current_local)
    overlap = compare(
        legacy_local, current_local,
        angle_tolerance_deg=angle_tolerance_deg,
        lateral_tolerance_m=lateral_tolerance_m)
    return {
        "legacy": legacy,
        "current": current,
        "delta": {
            "count": current["count"] - legacy["count"],
            "paired_count": current["paired_count"] - legacy["paired_count"],
            "unpaired_count": current["unpaired_count"] - legacy["unpaired_count"],
            "total_length_m": round(
                current["total_length_m"] - legacy["total_length_m"], 3),
            "paired_length_m": round(
                current["paired_length_m"] - legacy["paired_length_m"], 3),
            "unpaired_length_m": round(
                current["unpaired_length_m"] - legacy["unpaired_length_m"], 3),
        },
        "geometry_overlap_in_dxf_grid_local_coordinates": {
            "legacy_covered_by_current_at_least_50pct_count": overlap[
                "actual_instance_coverage"]["at_least_50pct_count"],
            "legacy_covered_by_current_at_least_50pct_rate": overlap[
                "actual_instance_coverage"]["at_least_50pct_rate"],
            "current_covered_by_legacy_at_least_50pct_count": overlap[
                "prediction_coverage"]["at_least_50pct_count"],
            "current_covered_by_legacy_at_least_50pct_rate": overlap[
                "prediction_coverage"]["at_least_50pct_rate"],
            "maximum_strict_one_to_one_match_count": overlap[
                "maximum_one_to_one_match_count"],
            "legacy_length_covered_by_current_rate": overlap[
                "length_weighted_recall"],
            "current_length_covered_by_legacy_rate": overlap[
                "length_weighted_precision"],
        },
    }


def summarize_unpaired_marginal_gain(
    paired_result: dict[str, Any], all_result: dict[str, Any]
) -> dict[str, Any]:
    paired_actual = paired_result["actual_instance_coverage"]
    all_actual = all_result["actual_instance_coverage"]
    return {
        "additional_candidate_count": (
            all_result["predicted_count"] - paired_result["predicted_count"]),
        "additional_candidate_length_m": round(
            all_result["predicted_total_length_m"]
            - paired_result["predicted_total_length_m"], 3),
        "additional_revit_walls_touched_count": (
            all_actual["any_count"] - paired_actual["any_count"]),
        "additional_revit_walls_covered_at_least_50pct_count": (
            all_actual["at_least_50pct_count"]
            - paired_actual["at_least_50pct_count"]),
        "additional_revit_covered_length_m": round(
            all_result["actual_covered_length_m"]
            - paired_result["actual_covered_length_m"], 3),
        "length_recall_delta": round(
            all_result["length_weighted_recall"]
            - paired_result["length_weighted_recall"], 6),
    }


def summarize_sensitivity_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "actual_walls_touched_count": result["actual_instance_coverage"]["any_count"],
        "actual_walls_covered_at_least_50pct_count": result[
            "actual_instance_coverage"]["at_least_50pct_count"],
        "actual_covered_length_m": result["actual_covered_length_m"],
        "length_weighted_recall": result["length_weighted_recall"],
        "predictions_covered_at_least_50pct_count": result[
            "prediction_coverage"]["at_least_50pct_count"],
        "length_weighted_precision": result["length_weighted_precision"],
    }


def render_overlay(
    path: Path, actual: list[dict[str, Any]], datasets: list[tuple[str, list[dict[str, Any]], dict[str, Any]]]
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    column_count = 1 if len(datasets) == 1 else (3 if len(datasets) >= 5 else 2)
    row_count = math.ceil(len(datasets) / column_count)
    figure, axes = plt.subplots(
        row_count, column_count, figsize=(18, 3.7 * row_count),
        sharex=True, sharey=True, squeeze=False)
    axes_list = list(axes.flat)
    actual_points = [
        point for wall in actual
        for point in (_point(wall["start"]), _point(wall["end"]))
    ]
    x_min, x_max = min(point[0] for point in actual_points), max(point[0] for point in actual_points)
    y_min, y_max = min(point[1] for point in actual_points), max(point[1] for point in actual_points)

    for axis, (title, predicted, comparison) in zip(axes_list, datasets):
        coverage = {row["id"]: row["coverage"] for row in comparison["actual_rows"]}
        for wall in actual:
            start, end = _point(wall["start"]), _point(wall["end"])
            color = "#2ca02c" if coverage[wall["id"]] >= 0.5 else "#d62728"
            axis.plot([start[0], end[0]], [start[1], end[1]], color=color,
                      linewidth=0.65, alpha=0.65, solid_capstyle="butt")
        for wall in predicted:
            start, end = _point(wall["start"]), _point(wall["end"])
            paired = bool(wall.get("paired"))
            axis.plot([start[0], end[0]], [start[1], end[1]],
                      color="#1f77b4" if paired else "#ff7f0e",
                      linewidth=1.35, alpha=0.9,
                      linestyle="-" if paired else "--")
        instance = comparison["actual_instance_coverage"]["at_least_50pct_rate"] * 100.0
        length = comparison["length_weighted_recall"] * 100.0
        outside_bbox = sum(
            max(wall["start"][0], wall["end"][0]) < x_min
            or min(wall["start"][0], wall["end"][0]) > x_max
            or max(wall["start"][1], wall["end"][1]) < y_min
            or min(wall["start"][1], wall["end"][1]) > y_max
            for wall in predicted
        )
        axis.set_title(
            f"{title}\ninstance>=50% {instance:.1f}% | length {length:.1f}%"
            f" | off-bbox {outside_bbox}", fontsize=10)
        axis.set_aspect("equal", adjustable="box")
        axis.grid(True, linewidth=0.3, alpha=0.25)
        axis.set_xlabel("Revit project X (m)")
        axis.set_xlim(x_min - 5.0, x_max + 5.0)
        axis.set_ylim(y_min - 5.0, y_max + 5.0)
    for axis in axes_list[::column_count]:
        axis.set_ylabel("Revit project Y (m)")
    for axis in axes_list[len(datasets):]:
        axis.set_visible(False)
    handles = [
        Line2D([0], [0], color="#2ca02c", lw=2, label="Revit wall covered >=50%"),
        Line2D([0], [0], color="#d62728", lw=2, label="Revit wall missed"),
        Line2D([0], [0], color="#1f77b4", lw=2, label="DXF paired wall"),
        Line2D([0], [0], color="#ff7f0e", lw=2, ls="--", label="DXF unpaired proposal"),
    ]
    figure.legend(handles=handles, loc="lower center", ncol=4, frameon=False)
    figure.tight_layout(rect=(0, 0.045, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=190, bbox_inches="tight")
    plt.close(figure)


def _without_details(comparison: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in comparison.items()
            if key not in {"actual_rows", "predicted_rows"}}


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    dump = _load(args.revit_dump)
    grid = _load(args.grid)
    review = _load(args.review)
    actual = _revit_walls(dump)
    legacy_local = _review_walls(review)
    current_local, extraction = await _current_dxf_walls(args.dxf, grid)
    current_local, source_filter = _filter_walls_by_source_layers(
        current_local,
        include_patterns=args.include_source_layer_pattern,
        exclude_patterns=args.exclude_source_layer_pattern)
    extraction = {**extraction, "source_filter": source_filter}
    diagnostic_s_local, diagnostic_s_extraction = _diagnostic_group_walls(
        args.dxf, grid, ("S",))
    diagnostic_a_plus_s_local = [*current_local, *diagnostic_s_local]
    translation = derive_grid_translation(dump, grid)
    normalization = derive_grid_normalization(translation)
    legacy_absolute = _translated(legacy_local, translation["offset_m"])
    current_absolute = _translated(current_local, translation["offset_m"])
    diagnostic_s_absolute = _translated(
        diagnostic_s_local, translation["offset_m"])
    diagnostic_a_plus_s_absolute = _translated(
        diagnostic_a_plus_s_local, translation["offset_m"])
    legacy_normalized = _grid_normalized(legacy_local, normalization)
    current_normalized = _grid_normalized(current_local, normalization)
    diagnostic_s_normalized = _grid_normalized(
        diagnostic_s_local, normalization)
    diagnostic_a_plus_s_normalized = _grid_normalized(
        diagnostic_a_plus_s_local, normalization)

    tolerance = {
        "angle_deg": args.angle_tolerance_deg,
        "lateral_m": args.lateral_tolerance_m,
        "instance_coverage_threshold": 0.5,
    }
    def compare_sets(
        legacy_walls: list[dict[str, Any]], current_walls: list[dict[str, Any]],
        diagnostic_s_walls: list[dict[str, Any]],
        diagnostic_a_plus_s_walls: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        current_paired = [wall for wall in current_walls if wall.get("paired")]
        current_unpaired = [wall for wall in current_walls if not wall.get("paired")]
        diagnostic_s_paired = [
            wall for wall in diagnostic_s_walls if wall.get("paired")]
        return {
            "legacy_standard_71": compare(
                actual, legacy_walls, angle_tolerance_deg=args.angle_tolerance_deg,
                lateral_tolerance_m=args.lateral_tolerance_m),
            "current_paired_only_56": compare(
                actual, current_paired, angle_tolerance_deg=args.angle_tolerance_deg,
                lateral_tolerance_m=args.lateral_tolerance_m),
            "current_unpaired_only_22": compare(
                actual, current_unpaired, angle_tolerance_deg=args.angle_tolerance_deg,
                lateral_tolerance_m=args.lateral_tolerance_m),
            "current_all_78": compare(
                actual, current_walls, angle_tolerance_deg=args.angle_tolerance_deg,
                lateral_tolerance_m=args.lateral_tolerance_m),
            "diagnostic_s_paired_only_656": compare(
                actual, diagnostic_s_paired,
                angle_tolerance_deg=args.angle_tolerance_deg,
                lateral_tolerance_m=args.lateral_tolerance_m),
            "diagnostic_s_all_832": compare(
                actual, diagnostic_s_walls,
                angle_tolerance_deg=args.angle_tolerance_deg,
                lateral_tolerance_m=args.lateral_tolerance_m),
            "diagnostic_a_plus_s_all_910": compare(
                actual, diagnostic_a_plus_s_walls,
                angle_tolerance_deg=args.angle_tolerance_deg,
                lateral_tolerance_m=args.lateral_tolerance_m),
        }

    absolute_results = compare_sets(
        legacy_absolute, current_absolute, diagnostic_s_absolute,
        diagnostic_a_plus_s_absolute)
    normalized_results = compare_sets(
        legacy_normalized, current_normalized, diagnostic_s_normalized,
        diagnostic_a_plus_s_normalized)
    compact_absolute = {
        key: _without_details(value) for key, value in absolute_results.items()
    }
    compact_normalized = {
        key: _without_details(value) for key, value in normalized_results.items()
    }
    grid_normalized_sensitivity = {}
    for lateral_tolerance in sorted({args.lateral_tolerance_m, 0.3}):
        if math.isclose(lateral_tolerance, args.lateral_tolerance_m):
            sensitivity_results = {
                "current_a_only_78": normalized_results["current_all_78"],
                "diagnostic_s_only_832": normalized_results["diagnostic_s_all_832"],
                "diagnostic_a_plus_s_910": normalized_results[
                    "diagnostic_a_plus_s_all_910"],
            }
        else:
            sensitivity_results = {
                "current_a_only_78": compare(
                    actual, current_normalized,
                    angle_tolerance_deg=args.angle_tolerance_deg,
                    lateral_tolerance_m=lateral_tolerance),
                "diagnostic_s_only_832": compare(
                    actual, diagnostic_s_normalized,
                    angle_tolerance_deg=args.angle_tolerance_deg,
                    lateral_tolerance_m=lateral_tolerance),
                "diagnostic_a_plus_s_910": compare(
                    actual, diagnostic_a_plus_s_normalized,
                    angle_tolerance_deg=args.angle_tolerance_deg,
                    lateral_tolerance_m=lateral_tolerance),
            }
        grid_normalized_sensitivity[f"lateral_{lateral_tolerance:g}m"] = {
            key: summarize_sensitivity_result(value)
            for key, value in sensitivity_results.items()
        }
    candidate_change = summarize_candidate_change(
        legacy_local, current_local,
        angle_tolerance_deg=args.angle_tolerance_deg,
        lateral_tolerance_m=args.lateral_tolerance_m)
    report = {
        "schema_version": "buildmate.revit-dxf-wall-comparison/1.1",
        "sources": {
            "revit_dump": str(args.revit_dump.resolve()),
            "dxf": str(args.dxf.resolve()),
            "legacy_review": str(args.review.resolve()),
            "grid": str(args.grid.resolve()),
            "revit_project": dump.get("project"),
            "dxf_extraction": extraction,
            "diagnostic_s_extraction": diagnostic_s_extraction,
        },
        "alignment": {
            "absolute_project_coordinates": translation,
            "grid_normalized": {
                **normalization,
                "legacy_extent": _normalization_extent_stats(
                    legacy_local, normalization),
                "current_extent": _normalization_extent_stats(
                    current_local, normalization),
                "diagnostic_s_extent": _normalization_extent_stats(
                    diagnostic_s_local, normalization),
                "diagnostic_a_plus_s_extent": _normalization_extent_stats(
                    diagnostic_a_plus_s_local, normalization),
            },
        },
        "coordinate_interpretation": {
            "absolute_project_coordinates": (
                "DXF grid origin 1/A translated to the matching Revit grids; "
                "other named-grid differences remain visible."),
            "grid_normalized": (
                "DXF coordinates piecewise mapped through every matching named "
                "grid; diagnostic for layout recognition only, not proof of "
                "absolute coordinate accuracy."),
        },
        "tolerance": tolerance,
        "ground_truth": {
            "wall_count": len(actual),
            "total_length_m": round(sum(_length(wall) for wall in actual), 3),
            "shorter_than_0_5m_count": sum(_length(wall) < 0.5 for wall in actual),
            "thickness_mm_counts": summarize_thickness_counts(actual),
        },
        "candidate_change_71_to_78": candidate_change,
        "diagnostic_candidate_sets": {
            "current_a_only_78": summarize_candidate_set(current_local),
            "diagnostic_s_only_832": summarize_candidate_set(diagnostic_s_local),
            "diagnostic_a_plus_s_910": summarize_candidate_set(
                diagnostic_a_plus_s_local),
        },
        "comparisons": {
            "absolute_project_coordinates": compact_absolute,
            "grid_normalized": compact_normalized,
        },
        "grid_normalized_lateral_tolerance_sensitivity": {
            "interpretation": (
                "0.30 m is a generous diagnostic upper bound; it can match "
                "nearby parallel walls and must not be read as production accuracy."),
            "results": grid_normalized_sensitivity,
        },
        "unpaired_22_marginal_gain": {
            "absolute_project_coordinates": summarize_unpaired_marginal_gain(
                absolute_results["current_paired_only_56"],
                absolute_results["current_all_78"]),
            "grid_normalized": summarize_unpaired_marginal_gain(
                normalized_results["current_paired_only_56"],
                normalized_results["current_all_78"]),
        },
        "diagnostic_s_unpaired_176_marginal_gain": {
            "absolute_project_coordinates": summarize_unpaired_marginal_gain(
                absolute_results["diagnostic_s_paired_only_656"],
                absolute_results["diagnostic_s_all_832"]),
            "grid_normalized": summarize_unpaired_marginal_gain(
                normalized_results["diagnostic_s_paired_only_656"],
                normalized_results["diagnostic_s_all_832"]),
        },
        "miss_analysis_current_all": {
            "absolute_project_coordinates": summarize_causes(
                actual, absolute_results["current_all_78"]),
            "grid_normalized": summarize_causes(
                actual, normalized_results["current_all_78"]),
        },
        "details": {
            "absolute_project_coordinates": absolute_results,
            "grid_normalized": normalized_results,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    if args.overlay:
        render_overlay(args.overlay, actual, [
            (f"Absolute | old {len(legacy_absolute)}", legacy_absolute,
             absolute_results["legacy_standard_71"]),
            (f"Absolute | A-only {len(current_absolute)}", current_absolute,
             absolute_results["current_all_78"]),
            (f"Absolute | S-only {len(diagnostic_s_absolute)}",
             diagnostic_s_absolute,
             absolute_results["diagnostic_s_all_832"]),
            (f"Grid-normalized | old {len(legacy_normalized)}",
             legacy_normalized,
             normalized_results["legacy_standard_71"]),
            (f"Grid-normalized | A-only {len(current_normalized)}",
             current_normalized,
             normalized_results["current_all_78"]),
            (f"Grid-normalized | S-only {len(diagnostic_s_normalized)}",
             diagnostic_s_normalized,
             normalized_results["diagnostic_s_all_832"]),
        ])
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revit-dump", type=Path, required=True)
    parser.add_argument("--dxf", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--grid", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--angle-tolerance-deg", type=float, default=2.0)
    parser.add_argument("--lateral-tolerance-m", type=float, default=0.15)
    parser.add_argument("--include-source-layer-pattern", action="append",
                        default=[])
    parser.add_argument("--exclude-source-layer-pattern", action="append",
                        default=[])
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    print(json.dumps({
        "alignment": report["alignment"],
        "ground_truth": report["ground_truth"],
        "comparisons": report["comparisons"],
        "output": str(args.output),
        "overlay": str(args.overlay) if args.overlay else None,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
