"""Trace extracted columns back to exact placed DXF profile segments.

This module is intentionally independent from the modeling pipeline.  It
enriches copies of existing column dictionaries and returns an audit report;
it never changes column geometry or writes files.
"""

from __future__ import annotations

import copy
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any


_CENTER_TOLERANCE_M = 0.005
_SIZE_TOLERANCE_M = 0.001
_MATCH_EPSILON_M = 1e-9
_RECTANGLE_ANGLE_TOLERANCE_DEG = 2.0


def _record_parts(record: Any) -> tuple[Any, str, dict]:
    if not isinstance(record, (tuple, list)) or len(record) < 3:
        return None, "", {}
    provenance = record[2] if isinstance(record[2], dict) else {}
    return record[0], str(record[1] or ""), provenance


def _semantic_column_layer(layer: str) -> bool:
    leaf = (layer or "").upper().rsplit("$0$", 1)[-1]
    return "S-COLU" in leaf or "COLUMN" in leaf or "柱" in leaf


def _affine_key(marker: dict) -> tuple | None:
    transform = marker.get("cumulative_transform")
    matrix = transform.get("affine_2d") if isinstance(transform, dict) else None
    if not isinstance(matrix, (list, tuple)) or len(matrix) != 3:
        return None
    try:
        normalized = tuple(
            tuple(round(float(value), 9) for value in row)
            for row in matrix
        )
    except (TypeError, ValueError):
        return None
    if any(len(row) != 3 for row in normalized):
        return None
    name = marker.get("block_name") or marker.get("name")
    handle = marker.get("insert_handle")
    if not name or not handle:
        return None
    array_index = marker.get("array_index")
    if isinstance(array_index, list):
        array_index = tuple(array_index)
    return str(name), str(handle), array_index, normalized


def _complete_placement_path(path: Any) -> bool:
    return bool(
        isinstance(path, list) and path and
        all(isinstance(marker, dict) and _affine_key(marker) is not None
            for marker in path)
    )


def _resolve_parent_marker(records: list[Any], selected_id: str) -> tuple:
    marker_by_key: dict[tuple, dict] = {}
    drawing_ids = set()
    anchor_count = 0
    for record in records:
        _entity, _layer, provenance = _record_parts(record)
        if provenance.get("structural_occurrence_id") != selected_id:
            continue
        anchor_count += 1
        drawing_identity = provenance.get("drawing_identity")
        if drawing_identity:
            drawing_ids.add(str(drawing_identity))
        path = provenance.get("placement_path")
        if not _complete_placement_path(path):
            continue
        marker = path[-1]
        key = _affine_key(marker)
        if key is not None:
            marker_by_key[key] = marker
    marker = None
    marker_key = None
    if len(marker_by_key) == 1 and len(drawing_ids) == 1:
        marker_key, marker = next(iter(marker_by_key.items()))
    return marker_key, copy.deepcopy(marker), drawing_ids, anchor_count


def _transform_point(point: Sequence[float], origin: Sequence[float],
                     rotation_deg: float,
                     grid_center: Sequence[float]) -> list[float]:
    x = float(point[0]) - float(grid_center[0])
    y = float(point[1]) - float(grid_center[1])
    radians = math.radians(float(rotation_deg))
    cosine, sine = math.cos(radians), math.sin(radians)
    rotated_x = x * cosine - y * sine + float(grid_center[0])
    rotated_y = x * sine + y * cosine + float(grid_center[1])
    return [
        (rotated_x - float(origin[0])) / 1000.0,
        (rotated_y - float(origin[1])) / 1000.0,
    ]


def _is_rectangle(points: list[list[float]]) -> bool:
    if len(points) != 4:
        return False
    edges = [
        (points[(index + 1) % 4][0] - points[index][0],
         points[(index + 1) % 4][1] - points[index][1])
        for index in range(4)
    ]
    lengths = [math.hypot(*edge) for edge in edges]
    if any(length <= 1e-9 for length in lengths):
        return False
    angle_limit = math.sin(math.radians(_RECTANGLE_ANGLE_TOLERANCE_DEG))
    for index in range(4):
        first, second = edges[index], edges[(index + 1) % 4]
        cosine = abs(
            (first[0] * second[0] + first[1] * second[1]) /
            (lengths[index] * lengths[(index + 1) % 4])
        )
        if cosine > angle_limit:
            return False
    return True


def _identity_limitations(provenance: dict, marker_key: tuple,
                          drawing_identity: str) -> list[str]:
    limitations = []
    for key in ("drawing_identity", "structural_occurrence_id",
                "source_occurrence_id", "placed_entity_id",
                "source_entity_handle"):
        if not provenance.get(key):
            limitations.append(f"{key}_missing")
    if provenance.get("drawing_identity") != drawing_identity:
        limitations.append("drawing_identity_mismatch")
    path = provenance.get("placement_path")
    if not _complete_placement_path(path):
        limitations.append("placement_path_incomplete")
    elif marker_key not in {_affine_key(marker) for marker in path}:
        limitations.append("selected_occurrence_marker_missing")
    segment_ids = provenance.get("segment_ids")
    if (not isinstance(segment_ids, list) or len(segment_ids) != 4 or
            any(not segment_id for segment_id in segment_ids)):
        limitations.append("four_segment_ids_required")
    elif len(set(segment_ids)) != 4:
        limitations.append("segment_ids_not_unique")
    return limitations


def _percentiles(values: list[float]) -> dict:
    if not values:
        return {"min": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = fraction * (len(ordered) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return {
        "min": round(ordered[0], 9),
        "p50": round(percentile(0.50), 9),
        "p95": round(percentile(0.95), 9),
        "max": round(ordered[-1], 9),
    }


def _base_audit(column_count: int, record_count: int,
                selected_id: str, center_tolerance_m: float,
                size_tolerance_m: float) -> dict:
    return {
        "status": "REVIEW",
        "auto_action": "NONE",
        "geometry_authority": "DXF_VECTOR",
        "selected_structural_occurrence_id": selected_id,
        "input_column_count": column_count,
        "vector_source_record_count": record_count,
        "semantic_closed_profile_count": 0,
        "selected_candidate_count": 0,
        "valid_geometry_candidate_count": 0,
        "eligible_candidate_count": 0,
        "matched_count": 0,
        "missing_count": column_count,
        "ambiguous_count": 0,
        "ambiguous_candidate_count": 0,
        "unmatched_candidate_count": 0,
        "identity_complete_count": 0,
        "identity_incomplete_count": 0,
        "invalid_geometry_count": 0,
        "center_tolerance_m": center_tolerance_m,
        "size_tolerance_m": size_tolerance_m,
        "match_epsilon_m": _MATCH_EPSILON_M,
        "center_error_m": _percentiles([]),
        "size_error_m": _percentiles([]),
        "one_to_one_complete": False,
        "selected_parent_marker": None,
        "matches": [],
        "missing_columns": [],
        "ambiguous_columns": [],
        "identity_issues": [],
        "blockers": [],
    }


def enrich_columns_with_provenance(
        columns: list[dict], vector_source_records: Iterable[Any], *,
        origin_dxf_mm: Sequence[float], rotation_deg: float,
        grid_center_dxf_mm: Sequence[float],
        selected_structural_occurrence_id: str,
        center_tolerance_m: float = _CENTER_TOLERANCE_M,
        size_tolerance_m: float = _SIZE_TOLERANCE_M,
) -> tuple[list[dict], dict]:
    """Return copied columns linked to exact placed four-edge DXF profiles.

    The selected structural occurrence is resolved to its parent INSERT marker
    first.  This is necessary because a selected plan may contain translated
    or overlaid copies of the same column diagram.  Only complete, uniquely
    matched evidence is attached to a column.
    """
    enriched = copy.deepcopy(columns) if isinstance(columns, list) else []
    records = list(vector_source_records or [])
    selected_id = str(selected_structural_occurrence_id or "")
    audit = _base_audit(
        len(enriched), len(records), selected_id,
        center_tolerance_m, size_tolerance_m)

    try:
        if (not selected_id or len(origin_dxf_mm) != 2 or
                len(grid_center_dxf_mm) != 2 or
                center_tolerance_m < 0 or size_tolerance_m < 0):
            raise ValueError("invalid matching configuration")
        origin = [float(value) for value in origin_dxf_mm]
        grid_center = [float(value) for value in grid_center_dxf_mm]
        rotation = float(rotation_deg)
    except (TypeError, ValueError, OverflowError):
        audit["blockers"].append("invalid_coordinate_or_tolerance_input")
        return enriched, audit

    marker_key, marker, drawing_ids, anchor_count = _resolve_parent_marker(
        records, selected_id)
    audit["selected_occurrence_anchor_count"] = anchor_count
    if marker_key is None or marker is None:
        audit["blockers"].append(
            "selected_structural_occurrence_parent_marker_not_unique")
        return enriched, audit
    drawing_identity = next(iter(drawing_ids))
    audit["selected_parent_marker"] = marker

    candidates = []
    semantic_count = 0
    selected_semantic_count = 0
    invalid_geometry_count = 0
    for record_index, record in enumerate(records):
        entity, layer, provenance = _record_parts(record)
        if (entity is None or not _semantic_column_layer(layer) or
                not hasattr(entity, "dxftype") or
                entity.dxftype() != "LWPOLYLINE" or
                not bool(getattr(entity, "closed", False))):
            continue
        try:
            raw_points = list(entity.get_points("xyb"))
        except (AttributeError, TypeError, ValueError):
            continue
        if len(raw_points) != 4:
            continue
        semantic_count += 1
        path = provenance.get("placement_path")
        if (not isinstance(path, list) or
                marker_key not in {_affine_key(item) for item in path
                                   if isinstance(item, dict)}):
            continue
        selected_semantic_count += 1
        limitations = _identity_limitations(
            provenance, marker_key, drawing_identity)
        try:
            if any(abs(float(point[2])) > 1e-9 for point in raw_points):
                raise ValueError("bulged profile")
            wcs_points = list(entity.vertices_in_wcs())
            local_points = [
                _transform_point(point, origin, rotation, grid_center)
                for point in wcs_points
            ]
            if not _is_rectangle(local_points):
                raise ValueError("not a rectangle")
        except (AttributeError, TypeError, ValueError, OverflowError):
            invalid_geometry_count += 1
            continue
        xs = [point[0] for point in local_points]
        ys = [point[1] for point in local_points]
        center = [sum(xs) / 4.0, sum(ys) / 4.0]
        size = [max(xs) - min(xs), max(ys) - min(ys)]
        segment_ids = provenance.get("segment_ids") or []
        profile_ref = {
            "geometry_source": "DXF_VECTOR",
            "semantic_role": "COLUMN_PROFILE",
            "layer": layer,
            "drawing_identity": provenance.get("drawing_identity"),
            "selected_structural_occurrence_id": selected_id,
            "source_structural_occurrence_id": provenance.get(
                "structural_occurrence_id"),
            "source_occurrence_id": provenance.get("source_occurrence_id"),
            "placed_entity_id": provenance.get("placed_entity_id"),
            "source_entity_handle": provenance.get("source_entity_handle"),
            "entity_type": "LWPOLYLINE",
            "entity_closed": True,
            "vertex_count": 4,
            "center_local_m": [round(value, 9) for value in center],
            "size_local_m": [round(value, 9) for value in size],
            "source_segment_ids": list(segment_ids),
            "placement_path": copy.deepcopy(path),
        }
        segment_refs = []
        if len(segment_ids) == 4:
            for segment_index in range(4):
                segment_refs.append({
                    "geometry_source": "DXF_VECTOR",
                    "semantic_role": "COLUMN_PROFILE_EDGE",
                    "segment_index": segment_index,
                    "source_segment_id": segment_ids[segment_index],
                    "drawing_identity": provenance.get("drawing_identity"),
                    "selected_structural_occurrence_id": selected_id,
                    "source_structural_occurrence_id": provenance.get(
                        "structural_occurrence_id"),
                    "source_occurrence_id": provenance.get(
                        "source_occurrence_id"),
                    "placed_entity_id": provenance.get("placed_entity_id"),
                    "source_entity_handle": provenance.get(
                        "source_entity_handle"),
                    "layer": layer,
                    "start_local_m": [round(value, 9) for value in
                                      local_points[segment_index]],
                    "end_local_m": [round(value, 9) for value in
                                    local_points[(segment_index + 1) % 4]],
                })
        candidates.append({
            "record_index": record_index,
            "center": center,
            "size": size,
            "profile_ref": profile_ref,
            "segment_refs": segment_refs,
            "identity_limitations": limitations,
        })

    audit["semantic_closed_profile_count"] = semantic_count
    audit["selected_candidate_count"] = selected_semantic_count
    audit["valid_geometry_candidate_count"] = len(candidates)
    audit["invalid_geometry_count"] = invalid_geometry_count

    placed_counts = Counter(
        candidate["profile_ref"].get("placed_entity_id")
        for candidate in candidates
        if candidate["profile_ref"].get("placed_entity_id"))
    occurrence_counts = Counter(
        candidate["profile_ref"].get("source_occurrence_id")
        for candidate in candidates
        if candidate["profile_ref"].get("source_occurrence_id"))
    segment_counts = Counter(
        segment_id
        for candidate in candidates
        for segment_id in candidate["profile_ref"].get(
            "source_segment_ids", [])
        if segment_id)
    for candidate in candidates:
        profile_ref = candidate["profile_ref"]
        if placed_counts[profile_ref.get("placed_entity_id")] > 1:
            candidate["identity_limitations"].append(
                "placed_entity_id_not_unique")
        if occurrence_counts[profile_ref.get("source_occurrence_id")] > 1:
            candidate["identity_limitations"].append(
                "source_occurrence_id_not_unique")
        if any(segment_counts[segment_id] > 1 for segment_id in
               profile_ref.get("source_segment_ids", [])):
            candidate["identity_limitations"].append(
                "source_segment_id_not_unique")

    complete_candidates = []
    for index, candidate in enumerate(candidates):
        limitations = candidate["identity_limitations"]
        if limitations:
            audit["identity_issues"].append({
                "candidate_index": index,
                "placed_entity_id": candidate["profile_ref"].get(
                    "placed_entity_id"),
                "limitations": limitations,
            })
        else:
            complete_candidates.append((index, candidate))
    audit["identity_complete_count"] = len(complete_candidates)
    audit["identity_incomplete_count"] = (
        len(candidates) - len(complete_candidates))
    audit["eligible_candidate_count"] = len(complete_candidates)

    column_edges: list[list[int]] = [[] for _ in enriched]
    candidate_edges: dict[int, list[int]] = {
        index: [] for index, _candidate in complete_candidates}
    pair_errors: dict[tuple[int, int], tuple[float, float]] = {}
    invalid_column_indices = []
    for column_index, column in enumerate(enriched):
        try:
            center = [float(value) for value in column["center"]]
            size = [float(value) for value in column["size"]]
            if (len(center) != 2 or len(size) != 2 or
                    any(value <= 0 for value in size)):
                raise ValueError("invalid column geometry")
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid_column_indices.append(column_index)
            continue
        for candidate_index, candidate in complete_candidates:
            center_error = math.dist(center, candidate["center"])
            size_error = max(
                abs(size[0] - candidate["size"][0]),
                abs(size[1] - candidate["size"][1]))
            if (center_error <= center_tolerance_m + _MATCH_EPSILON_M and
                    size_error <= size_tolerance_m + _MATCH_EPSILON_M):
                column_edges[column_index].append(candidate_index)
                candidate_edges[candidate_index].append(column_index)
                pair_errors[(column_index, candidate_index)] = (
                    center_error, size_error)

    matches = []
    ambiguous_columns = []
    missing_columns = []
    for column_index, edges in enumerate(column_edges):
        if column_index in invalid_column_indices or not edges:
            missing_columns.append(column_index)
            continue
        if (len(edges) != 1 or
                len(candidate_edges.get(edges[0], [])) != 1):
            ambiguous_columns.append({
                "column_index": column_index,
                "candidate_indices": sorted(edges),
            })
            continue
        candidate_index = edges[0]
        candidate = candidates[candidate_index]
        center_error, size_error = pair_errors[
            (column_index, candidate_index)]
        enriched[column_index]["source_profile_ref"] = copy.deepcopy(
            candidate["profile_ref"])
        enriched[column_index]["source_segment_refs"] = copy.deepcopy(
            candidate["segment_refs"])
        matches.append({
            "column_index": column_index,
            "candidate_index": candidate_index,
            "placed_entity_id": candidate["profile_ref"][
                "placed_entity_id"],
            "center_error_m": round(center_error, 9),
            "size_error_m": round(size_error, 9),
        })

    matched_candidate_indices = {
        item["candidate_index"] for item in matches}
    ambiguous_candidate_indices = {
        candidate_index
        for item in ambiguous_columns
        for candidate_index in item["candidate_indices"]
    }
    audit["matches"] = matches
    audit["matched_count"] = len(matches)
    audit["missing_columns"] = missing_columns
    audit["missing_count"] = len(missing_columns)
    audit["ambiguous_columns"] = ambiguous_columns
    audit["ambiguous_count"] = len(ambiguous_columns)
    audit["ambiguous_candidate_count"] = len(
        ambiguous_candidate_indices)
    audit["unmatched_candidate_count"] = sum(
        candidate_index not in matched_candidate_indices
        for candidate_index, _candidate in complete_candidates)
    audit["center_error_m"] = _percentiles([
        item["center_error_m"] for item in matches])
    audit["size_error_m"] = _percentiles([
        item["size_error_m"] for item in matches])
    audit["one_to_one_complete"] = bool(
        enriched and len(matches) == len(enriched) == len(candidates) and
        not missing_columns and not ambiguous_columns and
        not audit["identity_incomplete_count"] and
        not invalid_geometry_count)
    if audit["one_to_one_complete"]:
        audit["status"] = "PASS"
    else:
        if invalid_column_indices:
            audit["blockers"].append("invalid_column_geometry")
        if invalid_geometry_count:
            audit["blockers"].append("invalid_column_profile_geometry")
        if audit["identity_incomplete_count"]:
            audit["blockers"].append("column_profile_identity_incomplete")
        if missing_columns:
            audit["blockers"].append("column_profile_match_missing")
        if ambiguous_columns:
            audit["blockers"].append("column_profile_match_ambiguous")
        if audit["unmatched_candidate_count"]:
            audit["blockers"].append("unmatched_column_profile_candidate")
    return enriched, audit


__all__ = ["enrich_columns_with_provenance"]
