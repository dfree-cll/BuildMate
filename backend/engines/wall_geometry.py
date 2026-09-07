"""DXF 建筑墙精确几何：双线配对、退化过滤与拓扑质量。

坐标输入为 DXF 毫米，输出为项目局部米制。算法只配对真正共向且投影重叠的线，
避免旧实现把相距很远的平行线配成墙，或因端点方向相反生成零长度中心线。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from typing import Any

from backend.engines.short_wall_approval import (
    proposal_integrity_sha256,
    source_reference_from_review_unit,
    wall_precondition_sha256,
)


def _angle_delta(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def wall_geometry_group(layer: str) -> str | None:
    """Return the semantic wall group encoded by the leaf layer name.

    Nested block names are carried in converted DXF layers as ``$0$`` prefixes.
    Only the leaf is semantic: a bathroom block containing ``A-PART-S`` is still
    wall geometry, while a hydrant symbol merely containing ``WALL`` in an
    ancestor name is not.
    """
    leaf = (layer or "").upper().strip().rsplit("$0$", 1)[-1]
    if leaf == "S-WALL":
        return "S"
    if leaf in {"A-WALL", "A-WALL-S", "A-PART", "A-PART-S", "A-后砌",
                "A-MASONRY", "WALL"}:
        return "A"
    return None


def _is_wall_geometry_layer(layer: str) -> bool:
    """Accept structural and architectural wall geometry, not text/hatches."""
    return wall_geometry_group(layer) is not None


def _record_parts(record):
    """Accept legacy ``(entity, layer)`` and provenance-aware records."""
    if len(record) < 2:
        raise ValueError("wall record must contain entity and layer")
    return record[0], record[1], record[2] if len(record) > 2 else None


def _provenance_segment_id(provenance: dict | None,
                           segment_index: int) -> str | None:
    segment_ids = (provenance or {}).get("segment_ids") or []
    if 0 <= segment_index < len(segment_ids):
        return segment_ids[segment_index]
    if len(segment_ids) == 1 and segment_index == 0:
        return segment_ids[0]
    return None


def _provenance_identity_limitations(
        provenance: dict | None, source_handle: str | None,
        segment_index: int | None = None) -> list[str]:
    """Return exact missing pieces for a placed DXF entity/segment identity."""
    provenance = provenance or {}
    placement_path = provenance.get("placement_path") or []
    limitations = []
    if not provenance.get("drawing_identity"):
        limitations.append("drawing_identity_missing")
    if not source_handle:
        limitations.append("source_entity_handle_missing")
    if not provenance.get("source_occurrence_id"):
        limitations.append("source_occurrence_id_missing")
    if not provenance.get("placed_entity_id"):
        limitations.append("placed_entity_id_missing")
    if not provenance.get("structural_occurrence_id"):
        limitations.append("structural_occurrence_id_missing")
    if not placement_path:
        limitations.append("placement_path_missing")
    else:
        if not all(isinstance(item, dict) and item.get("insert_handle")
                   for item in placement_path):
            limitations.append("placement_path_insert_handles_missing")
        if not all(isinstance(item, dict) and
                   (item.get("block_name") or item.get("name"))
                   for item in placement_path):
            limitations.append("placement_path_block_names_missing")
        if not all(isinstance(item, dict) and "array_index" in item
                   for item in placement_path):
            limitations.append("placement_path_array_indices_missing")
        if not all(
                isinstance(item, dict) and
                isinstance(item.get("cumulative_transform"), dict) and
                item["cumulative_transform"].get("affine_2d") is not None
                for item in placement_path):
            limitations.append("placement_path_transforms_missing")
    if (segment_index is not None and
            not _provenance_segment_id(provenance, segment_index)):
        limitations.append("source_segment_id_missing")
    return limitations


def _source_segment_reference(entity, provenance: dict | None,
                              source_record_index: int,
                              segment_index: int) -> dict:
    """Return the immutable DXF source identity for one transformed segment."""
    origin = getattr(entity, "origin_of_copy", None) or entity
    source_handle = ((provenance or {}).get("source_entity_handle") or
                     getattr(getattr(origin, "dxf", None), "handle", None))
    limitations = _provenance_identity_limitations(
        provenance, source_handle, segment_index=segment_index)
    return {
        "drawing_identity": (provenance or {}).get("drawing_identity"),
        "source_occurrence_id": (provenance or {}).get(
            "source_occurrence_id"),
        "placed_entity_id": (provenance or {}).get("placed_entity_id"),
        "structural_occurrence_id": (provenance or {}).get(
            "structural_occurrence_id"),
        "source_segment_id": _provenance_segment_id(
            provenance, segment_index),
        "entity_handle": source_handle,
        "source_record_index": source_record_index,
        "segment_index": segment_index,
        "placement_path": copy.deepcopy(
            (provenance or {}).get("placement_path") or []),
        "identity_is_complete": not limitations,
        "identity_limitations": limitations,
    }


def _source_ref_key(reference: dict) -> tuple:
    interval = reference.get("source_interval_m")
    interval_key = tuple(float(value) for value in interval) if (
        isinstance(interval, (list, tuple))) else ()
    return (
        reference.get("drawing_identity"),
        reference.get("source_segment_id"),
        interval_key,
    )


def _stable_source_ref_union(walls: list[dict]) -> list[dict]:
    """Union material sources without folding distinct contributed intervals."""
    result = []
    seen = set()
    for wall in walls:
        for reference in wall.get("source_segment_refs") or []:
            if not isinstance(reference, dict):
                continue
            key = _source_ref_key(reference)
            if key in seen:
                continue
            seen.add(key)
            result.append(copy.deepcopy(reference))
    return result


def _source_ids(references: list[dict]) -> list[str]:
    result = []
    seen = set()
    for reference in references:
        source_id = reference.get("source_segment_id")
        if source_id is None or source_id in seen:
            continue
        seen.add(source_id)
        result.append(source_id)
    return result


def _stable_metadata_union(walls: list[dict], field: str) -> list[dict]:
    result = []
    seen = set()
    for wall in walls:
        value = wall.get(field) or []
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, dict):
                continue
            key = json.dumps(item, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str)
            if key in seen:
                continue
            seen.add(key)
            result.append(copy.deepcopy(item))
    return result


def _copy_wall_lineage(wall: dict) -> dict:
    item = dict(wall)
    for field in ("source_segment_refs", "derivation",
                  "endpoint_adjustments", "geometry_adjustments"):
        if field in wall:
            item[field] = copy.deepcopy(wall[field])
    return item


def _point_segment_distance(point, start, end) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length2 = dx * dx + dy * dy
    if length2 <= 1e-12:
        return math.dist(point, start)
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length2
    t = max(0.0, min(1.0, t))
    return math.hypot(point[0] - start[0] - t * dx,
                      point[1] - start[1] - t * dy)


def _line_record(p1, p2):
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return None
    ux, uy = dx / length, dy / length
    # 统一方向，确保反向绘制的两条边也能正确平均。
    if ux < -1e-9 or (abs(ux) <= 1e-9 and uy < 0):
        p1, p2 = p2, p1
        ux, uy = -ux, -uy
    angle = math.degrees(math.atan2(uy, ux)) % 180.0
    nx, ny = -uy, ux
    return {"p1": p1, "p2": p2, "u": (ux, uy), "n": (nx, ny),
            "angle": angle, "length": length}


def _pair_geometry(a: dict, b: dict, min_overlap_ratio: float = 0.6):
    if _angle_delta(a["angle"], b["angle"]) > 2.0:
        return None
    ux, uy = a["u"]
    nx, ny = a["n"]
    origin = a["p1"]

    def projection(p):
        vx, vy = p[0] - origin[0], p[1] - origin[1]
        return vx * ux + vy * uy, vx * nx + vy * ny

    a0, a1 = 0.0, a["length"]
    bp = [projection(b["p1"]), projection(b["p2"])]
    b0, b1 = sorted((bp[0][0], bp[1][0]))
    overlap0, overlap1 = max(a0, b0), min(a1, b1)
    overlap = overlap1 - overlap0
    if overlap <= 0 or overlap / min(a["length"], b["length"]) < min_overlap_ratio:
        return None
    offset = (bp[0][1] + bp[1][1]) / 2.0
    distance = abs(offset)
    if not 0.08 <= distance <= 0.60:
        return None
    mid_offset = offset / 2.0
    start = [origin[0] + overlap0 * ux + mid_offset * nx,
             origin[1] + overlap0 * uy + mid_offset * ny]
    end = [origin[0] + overlap1 * ux + mid_offset * nx,
           origin[1] + overlap1 * uy + mid_offset * ny]
    # 记录两条源线各自被配对的投影区间，使长边可以与多个不重叠短边配对。
    bux, buy = b["u"]
    b_origin = b["p1"]
    bvals = [((p[0] - b_origin[0]) * bux + (p[1] - b_origin[1]) * buy)
             for p in (start, end)]
    return {"start": start, "end": end, "distance": distance,
            "overlap": overlap, "a_interval": (overlap0, overlap1),
            "b_interval": (min(bvals), max(bvals))}


_SOURCE_FACE_MERGE_ANGLE_TOLERANCE_DEG = 0.05
_SOURCE_FACE_MERGE_OFFSET_TOLERANCE_M = 0.001
_SOURCE_FACE_MERGE_GAP_TOLERANCE_M = 0.001


def _consolidate_overlapping_source_faces(lines: list[dict]) -> list[dict]:
    """Merge overlapping fragments on one exact semantic wall face.

    Some drawings repeat one face as overlapping LINE entities with different
    end points. Pairing those records independently can create two coincident
    model walls. Consolidation is deliberately strict and occurrence-local;
    every original segment remains attached as material provenance.
    """
    baseline_groups: list[dict] = []
    for input_index, line in enumerate(lines):
        reference = line.get("source_segment_ref") or {}
        drawing_identity = reference.get("drawing_identity")
        occurrence_id = reference.get("structural_occurrence_id")
        if drawing_identity and occurrence_id:
            identity_scope = (drawing_identity, occurrence_id)
        else:
            # Incomplete identities must never be merged across records.
            identity_scope = ("INCOMPLETE", input_index)
        scope = (
            line.get("wall_group"), line.get("source_layer"),
            identity_scope,
        )
        matches = []
        for group in baseline_groups:
            representative = group["representative"]
            if (group["scope"] != scope or
                    _angle_delta(line["angle"],
                                 representative["angle"]) >
                    _SOURCE_FACE_MERGE_ANGLE_TOLERANCE_DEG):
                continue
            baseline_error = max(abs(
                (point[0] - representative["p1"][0]) *
                representative["n"][0] +
                (point[1] - representative["p1"][1]) *
                representative["n"][1])
                for point in (line["p1"], line["p2"]))
            if baseline_error <= _SOURCE_FACE_MERGE_OFFSET_TOLERANCE_M:
                matches.append(group)
        # Ambiguous baseline membership stays separate and therefore cannot
        # silently combine evidence from two nearby faces.
        if len(matches) == 1:
            matches[0]["members"].append((input_index, line))
        else:
            baseline_groups.append({
                "scope": scope,
                "representative": line,
                "members": [(input_index, line)],
            })

    consolidated = []
    for group in baseline_groups:
        representative = group["representative"]
        origin = representative["p1"]
        ux, uy = representative["u"]
        projected = []
        for input_index, line in group["members"]:
            values = [
                (point[0] - origin[0]) * ux +
                (point[1] - origin[1]) * uy
                for point in (line["p1"], line["p2"])
            ]
            projected.append({
                "start": min(values),
                "end": max(values),
                "input_index": input_index,
                "line": line,
            })
        projected.sort(key=lambda item: (
            item["start"], item["end"], item["input_index"]))
        runs = []
        for item in projected:
            if (runs and item["start"] <=
                    runs[-1]["end"] + _SOURCE_FACE_MERGE_GAP_TOLERANCE_M):
                runs[-1]["end"] = max(runs[-1]["end"], item["end"])
                runs[-1]["members"].append(item)
            else:
                runs.append({
                    "start": item["start"],
                    "end": item["end"],
                    "members": [item],
                })
        for run in runs:
            start = (origin[0] + run["start"] * ux,
                     origin[1] + run["start"] * uy)
            end = (origin[0] + run["end"] * ux,
                   origin[1] + run["end"] * uy)
            logical_line = _line_record(start, end)
            if logical_line is None:
                continue
            logical_line.update({
                "wall_group": representative["wall_group"],
                "source_layer": representative.get("source_layer"),
                "source_segments": [{
                    "line": item["line"],
                    "reference": copy.deepcopy(
                        item["line"].get("source_segment_ref") or {}),
                } for item in run["members"]],
                "input_order": min(
                    item["input_index"] for item in run["members"]),
            })
            consolidated.append(logical_line)
    consolidated.sort(key=lambda item: item["input_order"])
    return consolidated


def _source_refs_for_logical_interval(line: dict,
                                      interval) -> list[dict]:
    """Map one logical-face interval back to every contributing DXF segment."""
    try:
        interval_start, interval_end = sorted(
            (float(interval[0]), float(interval[1])))
    except (IndexError, TypeError, ValueError, OverflowError):
        return []
    origin = line["p1"]
    ux, uy = line["u"]
    references = []
    seen = set()
    for source in line.get("source_segments") or []:
        source_line = source.get("line")
        reference = source.get("reference")
        if not isinstance(source_line, dict) or not isinstance(reference, dict):
            continue
        source_values = [
            (point[0] - origin[0]) * ux +
            (point[1] - origin[1]) * uy
            for point in (source_line["p1"], source_line["p2"])
        ]
        overlap_start = max(interval_start, min(source_values))
        overlap_end = min(interval_end, max(source_values))
        if overlap_end - overlap_start <= 1e-9:
            continue
        overlap_points = [
            (origin[0] + overlap_start * ux,
             origin[1] + overlap_start * uy),
            (origin[0] + overlap_end * ux,
             origin[1] + overlap_end * uy),
        ]
        source_interval = sorted(
            (point[0] - source_line["p1"][0]) * source_line["u"][0] +
            (point[1] - source_line["p1"][1]) * source_line["u"][1]
            for point in overlap_points)
        low = max(0.0, min(source_line["length"], source_interval[0]))
        high = max(0.0, min(source_line["length"], source_interval[1]))
        if high - low <= 1e-9:
            continue
        material_ref = {
            **copy.deepcopy(reference),
            "source_interval_m": [round(low, 6), round(high, 6)],
        }
        key = _source_ref_key(material_ref)
        if key not in seen:
            seen.add(key)
            references.append(material_ref)
    return references


def _logical_source_face_groups(sources: list[dict]) -> list[list[dict]]:
    """Collapse repeated/adjacent evidence on the same face baseline."""
    groups: list[list[dict]] = []
    for source in sources:
        line = source.get("line")
        if line is None:
            continue
        matches = []
        for group in groups:
            representative = group[0]["line"]
            if (_angle_delta(line["angle"], representative["angle"]) >
                    _SOURCE_FACE_MERGE_ANGLE_TOLERANCE_DEG):
                continue
            baseline_error = max(abs(
                (point[0] - representative["p1"][0]) *
                representative["n"][0] +
                (point[1] - representative["p1"][1]) *
                representative["n"][1])
                for point in (line["p1"], line["p2"]))
            if baseline_error <= _SOURCE_FACE_MERGE_OFFSET_TOLERANCE_M:
                matches.append(group)
        if len(matches) == 1:
            matches[0].append(source)
        else:
            groups.append([source])
    return groups


def _closed_wall_strip_candidate(entity, layer: str, provenance: dict | None,
                                 transform, record_index: int,
                                 min_length_m: float) -> dict | None:
    """Recognise one wall strip only when one closed entity proves its shape.

    A closed four-edge outline is materially stronger evidence than four loose
    lines. Keeping this test entity-local prevents a short end cap from being
    paired with a face belonging to an adjacent outline. Ambiguous but valid
    outlines are still reserved as one review unit; they are never decomposed
    into general pairing or single-line fallback candidates.
    """
    if entity.dxftype() != "LWPOLYLINE" or not bool(
            getattr(entity, "closed", False)):
        return None
    wall_group = wall_geometry_group(layer)
    if wall_group is None:
        return None
    try:
        raw_points = list(entity.get_points("xyb"))
    except (AttributeError, TypeError, ValueError):
        return None
    if len(raw_points) != 4:
        return None
    if any(len(point) < 3 or not all(math.isfinite(float(value))
                                    for value in point[:3])
           for point in raw_points):
        return None
    # Arc segments cannot be represented by a straight Revit wall centreline.
    if any(abs(float(point[2])) > 1e-9 for point in raw_points):
        return None

    points = [transform((float(point[0]), float(point[1])))
              for point in raw_points]
    edges = []
    for index, first in enumerate(points):
        second = points[(index + 1) % 4]
        edge = _line_record(first, second)
        if edge is None:
            return None
        edges.append(edge)

    def cross(first, second, third):
        return ((second[0] - first[0]) * (third[1] - second[1]) -
                (second[1] - first[1]) * (third[0] - second[0]))

    turns = [cross(points[index], points[(index + 1) % 4],
                   points[(index + 2) % 4])
             for index in range(4)]
    # Strict convexity also rejects bow-ties, collinear corners and concavity.
    if not (all(value > 1e-9 for value in turns) or
            all(value < -1e-9 for value in turns)):
        return None

    parallel_errors = [
        _angle_delta(edges[0]["angle"], edges[2]["angle"]),
        _angle_delta(edges[1]["angle"], edges[3]["angle"]),
    ]
    if max(parallel_errors) > 2.0:
        return None

    right_angle_errors = []
    for index, edge in enumerate(edges):
        adjacent = edges[(index + 1) % 4]
        dot = abs(edge["u"][0] * adjacent["u"][0] +
                  edge["u"][1] * adjacent["u"][1])
        angle = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
        right_angle_errors.append(abs(90.0 - angle))
    if max(right_angle_errors) > 2.0:
        return None

    opposite_ratios = [
        min(edges[0]["length"], edges[2]["length"]) /
        max(edges[0]["length"], edges[2]["length"]),
        min(edges[1]["length"], edges[3]["length"]) /
        max(edges[1]["length"], edges[3]["length"]),
    ]
    if min(opposite_ratios) < 0.95:
        return None

    pair_lengths = [
        (edges[0]["length"] + edges[2]["length"]) / 2.0,
        (edges[1]["length"] + edges[3]["length"]) / 2.0,
    ]
    long_pair = 0 if pair_lengths[0] >= pair_lengths[1] else 1
    length = pair_lengths[long_pair]
    thickness = pair_lengths[1 - long_pair]
    if thickness <= 1e-9 or length / thickness < 3.0:
        return None

    if long_pair == 0:
        start = [(points[0][axis] + points[3][axis]) / 2.0
                 for axis in range(2)]
        end = [(points[1][axis] + points[2][axis]) / 2.0
               for axis in range(2)]
    else:
        start = [(points[0][axis] + points[1][axis]) / 2.0
                 for axis in range(2)]
        end = [(points[3][axis] + points[2][axis]) / 2.0
               for axis in range(2)]

    dxf = getattr(entity, "dxf", None)
    entity_linetype = str(
        dxf.get("linetype", "BYLAYER") if dxf is not None else "BYLAYER")
    effective_linetype = entity_linetype
    linetype_source = "ENTITY"
    linetype_resolved = entity_linetype.upper() not in {
        "", "BYLAYER", "BYBLOCK"}
    lookup_layers = []
    if not linetype_resolved:
        own_layer = str(dxf.get("layer", "") if dxf is not None else "")
        for candidate_layer in (layer, own_layer, layer.rsplit("$0$", 1)[-1]):
            if candidate_layer and candidate_layer not in lookup_layers:
                lookup_layers.append(candidate_layer)
        document = getattr(entity, "doc", None)
        if document is not None:
            for candidate_layer in lookup_layers:
                try:
                    layer_entry = document.layers.get(candidate_layer)
                except (AttributeError, KeyError, ValueError):
                    continue
                layer_linetype = str(
                    getattr(layer_entry.dxf, "linetype", "") or "")
                if layer_linetype and layer_linetype.upper() not in {
                        "BYLAYER", "BYBLOCK"}:
                    effective_linetype = layer_linetype
                    linetype_source = f"LAYER:{candidate_layer}"
                    linetype_resolved = True
                    break
    pattern_evidence = f"{layer}|{effective_linetype}".upper()
    pattern_conflicts = [token for token in ("DASH", "HIDDEN")
                         if token in pattern_evidence]
    blockers = []
    if length < min_length_m:
        blockers.append("centerline_below_modeling_min_length")
    if not 0.08 <= thickness <= 0.60:
        blockers.append("thickness_outside_80_600mm")
    if pattern_conflicts:
        blockers.append("dash_hidden_pattern_conflict")
    if not linetype_resolved:
        blockers.append("linetype_resolution_incomplete")

    origin = getattr(entity, "origin_of_copy", None) or entity
    source_handle = ((provenance or {}).get("source_entity_handle") or
                     getattr(getattr(origin, "dxf", None), "handle", None))
    drawing_identity = (provenance or {}).get("drawing_identity")
    source_occurrence_id = (provenance or {}).get("source_occurrence_id")
    placed_entity_id = (provenance or {}).get("placed_entity_id")
    structural_occurrence_id = (provenance or {}).get(
        "structural_occurrence_id")
    placement_path = (provenance or {}).get("placement_path") or []
    source_segment_ids = [
        _provenance_segment_id(provenance, index) for index in range(4)]
    identity_limitations = _provenance_identity_limitations(
        provenance, source_handle, segment_index=0)
    if any(segment_id is None for segment_id in source_segment_ids):
        if "source_segment_id_missing" not in identity_limitations:
            identity_limitations.append("source_segment_id_missing")
    identity_is_complete = not identity_limitations
    occurrence_evidence = {
        "drawing_identity": drawing_identity,
        "source_entity_handle": source_handle,
        "source_occurrence_id": source_occurrence_id,
        "placed_entity_id": placed_entity_id,
        "structural_occurrence_id": structural_occurrence_id,
        "source_segment_ids": source_segment_ids,
        "placement_path": placement_path,
        "points": [[round(value, 6) for value in point] for point in points],
    }
    digest = hashlib.sha256(json.dumps(
        occurrence_evidence, ensure_ascii=False, sort_keys=True,
        default=str).encode("utf-8")).hexdigest()[:16]
    occurrence_id = f"wall_occurrence_{digest}"
    profile_group_id = f"closed_wall_strip_group_{digest}"
    status = "NEEDS_REVIEW" if blockers else "APPROVED_BY_RULE"
    child_evidence = []
    for index, edge in enumerate(edges):
        edge_role = ("wall_face" if index in {long_pair, long_pair + 2}
                     else "end_cap")
        child_evidence.append({
            "segment_id": f"{profile_group_id}_segment_{index}",
            "source_segment_id": source_segment_ids[index],
            "source_record_index": record_index,
            "segment_index": index,
            "edge_role": edge_role,
            "entity_handle": source_handle,
            "occurrence_id": occurrence_id,
            "source_occurrence_id": source_occurrence_id,
            "placed_entity_id": placed_entity_id,
            "structural_occurrence_id": structural_occurrence_id,
            "drawing_identity": drawing_identity,
            "profile_group_id": profile_group_id,
            "start": [round(value, 6) for value in points[index]],
            "end": [round(value, 6)
                    for value in points[(index + 1) % 4]],
            "length_m": round(edge["length"], 6),
            "angle_deg": round(edge["angle"], 6),
            "reserved_from_general_pairing": True,
        })
    source_segment_refs = []
    for child in child_evidence:
        segment_index = child["segment_index"]
        limitations = _provenance_identity_limitations(
            provenance, source_handle, segment_index=segment_index)
        source_segment_refs.append({
            "drawing_identity": drawing_identity,
            "source_occurrence_id": source_occurrence_id,
            "placed_entity_id": placed_entity_id,
            "structural_occurrence_id": structural_occurrence_id,
            "source_segment_id": child["source_segment_id"],
            "entity_handle": source_handle,
            "source_record_index": record_index,
            "segment_index": segment_index,
            "source_interval_m": [0.0, child["length_m"]],
            "placement_path": copy.deepcopy(placement_path),
            "identity_is_complete": not limitations,
            "identity_limitations": limitations,
            "edge_role": child["edge_role"],
        })
    return {
        "candidate_id": f"closed_wall_strip_{digest}",
        "profile_group_id": profile_group_id,
        "record_index": record_index,
        "entity_handle": source_handle,
        "occurrence_id": occurrence_id,
        "drawing_identity": drawing_identity,
        "source_occurrence_id": source_occurrence_id,
        "placed_entity_id": placed_entity_id,
        "structural_occurrence_id": structural_occurrence_id,
        "source_segment_ids": source_segment_ids,
        "source_segment_refs": source_segment_refs,
        "identity_is_complete": identity_is_complete,
        "identity_limitations": identity_limitations,
        "provenance": {
            "drawing_identity": drawing_identity,
            "source_entity_handle": source_handle,
            "source_occurrence_id": source_occurrence_id,
            "placed_entity_id": placed_entity_id,
            "structural_occurrence_id": structural_occurrence_id,
            "segment_ids": source_segment_ids,
            "placement_path": placement_path,
        },
        "child_evidence": child_evidence,
        "layer": layer,
        "wall_group": wall_group,
        "geometry_source": "DXF_VECTOR",
        "status": status,
        "decision_reason": (blockers[0] if blockers else
                            "strict_closed_wall_strip"),
        "blockers": blockers,
        "pattern_conflicts": pattern_conflicts,
        "linetype_evidence": {
            "entity_linetype": entity_linetype,
            "effective_linetype": effective_linetype,
            "source": linetype_source,
            "resolved": linetype_resolved,
            "lookup_layers": lookup_layers,
        },
        "points": [[round(value, 6) for value in point] for point in points],
        "centerline": {
            "start": [round(value, 6) for value in start],
            "end": [round(value, 6) for value in end],
        },
        "length_m": round(length, 6),
        "thickness_mm": round(thickness * 1000.0, 3),
        "aspect_ratio": round(length / thickness, 4),
        "parallel_error_deg": round(max(parallel_errors), 6),
        "right_angle_error_deg": round(max(right_angle_errors), 6),
        "opposite_length_ratio": round(min(opposite_ratios), 6),
        "reserved_edge_count": 4,
        "reserved_from_general_pairing": True,
    }


def _collect_closed_wall_strips(kept, transform,
                                wall_groups: tuple[str, ...],
                                min_length_m: float) -> list[dict]:
    candidates = []
    for record_index, item in enumerate(kept):
        entity, layer, provenance = _record_parts(item)
        if wall_geometry_group(layer) not in wall_groups:
            continue
        candidate = _closed_wall_strip_candidate(
            entity, layer, provenance, transform, record_index, min_length_m)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def audit_closed_wall_strips(kept: list[tuple[Any, str]], ox: float, oy: float,
                             rot_deg: float, gcx: float, gcy: float,
                             min_length_m: float = 0.5,
                             wall_groups: tuple[str, ...] = ("S",)) -> dict:
    """Return traceable, occurrence-local closed wall-strip decisions."""
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def transform(point):
        x, y = point[0] - gcx, point[1] - gcy
        rx, ry = x * c - y * s + gcx, x * s + y * c + gcy
        return ((rx - ox) / 1000.0, (ry - oy) / 1000.0)

    candidates = _collect_closed_wall_strips(
        kept, transform, wall_groups, min_length_m)
    identity_incomplete_count = sum(
        not item["identity_is_complete"] for item in candidates)
    return {
        "status": "REVIEW" if (
            identity_incomplete_count or any(
                item["status"] == "NEEDS_REVIEW" for item in candidates)
        ) else "PASS",
        "candidate_count": len(candidates),
        "approved_by_rule_count": sum(
            item["status"] == "APPROVED_BY_RULE" for item in candidates),
        "needs_review_count": sum(item["status"] == "NEEDS_REVIEW"
                                  for item in candidates),
        "identity_complete_count": len(candidates) - identity_incomplete_count,
        "identity_incomplete_count": identity_incomplete_count,
        "reserved_occurrence_count": len(candidates),
        "candidates": [
            {key: value for key, value in item.items()
             if key != "record_index"}
            for item in candidates
        ],
    }


def extract_precise_walls(kept: list[tuple[Any, str]], ox: float, oy: float,
                          rot_deg: float, gcx: float, gcy: float,
                          min_length_m: float = 0.5,
                          wall_groups: tuple[str, ...] = ("S",),
                          recover_terminal_remainders: bool = False
                          ) -> list[dict]:
    """从已清洗 DXF 实体提取建筑墙中心线，输出局部米制。

    ``recover_terminal_remainders`` is deliberately opt-in.  A long semantic
    face can overlap-pair with only part of its opposing face; the old
    interval accounting then discarded the uncovered *terminal* portion.
    Recovering only a terminal (not an interior) remainder preserves door
    openings and requires a semantic wall layer plus complete source identity.
    """
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def transform(point):
        x, y = point[0] - gcx, point[1] - gcy
        rx, ry = x * c - y * s + gcx, x * s + y * c + gcy
        return ((rx - ox) / 1000.0, (ry - oy) / 1000.0)

    closed_strips = _collect_closed_wall_strips(
        kept, transform, wall_groups, min_length_m)
    reserved_records = {item["record_index"] for item in closed_strips}

    lines = []
    for record_index, item in enumerate(kept):
        entity, layer, provenance = _record_parts(item)
        wall_group = wall_geometry_group(layer)
        if wall_group not in wall_groups:
            continue
        # A qualifying closed outline is one occurrence. Even a review-only
        # outline must not leak its four edges into cross-entity pairing or the
        # single-line fallback below.
        if record_index in reserved_records:
            continue
        segments = []
        if entity.dxftype() == "LINE":
            segments.append((entity.dxf.start, entity.dxf.end))
        elif entity.dxftype() == "LWPOLYLINE":
            points = list(entity.get_points("xy"))
            for index in range(len(points) - 1):
                segments.append((points[index], points[index + 1]))
            if entity.closed and len(points) > 2:
                segments.append((points[-1], points[0]))
        for segment_index, (start, end) in enumerate(segments):
            record = _line_record(transform(start), transform(end))
            if record and record["length"] >= min_length_m:
                record["wall_group"] = wall_group
                record["source_layer"] = layer
                record["source_segment_ref"] = _source_segment_reference(
                    entity, provenance, record_index, segment_index)
                lines.append(record)

    lines = _consolidate_overlapping_source_faces(lines)

    candidates = []
    for i, a in enumerate(lines):
        for j in range(i + 1, len(lines)):
            if a["wall_group"] != lines[j]["wall_group"]:
                continue
            # A wall face pair must come from the same effective source layer.
            # Matching only the broad A/S group can join coincident geometry
            # from independent nested references into a synthetic wall.
            if a["source_layer"] != lines[j]["source_layer"]:
                continue
            pair = _pair_geometry(a, lines[j])
            if pair:
                candidates.append((pair["distance"], -pair["overlap"], i, j, pair))
    candidates.sort()
    used_intervals: dict[int, list[tuple[float, float]]] = {}
    pair_thicknesses: dict[int, list[float]] = {}
    pair_centerline_offsets: dict[int, list[tuple[float, float]]] = {}

    def uncovered_ratio(index, interval):
        start, end = interval
        length = max(1e-9, end - start)
        covered = 0.0
        pieces = []
        for a0, a1 in used_intervals.get(index, []):
            p0, p1 = max(start, a0), min(end, a1)
            if p1 > p0:
                pieces.append((p0, p1))
        pieces.sort()
        merged = []
        for p0, p1 in pieces:
            if merged and p0 <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], p1)
            else:
                merged.append([p0, p1])
        covered = sum(p1 - p0 for p0, p1 in merged)
        return max(0.0, 1.0 - covered / length)

    walls = []
    for candidate in closed_strips:
        if candidate["status"] != "APPROVED_BY_RULE":
            continue
        walls.append({
            "start": [round(value, 3)
                      for value in candidate["centerline"]["start"]],
            "end": [round(value, 3)
                    for value in candidate["centerline"]["end"]],
            "thickness": round(candidate["thickness_mm"], 1),
            "paired": True,
            "wall_group": candidate["wall_group"],
            "source_layers": [candidate["layer"]],
            "geometry_source": "DXF_CLOSED_WALL_STRIP",
            "source_candidate_id": candidate["candidate_id"],
            "profile_occurrence_id": candidate["occurrence_id"],
            "drawing_identity": candidate["drawing_identity"],
            "source_occurrence_id": candidate["source_occurrence_id"],
            "placed_entity_id": candidate["placed_entity_id"],
            "structural_occurrence_id": candidate[
                "structural_occurrence_id"],
            "source_segment_ids": candidate["source_segment_ids"],
            "source_segment_refs": copy.deepcopy(
                candidate["source_segment_refs"]),
        })
    for _, __, i, j, pair in candidates:
        # 已被其他配对覆盖过半的同一区间不再重复；互不重叠区间允许复用长线。
        if (uncovered_ratio(i, pair["a_interval"]) < 0.5 or
                uncovered_ratio(j, pair["b_interval"]) < 0.5):
            continue
        used_intervals.setdefault(i, []).append(pair["a_interval"])
        used_intervals.setdefault(j, []).append(pair["b_interval"])
        pair_thicknesses.setdefault(i, []).append(pair["distance"] * 1000.0)
        pair_thicknesses.setdefault(j, []).append(pair["distance"] * 1000.0)
        centerline_mid = (
            (pair["start"][0] + pair["end"][0]) / 2.0,
            (pair["start"][1] + pair["end"][1]) / 2.0)
        for line_index, interval_name in (
                (i, "a_interval"), (j, "b_interval")):
            start_value, end_value = pair[interval_name]
            line = lines[line_index]
            source_mid = (
                line["p1"][0] + (start_value + end_value) / 2.0 *
                line["u"][0],
                line["p1"][1] + (start_value + end_value) / 2.0 *
                line["u"][1])
            pair_centerline_offsets.setdefault(line_index, []).append((
                centerline_mid[0] - source_mid[0],
                centerline_mid[1] - source_mid[1]))
        source_refs = []
        for line_index, interval_name in (
                (i, "a_interval"), (j, "b_interval")):
            source_refs.extend(_source_refs_for_logical_interval(
                lines[line_index], pair[interval_name]))
        walls.append({
            "start": [round(v, 3) for v in pair["start"]],
            "end": [round(v, 3) for v in pair["end"]],
            "thickness": round(pair["distance"] * 1000.0, 1),
            "paired": True,
            "wall_group": lines[i]["wall_group"],
            "source_layers": sorted({
                lines[i]["source_layer"], lines[j]["source_layer"]}),
            "geometry_source": "DXF_VECTOR",
            "source_segment_refs": source_refs,
            "source_segment_ids": [
                item["source_segment_id"] for item in source_refs],
        })

    paired_thickness = [round(w["thickness"] / 10.0) * 10 for w in walls]
    default_thickness = (max(set(paired_thickness), key=paired_thickness.count)
                         if paired_thickness else 200)
    for i, line in enumerate(lines):
        # 仅完全未参与配对的线作为单线候选；部分已配对的剩余区间不直接建墙。
        if i in used_intervals:
            continue
        source_refs = _source_refs_for_logical_interval(
            line, (0.0, line["length"]))
        walls.append({
            "start": [round(v, 3) for v in line["p1"]],
            "end": [round(v, 3) for v in line["p2"]],
            "thickness": default_thickness,
            "paired": False,
            "wall_group": line["wall_group"],
            "source_layers": [line["source_layer"]],
            "geometry_source": "DXF_VECTOR",
            "source_segment_refs": source_refs,
            "source_segment_ids": [
                item["source_segment_id"] for item in source_refs],
        })

    if recover_terminal_remainders:
        # Do not fill gaps between two paired intervals: those are commonly
        # door openings.  Only preserve a continuous terminal extension of a
        # source face that was partially consumed by a valid pair.
        terminal_leaf_tokens = ("A-WALL-S", "A-PART-S")

        def used_interval_bounds(index):
            intervals = []
            for start, end in used_intervals.get(index, []):
                try:
                    low, high = sorted((float(start), float(end)))
                except (TypeError, ValueError, OverflowError):
                    continue
                if high - low > 1e-9:
                    intervals.append((low, high))
            if not intervals:
                return None
            return min(item[0] for item in intervals), max(
                item[1] for item in intervals)

        def complete_source_refs(refs):
            return bool(refs) and all(
                isinstance(ref, dict) and
                ref.get("identity_is_complete") is True and
                not ref.get("identity_limitations") and
                ref.get("source_segment_id")
                for ref in refs)

        for index, line in enumerate(lines):
            bounds = used_interval_bounds(index)
            if bounds is None:
                continue
            leaf = str(line.get("source_layer") or "").upper().rsplit(
                "$0$", 1)[-1]
            if not any(token in leaf for token in terminal_leaf_tokens):
                continue
            low_used, high_used = bounds
            remainder_intervals = []
            if low_used >= min_length_m:
                remainder_intervals.append((0.0, low_used))
            if line["length"] - high_used >= min_length_m:
                remainder_intervals.append((high_used, line["length"]))
            for start_value, end_value in remainder_intervals:
                source_refs = _source_refs_for_logical_interval(
                    line, (start_value, end_value))
                if not complete_source_refs(source_refs):
                    continue
                # A terminal extension must contain a real source segment of
                # at least modeling length; this rejects isolated end caps and
                # tiny symbol fragments while retaining long wall faces.
                if not any(
                        float(ref.get("source_interval_m", [0.0, 0.0])[1]) -
                        float(ref.get("source_interval_m", [0.0, 0.0])[0]) >=
                        min_length_m - 1e-9
                        for ref in source_refs):
                    continue
                start = [line["p1"][axis] + start_value * line["u"][axis]
                         for axis in range(2)]
                end = [line["p1"][axis] + end_value * line["u"][axis]
                       for axis in range(2)]
                offsets = pair_centerline_offsets.get(index) or []
                if offsets:
                    offset_x = sum(value[0] for value in offsets) / len(offsets)
                    offset_y = sum(value[1] for value in offsets) / len(offsets)
                    start = [start[0] + offset_x, start[1] + offset_y]
                    end = [end[0] + offset_x, end[1] + offset_y]
                supported_thicknesses = pair_thicknesses.get(index) or []
                terminal_thickness = (
                    round(Counter(round(value / 10.0) * 10
                                  for value in supported_thicknesses).most_common(1)[0][0], 1)
                    if supported_thicknesses else default_thickness)
                walls.append({
                    "start": [round(value, 3) for value in start],
                    "end": [round(value, 3) for value in end],
                    "thickness": terminal_thickness,
                    "paired": False,
                    "wall_group": line["wall_group"],
                    "source_layers": [line["source_layer"]],
                    "geometry_source": "DXF_VECTOR_TERMINAL_FACE_EXTENSION",
                    "source_segment_refs": source_refs,
                    "source_segment_ids": [
                        item["source_segment_id"] for item in source_refs],
                    "terminal_face_recovery": True,
                    "warnings": [
                        "单面墙端部延伸, 厚度取同一墙面配对厚度"],
                })
    return [w for w in walls if math.dist(w["start"], w["end"]) >= min_length_m]


def filter_wall_records_to_region(records, ox: float, oy: float,
                                  rot_deg: float, gcx: float, gcy: float,
                                  bounds: tuple[float, float, float, float],
                                  margin_m: float = 8.0):
    """Keep placed wall entities whose segment midpoint belongs to one layout.

    ``bounds`` uses the same local metre coordinate system as extracted walls.
    The function filters source evidence before double-line pairing, preventing
    translated comparison copies from pairing with the selected main plan.
    """
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def transform(point):
        x, y = float(point[0]) - gcx, float(point[1]) - gcy
        rx, ry = x * c - y * s + gcx, x * s + y * c + gcy
        return ((rx - ox) / 1000.0, (ry - oy) / 1000.0)

    min_x, min_y, max_x, max_y = bounds
    kept, dropped = [], []
    for record in records:
        entity, _layer, _provenance = _record_parts(record)
        segments = []
        if entity.dxftype() == "LINE":
            segments.append((entity.dxf.start, entity.dxf.end))
        elif entity.dxftype() == "LWPOLYLINE":
            points = list(entity.get_points("xy"))
            segments.extend(zip(points, points[1:]))
            if entity.closed and len(points) > 2:
                segments.append((points[-1], points[0]))
        inside = False
        for start, end in segments:
            first, second = transform(start), transform(end)
            mid_x = (first[0] + second[0]) / 2.0
            mid_y = (first[1] + second[1]) / 2.0
            if (min_x - margin_m <= mid_x <= max_x + margin_m and
                    min_y - margin_m <= mid_y <= max_y + margin_m):
                inside = True
                break
        (kept if inside else dropped).append(record)
    return kept, dropped


_STRICT_END_CAP_MIN_LENGTH_M = 0.075
_STRICT_END_CAP_MAX_LENGTH_M = 0.125
_STRICT_END_CAP_ENDPOINT_TOLERANCE_M = 0.005
_STRICT_END_CAP_MIN_SUPPORT_LENGTH_M = 0.150
_STRICT_END_CAP_SUPPORT_LENGTH_RATIO = 1.5
_STRICT_END_CAP_ANGLE_TOLERANCE_DEG = 2.0


def _classify_strict_topology_end_caps(source_records: list[dict],
                                       review_items: list[dict]) -> int:
    """Prove short wall end-cap evidence without promoting geometry.

    This is deliberately stricter than ordinary junction detection. Both ends
    of the candidate must coincide with real endpoints of two different long
    semantic DXF wall edges in the same placed occurrence. Closed-profile
    children and same-entity open polylines are excluded; coordinates alone
    are never sufficient identity evidence.
    """
    def exact_source_key(item):
        return item.get("source_segment_id") or (
            item.get("source_record_index"), item.get("segment_index"))

    source_key_counts = Counter(exact_source_key(item)
                                for item in source_records)
    eligible_supports = []
    for source in source_records:
        source_key = exact_source_key(source)
        if (source_key_counts[source_key] != 1 or
                source.get("wall_group") != "A" or
                source.get("geometry_source") != "DXF_VECTOR" or
                not source.get("entity_handle") or
                not source.get("identity_is_complete") or
                not source.get("structural_occurrence_id") or
                source.get("parent_profile_id") is not None or
                source.get("profile_association_status") is not None):
            continue
        eligible_supports.append(source)

    tagged_count = 0
    for candidate in review_items:
        length = float(candidate.get("length_m", 0.0) or 0.0)
        if not (_STRICT_END_CAP_MIN_LENGTH_M <= length <=
                _STRICT_END_CAP_MAX_LENGTH_M):
            continue
        # Profile children already have a stronger entity-local explanation.
        # DUPLICATE/ERROR associations must also remain fail-closed.
        if (candidate.get("parent_profile_id") is not None or
                candidate.get("profile_association_status") is not None):
            continue
        candidate_key = exact_source_key(candidate)
        if source_key_counts[candidate_key] != 1:
            continue
        candidate_source = next(
            (source for source in source_records
             if exact_source_key(source) == candidate_key), None)
        if (candidate_source is None or
                candidate_source.get("wall_group") != "A" or
                candidate_source.get("geometry_source") != "DXF_VECTOR" or
                not candidate_source.get("identity_is_complete")):
            continue
        if (candidate_source.get("entity_type") == "LWPOLYLINE" and
                not candidate_source.get("entity_closed") and
                int(candidate_source.get("entity_segment_count", 0)) > 1):
            continue
        candidate_handle = candidate_source.get("entity_handle")
        occurrence_id = candidate_source.get("structural_occurrence_id")
        drawing_identity = candidate_source.get("drawing_identity")
        if not candidate_handle or not occurrence_id or not drawing_identity:
            continue

        minimum_support_length = max(
            _STRICT_END_CAP_MIN_SUPPORT_LENGTH_M,
            _STRICT_END_CAP_SUPPORT_LENGTH_RATIO * length)
        candidate_endpoints = [
            ("start", candidate_source["start"]),
            ("end", candidate_source["end"]),
        ]
        endpoint_matches: list[list[dict]] = [[], []]
        for support in eligible_supports:
            support_key = exact_source_key(support)
            support_handle = support["entity_handle"]
            if (support_key == candidate_key or
                    support_handle == candidate_handle or
                    support["structural_occurrence_id"] != occurrence_id or
                    support.get("drawing_identity") != drawing_identity or
                    support["line"]["length"] + 1e-9 <
                    minimum_support_length):
                continue
            perpendicular_error = abs(
                90.0 - _angle_delta(candidate_source["line"]["angle"],
                                    support["line"]["angle"]))
            if perpendicular_error > _STRICT_END_CAP_ANGLE_TOLERANCE_DEG:
                continue
            support_endpoints = [
                ("start", support["start"]),
                ("end", support["end"]),
            ]
            for endpoint_index, (_name, point) in enumerate(
                    candidate_endpoints):
                nearest_name, nearest_point = min(
                    support_endpoints,
                    key=lambda item: math.dist(point, item[1]))
                distance = math.dist(point, nearest_point)
                if distance <= _STRICT_END_CAP_ENDPOINT_TOLERANCE_M:
                    endpoint_matches[endpoint_index].append({
                        "source": support,
                        "source_key": support_key,
                        "matched_support_endpoint": nearest_name,
                        "endpoint_distance_m": distance,
                        "perpendicular_error_deg": perpendicular_error,
                    })

        valid_pairs = []
        for first in endpoint_matches[0]:
            for second in endpoint_matches[1]:
                first_source = first["source"]
                second_source = second["source"]
                if (first["source_key"] == second["source_key"] or
                        first_source["entity_handle"] ==
                        second_source["entity_handle"]):
                    continue
                parallel_error = _angle_delta(
                    first_source["line"]["angle"],
                    second_source["line"]["angle"])
                if parallel_error > _STRICT_END_CAP_ANGLE_TOLERANCE_DEG:
                    continue
                handles = {
                    candidate_handle,
                    first_source["entity_handle"],
                    second_source["entity_handle"],
                }
                if len(handles) != 3:
                    continue

                def extension_direction(match):
                    source = match["source"]
                    if match["matched_support_endpoint"] == "start":
                        dx = source["end"][0] - source["start"][0]
                        dy = source["end"][1] - source["start"][1]
                    else:
                        dx = source["start"][0] - source["end"][0]
                        dy = source["start"][1] - source["end"][1]
                    length = math.hypot(dx, dy)
                    return (dx / length, dy / length) if length > 1e-9 else None

                first_extension = extension_direction(first)
                second_extension = extension_direction(second)
                if first_extension is None or second_extension is None:
                    continue
                extension_dot = max(-1.0, min(
                    1.0, first_extension[0] * second_extension[0] +
                    first_extension[1] * second_extension[1]))
                same_side_error = math.degrees(math.acos(extension_dot))
                # A real strip cap is U-shaped: both faces continue into the
                # same half-plane. Opposite extensions form a Z/junction and
                # must stay in the generic review bucket.
                if same_side_error > _STRICT_END_CAP_ANGLE_TOLERANCE_DEG:
                    continue
                valid_pairs.append((
                    first, second, parallel_error, extension_dot,
                    same_side_error))

        # Multiple valid pairs are ambiguous network evidence, not strict
        # proof. Leave the original review reason unchanged in that case.
        if len(valid_pairs) != 1:
            continue
        (first, second, parallel_error, extension_dot,
         same_side_error) = valid_pairs[0]
        support_evidence = []
        for endpoint, match in zip(("start", "end"), (first, second)):
            support = match["source"]
            support_evidence.append({
                "candidate_endpoint": endpoint,
                "source_record_index": support["source_record_index"],
                "segment_index": support["segment_index"],
                "entity_handle": support["entity_handle"],
                "drawing_identity": support["drawing_identity"],
                "source_occurrence_id": support["source_occurrence_id"],
                "placed_entity_id": support["placed_entity_id"],
                "structural_occurrence_id": support[
                    "structural_occurrence_id"],
                "source_segment_id": support["source_segment_id"],
                "layer": support["layer"],
                "geometry_source": "DXF_VECTOR",
                "length_m": round(support["line"]["length"], 6),
                "angle_deg": round(support["line"]["angle"], 6),
                "matched_support_endpoint": match[
                    "matched_support_endpoint"],
                "endpoint_distance_m": round(
                    match["endpoint_distance_m"], 6),
                "perpendicular_error_deg": round(
                    match["perpendicular_error_deg"], 6),
            })
        candidate["prior_decision_reason"] = candidate.get(
            "decision_reason")
        candidate["decision_reason"] = "strict_topology_end_cap"
        candidate["review_bucket"] = "STRICT_TOPOLOGY_END_CAP"
        candidate["support_evidence"] = support_evidence
        candidate["strict_topology_evidence"] = {
            "geometry_source": "DXF_VECTOR",
            "source_record_index": candidate_source[
                "source_record_index"],
            "segment_index": candidate_source["segment_index"],
            "entity_handle": candidate_handle,
            "drawing_identity": drawing_identity,
            "source_occurrence_id": candidate_source[
                "source_occurrence_id"],
            "placed_entity_id": candidate_source["placed_entity_id"],
            "structural_occurrence_id": occurrence_id,
            "source_segment_id": candidate_source["source_segment_id"],
            "candidate_length_m": round(length, 6),
            "length_range_m": [
                _STRICT_END_CAP_MIN_LENGTH_M,
                _STRICT_END_CAP_MAX_LENGTH_M,
            ],
            "endpoint_tolerance_m": (
                _STRICT_END_CAP_ENDPOINT_TOLERANCE_M),
            "minimum_support_length_m": round(
                minimum_support_length, 6),
            "parallel_error_deg": round(parallel_error, 6),
            "same_side_extension_dot": round(extension_dot, 6),
            "same_side_extension_error_deg": round(same_side_error, 6),
            "same_half_plane_tolerance_deg": (
                _STRICT_END_CAP_ANGLE_TOLERANCE_DEG),
            "parallel_tolerance_deg": (
                _STRICT_END_CAP_ANGLE_TOLERANCE_DEG),
            "perpendicular_tolerance_deg": (
                _STRICT_END_CAP_ANGLE_TOLERANCE_DEG),
            "support_count": 2,
        }
        candidate["auto_action"] = "NONE"
        tagged_count += 1
    return tagged_count


_MODELED_JUNCTION_ENDPOINT_TOLERANCE_M = 0.001
_MODELED_JUNCTION_LENGTH_TOLERANCE_M = 0.002
_MODELED_JUNCTION_ANGLE_TOLERANCE_DEG = 1.0
_SHORT_WALL_PAIR_MIN_OVERLAP_M = 0.150
_SHORT_WALL_PAIR_ANGLE_TOLERANCE_DEG = 2.0
_SHORT_WALL_PAIR_BASELINE_TOLERANCE_M = 0.005
_SHORT_WALL_SUPPORT_TOLERANCE_M = 0.005


def _resolve_modeled_junction_review_units(
        review_units: list[dict], raw_review_items: list[dict],
        source_records: list[dict], walls: list[dict]) -> int:
    """Resolve terminal wall-face tails already explained by an exact joint.

    A range is resolved only when its source face already belongs to one
    paired wall and its two endpoints land one-to-one on the two exact source
    faces of one unique perpendicular paired wall.  The range must be terminal
    and equal the support wall thickness.  This classifies retained source
    evidence; it never creates, extends, snaps, or connects model geometry.
    """
    sources_by_id: dict[str, list[dict]] = {}
    for source in source_records:
        source_id = source.get("source_segment_id")
        if source_id:
            sources_by_id.setdefault(str(source_id), []).append(source)

    wall_records = []
    owners_by_source_id: dict[str, list[dict]] = {}
    for index, wall in enumerate(walls):
        start, end = wall.get("start"), wall.get("end")
        if (not isinstance(start, (list, tuple)) or len(start) < 2 or
                not isinstance(end, (list, tuple)) or len(end) < 2):
            continue
        line = _line_record(tuple(start[:2]), tuple(end[:2]))
        if line is None:
            continue
        source_ids = [str(value) for value in
                      (wall.get("source_segment_ids") or []) if value]
        record = {
            "id": wall.get("id", f"wall_{index}"),
            "wall": wall,
            "line": line,
            "source_ids": source_ids,
        }
        wall_records.append(record)
        for source_id in set(source_ids):
            owners_by_source_id.setdefault(source_id, []).append(record)

    def infinite_line_intersection(first_line, second_line):
        first_point = first_line["p1"]
        second_point = second_line["p1"]
        first_direction = first_line["u"]
        second_direction = second_line["u"]
        denominator = (
            first_direction[0] * second_direction[1] -
            first_direction[1] * second_direction[0])
        if abs(denominator) <= 1e-9:
            return None
        dx = second_point[0] - first_point[0]
        dy = second_point[1] - first_point[1]
        ratio = (dx * second_direction[1] -
                 dy * second_direction[0]) / denominator
        return [first_point[0] + ratio * first_direction[0],
                first_point[1] + ratio * first_direction[1]]

    def nearest_endpoint(line, point):
        choices = [
            (math.dist(line["p1"], point), line["p1"]),
            (math.dist(line["p2"], point), line["p2"]),
        ]
        return min(choices, key=lambda item: item[0])

    def has_parallel_short_wall_evidence(
            range_line, excluded_source_ids, drawing_identity,
            structural_occurrence_id):
        origin = range_line["p1"]
        ux, uy = range_line["u"]
        nx, ny = range_line["n"]
        for candidate_source in source_records:
            candidate_id = candidate_source.get("source_segment_id")
            candidate_line = candidate_source.get("line")
            if (candidate_id in excluded_source_ids or
                    candidate_line is None or
                    candidate_source.get("identity_is_complete") is not True or
                    candidate_source.get("drawing_identity") !=
                    drawing_identity or
                    candidate_source.get("structural_occurrence_id") !=
                    structural_occurrence_id or
                    _angle_delta(candidate_line["angle"],
                                 range_line["angle"]) >
                    _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG):
                continue
            midpoint = [
                (candidate_line["p1"][0] + candidate_line["p2"][0]) / 2.0,
                (candidate_line["p1"][1] + candidate_line["p2"][1]) / 2.0,
            ]
            relative = [midpoint[0] - origin[0], midpoint[1] - origin[1]]
            perpendicular = abs(relative[0] * nx + relative[1] * ny)
            if not 0.06 <= perpendicular <= 0.60:
                continue
            projections = [
                (point[0] - origin[0]) * ux +
                (point[1] - origin[1]) * uy
                for point in (candidate_line["p1"], candidate_line["p2"])
            ]
            overlap = max(
                0.0,
                min(range_line["length"], max(projections)) -
                max(0.0, min(projections)))
            if overlap >= max(0.05, 0.5 * range_line["length"]):
                return True
        return False

    resolved_count = 0
    for unit in review_units:
        if (unit.get("status") != "NEEDS_REVIEW" or
                unit.get("unit_type") != "UNMAPPED_SOURCE_RANGE" or
                unit.get("review_bucket") != "POSSIBLE_OMISSION" or
                unit.get("identity_is_complete") is not True or
                unit.get("identity_limitations")):
            continue
        source_id = str(unit.get("source_segment_id") or "")
        source_choices = sources_by_id.get(source_id, [])
        owner_choices = owners_by_source_id.get(source_id, [])
        if len(source_choices) != 1 or len(owner_choices) != 1:
            continue
        source = source_choices[0]
        owner = owner_choices[0]
        owner_wall = owner["wall"]
        if (source.get("identity_is_complete") is not True or
                source.get("drawing_identity") !=
                unit.get("drawing_identity") or
                source.get("structural_occurrence_id") !=
                unit.get("structural_occurrence_id") or
                owner_wall.get("paired") is not True or
                owner["line"] is None or
                len(owner["source_ids"]) != 2 or
                len(set(owner["source_ids"])) != 2):
            continue
        first, second = unit.get("start"), unit.get("end")
        if (not isinstance(first, (list, tuple)) or len(first) < 2 or
                not isinstance(second, (list, tuple)) or len(second) < 2):
            continue
        range_line = _line_record(tuple(first[:2]), tuple(second[:2]))
        if (range_line is None or
                _angle_delta(range_line["angle"],
                             owner["line"]["angle"]) >
                _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG):
            continue
        source_endpoints = (source["start"], source["end"])
        terminal_distance, target_terminal_node = min(
            (math.dist(point, source_endpoint), source_endpoint)
            for point in (first[:2], second[:2])
            for source_endpoint in source_endpoints)
        if terminal_distance > _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M:
            continue
        try:
            owner_thickness_m = float(
                owner_wall.get("thickness")) / 1000.0
        except (TypeError, ValueError, OverflowError):
            continue
        length_tolerance = max(
            _MODELED_JUNCTION_LENGTH_TOLERANCE_M,
            0.01 * owner_thickness_m)
        owner_offset = abs(
            (source["line"]["p1"][0] - owner["line"]["p1"][0]) *
            owner["line"]["n"][0] +
            (source["line"]["p1"][1] - owner["line"]["p1"][1]) *
            owner["line"]["n"][1])
        if (owner_thickness_m <= 0.0 or
                owner["line"]["length"] < max(
                    0.30, 3.0 * owner_thickness_m) or
                abs(range_line["length"] - owner_thickness_m) >
                length_tolerance or
                abs(owner_offset - owner_thickness_m / 2.0) >
                length_tolerance):
            continue
        paired_owner_id = next(
            source_value for source_value in owner["source_ids"]
            if source_value != source_id)
        paired_owner_choices = sources_by_id.get(paired_owner_id, [])
        if len(paired_owner_choices) != 1:
            continue
        paired_owner_source = paired_owner_choices[0]
        if (paired_owner_source.get("identity_is_complete") is not True or
                paired_owner_source.get("drawing_identity") !=
                unit.get("drawing_identity") or
                paired_owner_source.get("structural_occurrence_id") !=
                unit.get("structural_occurrence_id") or
                _angle_delta(paired_owner_source["line"]["angle"],
                             owner["line"]["angle"]) >
                _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG):
            continue

        support_matches = []
        for support in wall_records:
            support_wall = support["wall"]
            support_ids = support["source_ids"]
            if (support is owner or support_wall.get("paired") is not True or
                    support["line"] is None or len(support_ids) != 2 or
                    len(set(support_ids)) != 2 or
                    set(support_ids) & set(owner["source_ids"])):
                continue
            perpendicular_error = abs(
                90.0 - _angle_delta(owner["line"]["angle"],
                                    support["line"]["angle"]))
            if perpendicular_error > _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG:
                continue
            try:
                support_thickness_m = (
                    float(support_wall.get("thickness")) / 1000.0)
            except (TypeError, ValueError, OverflowError):
                continue
            support_length_tolerance = max(
                _MODELED_JUNCTION_LENGTH_TOLERANCE_M,
                0.01 * support_thickness_m)
            if (support_thickness_m <= 0.0 or
                    abs(range_line["length"] - support_thickness_m) >
                    support_length_tolerance):
                continue
            support_sources = []
            for support_id in support_ids:
                choices = sources_by_id.get(support_id, [])
                if len(choices) != 1:
                    support_sources = []
                    break
                support_source = choices[0]
                if (support_source.get("identity_is_complete") is not True or
                        support_source.get("drawing_identity") !=
                        unit.get("drawing_identity") or
                        support_source.get("structural_occurrence_id") !=
                        unit.get("structural_occurrence_id") or
                        _angle_delta(support_source["line"]["angle"],
                                     support["line"]["angle"]) >
                        _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG):
                    support_sources = []
                    break
                support_sources.append(support_source)
            if len(support_sources) != 2:
                continue
            intersection = infinite_line_intersection(
                owner["line"], support["line"])
            if intersection is None:
                continue
            owner_endpoint_distance, _owner_endpoint = nearest_endpoint(
                owner["line"], intersection)
            support_endpoint_distance, _support_endpoint = nearest_endpoint(
                support["line"], intersection)
            owner_center_tolerance = max(
                _MODELED_JUNCTION_LENGTH_TOLERANCE_M,
                0.01 * support_thickness_m)
            support_center_tolerance = max(
                _MODELED_JUNCTION_LENGTH_TOLERANCE_M,
                0.01 * owner_thickness_m)
            if (abs(owner_endpoint_distance - support_thickness_m / 2.0) >
                    owner_center_tolerance or
                    abs(support_endpoint_distance - owner_thickness_m / 2.0) >
                    support_center_tolerance):
                continue
            _distance, paired_owner_node = nearest_endpoint(
                paired_owner_source["line"], intersection)
            support_nodes = [
                nearest_endpoint(support_source["line"], intersection)[1]
                for support_source in support_sources
            ]
            owner_nodes = [target_terminal_node, paired_owner_node]
            assignments = []
            for order in ((0, 1), (1, 0)):
                distances = [
                    math.dist(owner_node, support_nodes[support_index])
                    for owner_node, support_index in zip(owner_nodes, order)
                ]
                if max(distances) <= _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M:
                    assignments.append((order, distances))
            if len(assignments) != 1:
                continue
            order, distances = assignments[0]
            excluded_source_ids = (
                set(owner["source_ids"]) | set(support["source_ids"]))
            if has_parallel_short_wall_evidence(
                    range_line, excluded_source_ids,
                    unit.get("drawing_identity"),
                    unit.get("structural_occurrence_id")):
                continue
            support_matches.append({
                "support": support,
                "support_sources": support_sources,
                "assignment": order,
                "distances": distances,
                "perpendicular_error": perpendicular_error,
                "support_thickness_m": support_thickness_m,
                "owner_thickness_m": owner_thickness_m,
                "intersection": intersection,
                "owner_endpoint_distance": owner_endpoint_distance,
                "support_endpoint_distance": support_endpoint_distance,
            })
        if len(support_matches) != 1:
            continue
        match = support_matches[0]
        support = match["support"]
        support_sources = match["support_sources"]
        assignment = match["assignment"]
        unit.update({
            "status": "RESOLVED_MODELED_JUNCTION_EDGE",
            "decision_reason": "exact_modeled_wall_junction_edge",
            "review_bucket": "RESOLVED_MODELED_JUNCTION_EDGE",
            "resolution_evidence": {
                "geometry_source": "DXF_VECTOR",
                "owner_wall_id": owner["id"],
                "owner_source_segment_id": source_id,
                "support_wall_id": support["id"],
                "support_source_segment_ids": [
                    support_sources[index]["source_segment_id"]
                    for index in assignment
                ],
                "endpoint_distances_m": [
                    round(value, 6) for value in match["distances"]],
                "range_length_m": round(range_line["length"], 6),
                "support_wall_thickness_m": round(
                    match["support_thickness_m"], 6),
                "owner_wall_thickness_m": round(
                    match["owner_thickness_m"], 6),
                "theoretical_centerline_intersection": [
                    round(value, 6) for value in match["intersection"]],
                "owner_endpoint_to_intersection_m": round(
                    match["owner_endpoint_distance"], 6),
                "support_endpoint_to_intersection_m": round(
                    match["support_endpoint_distance"], 6),
                "perpendicular_error_deg": round(
                    match["perpendicular_error"], 6),
                "endpoint_tolerance_m": (
                    _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M),
                "length_tolerance_m": (
                    _MODELED_JUNCTION_LENGTH_TOLERANCE_M),
                "angle_tolerance_deg": (
                    _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG),
            },
            "auto_action": "NONE",
        })
        resolved_count += 1

    units_by_id = {
        unit.get("review_unit_id"): unit for unit in review_units
        if unit.get("review_unit_id")
    }
    for item in raw_review_items:
        resolved = 0
        needs_review = 0
        for summary in item.get("range_review_units", []):
            unit = units_by_id.get(summary.get("review_unit_id"))
            if unit is None:
                continue
            summary.update({
                "status": unit.get("status"),
                "decision_reason": unit.get("decision_reason"),
                "review_bucket": unit.get("review_bucket"),
            })
            if unit.get("status") == "RESOLVED_MODELED_JUNCTION_EDGE":
                resolved += 1
            elif unit.get("status") == "NEEDS_REVIEW":
                needs_review += 1
        item["resolved_range_count"] = resolved
        item["needs_review_range_count"] = needs_review
    return resolved_count


def _resolve_l_junction_closure_review_units(
        review_units: list[dict], raw_review_items: list[dict],
        source_records: list[dict], walls: list[dict]) -> int:
    """Resolve a unique source-face closure at an already modeled L joint."""
    sources_by_id: dict[str, list[dict]] = {}
    for source in source_records:
        source_id = source.get("source_segment_id")
        if source_id:
            sources_by_id.setdefault(str(source_id), []).append(source)

    wall_records = []
    for index, wall in enumerate(walls):
        start, end = wall.get("start"), wall.get("end")
        if (not isinstance(start, (list, tuple)) or len(start) < 2 or
                not isinstance(end, (list, tuple)) or len(end) < 2):
            continue
        line = _line_record(tuple(start[:2]), tuple(end[:2]))
        if line is None:
            continue
        wall_records.append({
            "id": wall.get("id", f"wall_{index}"),
            "wall": wall,
            "line": line,
            "source_ids": [str(value) for value in
                           (wall.get("source_segment_ids") or []) if value],
            "source_refs": list(wall.get("source_segment_refs") or []),
        })

    def interval_supports_endpoint(wall_record, source, point):
        source_id = source.get("source_segment_id")
        line = source["line"]
        projection = (
            (float(point[0]) - line["p1"][0]) * line["u"][0] +
            (float(point[1]) - line["p1"][1]) * line["u"][1])
        matches = []
        for reference in wall_record["source_refs"]:
            if (str(reference.get("source_segment_id") or "") !=
                    str(source_id or "") or
                    reference.get("identity_is_complete") is not True or
                    reference.get("identity_limitations") not in ([], ()) or
                    reference.get("drawing_identity") !=
                    source.get("drawing_identity") or
                    reference.get("structural_occurrence_id") !=
                    source.get("structural_occurrence_id")):
                continue
            interval = reference.get("source_interval_m")
            try:
                start = float(interval[0])
                end = float(interval[1])
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
            low, high = min(start, end), max(start, end)
            if (low - _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M <= projection <=
                    high + _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M and
                    min(abs(projection - low), abs(projection - high)) <=
                    _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M):
                matches.append(reference)
        return matches

    def endpoint_source_matches(point, unit, candidate_source):
        matches = []
        for source_id, choices in sources_by_id.items():
            if source_id == unit.get("source_segment_id") or len(choices) != 1:
                continue
            source = choices[0]
            if (source.get("identity_is_complete") is not True or
                    source.get("drawing_identity") !=
                    unit.get("drawing_identity") or
                    source.get("structural_occurrence_id") !=
                    unit.get("structural_occurrence_id")):
                continue
            endpoint_distance = min(
                math.dist(point, source_endpoint)
                for source_endpoint in (source["start"], source["end"]))
            if endpoint_distance > _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M:
                continue
            delta = _angle_delta(
                candidate_source["line"]["angle"], source["line"]["angle"])
            if delta <= _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG:
                role = "COLLINEAR"
            elif abs(90.0 - delta) <= _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG:
                role = "PERPENDICULAR"
            else:
                continue
            for wall_record in wall_records:
                if source_id not in wall_record["source_ids"]:
                    continue
                references = interval_supports_endpoint(
                    wall_record, source, point)
                if not references:
                    continue
                matches.append({
                    "role": role,
                    "source": source,
                    "source_id": source_id,
                    "wall": wall_record,
                    "reference": references[0],
                    "endpoint_distance": endpoint_distance,
                    "angle_delta": delta,
                })
        unique = {}
        for match in matches:
            key = (match["role"], match["source_id"], match["wall"]["id"])
            unique[key] = match
        return list(unique.values())

    def has_parallel_pair(candidate_source, excluded_ids):
        candidate_line = candidate_source["line"]
        origin = candidate_line["p1"]
        ux, uy = candidate_line["u"]
        nx, ny = candidate_line["n"]
        for source in source_records:
            source_id = source.get("source_segment_id")
            line = source.get("line")
            if (source_id in excluded_ids or line is None or
                    source.get("identity_is_complete") is not True or
                    source.get("drawing_identity") !=
                    candidate_source.get("drawing_identity") or
                    source.get("structural_occurrence_id") !=
                    candidate_source.get("structural_occurrence_id") or
                    _angle_delta(line["angle"], candidate_line["angle"]) >
                    _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG):
                continue
            midpoint = [(line["p1"][0] + line["p2"][0]) / 2.0,
                        (line["p1"][1] + line["p2"][1]) / 2.0]
            relative = [midpoint[0] - origin[0], midpoint[1] - origin[1]]
            perpendicular = abs(relative[0] * nx + relative[1] * ny)
            if not 0.06 <= perpendicular <= 0.60:
                continue
            projections = [
                (point[0] - origin[0]) * ux +
                (point[1] - origin[1]) * uy
                for point in (line["p1"], line["p2"])
            ]
            overlap = max(
                0.0,
                min(candidate_line["length"], max(projections)) -
                max(0.0, min(projections)))
            if overlap >= max(0.05, 0.5 * candidate_line["length"]):
                return True
        return False

    resolved_count = 0
    for unit in review_units:
        if (unit.get("status") != "NEEDS_REVIEW" or
                unit.get("unit_type") != "UNMAPPED_SOURCE_RANGE" or
                unit.get("review_bucket") != "POSSIBLE_OMISSION" or
                unit.get("identity_is_complete") is not True or
                unit.get("identity_limitations") or
                unit.get("entity_type") != "LINE" or
                unit.get("parent_profile_id") is not None or
                float(unit.get("mapped_ratio", 0.0)) > 1e-6 or
                float(unit.get("length_m", 0.0)) >= 0.50):
            continue
        source_id = str(unit.get("source_segment_id") or "")
        source_choices = sources_by_id.get(source_id, [])
        if len(source_choices) != 1:
            continue
        candidate_source = source_choices[0]
        first, second = unit.get("start"), unit.get("end")
        if (not isinstance(first, (list, tuple)) or len(first) < 2 or
                not isinstance(second, (list, tuple)) or len(second) < 2 or
                max(min(math.dist(point, source_endpoint)
                        for source_endpoint in (
                            candidate_source["start"],
                            candidate_source["end"]))
                    for point in (first[:2], second[:2])) >
                _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M):
            continue
        endpoint_matches = [
            endpoint_source_matches(point, unit, candidate_source)
            for point in (first[:2], second[:2])
        ]
        support_pairs = []
        for collinear_endpoint, perpendicular_endpoint in ((0, 1), (1, 0)):
            for collinear in endpoint_matches[collinear_endpoint]:
                if collinear["role"] != "COLLINEAR":
                    continue
                for perpendicular in endpoint_matches[perpendicular_endpoint]:
                    if (perpendicular["role"] != "PERPENDICULAR" or
                            perpendicular["wall"]["id"] ==
                            collinear["wall"]["id"]):
                        continue
                    collinear_wall = collinear["wall"]
                    perpendicular_wall = perpendicular["wall"]
                    collinear_point = (first[:2] if collinear_endpoint == 0
                                       else second[:2])
                    candidate_other = (second[:2] if collinear_endpoint == 0
                                       else first[:2])
                    support_other = max(
                        (collinear["source"]["start"],
                         collinear["source"]["end"]),
                        key=lambda point: math.dist(point, collinear_point))
                    candidate_direction = [
                        candidate_other[0] - collinear_point[0],
                        candidate_other[1] - collinear_point[1],
                    ]
                    support_direction = [
                        support_other[0] - collinear_point[0],
                        support_other[1] - collinear_point[1],
                    ]
                    if (candidate_direction[0] * support_direction[0] +
                            candidate_direction[1] * support_direction[1] >=
                            -1e-9):
                        continue
                    try:
                        collinear_thickness_m = float(
                            collinear_wall["wall"].get("thickness")) / 1000.0
                        perpendicular_thickness_m = float(
                            perpendicular_wall["wall"].get(
                                "thickness")) / 1000.0
                    except (TypeError, ValueError, OverflowError):
                        continue
                    length_tolerance = max(
                        _MODELED_JUNCTION_LENGTH_TOLERANCE_M,
                        0.01 * collinear_thickness_m)
                    if (collinear_thickness_m <= 0.0 or
                            perpendicular_thickness_m <= 0.0 or
                            abs(candidate_source["line"]["length"] -
                                collinear_thickness_m) > length_tolerance):
                        continue
                    collinear_endpoint_distance = min(
                        math.dist(collinear_point, point)
                        for point in (collinear_wall["line"]["p1"],
                                      collinear_wall["line"]["p2"]))
                    if collinear_endpoint_distance > 0.005:
                        continue
                    connected_distance = _point_segment_distance(
                        min((collinear_wall["line"]["p1"],
                             collinear_wall["line"]["p2"]),
                            key=lambda point: math.dist(
                                point, collinear_point)),
                        perpendicular_wall["line"]["p1"],
                        perpendicular_wall["line"]["p2"])
                    if connected_distance > (
                            perpendicular_thickness_m / 2.0 +
                            _MODELED_JUNCTION_LENGTH_TOLERANCE_M):
                        continue
                    excluded_ids = {
                        source_id, collinear["source_id"],
                        perpendicular["source_id"],
                    }
                    if has_parallel_pair(candidate_source, excluded_ids):
                        continue
                    support_pairs.append({
                        "collinear": collinear,
                        "perpendicular": perpendicular,
                        "collinear_endpoint": collinear_endpoint,
                        "connected_distance": connected_distance,
                        "collinear_thickness_m": collinear_thickness_m,
                        "perpendicular_thickness_m": (
                            perpendicular_thickness_m),
                    })
        unique_pairs = {}
        for pair in support_pairs:
            key = (
                pair["collinear"]["source_id"],
                pair["collinear"]["wall"]["id"],
                pair["perpendicular"]["source_id"],
                pair["perpendicular"]["wall"]["id"],
            )
            unique_pairs[key] = pair
        if len(unique_pairs) != 1:
            continue
        pair = next(iter(unique_pairs.values()))
        collinear = pair["collinear"]
        perpendicular = pair["perpendicular"]
        unit.update({
            "status": "RESOLVED_MODELED_JUNCTION_EDGE",
            "decision_reason": "exact_l_junction_closure_edge",
            "review_bucket": "RESOLVED_MODELED_JUNCTION_EDGE",
            "resolution_type": "L_JUNCTION_CLOSURE_EDGE",
            "resolution_evidence": {
                "geometry_source": "DXF_VECTOR",
                "collinear_source_segment_id": collinear["source_id"],
                "collinear_wall_id": collinear["wall"]["id"],
                "perpendicular_source_segment_id": perpendicular[
                    "source_id"],
                "perpendicular_wall_id": perpendicular["wall"]["id"],
                "collinear_endpoint_index": pair[
                    "collinear_endpoint"],
                "collinear_endpoint_distance_m": round(
                    collinear["endpoint_distance"], 6),
                "perpendicular_endpoint_distance_m": round(
                    perpendicular["endpoint_distance"], 6),
                "modeled_wall_connected_distance_m": round(
                    pair["connected_distance"], 6),
                "collinear_wall_thickness_m": round(
                    pair["collinear_thickness_m"], 6),
                "perpendicular_wall_thickness_m": round(
                    pair["perpendicular_thickness_m"], 6),
                "source_interval_endpoint_tolerance_m": (
                    _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M),
                "angle_tolerance_deg": (
                    _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG),
            },
            "auto_action": "NONE",
        })
        resolved_count += 1

    units_by_id = {
        unit.get("review_unit_id"): unit for unit in review_units
        if unit.get("review_unit_id")
    }
    for item in raw_review_items:
        for summary in item.get("range_review_units", []):
            unit = units_by_id.get(summary.get("review_unit_id"))
            if unit is None:
                continue
            summary.update({
                "status": unit.get("status"),
                "decision_reason": unit.get("decision_reason"),
                "review_bucket": unit.get("review_bucket"),
            })
        item["resolved_range_count"] = sum(
            summary.get("status") == "RESOLVED_MODELED_JUNCTION_EDGE"
            for summary in item.get("range_review_units", []))
        item["needs_review_range_count"] = sum(
            summary.get("status") == "NEEDS_REVIEW"
            for summary in item.get("range_review_units", []))
    return resolved_count


def _resolve_endpoint_adjustment_review_units(
        review_units: list[dict], raw_review_items: list[dict],
        source_records: list[dict], walls: list[dict]) -> int:
    """Resolve tails consumed by an explicit, traceable endpoint adjustment.

    ``heal_wall_junctions`` can move a paired wall endpoint onto a supported
    junction.  Source-face coverage is measured against the final centerline,
    so the small face interval between the original source endpoint and that
    adjusted centerline endpoint would otherwise remain a false omission.
    This resolver only annotates evidence; it never extends or mutates walls.
    """
    sources_by_id: dict[str, list[dict]] = {}
    for source in source_records:
        source_id = source.get("source_segment_id")
        if source_id:
            sources_by_id.setdefault(str(source_id), []).append(source)

    owners_by_source_id: dict[str, list[dict]] = {}
    for index, wall in enumerate(walls):
        start, end = wall.get("start"), wall.get("end")
        if (not isinstance(start, (list, tuple)) or len(start) < 2 or
                not isinstance(end, (list, tuple)) or len(end) < 2):
            continue
        wall_line = _line_record(tuple(start[:2]), tuple(end[:2]))
        if wall_line is None:
            continue
        record = {
            "id": wall.get("id", f"wall_{index}"),
            "wall": wall,
            "line": wall_line,
            "source_ids": [str(value) for value in
                           (wall.get("source_segment_ids") or []) if value],
        }
        for source_id in set(record["source_ids"]):
            owners_by_source_id.setdefault(source_id, []).append(record)

    def projection(line, point):
        return ((float(point[0]) - line["p1"][0]) * line["u"][0] +
                (float(point[1]) - line["p1"][1]) * line["u"][1])

    def perpendicular_distance(line, point):
        return abs((float(point[0]) - line["p1"][0]) * line["n"][0] +
                   (float(point[1]) - line["p1"][1]) * line["n"][1])

    def identity_complete(reference, unit):
        if (not isinstance(reference, dict) or
                reference.get("identity_is_complete") is not True or
                reference.get("identity_limitations") not in ([], ())):
            return False
        return (reference.get("drawing_identity") ==
                unit.get("drawing_identity") and
                reference.get("structural_occurrence_id") ==
                unit.get("structural_occurrence_id"))

    resolved_count = 0
    for unit in review_units:
        if (unit.get("status") != "NEEDS_REVIEW" or
                unit.get("unit_type") != "UNMAPPED_SOURCE_RANGE" or
                unit.get("review_bucket") != "POSSIBLE_OMISSION" or
                unit.get("identity_is_complete") is not True or
                unit.get("identity_limitations") or
                float(unit.get("length_m", 0.0) or 0.0) >= 0.50):
            continue
        source_id = str(unit.get("source_segment_id") or "")
        source_choices = sources_by_id.get(source_id, [])
        owner_choices = owners_by_source_id.get(source_id, [])
        if len(source_choices) != 1 or len(owner_choices) != 1:
            continue
        source = source_choices[0]
        owner = owner_choices[0]
        owner_wall = owner["wall"]
        if owner_wall.get("paired") is not True:
            continue
        source_line = source.get("line")
        unit_start, unit_end = unit.get("start"), unit.get("end")
        if (not isinstance(unit_start, (list, tuple)) or len(unit_start) < 2 or
                not isinstance(unit_end, (list, tuple)) or len(unit_end) < 2):
            continue
        range_line = _line_record(
            tuple(unit_start[:2]), tuple(unit_end[:2]))
        if source_line is None or range_line is None:
            continue
        if (_angle_delta(source_line["angle"], owner["line"]["angle"]) >
                _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG or
                _angle_delta(range_line["angle"], owner["line"]["angle"]) >
                _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG):
            continue
        try:
            thickness_m = float(owner_wall.get("thickness")) / 1000.0
        except (TypeError, ValueError, OverflowError):
            continue
        if thickness_m <= 0.0:
            continue
        offset_tolerance = max(0.01, 0.05 * thickness_m)
        source_offset = perpendicular_distance(owner["line"],
                                               source_line["p1"])
        range_offset = perpendicular_distance(owner["line"],
                                              range_line["p1"])
        if (abs(source_offset - thickness_m / 2.0) > offset_tolerance or
                abs(range_offset - source_offset) > offset_tolerance):
            continue

        range_points = (range_line["p1"], range_line["p2"])
        source_end_distance = min(
            math.dist(point, source_endpoint)
            for point in range_points
            for source_endpoint in (source_line["p1"], source_line["p2"]))
        if source_end_distance > _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M:
            continue
        source_endpoint_index = min(
            ((math.dist(point, source_endpoint), point_index)
             for point_index, point in enumerate(range_points)
             for source_endpoint in (source_line["p1"], source_line["p2"])),
            key=lambda value: value[0])[1]
        target_point = range_points[1 - source_endpoint_index]
        target_projection = projection(owner["line"], target_point)
        if min(abs(target_projection),
               abs(target_projection - owner["line"]["length"])) > \
                _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M:
            continue

        adjustment_matches = []
        for adjustment in owner_wall.get("endpoint_adjustments") or []:
            if (not isinstance(adjustment, dict) or
                    adjustment.get("operation") != "heal_wall_junctions"):
                continue
            after = adjustment.get("after")
            if (not isinstance(after, (list, tuple)) or len(after) < 2 or
                    perpendicular_distance(owner["line"], after) >
                    _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M or
                    min(abs(projection(owner["line"], after) -
                            projection(owner["line"], point))
                        for point in range_points) >
                    _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M):
                continue
            constraint_refs = adjustment.get("constraint_source_refs") or []
            constraint_ids = {
                str(value) for value in
                (adjustment.get("constraint_source_segment_ids") or [])
                if value
            }
            if (not constraint_refs or
                    not all(identity_complete(reference, unit)
                            for reference in constraint_refs) or
                    not constraint_ids or
                    len(constraint_ids) != len(
                        adjustment.get("constraint_source_segment_ids") or [])):
                continue
            adjustment_matches.append(adjustment)
        if len(adjustment_matches) != 1:
            continue
        adjustment = adjustment_matches[0]
        unit.update({
            "status": "RESOLVED_ENDPOINT_ADJUSTMENT",
            "decision_reason": "modeled_wall_endpoint_adjustment",
            "review_bucket": "RESOLVED_ENDPOINT_ADJUSTMENT",
            "resolution_type": "MODELED_ENDPOINT_ADJUSTMENT",
            "resolution_evidence": {
                "classification": "EVIDENCE_RESOLUTION_ONLY",
                "geometry_source": "DXF_VECTOR",
                "wall_id": owner["id"],
                "source_segment_id": source_id,
                "adjustment_endpoint": adjustment.get("endpoint"),
                "adjustment_after": list(adjustment.get("after")[:2]),
                "constraint_source_segment_ids": sorted(constraint_ids),
                "range_length_m": round(range_line["length"], 6),
                "wall_thickness_m": round(thickness_m, 6),
                "endpoint_tolerance_m": (
                    _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M),
                "model_geometry_created": False,
            },
            "auto_action": "NONE",
        })
        resolved_count += 1

    units_by_id = {
        unit.get("review_unit_id"): unit for unit in review_units
        if unit.get("review_unit_id")
    }
    for item in raw_review_items:
        for summary in item.get("range_review_units", []):
            unit = units_by_id.get(summary.get("review_unit_id"))
            if unit is None:
                continue
            summary.update({
                "status": unit.get("status"),
                "decision_reason": unit.get("decision_reason"),
                "review_bucket": unit.get("review_bucket"),
            })
        item["resolved_range_count"] = sum(
            str(summary.get("status") or "").startswith("RESOLVED_")
            for summary in item.get("range_review_units", []))
        item["needs_review_range_count"] = sum(
            summary.get("status") == "NEEDS_REVIEW"
            for summary in item.get("range_review_units", []))
    return resolved_count


def _audit_short_wall_pair_groups(review_units: list[dict]) -> dict:
    """Group exact short parallel-face evidence without proposing geometry.

    This is the first, deliberately non-executable stage of short-wall
    recovery.  Only unresolved, identity-complete DXF ranges participate.
    Parallel fragments on the same two infinite face lines are rolled up, but
    their true overlap runs and gaps remain separate.  Endpoint support,
    openings, junction hand-offs and existing-wall replacement are later
    decisions; this audit never fills a hull or mutates a model wall.
    """
    eligible = []
    for unit in review_units:
        if (unit.get("status") != "NEEDS_REVIEW" or
                unit.get("unit_type") != "UNMAPPED_SOURCE_RANGE" or
                unit.get("review_bucket") != "POSSIBLE_OMISSION" or
                unit.get("identity_is_complete") is not True or
                unit.get("identity_limitations") or
                unit.get("parent_profile_id") is not None or
                not unit.get("source_segment_id") or
                not unit.get("drawing_identity") or
                not unit.get("structural_occurrence_id")):
            continue
        first, second = unit.get("start"), unit.get("end")
        if (not isinstance(first, (list, tuple)) or len(first) < 2 or
                not isinstance(second, (list, tuple)) or len(second) < 2):
            continue
        line = _line_record(tuple(first[:2]), tuple(second[:2]))
        if line is None or line["length"] + 1e-9 < (
                _SHORT_WALL_PAIR_MIN_OVERLAP_M):
            continue
        leaf_layer = str(unit.get("layer") or "").upper().strip().rsplit(
            "$0$", 1)[-1]
        semantic_group = wall_geometry_group(leaf_layer)
        if semantic_group is None:
            continue
        eligible.append({
            "unit": unit,
            "line": line,
            "leaf_layer": leaf_layer,
            "wall_group": semantic_group,
        })

    raw_pairs = []
    for first_index, first in enumerate(eligible):
        first_unit = first["unit"]
        for second in eligible[first_index + 1:]:
            second_unit = second["unit"]
            if (first["wall_group"] != second["wall_group"] or
                    first_unit.get("drawing_identity") !=
                    second_unit.get("drawing_identity") or
                    first_unit.get("structural_occurrence_id") !=
                    second_unit.get("structural_occurrence_id") or
                    first_unit.get("source_segment_id") ==
                    second_unit.get("source_segment_id")):
                continue
            pair_geometry = _pair_geometry(first["line"], second["line"])
            if (pair_geometry is None or
                    pair_geometry["overlap"] + 1e-9 <
                    _SHORT_WALL_PAIR_MIN_OVERLAP_M):
                continue
            signed_offset = (
                (second["line"]["p1"][0] - first["line"]["p1"][0]) *
                first["line"]["n"][0] +
                (second["line"]["p1"][1] - first["line"]["p1"][1]) *
                first["line"]["n"][1])
            face_a, face_b = ((first, second) if signed_offset >= 0.0
                              else (second, first))
            unit_ids = sorted((
                str(first_unit.get("review_unit_id")),
                str(second_unit.get("review_unit_id")),
            ))
            signature = json.dumps(
                ["SHORT_WALL_RAW_PAIR", *unit_ids],
                ensure_ascii=False, separators=(",", ":"))
            raw_pairs.append({
                "raw_pair_id": "short_wall_pair_" + hashlib.sha256(
                    signature.encode("utf-8")).hexdigest()[:16],
                "face_a": face_a,
                "face_b": face_b,
                "pair_geometry": pair_geometry,
                "thickness_m": pair_geometry["distance"],
                "overlap_m": pair_geometry["overlap"],
                "parallel_error_deg": _angle_delta(
                    first["line"]["angle"], second["line"]["angle"]),
            })
    raw_pairs.sort(key=lambda item: item["raw_pair_id"])

    def baseline_error(reference_line, candidate_line):
        return max(abs(
            (point[0] - reference_line["p1"][0]) *
            reference_line["n"][0] +
            (point[1] - reference_line["p1"][1]) *
            reference_line["n"][1])
            for point in (candidate_line["p1"], candidate_line["p2"]))

    groups = []
    for pair in raw_pairs:
        matches = []
        for group in groups:
            representative = group["representative"]
            if (representative["face_a"]["wall_group"] !=
                    pair["face_a"]["wall_group"] or
                    representative["face_a"]["unit"].get(
                        "drawing_identity") !=
                    pair["face_a"]["unit"].get("drawing_identity") or
                    representative["face_a"]["unit"].get(
                        "structural_occurrence_id") !=
                    pair["face_a"]["unit"].get(
                        "structural_occurrence_id") or
                    abs(representative["thickness_m"] -
                        pair["thickness_m"]) >
                    _SHORT_WALL_PAIR_BASELINE_TOLERANCE_M or
                    _angle_delta(
                        representative["face_a"]["line"]["angle"],
                        pair["face_a"]["line"]["angle"]) >
                    _SHORT_WALL_PAIR_ANGLE_TOLERANCE_DEG):
                continue
            direct_error = max(
                baseline_error(representative["face_a"]["line"],
                               pair["face_a"]["line"]),
                baseline_error(representative["face_b"]["line"],
                               pair["face_b"]["line"]),
            )
            swapped_error = max(
                baseline_error(representative["face_a"]["line"],
                               pair["face_b"]["line"]),
                baseline_error(representative["face_b"]["line"],
                               pair["face_a"]["line"]),
            )
            error = min(direct_error, swapped_error)
            if error <= _SHORT_WALL_PAIR_BASELINE_TOLERANCE_M:
                matches.append((group, swapped_error < direct_error, error))
        # Ambiguous grouping is not silently collapsed.  A separate evidence
        # group is safer than assigning one raw pair to multiple wall strips.
        if len(matches) == 1:
            group, swap, alignment_error = matches[0]
            if swap:
                pair["face_a"], pair["face_b"] = (
                    pair["face_b"], pair["face_a"])
            pair["baseline_alignment_error_m"] = alignment_error
            group["pairs"].append(pair)
        else:
            pair["baseline_alignment_error_m"] = 0.0
            groups.append({"representative": pair, "pairs": [pair]})

    def source_summary(entry):
        unit = entry["unit"]
        return {
            "review_unit_id": unit.get("review_unit_id"),
            "source_segment_id": unit.get("source_segment_id"),
            "entity_handle": unit.get("entity_handle"),
            "source_record_index": unit.get("source_record_index"),
            "segment_index": unit.get("segment_index"),
            "drawing_identity": unit.get("drawing_identity"),
            "source_occurrence_id": unit.get("source_occurrence_id"),
            "placed_entity_id": unit.get("placed_entity_id"),
            "structural_occurrence_id": unit.get(
                "structural_occurrence_id"),
            "placement_path": copy.deepcopy(
                unit.get("placement_path", [])),
            "identity_is_complete": unit.get("identity_is_complete"),
            "identity_limitations": copy.deepcopy(
                unit.get("identity_limitations", [])),
            "start": [round(value, 6) for value in entry["line"]["p1"]],
            "end": [round(value, 6) for value in entry["line"]["p2"]],
            "length_m": round(entry["line"]["length"], 6),
        }

    logical_groups = []
    unit_group_ids: dict[str, list[str]] = {}
    for group in groups:
        representative = group["representative"]
        pairs = sorted(group["pairs"], key=lambda item: item["raw_pair_id"])
        origin = representative["pair_geometry"]["start"]
        ux, uy = representative["face_a"]["line"]["u"]

        projected_runs = []
        for pair in pairs:
            values = [
                (point[0] - origin[0]) * ux +
                (point[1] - origin[1]) * uy
                for point in (pair["pair_geometry"]["start"],
                              pair["pair_geometry"]["end"])
            ]
            projected_runs.append({
                "start": min(values),
                "end": max(values),
                "raw_pair_ids": [pair["raw_pair_id"]],
            })
        projected_runs.sort(key=lambda item: (item["start"], item["end"]))
        merged_runs = []
        for run in projected_runs:
            if (merged_runs and run["start"] <=
                    merged_runs[-1]["end"] + 1e-6):
                merged_runs[-1]["end"] = max(
                    merged_runs[-1]["end"], run["end"])
                merged_runs[-1]["raw_pair_ids"].extend(
                    run["raw_pair_ids"])
            else:
                merged_runs.append(copy.deepcopy(run))

        face_entries = {"face_a": {}, "face_b": {}}
        for pair in pairs:
            for face_name in ("face_a", "face_b"):
                entry = pair[face_name]
                unit_id = str(entry["unit"].get("review_unit_id"))
                face_entries[face_name][unit_id] = entry
        all_entries = [
            entry for face in face_entries.values()
            for entry in face.values()
        ]
        face_extent_values = [
            (point[0] - origin[0]) * ux +
            (point[1] - origin[1]) * uy
            for entry in all_entries
            for point in (entry["line"]["p1"], entry["line"]["p2"])
        ]
        hull_start, hull_end = min(face_extent_values), max(
            face_extent_values)
        overlap_runs = []
        for index, run in enumerate(merged_runs):
            overlap_runs.append({
                "run_index": index,
                "start": [round(origin[0] + run["start"] * ux, 6),
                          round(origin[1] + run["start"] * uy, 6)],
                "end": [round(origin[0] + run["end"] * ux, 6),
                        round(origin[1] + run["end"] * uy, 6)],
                "length_m": round(run["end"] - run["start"], 6),
                "raw_pair_ids": sorted(set(run["raw_pair_ids"])),
            })
        gaps = []
        for index in range(len(merged_runs) - 1):
            start = merged_runs[index]["end"]
            end = merged_runs[index + 1]["start"]
            gaps.append({
                "after_run_index": index,
                "start": [round(origin[0] + start * ux, 6),
                          round(origin[1] + start * uy, 6)],
                "end": [round(origin[0] + end * ux, 6),
                        round(origin[1] + end * uy, 6)],
                "length_m": round(end - start, 6),
                "classification": "UNRESOLVED_BETWEEN_OVERLAP_RUNS",
            })

        source_ids = sorted({
            str(entry["unit"].get("source_segment_id"))
            for entry in all_entries
        })
        pair_ids = [pair["raw_pair_id"] for pair in pairs]
        signature = json.dumps([
            "SHORT_WALL_PAIR_GROUP",
            representative["face_a"]["unit"].get("drawing_identity"),
            representative["face_a"]["unit"].get(
                "structural_occurrence_id"),
            representative["face_a"]["wall_group"],
            sorted(pair_ids),
        ], ensure_ascii=False, separators=(",", ":"))
        group_id = "short_wall_pair_group_" + hashlib.sha256(
            signature.encode("utf-8")).hexdigest()[:16]
        for entry in all_entries:
            unit_id = str(entry["unit"].get("review_unit_id"))
            unit_group_ids.setdefault(unit_id, []).append(group_id)
        thickness_values = [pair["thickness_m"] for pair in pairs]
        logical_groups.append({
            "pair_group_id": group_id,
            "status": "NEEDS_REVIEW",
            "classification": "DOUBLE_FACE_EVIDENCE",
            "geometry_source": "DXF_VECTOR",
            "drawing_identity": representative["face_a"]["unit"].get(
                "drawing_identity"),
            "structural_occurrence_id": representative["face_a"][
                "unit"].get("structural_occurrence_id"),
            "wall_group": representative["face_a"]["wall_group"],
            "leaf_layers": sorted({
                entry["leaf_layer"] for entry in all_entries}),
            "semantic_layer_mismatch": len({
                entry["leaf_layer"] for entry in all_entries}) > 1,
            "raw_pair_count": len(pairs),
            "raw_pair_ids": sorted(pair_ids),
            "raw_pair_evidence": [{
                "raw_pair_id": pair["raw_pair_id"],
                "face_a_review_unit_id": pair["face_a"]["unit"].get(
                    "review_unit_id"),
                "face_b_review_unit_id": pair["face_b"]["unit"].get(
                    "review_unit_id"),
                "face_a_source_segment_id": pair["face_a"]["unit"].get(
                    "source_segment_id"),
                "face_b_source_segment_id": pair["face_b"]["unit"].get(
                    "source_segment_id"),
                "overlap_start": [round(value, 6) for value in
                                  pair["pair_geometry"]["start"]],
                "overlap_end": [round(value, 6) for value in
                                pair["pair_geometry"]["end"]],
                "overlap_m": round(pair["overlap_m"], 6),
                "thickness_mm": round(
                    pair["thickness_m"] * 1000.0, 3),
            } for pair in pairs],
            "source_segment_ids": source_ids,
            "face_a_sources": [source_summary(entry) for _, entry in sorted(
                face_entries["face_a"].items())],
            "face_b_sources": [source_summary(entry) for _, entry in sorted(
                face_entries["face_b"].items())],
            "thickness_mm": round(
                sum(thickness_values) / len(thickness_values) * 1000.0, 3),
            "thickness_spread_mm": round(
                (max(thickness_values) - min(thickness_values)) * 1000.0,
                3),
            "parallel_error_deg": round(max(
                pair["parallel_error_deg"] for pair in pairs), 6),
            "baseline_alignment_error_m": round(max(
                pair["baseline_alignment_error_m"] for pair in pairs), 6),
            "effective_overlap_length_m": round(sum(
                run["end"] - run["start"] for run in merged_runs), 6),
            "overlap_run_count": len(overlap_runs),
            "overlap_runs": overlap_runs,
            "face_extent_hull": {
                "start": [round(origin[0] + hull_start * ux, 6),
                          round(origin[1] + hull_start * uy, 6)],
                "end": [round(origin[0] + hull_end * ux, 6),
                        round(origin[1] + hull_end * uy, 6)],
                "length_m": round(hull_end - hull_start, 6),
            },
            "inter_run_gaps": gaps,
            "requires_endpoint_support_review": True,
            "requires_gap_classification": bool(gaps),
            "model_geometry_created": False,
            "auto_action": "NONE",
        })

    ambiguous_review_unit_ids = sorted(
        unit_id for unit_id, group_ids in unit_group_ids.items()
        if len(set(group_ids)) > 1)
    ambiguous_review_unit_id_set = set(ambiguous_review_unit_ids)
    for group in logical_groups:
        group_unit_ids = {
            str(source.get("review_unit_id"))
            for source in (group.get("face_a_sources", []) +
                           group.get("face_b_sources", []))
        }
        group["pairing_ambiguous"] = bool(
            group_unit_ids & ambiguous_review_unit_id_set)
        group["pairing_ambiguity_reason"] = (
            "source_face_participates_in_multiple_pair_groups"
            if group["pairing_ambiguous"] else None)

    for unit in review_units:
        unit_id = str(unit.get("review_unit_id"))
        if unit_id in unit_group_ids:
            unit["short_wall_pair_group_ids"] = sorted(set(
                unit_group_ids[unit_id]))

    logical_groups.sort(key=lambda item: item["pair_group_id"])
    return {
        "schema_version": "buildmate.short-wall-pair-audit/1.0",
        "status": "REVIEW" if logical_groups else "PASS",
        "classification": "EVIDENCE_ONLY",
        "eligible_review_unit_count": len(eligible),
        "raw_pair_count": len(raw_pairs),
        "pair_group_count": len(logical_groups),
        "needs_review_count": len(logical_groups),
        "approved_proposal_count": 0,
        "ambiguous_review_unit_count": len(ambiguous_review_unit_ids),
        "ambiguous_review_unit_ids": ambiguous_review_unit_ids,
        "groups": logical_groups,
        "rule": {
            "parallel_tolerance_deg": (
                _SHORT_WALL_PAIR_ANGLE_TOLERANCE_DEG),
            "face_distance_range_m": [0.08, 0.60],
            "minimum_raw_overlap_m": _SHORT_WALL_PAIR_MIN_OVERLAP_M,
            "minimum_overlap_ratio": 0.60,
            "baseline_grouping_tolerance_m": (
                _SHORT_WALL_PAIR_BASELINE_TOLERANCE_M),
            "same_wall_group_required": True,
            "leaf_layer_mismatch_requires_review": True,
            "shared_face_across_pair_groups_is_ambiguous": True,
            "preserve_overlap_runs": True,
            "fill_face_extent_hull": False,
            "auto_action": "NONE",
        },
    }


def _classify_short_wall_proposals(pair_audit: dict,
                                   review_units: list[dict],
                                   source_records: list[dict],
                                   walls: list[dict]) -> dict:
    """Split face evidence into support-audited, non-executable proposals."""
    def safe_float(value, default=0.0):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return result if math.isfinite(result) else default

    units_by_id = {
        str(unit.get("review_unit_id")): unit for unit in review_units
        if unit.get("review_unit_id")
    }

    def proposal_material_refs(unit_ids, source_ids,
                               existing_refs=()):
        expected_ids = {str(value) for value in source_ids if value}
        references = []
        for reference in existing_refs:
            if (isinstance(reference, dict) and
                    str(reference.get("source_segment_id") or "") in
                    expected_ids):
                references.append(copy.deepcopy(reference))
        for unit_id in unit_ids:
            unit = units_by_id.get(str(unit_id))
            if (unit is None or
                    str(unit.get("source_segment_id") or "") not in
                    expected_ids):
                continue
            reference = source_reference_from_review_unit(unit)
            if reference is not None:
                references.append(reference)
        unique = {}
        for reference in references:
            unique[_source_ref_key(reference)] = reference
        ordered = [unique[key] for key in sorted(unique)]
        actual_ids = {
            str(reference.get("source_segment_id"))
            for reference in ordered if reference.get("source_segment_id")
        }
        return ordered, sorted(expected_ids - actual_ids)
    strict_caps = [
        unit for unit in review_units
        if (unit.get("status") in {
                "NEEDS_REVIEW",
                "RESOLVED_STRICT_TOPOLOGY_END_CAP",
            } and
            unit.get("review_bucket") == "STRICT_TOPOLOGY_END_CAP" and
            unit.get("decision_reason") == "strict_topology_end_cap" and
            unit.get("identity_is_complete") is True and
            not unit.get("identity_limitations"))
    ]
    sources_by_id: dict[str, list[dict]] = {}
    for source in source_records:
        source_id = source.get("source_segment_id")
        if source_id:
            sources_by_id.setdefault(str(source_id), []).append(source)

    wall_records = []
    for index, wall in enumerate(walls):
        start, end = wall.get("start"), wall.get("end")
        if (not isinstance(start, (list, tuple)) or len(start) < 2 or
                not isinstance(end, (list, tuple)) or len(end) < 2):
            continue
        line = _line_record(tuple(start[:2]), tuple(end[:2]))
        references = wall.get("source_segment_refs") or []
        if line is None or not references or not all(
                isinstance(reference, dict) and
                reference.get("identity_is_complete") is True and
                reference.get("identity_limitations") in ([], ()) and
                reference.get("drawing_identity") and
                reference.get("structural_occurrence_id")
                for reference in references):
            continue
        try:
            thickness_m = float(wall.get("thickness")) / 1000.0
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(thickness_m) or thickness_m <= 0.0:
            continue
        source_lines = [
            sources_by_id[source_id][0]
            for source_id in sorted({
                str(reference.get("source_segment_id"))
                for reference in references
                if reference.get("source_segment_id")
            })
            if len(sources_by_id.get(source_id, [])) == 1 and
            sources_by_id[source_id][0].get("identity_is_complete") is True
        ]
        wall_records.append({
            "id": wall.get("id", f"wall_{index}"),
            "wall": wall,
            "line": line,
            "thickness_m": thickness_m,
            "drawing_identity": references[0].get("drawing_identity"),
            "structural_occurrence_id": references[0].get(
                "structural_occurrence_id"),
            "source_segment_ids": sorted({
                str(reference.get("source_segment_id"))
                for reference in references
                if reference.get("source_segment_id")
            }),
            "source_lines": source_lines,
            "source_face_groups": _logical_source_face_groups(source_lines),
        })

    def source_line(source):
        start, end = source.get("start"), source.get("end")
        if (not isinstance(start, (list, tuple)) or len(start) < 2 or
                not isinstance(end, (list, tuple)) or len(end) < 2):
            return None
        return _line_record(tuple(start[:2]), tuple(end[:2]))

    def point_on_face(face_line, center_point):
        distance = (
            (center_point[0] - face_line["p1"][0]) * face_line["u"][0] +
            (center_point[1] - face_line["p1"][1]) * face_line["u"][1])
        return [face_line["p1"][0] + distance * face_line["u"][0],
                face_line["p1"][1] + distance * face_line["u"][1]]

    def model_wall_matches(group, candidate_line, center_point,
                           face_points, *, paired: bool | None = None):
        matches = []
        for wall_record in wall_records:
            wall = wall_record["wall"]
            if (paired is not None and bool(wall.get("paired")) != paired):
                continue
            if (wall_record["drawing_identity"] !=
                    group.get("drawing_identity") or
                    wall_record["structural_occurrence_id"] !=
                    group.get("structural_occurrence_id")):
                continue
            perpendicular_error = abs(90.0 - _angle_delta(
                candidate_line["angle"], wall_record["line"]["angle"]))
            if perpendicular_error > _SHORT_WALL_PAIR_ANGLE_TOLERANCE_DEG:
                continue
            envelope = (wall_record["thickness_m"] / 2.0 +
                        _SHORT_WALL_SUPPORT_TOLERANCE_M)
            distances = [
                _point_segment_distance(point, wall_record["line"]["p1"],
                                        wall_record["line"]["p2"])
                for point in [center_point, *face_points]
            ]
            if max(distances) > envelope:
                continue
            matches.append({
                "wall_id": wall_record["id"],
                "paired": bool(wall.get("paired")),
                "thickness_mm": round(
                    wall_record["thickness_m"] * 1000.0, 3),
                "source_segment_ids": wall_record["source_segment_ids"],
                "maximum_envelope_distance_m": round(max(distances), 6),
                "perpendicular_error_deg": round(
                    perpendicular_error, 6),
            })
        return sorted(matches, key=lambda item: item["wall_id"])

    def wall_summary(wall_record, *, source_node_matches=None,
                     duplicate_of=None):
        return {
            "wall_id": wall_record["id"],
            "paired": bool(wall_record["wall"].get("paired")),
            "thickness_mm": round(
                wall_record["thickness_m"] * 1000.0, 3),
            "source_segment_ids": wall_record["source_segment_ids"],
            "source_node_matches": copy.deepcopy(source_node_matches or []),
            "duplicate_of_wall_id": duplicate_of,
        }

    def exact_paired_endpoint_matches(group, candidate_line, face_points,
                                      component_face_sources):
        exact_records = []
        for wall_record in wall_records:
            if (wall_record["wall"].get("paired") is not True or
                    wall_record["drawing_identity"] !=
                    group.get("drawing_identity") or
                    wall_record["structural_occurrence_id"] !=
                    group.get("structural_occurrence_id") or
                    abs(90.0 - _angle_delta(
                        candidate_line["angle"],
                        wall_record["line"]["angle"])) >
                    _SHORT_WALL_PAIR_ANGLE_TOLERANCE_DEG or
                    len(wall_record["source_face_groups"]) != 2):
                continue
            node_matches = []
            for face_index, face_point in enumerate(face_points):
                candidate_sources = component_face_sources[face_index]
                candidate_nodes = [
                    source for source in candidate_sources
                    if (source_line(source) is not None and
                        min(math.dist(face_point, point) for point in (
                            source_line(source)["p1"],
                            source_line(source)["p2"])) <=
                        _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M)
                ]
                support_nodes = [
                    source for source in wall_record["source_lines"]
                    if (source.get("drawing_identity") ==
                        group.get("drawing_identity") and
                        source.get("structural_occurrence_id") ==
                        group.get("structural_occurrence_id") and
                        min(math.dist(face_point, point) for point in (
                            source["line"]["p1"],
                            source["line"]["p2"])) <=
                        _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M)
                ]
                if candidate_nodes and support_nodes:
                    node_matches.append({
                        "face_index": face_index,
                        "point": [round(value, 6) for value in face_point],
                        "candidate_source_segment_ids": sorted({
                            str(source.get("source_segment_id"))
                            for source in candidate_nodes}),
                        "support_source_segment_ids": sorted({
                            str(source.get("source_segment_id"))
                            for source in support_nodes}),
                    })
            if node_matches:
                exact_records.append((wall_record, node_matches))

        matches = [wall_summary(record, source_node_matches=node_matches)
                   for record, node_matches in exact_records]
        exact_ids = {record["id"] for record, _nodes in exact_records}
        for exact_record, _nodes in exact_records:
            for other in wall_records:
                if (other["id"] in exact_ids or
                        other["wall"].get("paired") is not True or
                        other["drawing_identity"] !=
                        exact_record["drawing_identity"] or
                        other["structural_occurrence_id"] !=
                        exact_record["structural_occurrence_id"] or
                        abs(other["thickness_m"] -
                            exact_record["thickness_m"]) >
                        _SHORT_WALL_PAIR_BASELINE_TOLERANCE_M or
                        _angle_delta(other["line"]["angle"],
                                     exact_record["line"]["angle"]) >
                        _SHORT_WALL_PAIR_ANGLE_TOLERANCE_DEG or
                        max(abs(
                            (point[0] - exact_record["line"]["p1"][0]) *
                            exact_record["line"]["n"][0] +
                            (point[1] - exact_record["line"]["p1"][1]) *
                            exact_record["line"]["n"][1])
                            for point in (other["line"]["p1"],
                                          other["line"]["p2"])) >
                        _SHORT_WALL_PAIR_BASELINE_TOLERANCE_M):
                    continue
                origin = exact_record["line"]["p1"]
                ux, uy = exact_record["line"]["u"]
                values = [
                    (point[0] - origin[0]) * ux +
                    (point[1] - origin[1]) * uy
                    for point in (other["line"]["p1"],
                                  other["line"]["p2"])
                ]
                overlap = max(0.0, min(
                    exact_record["line"]["length"], max(values)) -
                    max(0.0, min(values)))
                if overlap / min(exact_record["line"]["length"],
                                 other["line"]["length"]) < 0.90:
                    continue
                matches.append(wall_summary(
                    other, duplicate_of=exact_record["id"]))
        unique = {item["wall_id"]: item for item in matches}
        return [unique[key] for key in sorted(unique)]

    def exact_gap_wall_matches(group, candidate_line, gap_line,
                               missing_face_line, missing_face_sources):
        boundary_points = [
            point_on_face(missing_face_line, point)
            for point in (gap_line["p1"], gap_line["p2"])
        ]
        matches = []
        for wall_record in wall_records:
            if (wall_record["wall"].get("paired") is not True or
                    wall_record["drawing_identity"] !=
                    group.get("drawing_identity") or
                    wall_record["structural_occurrence_id"] !=
                    group.get("structural_occurrence_id") or
                    abs(90.0 - _angle_delta(
                        candidate_line["angle"],
                        wall_record["line"]["angle"])) >
                    _SHORT_WALL_PAIR_ANGLE_TOLERANCE_DEG or
                    len(wall_record["source_face_groups"]) != 2):
                continue
            tolerance = max(
                _MODELED_JUNCTION_LENGTH_TOLERANCE_M,
                0.01 * wall_record["thickness_m"])
            if abs(wall_record["thickness_m"] -
                   gap_line["length"]) > tolerance:
                continue
            node_matches = []
            for boundary_index, point in enumerate(boundary_points):
                candidate_nodes = [
                    source for source in missing_face_sources
                    if (source_line(source) is not None and
                        min(math.dist(point, endpoint) for endpoint in (
                            source_line(source)["p1"],
                            source_line(source)["p2"])) <=
                        _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M)
                ]
                support_nodes = [
                    source for source in wall_record["source_lines"]
                    if min(math.dist(point, endpoint) for endpoint in (
                        source["line"]["p1"],
                        source["line"]["p2"])) <=
                    _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M
                ]
                if not candidate_nodes or not support_nodes:
                    node_matches = []
                    break
                node_matches.append({
                    "boundary_index": boundary_index,
                    "point": [round(value, 6) for value in point],
                    "candidate_source_segment_ids": sorted({
                        str(source.get("source_segment_id"))
                        for source in candidate_nodes}),
                    "support_source_segment_ids": sorted({
                        str(source.get("source_segment_id"))
                        for source in support_nodes}),
                })
            if len(node_matches) == 2:
                matches.append(wall_summary(
                    wall_record, source_node_matches=node_matches))
        return sorted(matches, key=lambda item: item["wall_id"])

    def cap_matches(group, face_points, component_source_ids):
        matches = []
        for cap in strict_caps:
            if (cap.get("drawing_identity") !=
                    group.get("drawing_identity") or
                    cap.get("structural_occurrence_id") !=
                    group.get("structural_occurrence_id")):
                continue
            cap_line = source_line(cap)
            if cap_line is None:
                continue
            cap_points = [cap_line["p1"], cap_line["p2"]]
            assignment_errors = [
                max(math.dist(face_points[0], cap_points[order[0]]),
                    math.dist(face_points[1], cap_points[order[1]]))
                for order in ((0, 1), (1, 0))
            ]
            endpoint_error = min(assignment_errors)
            if endpoint_error > _SHORT_WALL_SUPPORT_TOLERANCE_M:
                continue
            support_ids = {
                str(item.get("source_segment_id"))
                for item in cap.get("support_evidence", [])
                if item.get("source_segment_id")
            }
            if len(support_ids) != 2 or not support_ids.issubset(
                    component_source_ids):
                continue
            matches.append({
                "review_unit_id": cap.get("review_unit_id"),
                "entity_handle": cap.get("entity_handle"),
                "source_segment_id": cap.get("source_segment_id"),
                "support_source_segment_ids": sorted(support_ids),
                "endpoint_error_m": round(endpoint_error, 6),
            })
        return sorted(matches, key=lambda item: str(
            item.get("review_unit_id")))

    def exact_source_cap_matches(group, face_points,
                                 wall_face_source_ids, thickness_m):
        matches = []
        length_tolerance = max(
            _MODELED_JUNCTION_LENGTH_TOLERANCE_M,
            0.01 * thickness_m)
        for unit in review_units:
            if (unit.get("status") != "NEEDS_REVIEW" or
                    unit.get("unit_type") != "UNMAPPED_SOURCE_RANGE" or
                    unit.get("review_bucket") not in {
                        "POSSIBLE_OMISSION", "STRICT_TOPOLOGY_END_CAP"} or
                    unit.get("identity_is_complete") is not True or
                    unit.get("identity_limitations") or
                    unit.get("parent_profile_id") is not None or
                    unit.get("drawing_identity") !=
                    group.get("drawing_identity") or
                    unit.get("structural_occurrence_id") !=
                    group.get("structural_occurrence_id") or
                    str(unit.get("source_segment_id")) in
                    wall_face_source_ids):
                continue
            line = source_line(unit)
            if (line is None or abs(line["length"] - thickness_m) >
                    length_tolerance):
                continue
            points = [line["p1"], line["p2"]]
            error = min(
                max(math.dist(face_points[0], points[order[0]]),
                    math.dist(face_points[1], points[order[1]]))
                for order in ((0, 1), (1, 0)))
            if error <= _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M:
                matches.append({
                    "review_unit_id": unit.get("review_unit_id"),
                    "entity_handle": unit.get("entity_handle"),
                    "source_segment_id": unit.get("source_segment_id"),
                    "endpoint_error_m": round(error, 6),
                    "classification": "EXACT_SOURCE_CAP",
                })
        return sorted(matches, key=lambda item: str(
            item.get("review_unit_id")))

    def face_covers_interval(face_sources, origin, ux, uy,
                             interval_start, interval_end):
        intervals = []
        for source in face_sources:
            line = source_line(source)
            if line is None:
                continue
            values = [
                (point[0] - origin[0]) * ux +
                (point[1] - origin[1]) * uy
                for point in (line["p1"], line["p2"])
            ]
            intervals.append(sorted(values))
        intervals.sort()
        cursor = interval_start
        for start, end in intervals:
            if end < cursor - _SHORT_WALL_SUPPORT_TOLERANCE_M:
                continue
            if start > cursor + _SHORT_WALL_SUPPORT_TOLERANCE_M:
                break
            cursor = max(cursor, end)
            if cursor >= interval_end - _SHORT_WALL_SUPPORT_TOLERANCE_M:
                return True
        return False

    proposals = []
    insufficient = []
    for group in pair_audit.get("groups", []):
        face_a_sources = list(group.get("face_a_sources") or [])
        face_b_sources = list(group.get("face_b_sources") or [])
        face_a_line = source_line(face_a_sources[0]) if face_a_sources else None
        face_b_line = source_line(face_b_sources[0]) if face_b_sources else None
        runs = sorted(group.get("overlap_runs") or [],
                      key=lambda item: item.get("run_index", 0))
        if face_a_line is None or face_b_line is None or not runs:
            continue
        axis_line = _line_record(tuple(runs[0]["start"]),
                                 tuple(runs[0]["end"]))
        if axis_line is None:
            continue
        origin = axis_line["p1"]
        ux, uy = axis_line["u"]
        raw_pair_evidence = {
            item.get("raw_pair_id"): item
            for item in group.get("raw_pair_evidence", [])
            if item.get("raw_pair_id")
        }

        gap_supports = {}
        for gap in group.get("inter_run_gaps", []):
            gap_index = gap.get("after_run_index")
            gap_line = _line_record(tuple(gap.get("start", [])),
                                    tuple(gap.get("end", [])))
            if gap_line is None:
                continue
            try:
                thickness_m = float(group.get("thickness_mm")) / 1000.0
            except (TypeError, ValueError, OverflowError):
                continue
            length_tolerance = max(
                _MODELED_JUNCTION_LENGTH_TOLERANCE_M,
                0.01 * thickness_m)
            if abs(gap_line["length"] - thickness_m) > length_tolerance:
                continue
            projected = [
                (point[0] - origin[0]) * ux +
                (point[1] - origin[1]) * uy
                for point in (gap_line["p1"], gap_line["p2"])
            ]
            interval_start, interval_end = min(projected), max(projected)
            face_coverage = [
                face_covers_interval(face_sources, origin, ux, uy,
                                     interval_start, interval_end)
                for face_sources in (face_a_sources, face_b_sources)
            ]
            if sum(face_coverage) != 1:
                continue
            missing_face_index = 0 if not face_coverage[0] else 1
            missing_face_line = (face_a_line if missing_face_index == 0
                                 else face_b_line)
            missing_face_sources = (face_a_sources
                                    if missing_face_index == 0
                                    else face_b_sources)
            matches = exact_gap_wall_matches(
                group, axis_line, gap_line, missing_face_line,
                missing_face_sources)
            if len(matches) == 1:
                gap_supports[gap_index] = {
                    "classification": "EXACT_PAIRED_JUNCTION_HANDOFF",
                    "gap": copy.deepcopy(gap),
                    "continuous_face": (
                        "face_a" if face_coverage[0] else "face_b"),
                    "support_wall": matches[0],
                }

        components = []
        current = {"runs": [runs[0]], "junction_handoffs": []}
        for index, next_run in enumerate(runs[1:]):
            handoff = gap_supports.get(index)
            if handoff is not None:
                current["runs"].append(next_run)
                current["junction_handoffs"].append(handoff)
            else:
                components.append(current)
                current = {"runs": [next_run], "junction_handoffs": []}
        components.append(current)

        for component_index, component in enumerate(components):
            component_runs = component["runs"]
            start = component_runs[0]["start"]
            end = component_runs[-1]["end"]
            candidate_line = _line_record(tuple(start), tuple(end))
            if candidate_line is None:
                continue
            pair_ids = sorted({
                pair_id for run in component_runs
                for pair_id in run.get("raw_pair_ids", [])
            })
            component_pairs = [raw_pair_evidence[pair_id]
                               for pair_id in pair_ids
                               if pair_id in raw_pair_evidence]
            component_unit_ids = sorted({
                str(pair.get(field))
                for pair in component_pairs
                for field in ("face_a_review_unit_id",
                              "face_b_review_unit_id")
                if pair.get(field)
            })
            component_source_ids = {
                str(pair.get(field))
                for pair in component_pairs
                for field in ("face_a_source_segment_id",
                              "face_b_source_segment_id")
                if pair.get(field)
            }
            component_face_sources = [
                [source for source in face_sources
                 if str(source.get("review_unit_id")) in
                 component_unit_ids]
                for face_sources in (face_a_sources, face_b_sources)
            ]
            material_refs, missing_material_source_ids = (
                proposal_material_refs(
                    component_unit_ids, component_source_ids))

            endpoint_support = []
            for endpoint_index, point in enumerate((
                    candidate_line["p1"], candidate_line["p2"])):
                face_points = [point_on_face(face_line, point)
                               for face_line in (face_a_line, face_b_line)]
                caps = cap_matches(
                    group, face_points, component_source_ids)
                paired_walls = exact_paired_endpoint_matches(
                    group, candidate_line, face_points,
                    component_face_sources)
                single_walls = model_wall_matches(
                    group, candidate_line, point, face_points, paired=False)
                if len(caps) == 1:
                    status = "EXACT_STRICT_CAP"
                elif len(caps) > 1 or len(paired_walls) > 1 or len(
                        single_walls) > 1:
                    status = "AMBIGUOUS_SUPPORT"
                elif len(paired_walls) == 1:
                    status = "EXACT_PAIRED_WALL_JUNCTION"
                elif len(single_walls) == 1:
                    status = "SINGLE_WALL_JUNCTION"
                else:
                    status = "UNSUPPORTED"
                endpoint_support.append({
                    "endpoint_index": endpoint_index,
                    "point": [round(value, 6) for value in point],
                    "face_points": [[round(value, 6) for value in face_point]
                                    for face_point in face_points],
                    "status": status,
                    "strict_cap_matches": caps,
                    "paired_wall_matches": paired_walls,
                    "single_wall_matches": single_walls,
                })

            support_statuses = [item["status"] for item in endpoint_support]
            blockers = []
            if "UNSUPPORTED" in support_statuses:
                blockers.append("endpoint_unsupported")
            if "AMBIGUOUS_SUPPORT" in support_statuses:
                blockers.append("endpoint_support_ambiguous")
            if "SINGLE_WALL_JUNCTION" in support_statuses:
                blockers.append("endpoint_supported_by_single_wall_only")
            if group.get("pairing_ambiguous"):
                blockers.append("parallel_face_pairing_ambiguous")
            if group.get("semantic_layer_mismatch"):
                blockers.append("semantic_leaf_layer_mismatch")
            if missing_material_source_ids:
                blockers.append("material_source_refs_incomplete")
            strict_eligible = (
                support_statuses == ["EXACT_STRICT_CAP",
                                     "EXACT_STRICT_CAP"] and
                len(component_runs) ==
                len(component["junction_handoffs"]) + 1 and
                not group.get("pairing_ambiguous") and
                not group.get("semantic_layer_mismatch") and
                not missing_material_source_ids)
            proposal_status = (
                "INSUFFICIENT_SUPPORT" if "UNSUPPORTED" in support_statuses
                else "PROPOSED_SHORT_WALL")
            approval_status = (
                "ELIGIBLE_FOR_STRICT_APPROVAL" if strict_eligible else
                "NEEDS_REVIEW")
            signature = json.dumps([
                "SHORT_WALL_PROPOSAL", group.get("pair_group_id"),
                component_index, pair_ids,
                [round(value, 6) for value in candidate_line["p1"]],
                [round(value, 6) for value in candidate_line["p2"]],
            ], ensure_ascii=False, separators=(",", ":"))
            proposal_id = "short_wall_proposal_" + hashlib.sha256(
                signature.encode("utf-8")).hexdigest()[:16]
            proposal = {
                "proposal_id": proposal_id,
                "pair_group_id": group.get("pair_group_id"),
                "status": proposal_status,
                "approval_status": approval_status,
                "geometry_source": "DXF_VECTOR",
                "drawing_identity": group.get("drawing_identity"),
                "structural_occurrence_id": group.get(
                    "structural_occurrence_id"),
                "start": [round(value, 6)
                          for value in candidate_line["p1"]],
                "end": [round(value, 6)
                        for value in candidate_line["p2"]],
                "length_m": round(candidate_line["length"], 6),
                "thickness_mm": group.get("thickness_mm"),
                "source_review_unit_ids": component_unit_ids,
                "source_segment_ids": sorted(component_source_ids),
                "material_source_segment_refs": material_refs,
                "missing_material_source_segment_ids": (
                    missing_material_source_ids),
                "source_entity_handles": sorted({
                    str(units_by_id[unit_id].get("entity_handle"))
                    for unit_id in component_unit_ids
                    if unit_id in units_by_id and
                    units_by_id[unit_id].get("entity_handle")
                }),
                "raw_pair_ids": pair_ids,
                "overlap_runs": copy.deepcopy(component_runs),
                "junction_handoffs": copy.deepcopy(
                    component["junction_handoffs"]),
                "endpoint_support": endpoint_support,
                "blockers": blockers,
                "model_geometry_created": False,
                "auto_action": "NONE",
            }
            proposal["integrity_sha256"] = (
                proposal_integrity_sha256(proposal))
            (insufficient if proposal_status == "INSUFFICIENT_SUPPORT"
             else proposals).append(proposal)
            for unit_id in component_unit_ids:
                unit = units_by_id.get(unit_id)
                if unit is not None:
                    unit.setdefault("short_wall_proposal_ids", []).append(
                        proposal_id)

    single_wall_upgrade_count = 0
    replacement_ambiguity_count = 0
    upgrade_seed_units = [
        unit for unit in review_units
        if (unit.get("status") == "NEEDS_REVIEW" and
            unit.get("unit_type") == "UNMAPPED_SOURCE_RANGE" and
            unit.get("review_bucket") == "POSSIBLE_OMISSION" and
            unit.get("identity_is_complete") is True and
            not unit.get("identity_limitations") and
            unit.get("parent_profile_id") is None and
            safe_float(unit.get("mapped_ratio")) >= 0.50 and
            safe_float(unit.get("length_m"), math.inf) <
            _SHORT_WALL_PAIR_MIN_OVERLAP_M)
    ]
    omission_units_by_source_id: dict[str, list[dict]] = {}
    for unit in review_units:
        if (unit.get("status") == "NEEDS_REVIEW" and
                unit.get("review_bucket") == "POSSIBLE_OMISSION" and
                unit.get("identity_is_complete") is True and
                not unit.get("identity_limitations") and
                unit.get("source_segment_id")):
            omission_units_by_source_id.setdefault(
                str(unit["source_segment_id"]), []).append(unit)

    def exact_bridge_sources(group, baseline_line, first_point,
                             second_point, excluded_source_ids):
        matches = []
        for source in source_records:
            source_id = str(source.get("source_segment_id") or "")
            line = source.get("line")
            if (not source_id or source_id in excluded_source_ids or
                    len(omission_units_by_source_id.get(source_id, [])) != 1 or
                    line is None or
                    source.get("identity_is_complete") is not True or
                    source.get("identity_limitations") or
                    source.get("drawing_identity") !=
                    group.get("drawing_identity") or
                    source.get("structural_occurrence_id") !=
                    group.get("structural_occurrence_id") or
                    _angle_delta(line["angle"], baseline_line["angle"]) >
                    _SHORT_WALL_PAIR_ANGLE_TOLERANCE_DEG):
                continue
            baseline_distance = max(abs(
                (point[0] - baseline_line["p1"][0]) *
                baseline_line["n"][0] +
                (point[1] - baseline_line["p1"][1]) *
                baseline_line["n"][1])
                for point in (line["p1"], line["p2"]))
            endpoint_error = min(
                max(math.dist(first_point, endpoints[0]),
                    math.dist(second_point, endpoints[1]))
                for endpoints in ((line["p1"], line["p2"]),
                                  (line["p2"], line["p1"])))
            if (baseline_distance <=
                    _SHORT_WALL_PAIR_BASELINE_TOLERANCE_M and
                    endpoint_error <=
                    _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M):
                matches.append({
                    "source": source,
                    "unit": omission_units_by_source_id[source_id][0],
                    "endpoint_error_m": endpoint_error,
                })
        return matches

    for wall_record in wall_records:
        wall = wall_record["wall"]
        if (wall.get("paired") is not False or
                len(wall_record["source_segment_ids"]) != 1 or
                len(wall_record["source_lines"]) != 1):
            continue
        existing_source = wall_record["source_lines"][0]
        existing_line = existing_source["line"]
        old_wall_baseline_error = max(abs(
            (point[0] - existing_line["p1"][0]) *
            existing_line["n"][0] +
            (point[1] - existing_line["p1"][1]) *
            existing_line["n"][1])
            for point in (wall_record["line"]["p1"],
                          wall_record["line"]["p2"]))
        if old_wall_baseline_error > _SHORT_WALL_PAIR_BASELINE_TOLERANCE_M:
            continue
        upgrade_matches = []
        for seed_unit in upgrade_seed_units:
            seed_id = str(seed_unit.get("source_segment_id") or "")
            seed_sources = sources_by_id.get(seed_id, [])
            if (len(seed_sources) != 1 or seed_id in
                    wall_record["source_segment_ids"] or
                    seed_unit.get("drawing_identity") !=
                    wall_record["drawing_identity"] or
                    seed_unit.get("structural_occurrence_id") !=
                    wall_record["structural_occurrence_id"]):
                continue
            seed_source = seed_sources[0]
            if (seed_source.get("identity_is_complete") is not True or
                    seed_source.get("profile_association_status") is not None or
                    wall_geometry_group(seed_source.get("layer")) !=
                    wall_geometry_group(existing_source.get("layer"))):
                continue
            pair = _pair_geometry(existing_line, seed_source["line"])
            thickness_tolerance = max(
                _MODELED_JUNCTION_LENGTH_TOLERANCE_M,
                0.01 * wall_record["thickness_m"])
            if (pair is None or pair["overlap"] <
                    _SHORT_WALL_PAIR_MIN_OVERLAP_M or
                    abs(pair["distance"] - wall_record["thickness_m"]) >
                    thickness_tolerance):
                continue
            upgrade_matches.append({
                "seed_unit": seed_unit,
                "seed_source": seed_source,
                "pair": pair,
            })
        if len(upgrade_matches) != 1:
            if len(upgrade_matches) > 1:
                replacement_ambiguity_count += 1
            continue
        upgrade = upgrade_matches[0]
        seed_unit = upgrade["seed_unit"]
        opposing_source = upgrade["seed_source"]
        pair = upgrade["pair"]
        group = {
            "drawing_identity": wall_record["drawing_identity"],
            "structural_occurrence_id": wall_record[
                "structural_occurrence_id"],
        }
        origin = existing_line["p1"]
        ux, uy = existing_line["u"]
        nx, ny = existing_line["n"]

        def bounds(line):
            values = [
                (point[0] - origin[0]) * ux +
                (point[1] - origin[1]) * uy
                for point in (line["p1"], line["p2"])
            ]
            return min(values), max(values)

        existing_bounds = bounds(existing_line)
        opposing_bounds = bounds(opposing_source["line"])
        low = max(existing_bounds[0], opposing_bounds[0])
        high = min(existing_bounds[1], opposing_bounds[1])
        if high - low < _SHORT_WALL_PAIR_MIN_OVERLAP_M:
            continue
        signed_offset = (
            (opposing_source["line"]["p1"][0] - origin[0]) * nx +
            (opposing_source["line"]["p1"][1] - origin[1]) * ny)
        mid_origin = [origin[0] + signed_offset * nx / 2.0,
                      origin[1] + signed_offset * ny / 2.0]
        face_sources = [[existing_source], [opposing_source]]
        bridge_units = []
        excluded_ids = {
            str(existing_source.get("source_segment_id")),
            str(opposing_source.get("source_segment_id")),
        }

        def point_at(line, distance):
            return [line["p1"][0] + distance * ux,
                    line["p1"][1] + distance * uy]

        if opposing_bounds[0] < existing_bounds[0] - 1e-9:
            first = point_at(existing_line, opposing_bounds[0])
            second = point_at(existing_line, existing_bounds[0])
            bridges = exact_bridge_sources(
                group, existing_line, first, second, excluded_ids)
            if len(bridges) == 1:
                low = opposing_bounds[0]
                face_sources[0].append(bridges[0]["source"])
                bridge_units.append(bridges[0]["unit"])
        elif existing_bounds[0] < opposing_bounds[0] - 1e-9:
            first = point_at(opposing_source["line"], existing_bounds[0])
            second = point_at(opposing_source["line"], opposing_bounds[0])
            bridges = exact_bridge_sources(
                group, opposing_source["line"], first, second,
                excluded_ids)
            if len(bridges) == 1:
                low = existing_bounds[0]
                face_sources[1].append(bridges[0]["source"])
                bridge_units.append(bridges[0]["unit"])
        if opposing_bounds[1] > existing_bounds[1] + 1e-9:
            first = point_at(existing_line, existing_bounds[1])
            second = point_at(existing_line, opposing_bounds[1])
            bridges = exact_bridge_sources(
                group, existing_line, first, second, excluded_ids)
            if len(bridges) == 1:
                high = opposing_bounds[1]
                face_sources[0].append(bridges[0]["source"])
                bridge_units.append(bridges[0]["unit"])
        elif existing_bounds[1] > opposing_bounds[1] + 1e-9:
            first = point_at(opposing_source["line"], opposing_bounds[1])
            second = point_at(opposing_source["line"], existing_bounds[1])
            bridges = exact_bridge_sources(
                group, opposing_source["line"], first, second,
                excluded_ids)
            if len(bridges) == 1:
                high = existing_bounds[1]
                face_sources[1].append(bridges[0]["source"])
                bridge_units.append(bridges[0]["unit"])

        proposed_start = [mid_origin[0] + low * ux,
                          mid_origin[1] + low * uy]
        proposed_end = [mid_origin[0] + high * ux,
                        mid_origin[1] + high * uy]
        proposed_line = _line_record(tuple(proposed_start),
                                     tuple(proposed_end))
        if proposed_line is None:
            continue
        endpoint_support = []
        wall_face_source_ids = {
            str(source.get("source_segment_id"))
            for sources in face_sources for source in sources
        }
        cap_unit_ids = []
        for endpoint_index, point in enumerate((
                proposed_line["p1"], proposed_line["p2"])):
            face_points = [point_on_face(line, point) for line in (
                existing_line, opposing_source["line"])]
            caps = exact_source_cap_matches(
                group, face_points, wall_face_source_ids,
                wall_record["thickness_m"])
            paired_walls = exact_paired_endpoint_matches(
                group, proposed_line, face_points, face_sources)
            if len(caps) == 1:
                support_status = "EXACT_SOURCE_CAP"
                cap_unit_ids.append(str(caps[0]["review_unit_id"]))
            elif len(caps) > 1 or len(paired_walls) > 1:
                support_status = "AMBIGUOUS_SUPPORT"
            elif len(paired_walls) == 1:
                support_status = "EXACT_PAIRED_WALL_JUNCTION"
            else:
                support_status = "UNSUPPORTED"
            endpoint_support.append({
                "endpoint_index": endpoint_index,
                "point": [round(value, 6) for value in point],
                "face_points": [[round(value, 6) for value in face_point]
                                for face_point in face_points],
                "status": support_status,
                "source_cap_matches": caps,
                "paired_wall_matches": paired_walls,
            })
        support_statuses = [item["status"] for item in endpoint_support]
        # The preview applier performs this replacement atomically, validates
        # the expected wall fingerprint, and preserves the logical wall id.
        blockers = []
        if "UNSUPPORTED" in support_statuses:
            blockers.append("endpoint_unsupported")
        if "AMBIGUOUS_SUPPORT" in support_statuses:
            blockers.append("endpoint_support_ambiguous")
        source_review_unit_ids = sorted({
            str(seed_unit.get("review_unit_id")),
            *(str(unit.get("review_unit_id")) for unit in bridge_units),
            *cap_unit_ids,
        })
        material_refs, missing_material_source_ids = (
            proposal_material_refs(
                source_review_unit_ids, wall_face_source_ids,
                wall.get("source_segment_refs") or []))
        if missing_material_source_ids:
            blockers.append("material_source_refs_incomplete")
        signature = json.dumps([
            "SHORT_WALL_SINGLE_UPGRADE", wall_record["id"],
            sorted(wall_face_source_ids),
            [round(value, 6) for value in proposed_line["p1"]],
            [round(value, 6) for value in proposed_line["p2"]],
        ], ensure_ascii=False, separators=(",", ":"))
        proposal_id = "short_wall_proposal_" + hashlib.sha256(
            signature.encode("utf-8")).hexdigest()[:16]
        proposal = {
            "proposal_id": proposal_id,
            "pair_group_id": None,
            "status": "PROPOSED_WALL_UPGRADE",
            "approval_status": "NEEDS_REVIEW",
            "geometry_source": "DXF_VECTOR",
            "drawing_identity": group["drawing_identity"],
            "structural_occurrence_id": group[
                "structural_occurrence_id"],
            "start": [round(value, 6) for value in proposed_line["p1"]],
            "end": [round(value, 6) for value in proposed_line["p2"]],
            "length_m": round(proposed_line["length"], 6),
            "thickness_mm": round(
                wall_record["thickness_m"] * 1000.0, 3),
            "source_review_unit_ids": source_review_unit_ids,
            "source_segment_ids": sorted(wall_face_source_ids),
            "material_source_segment_refs": material_refs,
            "missing_material_source_segment_ids": (
                missing_material_source_ids),
            "source_entity_handles": sorted({
                str(source.get("entity_handle"))
                for sources in face_sources for source in sources
                if source.get("entity_handle")
            } | {
                str(units_by_id[unit_id].get("entity_handle"))
                for unit_id in source_review_unit_ids
                if unit_id in units_by_id and
                units_by_id[unit_id].get("entity_handle")
            }),
            "raw_pair_ids": [],
            "endpoint_support": endpoint_support,
            "blockers": blockers,
            "replacement_evidence": {
                "existing_wall_id": wall_record["id"],
                "existing_wall_paired": False,
                "existing_wall_source_segment_ids": wall_record[
                    "source_segment_ids"],
                "opposing_source_segment_id": opposing_source.get(
                    "source_segment_id"),
                "bridge_source_segment_ids": sorted({
                    str(source.get("source_segment_id"))
                    for source in face_sources[0][1:] + face_sources[1][1:]
                }),
                "core_overlap_m": round(pair["overlap"], 6),
                "old_wall_baseline_error_m": round(
                    old_wall_baseline_error, 6),
                "existing_wall_geometry": {
                    "start": copy.deepcopy(wall.get("start")),
                    "end": copy.deepcopy(wall.get("end")),
                    "thickness_mm": wall.get("thickness"),
                    "paired": wall.get("paired"),
                },
                "expected_existing_wall_fingerprint": (
                    wall_precondition_sha256(wall)),
                "required_action": "ATOMIC_REPLACE_SINGLE_WALL",
            },
            "model_geometry_created": False,
            "auto_action": "NONE",
        }
        proposal["integrity_sha256"] = (
            proposal_integrity_sha256(proposal))
        proposals.append(proposal)
        single_wall_upgrade_count += 1
        for unit_id in source_review_unit_ids:
            unit = units_by_id.get(unit_id)
            if unit is not None:
                unit.setdefault("short_wall_proposal_ids", []).append(
                    proposal_id)

    for unit in review_units:
        if unit.get("short_wall_proposal_ids"):
            unit["short_wall_proposal_ids"] = sorted(set(
                unit["short_wall_proposal_ids"]))
    proposals.sort(key=lambda item: item["proposal_id"])
    insufficient.sort(key=lambda item: item["proposal_id"])
    return {
        "schema_version": "buildmate.short-wall-proposal-audit/1.0",
        "status": "REVIEW" if proposals or insufficient else "PASS",
        "classification": "REVIEW_ONLY",
        "component_count": len(proposals) + len(insufficient),
        "proposed_short_wall_count": len(proposals),
        "strict_approval_eligible_count": sum(
            item["approval_status"] == "ELIGIBLE_FOR_STRICT_APPROVAL"
            for item in proposals),
        "single_wall_upgrade_proposal_count": (
            single_wall_upgrade_count),
        "replacement_ambiguity_count": replacement_ambiguity_count,
        "insufficient_support_count": len(insufficient),
        "proposals": proposals,
        "insufficient_support_candidates": insufficient,
        "rule": {
            "endpoint_tolerance_m": _SHORT_WALL_SUPPORT_TOLERANCE_M,
            "junction_gap_must_equal_wall_thickness": True,
            "junction_gap_requires_exactly_one_continuous_face": True,
            "junction_gap_requires_unique_paired_wall": True,
            "strict_caps_are_support_only": True,
            "model_geometry_created": False,
            "auto_action": "NONE",
        },
    }


def recover_short_wall_group_spans(
        source_coverage: dict, *, max_gap_m: float = 4.0,
        min_span_m: float = 0.5, existing_walls: list[dict] | None = None,
        door_vectors: list[dict] | None = None
        ) -> dict:
    """Recover a gross wall from fragmented, paired face runs.

    ODA often exposes a wall as several short LINE fragments around a door
    opening.  The ordinary extractor deliberately ignores those fragments as
    standalone walls.  This recovery pass joins only adjacent runs that have
    a measured double-face pair and an unambiguous perpendicular end-cap at
    both sides of the gap.  It therefore ignores unsupported single faces and
    does not join unrelated runs that merely share a long baseline.
    """
    coverage = source_coverage if isinstance(source_coverage, dict) else {}
    pair_audit = coverage.get("short_wall_pair_audit") or {}
    groups = pair_audit.get("groups") or []
    review_units = coverage.get("review_units") or []
    door_vectors = list(door_vectors or [])
    units_by_id = {
        str(unit.get("review_unit_id")): unit for unit in review_units
        if unit.get("review_unit_id")
    }
    existing_walls = list(existing_walls or [])

    def finite_float(value, default=0.0):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return result if math.isfinite(result) else default

    def unit_line(unit):
        start, end = unit.get("start"), unit.get("end")
        if (not isinstance(start, (list, tuple)) or len(start) < 2 or
                not isinstance(end, (list, tuple)) or len(end) < 2):
            return None
        return _line_record(tuple(start[:2]), tuple(end[:2]))

    def leaf_layer(layer):
        return str(layer or "").upper().strip().rsplit("$0$", 1)[-1]

    def projected(line, origin, ux, uy):
        return [
            (point[0] - origin[0]) * ux +
            (point[1] - origin[1]) * uy
            for point in (line["p1"], line["p2"])
        ]

    def baseline_error(line, origin, nx, ny):
        return max(abs(
            (point[0] - origin[0]) * nx +
            (point[1] - origin[1]) * ny)
            for point in (line["p1"], line["p2"]))

    def cap_candidates(group, boundary, axis_line, thickness_m,
                       excluded_ids):
        candidates = []
        group_identity = group.get("drawing_identity")
        group_occurrence = group.get("structural_occurrence_id")
        layers = {
            str(value).upper() for value in group.get("leaf_layers") or []
            if value
        }
        tolerance = max(0.03, min(0.08, thickness_m * 0.30))
        length_tolerance = max(0.05, thickness_m * 0.35)
        for unit in review_units:
            source_id = str(unit.get("source_segment_id") or "")
            if not source_id or source_id in excluded_ids:
                continue
            if (unit.get("drawing_identity") != group_identity or
                    unit.get("structural_occurrence_id") != group_occurrence or
                    unit.get("identity_is_complete") is not True or
                    unit.get("identity_limitations")):
                continue
            if (layers and leaf_layer(unit.get("layer")) not in layers or
                    wall_geometry_group(unit.get("layer")) !=
                    group.get("wall_group")):
                continue
            line = unit_line(unit)
            if line is None:
                continue
            if abs(90.0 - _angle_delta(line["angle"],
                                       axis_line["angle"])) > 2.0:
                continue
            if abs(line["length"] - thickness_m) > length_tolerance:
                continue
            values = projected(line, axis_line["p1"],
                               axis_line["u"][0], axis_line["u"][1])
            if max(abs(value - boundary) for value in values) > tolerance:
                continue
            if baseline_error(line, axis_line["p1"],
                              axis_line["n"][0], axis_line["n"][1]) > (
                    thickness_m + tolerance):
                continue
            candidates.append(unit)
        # Multiple source entities at the same boundary are ambiguous.  Keep
        # one entity only when the evidence is genuinely unique.
        unique = {}
        for unit in candidates:
            key = (str(unit.get("entity_handle") or ""),
                   str(unit.get("source_segment_id") or ""))
            unique[key] = unit
        return list(unique.values())

    def door_support(group, gap_start, gap_end, axis_line, thickness_m):
        """Return explicit door vectors crossing the unresolved gap."""
        low, high = sorted((gap_start, gap_end))
        tolerance = max(0.10, thickness_m + 0.05)
        for vector in door_vectors:
            if (vector.get("drawing_identity") !=
                    group.get("drawing_identity") or
                    vector.get("structural_occurrence_id") !=
                    group.get("structural_occurrence_id")):
                continue
            line = unit_line(vector)
            if line is None:
                continue
            values = projected(line, axis_line["p1"],
                               axis_line["u"][0], axis_line["u"][1])
            vector_low, vector_high = min(values), max(values)
            if vector_high < low - tolerance or vector_low > high + tolerance:
                continue
            if baseline_error(line, axis_line["p1"], axis_line["n"][0],
                              axis_line["n"][1]) > max(0.75,
                                                        thickness_m + 0.50):
                continue
            return True
        return False

    def refs_for_ids(source_ids):
        references = []
        seen = set()
        for source_id in sorted(source_ids):
            unit_matches = [
                unit for unit in review_units
                if str(unit.get("source_segment_id") or "") == source_id
            ]
            for unit in unit_matches:
                reference = source_reference_from_review_unit(unit)
                if reference is None:
                    continue
                key = _source_ref_key(reference)
                if key not in seen:
                    seen.add(key)
                    references.append(reference)
        return references

    def overlaps_existing(candidate):
        candidate_line = _line_record(
            tuple(candidate["start"]), tuple(candidate["end"]))
        if candidate_line is None:
            return True
        thickness_m = finite_float(candidate.get("thickness_mm")) / 1000.0
        for existing in existing_walls:
            existing_line = _line_record(
                tuple(existing.get("start", [])),
                tuple(existing.get("end", [])))
            if existing_line is None:
                continue
            if _angle_delta(candidate_line["angle"],
                            existing_line["angle"]) > 2.0:
                continue
            if baseline_error(existing_line, candidate_line["p1"],
                              candidate_line["n"][0],
                              candidate_line["n"][1]) > 0.08:
                continue
            values = projected(existing_line, candidate_line["p1"],
                               candidate_line["u"][0],
                               candidate_line["u"][1])
            overlap = max(0.0, min(candidate_line["length"], max(values)) -
                          max(0.0, min(values)))
            if overlap / max(candidate_line["length"], 1e-9) >= 0.30:
                # A candidate which substantially duplicates an existing wall
                # is not added; its fragments are already represented.
                if abs(finite_float(existing.get("thickness")) / 1000.0 -
                       thickness_m) <= 0.08:
                    return True
        return False

    candidates = []
    evaluated_gap_count = 0
    cap_supported_gap_count = 0
    door_supported_gap_count = 0
    for group in groups:
        if (group.get("pairing_ambiguous") or
                group.get("semantic_layer_mismatch") or
                not group.get("drawing_identity") or
                not group.get("structural_occurrence_id")):
            continue
        runs = list(group.get("overlap_runs") or [])
        if len(runs) < 2:
            continue
        try:
            thickness_m = finite_float(group.get("thickness_mm")) / 1000.0
        except (TypeError, ValueError, OverflowError):
            continue
        if thickness_m <= 0.0:
            continue
        axis_line = _line_record(tuple(runs[0].get("start", [])),
                                 tuple(runs[0].get("end", [])))
        if axis_line is None:
            continue
        origin = axis_line["p1"]
        ux, uy = axis_line["u"]
        intervals = []
        for run in runs:
            line = _line_record(tuple(run.get("start", [])),
                                tuple(run.get("end", [])))
            if line is None or _angle_delta(line["angle"],
                                            axis_line["angle"]) > 2.0:
                intervals.append(None)
                continue
            values = projected(line, origin, ux, uy)
            intervals.append((min(values), max(values), line))
        if any(item is None for item in intervals):
            continue
        gaps_by_index = {
            int(gap.get("after_run_index")): gap
            for gap in group.get("inter_run_gaps") or []
            if gap.get("after_run_index") is not None
        }
        sequence = [0]
        sequence_gaps = []

        def emit_sequence(run_indexes, gap_indexes):
            if len(run_indexes) < 2:
                return
            low = min(intervals[index][0] for index in run_indexes)
            high = max(intervals[index][1] for index in run_indexes)
            if high - low < max(min_span_m, 0.5):
                return
            start = [origin[0] + low * ux, origin[1] + low * uy]
            end = [origin[0] + high * ux, origin[1] + high * uy]
            source_ids = set()
            pair_ids = set()
            raw_pairs = {
                item.get("raw_pair_id"): item
                for item in group.get("raw_pair_evidence") or []
                if item.get("raw_pair_id")
            }
            for run_index in run_indexes:
                pair_ids.update(runs[run_index].get("raw_pair_ids") or [])
            for pair_id in pair_ids:
                pair = raw_pairs.get(pair_id) or {}
                for field in ("face_a_source_segment_id",
                              "face_b_source_segment_id"):
                    value = pair.get(field)
                    if value:
                        source_ids.add(str(value))
            refs = refs_for_ids(source_ids)
            if not refs or len({str(item.get("source_segment_id"))
                               for item in refs}) != len(source_ids):
                return
            candidate = {
                "start": [round(value, 3) for value in start],
                "end": [round(value, 3) for value in end],
                "thickness": round(thickness_m * 1000.0, 1),
                "thickness_mm": round(thickness_m * 1000.0, 1),
                "paired": True,
                "wall_group": group.get("wall_group", "A"),
                "source_layers": sorted(group.get("leaf_layers") or []),
                "geometry_source": "DXF_VECTOR_FRAGMENTED_FACE_HULL",
                "drawing_identity": group.get("drawing_identity"),
                "structural_occurrence_id": group.get(
                    "structural_occurrence_id"),
                "source_segment_ids": sorted(source_ids),
                "source_segment_refs": refs,
                "short_wall_recovery": {
                    "pair_group_id": group.get("pair_group_id"),
                    "run_indexes": list(run_indexes),
                    "bridged_gap_indexes": list(gap_indexes),
                    "max_gap_m": round(max(
                        finite_float((gaps_by_index[index] or {}).get(
                            "length_m")) for index in gap_indexes), 6),
                },
            }
            if not overlaps_existing(candidate):
                signature = json.dumps([
                    candidate["source_segment_ids"], candidate["start"],
                    candidate["end"], candidate["thickness"],
                ], separators=(",", ":"))
                candidate["fragmented_face_recovery_id"] = (
                    "fragmented_face_hull_" + hashlib.sha256(
                        signature.encode("utf-8")).hexdigest()[:16])
                candidates.append(candidate)

        raw_pairs = {
            item.get("raw_pair_id"): item
            for item in group.get("raw_pair_evidence") or []
            if item.get("raw_pair_id")
        }

        def run_source_ids(run_indexes):
            source_ids = set()
            for run_index in run_indexes:
                for pair_id in runs[run_index].get("raw_pair_ids") or []:
                    pair = raw_pairs.get(pair_id) or {}
                    for field in ("face_a_source_segment_id",
                                  "face_b_source_segment_id"):
                        value = pair.get(field)
                        if value:
                            source_ids.add(str(value))
            return source_ids

        for index in range(len(runs) - 1):
            gap = gaps_by_index.get(index)
            if gap is None:
                emit_sequence(sequence, sequence_gaps)
                sequence = [index + 1]
                sequence_gaps = []
                continue
            evaluated_gap_count += 1
            gap_length = finite_float(gap.get("length_m"), math.inf)
            if gap_length <= 0.0 or gap_length > max_gap_m:
                emit_sequence(sequence, sequence_gaps)
                sequence = [index + 1]
                sequence_gaps = []
                continue
            # Project the recorded endpoints rather than assuming a fixed
            # direction; this also handles reversed source LINE entities.
            gap_start = gap.get("start")
            gap_end = gap.get("end")
            if (not isinstance(gap_start, (list, tuple)) or
                    not isinstance(gap_end, (list, tuple)) or
                    len(gap_start) < 2 or len(gap_end) < 2):
                emit_sequence(sequence, sequence_gaps)
                sequence = [index + 1]
                sequence_gaps = []
                continue
            gap_start_value = (gap_start[0] - origin[0]) * ux + (
                gap_start[1] - origin[1]) * uy
            gap_end_value = (gap_end[0] - origin[0]) * ux + (
                gap_end[1] - origin[1]) * uy
            cap_start = cap_candidates(
                group, gap_start_value, axis_line, thickness_m,
                run_source_ids(sequence))
            cap_end = cap_candidates(
                group, gap_end_value, axis_line, thickness_m,
                run_source_ids([index + 1]))
            caps_supported = len(cap_start) == 1 and len(cap_end) == 1
            door_supported = door_support(
                group, gap_start_value, gap_end_value, axis_line,
                thickness_m)
            if not caps_supported and not door_supported:
                emit_sequence(sequence, sequence_gaps)
                sequence = [index + 1]
                sequence_gaps = []
                continue
            if caps_supported:
                cap_supported_gap_count += 1
            else:
                door_supported_gap_count += 1
            sequence.append(index + 1)
            sequence_gaps.append(index)
        emit_sequence(sequence, sequence_gaps)

    unique = {}
    for candidate in candidates:
        key = (tuple(candidate["start"]), tuple(candidate["end"]),
               candidate["thickness"])
        unique[key] = candidate
    candidates = [unique[key] for key in sorted(unique)]
    return {
        "schema_version": "buildmate.fragmented-wall-recovery/1.0",
        "status": "PASS" if not candidates else "REVIEW",
        "candidate_count": len(candidates),
        "evaluated_gap_count": evaluated_gap_count,
        "cap_supported_gap_count": cap_supported_gap_count,
        "door_supported_gap_count": door_supported_gap_count,
        "max_gap_m": round(max_gap_m, 3),
        "min_span_m": round(max(min_span_m, 0.5), 3),
        "rule": {
            "requires_double_face_pair": True,
            "requires_unique_perpendicular_cap_each_side": True,
            "explicit_door_vector_can_authorize_gap": True,
            "ambiguous_baseline_groups_skipped": True,
            "auto_action": "ADD_GROSS_WALL",
        },
        "candidates": candidates,
    }


def wall_source_coverage(records, walls: list[dict], ox: float, oy: float,
                         rot_deg: float, gcx: float, gcy: float,
                         min_length_m: float = 0.01,
                         opening_bridges: list[dict] | None = None,
                         closed_profile_audit: dict | None = None) -> dict:
    """Report how much semantic source-face length became wall centerlines.

    The audit threshold is intentionally below the 0.5m modeling threshold.
    Short semantic faces must remain traceable evidence even when they are not
    yet safe to promote into Revit walls. Inferred opening bridges are room-
    topology evidence only; they never suppress source-face review or pairing.
    """
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def transform(point):
        x, y = float(point[0]) - gcx, float(point[1]) - gcy
        rx, ry = x * c - y * s + gcx, x * s + y * c + gcy
        return ((rx - ox) / 1000.0, (ry - oy) / 1000.0)

    source_records = []
    source_length = 0.0
    raw_source_segment_count = 0
    ignored_degenerate_source_segment_count = 0
    short_source_segment_count = 0
    for source_record_index, record in enumerate(records):
        entity, layer, provenance = _record_parts(record)
        segments = []
        if entity.dxftype() == "LINE":
            segments.append((entity.dxf.start, entity.dxf.end))
        elif entity.dxftype() == "LWPOLYLINE":
            points = list(entity.get_points("xy"))
            segments.extend(zip(points, points[1:]))
            if entity.closed and len(points) > 2:
                segments.append((points[-1], points[0]))
        for segment_index, (first, second) in enumerate(segments):
            raw_source_segment_count += 1
            first_point, second_point = transform(first), transform(second)
            source_line = _line_record(first_point, second_point)
            length = source_line["length"] if source_line else 0.0
            if length < min_length_m:
                ignored_degenerate_source_segment_count += 1
                continue
            if length >= min_length_m:
                if length < 0.5:
                    short_source_segment_count += 1
                source_length += length
                origin = getattr(entity, "origin_of_copy", None) or entity
                source_handle = (
                    (provenance or {}).get("source_entity_handle") or
                    getattr(getattr(origin, "dxf", None), "handle", None)
                )
                source_segment_id = _provenance_segment_id(
                    provenance, segment_index)
                identity_limitations = _provenance_identity_limitations(
                    provenance, source_handle, segment_index=segment_index)
                source_records.append({
                    "line": source_line,
                    "layer": layer,
                    "wall_group": wall_geometry_group(layer),
                    "geometry_source": "DXF_VECTOR",
                    "entity_handle": source_handle,
                    "drawing_identity": (provenance or {}).get(
                        "drawing_identity"),
                    "source_occurrence_id": (provenance or {}).get(
                        "source_occurrence_id"),
                    "structural_occurrence_id": (provenance or {}).get(
                        "structural_occurrence_id"),
                    "placed_entity_id": (provenance or {}).get(
                        "placed_entity_id"),
                    "source_segment_id": source_segment_id,
                    "placement_path": (provenance or {}).get(
                        "placement_path") or [],
                    "equivalent_placements": (provenance or {}).get(
                        "equivalent_placements") or [],
                    "identity_is_complete": not identity_limitations,
                    "identity_limitations": identity_limitations,
                    "entity_type": entity.dxftype(),
                    "entity_segment_count": len(segments),
                    "entity_closed": bool(getattr(entity, "closed", False)),
                    "source_record_index": source_record_index,
                    "segment_index": segment_index,
                    "start": [round(value, 4) for value in first_point],
                    "end": [round(value, 4) for value in second_point],
                })

    closed_profile_association = None
    profile_by_id: dict[str, dict] = {}
    if closed_profile_audit is not None:
        invalid_audit_input = not isinstance(closed_profile_audit, dict)
        audit_candidates = (
            closed_profile_audit.get("candidates", [])
            if isinstance(closed_profile_audit, dict) else [])
        invalid_candidate_list = not isinstance(audit_candidates, list)
        audit_candidates = audit_candidates if isinstance(
            audit_candidates, list) else []
        declared_candidate_count = (
            closed_profile_audit.get("candidate_count")
            if isinstance(closed_profile_audit, dict) else None)
        candidate_count_mismatch = (
            declared_candidate_count is not None and
            declared_candidate_count != len(audit_candidates))
        source_by_key = {
            (item["source_record_index"], item["segment_index"]): item
            for item in source_records
        }
        child_by_key: dict[tuple[int, int], list[dict]] = {}
        missing = []
        duplicate_profile_ids = []
        child_evidence_count = 0
        malformed_profile_count = 0
        for profile in audit_candidates:
            if not isinstance(profile, dict):
                malformed_profile_count += 1
                continue
            profile_id = profile.get("candidate_id")
            if not profile_id:
                malformed_profile_count += 1
                continue
            if profile_id in profile_by_id:
                duplicate_profile_ids.append(profile_id)
            else:
                profile_by_id[profile_id] = profile
            children = profile.get("child_evidence", [])
            children = children if isinstance(children, list) else []
            expected_value = profile.get("reserved_edge_count", 0)
            if not isinstance(expected_value, int) or expected_value < 0:
                malformed_profile_count += 1
                expected_children = len(children)
            else:
                expected_children = expected_value
            if expected_children > len(children):
                for _index in range(expected_children - len(children)):
                    missing.append({
                        "parent_profile_id": profile_id,
                        "reason": "child_evidence_missing_from_audit",
                    })
            for child in children:
                child_evidence_count += 1
                if not isinstance(child, dict):
                    missing.append({
                        "parent_profile_id": profile_id,
                        "reason": "invalid_child_evidence",
                    })
                    continue
                record_index = child.get("source_record_index")
                segment_index = child.get("segment_index")
                edge_role = child.get("edge_role")
                if (not isinstance(record_index, int) or
                        not isinstance(segment_index, int) or
                        edge_role not in {"wall_face", "end_cap"}):
                    missing.append({
                        "parent_profile_id": profile_id,
                        "source_record_index": record_index,
                        "segment_index": segment_index,
                        "edge_role": edge_role,
                        "reason": "exact_source_key_missing",
                    })
                    continue
                association = {
                    "parent_profile_id": profile_id,
                    "parent_profile_status": profile.get("status"),
                    "source_record_index": record_index,
                    "segment_index": segment_index,
                    "edge_role": edge_role,
                }
                key = (record_index, segment_index)
                child_by_key.setdefault(key, []).append(association)
                if key not in source_by_key:
                    missing.append({
                        **association,
                        "reason": "source_segment_not_found",
                    })

        duplicate = []
        associated_source_segment_count = 0
        for key, associations in child_by_key.items():
            source = source_by_key.get(key)
            if source is None:
                continue
            if len(associations) == 1:
                association = associations[0]
                source.update({
                    "parent_profile_id": association["parent_profile_id"],
                    "parent_profile_status": association[
                        "parent_profile_status"],
                    "edge_role": association["edge_role"],
                    "profile_association_status": "EXACT",
                })
                associated_source_segment_count += 1
                continue
            parent_ids = sorted({
                str(item["parent_profile_id"]) for item in associations})
            edge_roles = sorted({
                str(item["edge_role"]) for item in associations})
            duplicate.append({
                "source_record_index": key[0],
                "segment_index": key[1],
                "association_count": len(associations),
                "parent_profile_ids": parent_ids,
                "edge_roles": edge_roles,
            })
            # Expose unanimous evidence for review, but do not use an
            # ambiguous association to alter Gate coverage or roll up units.
            if len(parent_ids) == 1 and len(edge_roles) == 1:
                source.update({
                    "parent_profile_id": parent_ids[0],
                    "parent_profile_status": associations[0][
                        "parent_profile_status"],
                    "edge_role": edge_roles[0],
                })
            source["profile_association_status"] = "DUPLICATE"

        approved_profiles = {
            profile_id: profile for profile_id, profile in profile_by_id.items()
            if profile.get("status") == "APPROVED_BY_RULE"
        }
        profile_wall_counts = Counter(
            wall.get("source_candidate_id") for wall in walls
            if wall.get("source_candidate_id") in approved_profiles)
        approved_wall_missing = sorted(
            profile_id for profile_id in approved_profiles
            if profile_wall_counts[profile_id] == 0)
        approved_wall_duplicate = [
            {"parent_profile_id": profile_id,
             "model_wall_count": profile_wall_counts[profile_id]}
            for profile_id in sorted(approved_profiles)
            if profile_wall_counts[profile_id] > 1
        ]
        approved_wall_one_to_one_count = sum(
            profile_wall_counts[profile_id] == 1
            for profile_id in approved_profiles)
        known_profile_ids = set(profile_by_id)
        orphan_model_walls = [
            {"wall_index": index,
             "source_candidate_id": wall.get("source_candidate_id")}
            for index, wall in enumerate(walls)
            if (wall.get("source_candidate_id") and
                wall.get("source_candidate_id") not in known_profile_ids)
        ]
        review_profile_model_walls = [
            {"wall_index": index,
             "source_candidate_id": wall.get("source_candidate_id")}
            for index, wall in enumerate(walls)
            if (wall.get("source_candidate_id") in known_profile_ids and
                wall.get("source_candidate_id") not in approved_profiles)
        ]
        missing_count = len(missing) + len(approved_wall_missing)
        duplicate_count = (len(duplicate) + len(duplicate_profile_ids) +
                           len(approved_wall_duplicate))
        has_error = bool(
            missing_count or duplicate_count or malformed_profile_count or
            invalid_audit_input or invalid_candidate_list or
            candidate_count_mismatch or orphan_model_walls or
            review_profile_model_walls)
        closed_profile_association = {
            "status": "ERROR" if has_error else "COMPLETE",
            "profile_count": len(audit_candidates),
            "child_evidence_count": child_evidence_count,
            "associated_source_segment_count": (
                associated_source_segment_count),
            "missing_count": missing_count,
            "duplicate_count": duplicate_count,
            "missing": missing,
            "duplicate": duplicate,
            "duplicate_profile_id_count": len(duplicate_profile_ids),
            "duplicate_profile_ids": sorted(set(duplicate_profile_ids)),
            "malformed_profile_count": malformed_profile_count,
            "invalid_audit_input": invalid_audit_input,
            "invalid_candidate_list": invalid_candidate_list,
            "candidate_count_mismatch": candidate_count_mismatch,
            "approved_profile_count": len(approved_profiles),
            "approved_model_wall_count": sum(profile_wall_counts.values()),
            "approved_wall_one_to_one_count": (
                approved_wall_one_to_one_count),
            "approved_wall_missing_count": len(approved_wall_missing),
            "approved_wall_duplicate_count": len(approved_wall_duplicate),
            "approved_wall_missing": approved_wall_missing,
            "approved_wall_duplicate": approved_wall_duplicate,
            "orphan_model_wall_count": len(orphan_model_walls),
            "orphan_model_walls": orphan_model_walls,
            "review_profile_model_wall_count": len(
                review_profile_model_walls),
            "review_profile_model_walls": review_profile_model_walls,
        }

    wall_lines = []
    for wall in walls:
        start, end = wall.get("start"), wall.get("end")
        if (not isinstance(start, (list, tuple)) or len(start) < 2 or
                not isinstance(end, (list, tuple)) or len(end) < 2):
            continue
        line = _line_record(tuple(start[:2]), tuple(end[:2]))
        if line is not None:
            wall_lines.append((wall, line))
    valid_walls = [wall for wall, _line in wall_lines]

    opening_bridges = opening_bridges or []

    def mapped_detail(source: dict) -> tuple[float, list[list[float]]]:
        line = source["line"]
        ux, uy = line["u"]
        nx, ny = line["n"]
        origin = line["p1"]
        intervals = []
        for wall, wall_line in wall_lines:
            if _angle_delta(line["angle"], wall_line["angle"]) > 2.5:
                continue
            midpoint = ((wall_line["p1"][0] + wall_line["p2"][0]) / 2.0,
                        (wall_line["p1"][1] + wall_line["p2"][1]) / 2.0)
            relative = (midpoint[0] - origin[0], midpoint[1] - origin[1])
            perpendicular = abs(relative[0] * nx + relative[1] * ny)
            allowed_offset = max(
                0.08, float(wall.get("thickness", 200.0)) / 2000.0 + 0.08)
            if perpendicular > allowed_offset:
                continue
            projections = [
                (point[0] - origin[0]) * ux +
                (point[1] - origin[1]) * uy
                for point in (wall_line["p1"], wall_line["p2"])
            ]
            start = max(0.0, min(projections))
            end = min(line["length"], max(projections))
            if end > start:
                intervals.append((start, end))
        intervals.sort()
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        covered = sum(end - start for start, end in merged)
        unmapped = []
        cursor = 0.0
        for start, end in merged:
            if start > cursor + 1e-6:
                unmapped.append([cursor, start])
            cursor = max(cursor, end)
        if cursor < line["length"] - 1e-6:
            unmapped.append([cursor, line["length"]])
        return min(1.0, covered / line["length"]), unmapped

    exact_covered_length = 0.0
    gate_source_length = 0.0
    gate_exact_covered_length = 0.0
    uncovered, partial = [], []
    for source in source_records:
        ratio, unmapped_intervals = mapped_detail(source)
        mapped_length = source["line"]["length"] * ratio
        exact_covered_length += mapped_length
        gate_included = not (
            source.get("profile_association_status") == "EXACT" and
            (source.get("edge_role") == "end_cap" or
             source.get("parent_profile_status") == "REJECTED_BY_RULE"))
        if gate_included:
            gate_source_length += source["line"]["length"]
            gate_exact_covered_length += mapped_length
        item = {key: value for key, value in source.items() if key != "line"}
        item["length_m"] = round(source["line"]["length"], 4)
        item["mapped_ratio"] = round(ratio, 4)
        signature = "|".join((
            str(item.get("drawing_identity") or "NO_DRAWING_ID"),
            str(item.get("placed_entity_id") or "NO_PLACED_ENTITY"),
            str(item.get("source_segment_id") or "NO_SEGMENT_ID"),
            str(item.get("entity_handle") or "NO_HANDLE"),
            str(item.get("layer") or ""),
            repr(item["start"]), repr(item["end"]),
        ))
        item["candidate_id"] = (
            "semantic_face_" + hashlib.sha256(
                signature.encode("utf-8")).hexdigest()[:16]
        )
        item["geometry_source"] = "DXF_VECTOR"
        item["status"] = "NEEDS_REVIEW"
        line = source["line"]
        item["source_axis_start"] = [
            round(value, 6) for value in line["p1"]]
        item["source_axis_end"] = [
            round(value, 6) for value in line["p2"]]
        item["unmapped_ranges"] = []
        for start_distance, end_distance in unmapped_intervals:
            start_point = [
                line["p1"][0] + start_distance * line["u"][0],
                line["p1"][1] + start_distance * line["u"][1],
            ]
            end_point = [
                line["p1"][0] + end_distance * line["u"][0],
                line["p1"][1] + end_distance * line["u"][1],
            ]
            midpoint = [(start_point[0] + end_point[0]) / 2.0,
                        (start_point[1] + end_point[1]) / 2.0]
            opening_supported = any(
                _point_segment_distance(
                    midpoint, bridge["start"], bridge["end"]) <= 0.35
                for bridge in opening_bridges
            )
            item["unmapped_ranges"].append({
                "start": [round(value, 4) for value in start_point],
                "end": [round(value, 4) for value in end_point],
                "length_m": round(end_distance - start_distance, 4),
                "source_interval_m": [round(start_distance, 6),
                                      round(end_distance, 6)],
                "opening_bridge_supported": opening_supported,
            })
        item["opening_bridge_supported"] = bool(
            # Sub-centimetre tails are projection/rounding residue, not a
            # meaningful reason to overrule otherwise useful topology evidence.
            (material_ranges := [
                part for part in item["unmapped_ranges"]
                if part["length_m"] >= 0.01
            ]) and
            all(part["opening_bridge_supported"]
                for part in material_ranges)
        )
        # A bridge inferred from modeled wall endpoints is useful for room
        # enclosure, but it is not door semantics.  Keep it as diagnostics
        # without allowing it to hide missing source-face geometry.
        item["opening_bridge_semantic_authority"] = False
        if ratio < 0.50:
            item["decision_reason"] = "semantic_wall_face_unmapped"
            uncovered.append(item)
        elif ratio < 0.90:
            item["decision_reason"] = (
                "semantic_wall_face_partially_mapped")
            partial.append(item)
    raw_review_items = uncovered + partial
    if closed_profile_audit is not None:
        for item in raw_review_items:
            item.setdefault("parent_profile_id", None)
            item.setdefault("edge_role", None)
            item["rollup_counted"] = (
                item.get("profile_association_status") != "EXACT")
            parent = profile_by_id.get(item.get("parent_profile_id"))
            if (item.get("profile_association_status") == "EXACT" and
                    isinstance(parent, dict) and
                    parent.get("status") == "REJECTED_BY_RULE"):
                item.update({
                    "status": "REJECTED_BY_RULE",
                    "decision_reason": parent.get("decision_reason") or
                    "closed_profile_rejected_by_rule",
                    "review_bucket": parent.get("review_bucket") or
                    "KNOWN_NON_WALL",
                    "auto_action": "NONE",
                    "rollup_counted": False,
                })
                if parent.get("door_leaf_swing_evidence"):
                    item["door_leaf_swing_evidence"] = parent[
                        "door_leaf_swing_evidence"]
            elif (item.get("profile_association_status") == "EXACT" and
                  isinstance(parent, dict) and
                  parent.get("status") == "APPROVED_BY_RULE" and
                  item.get("edge_role") == "end_cap"):
                item.update({
                    "status": "RESOLVED_BY_PROFILE",
                    "decision_reason": "approved_closed_wall_strip_end_cap",
                    "review_bucket": "RESOLVED_PROFILE_EDGE",
                    "auto_action": "NONE",
                    "rollup_counted": False,
                })

        exact_profile_review_items: dict[str, list[dict]] = {}
        for item in raw_review_items:
            if item.get("profile_association_status") == "EXACT":
                exact_profile_review_items.setdefault(
                    item["parent_profile_id"], []).append(item)

        logical_profile_ids = set()
        for profile_id, profile in profile_by_id.items():
            items = exact_profile_review_items.get(profile_id, [])
            wall_face_items = [item for item in items
                               if item.get("edge_role") == "wall_face"]
            if (profile.get("status") != "REJECTED_BY_RULE" and
                    (profile.get("status") == "NEEDS_REVIEW" or
                     wall_face_items)):
                logical_profile_ids.add(profile_id)
                choices = wall_face_items or items
                if choices:
                    min(choices, key=lambda item: (
                        item["source_record_index"], item["segment_index"]
                    ))["rollup_counted"] = True
        strict_topology_end_cap_count = _classify_strict_topology_end_caps(
            source_records, raw_review_items)
        strict_end_cap_source_keys = {
            (item.get("source_record_index"), item.get("segment_index"))
            for item in raw_review_items
            if item.get("decision_reason") == "strict_topology_end_cap"
        }
        strict_end_cap_face_length = 0.0
        for source in source_records:
            source_key = (source.get("source_record_index"),
                          source.get("segment_index"))
            if source_key not in strict_end_cap_source_keys:
                continue
            mapped_ratio, _unmapped = mapped_detail(source)
            source_line_length = source["line"]["length"]
            strict_end_cap_face_length += source_line_length
            gate_source_length -= source_line_length
            gate_exact_covered_length -= source_line_length * mapped_ratio
    else:
        exact_profile_review_items = {}
        logical_profile_ids = set()
        # Without the profile audit there is no exact proof that a 75-125 mm
        # segment is not a child edge of a closed entity, so fail closed.
        strict_topology_end_cap_count = 0
        strict_end_cap_face_length = 0.0

    review_units = []
    for profile_id in sorted(logical_profile_ids):
        profile = profile_by_id.get(profile_id) or {}
        profile_items = exact_profile_review_items.get(profile_id, [])
        wall_face_items = [
            item for item in profile_items
            if item.get("edge_role") == "wall_face"
        ]
        choices = wall_face_items or profile_items
        representative = (min(
            choices, key=lambda item: (
                item.get("source_record_index", math.inf),
                item.get("segment_index", math.inf),
                str(item.get("candidate_id") or ""),
            )) if choices else None)
        signature = "|".join((
            "CLOSED_WALL_PROFILE",
            str(profile_id),
            str(profile.get("drawing_identity") or "NO_DRAWING_ID"),
            str(profile.get("placed_entity_id") or "NO_PLACED_ENTITY"),
        ))
        review_units.append({
            "review_unit_id": "wall_source_review_" + hashlib.sha256(
                signature.encode("utf-8")).hexdigest()[:16],
            "unit_type": "CLOSED_WALL_PROFILE",
            "status": "NEEDS_REVIEW",
            "candidate_id": profile_id,
            "source_candidate_ids": sorted(
                str(item.get("candidate_id")) for item in profile_items
                if item.get("candidate_id")),
            "parent_profile_id": profile_id,
            "profile_status": profile.get("status"),
            "drawing_identity": profile.get("drawing_identity"),
            "source_occurrence_id": profile.get("source_occurrence_id"),
            "placed_entity_id": profile.get("placed_entity_id"),
            "structural_occurrence_id": profile.get(
                "structural_occurrence_id"),
            "identity_is_complete": profile.get(
                "identity_is_complete", False),
            "identity_limitations": copy.deepcopy(
                profile.get("identity_limitations", [])),
            "decision_reason": (
                "closed_wall_profile_wall_face_unmapped"
                if wall_face_items else
                "closed_wall_profile_requires_review"),
            "review_bucket": "CLOSED_WALL_PROFILE",
            "representative_source_candidate_id": (
                representative.get("candidate_id")
                if representative else None),
            "unmapped_ranges": [
                copy.deepcopy(part)
                for item in profile_items
                for part in item.get("unmapped_ranges", [])
                if float(part.get("length_m", 0.0)) >= 0.01
            ],
            "auto_action": "NONE",
        })

    for item in raw_review_items:
        item["rollup_unit_count"] = 0
        item["range_review_units"] = []
        item["mixed_range_semantics"] = False
        if (item.get("status") != "NEEDS_REVIEW" or
                item.get("profile_association_status") == "EXACT"):
            continue
        material_ranges = [
            (index, part)
            for index, part in enumerate(item.get("unmapped_ranges", []))
            if float(part.get("length_m", 0.0)) >= 0.01
        ]
        if not material_ranges:
            material_ranges = [(0, {
                "start": copy.deepcopy(item.get("start")),
                "end": copy.deepcopy(item.get("end")),
                "length_m": float(item.get("length_m", 0.0)),
                "opening_bridge_supported": False,
            })]
        range_reasons = []
        for range_index, part in material_ranges:
            if item.get("decision_reason") == "strict_topology_end_cap":
                decision_reason = "strict_topology_end_cap"
                review_bucket = "STRICT_TOPOLOGY_END_CAP"
                unit_status = "RESOLVED_STRICT_TOPOLOGY_END_CAP"
            else:
                decision_reason = (
                    "semantic_wall_face_unmapped"
                    if float(item.get("mapped_ratio", 0.0)) < 0.50 else
                    "semantic_wall_face_partially_mapped")
                review_bucket = "POSSIBLE_OMISSION"
                unit_status = "NEEDS_REVIEW"
            range_reasons.append(decision_reason)
            signature = json.dumps(
                [item.get("candidate_id"), range_index,
                 part.get("start"), part.get("end")],
                ensure_ascii=False, separators=(",", ":"))
            review_unit_id = "wall_source_review_" + hashlib.sha256(
                signature.encode("utf-8")).hexdigest()[:16]
            unit = {
                "review_unit_id": review_unit_id,
                "unit_type": "UNMAPPED_SOURCE_RANGE",
                "status": unit_status,
                "candidate_id": item.get("candidate_id"),
                "source_candidate_id": item.get("candidate_id"),
                "range_index": range_index,
                "geometry_source": "DXF_VECTOR",
                "layer": item.get("layer"),
                "entity_handle": item.get("entity_handle"),
                "drawing_identity": item.get("drawing_identity"),
                "source_occurrence_id": item.get("source_occurrence_id"),
                "placed_entity_id": item.get("placed_entity_id"),
                "structural_occurrence_id": item.get(
                    "structural_occurrence_id"),
                "source_segment_id": item.get("source_segment_id"),
                "source_record_index": item.get("source_record_index"),
                "segment_index": item.get("segment_index"),
                "entity_type": item.get("entity_type"),
                "entity_segment_count": item.get("entity_segment_count"),
                "entity_closed": item.get("entity_closed"),
                "parent_profile_id": item.get("parent_profile_id"),
                "profile_association_status": item.get(
                    "profile_association_status"),
                "placement_path": copy.deepcopy(
                    item.get("placement_path", [])),
                "identity_is_complete": item.get(
                    "identity_is_complete", False),
                "identity_limitations": copy.deepcopy(
                    item.get("identity_limitations", [])),
                "source_start": copy.deepcopy(item.get("start")),
                "source_end": copy.deepcopy(item.get("end")),
                "source_axis_start": copy.deepcopy(
                    item.get("source_axis_start")),
                "source_axis_end": copy.deepcopy(
                    item.get("source_axis_end")),
                "start": copy.deepcopy(part.get("start")),
                "end": copy.deepcopy(part.get("end")),
                "length_m": round(float(part.get("length_m", 0.0)), 4),
                "source_interval_m": copy.deepcopy(
                    part.get("source_interval_m")),
                "mapped_ratio": item.get("mapped_ratio"),
                "opening_bridge_supported": bool(
                    part.get("opening_bridge_supported")),
                "opening_bridge_semantic_authority": False,
                "decision_reason": decision_reason,
                "review_bucket": review_bucket,
                "parent_candidate_decision_reason": item.get(
                    "decision_reason"),
                "support_evidence": copy.deepcopy(
                    item.get("support_evidence")),
                "strict_topology_evidence": copy.deepcopy(
                    item.get("strict_topology_evidence")),
                "auto_action": "NONE",
            }
            if unit_status == "RESOLVED_STRICT_TOPOLOGY_END_CAP":
                unit.update({
                    "resolution_type": "STRICT_TOPOLOGY_END_CAP",
                    "resolution_evidence": {
                        "classification": "EVIDENCE_RESOLUTION_ONLY",
                        "support_count": len(
                            unit.get("support_evidence") or []),
                        "model_geometry_created": False,
                    },
                    "model_geometry_created": False,
                })
            review_units.append(unit)
            item["range_review_units"].append({
                "review_unit_id": review_unit_id,
                "range_index": range_index,
                "decision_reason": decision_reason,
                "review_bucket": review_bucket,
                "status": unit_status,
            })
        item["rollup_unit_count"] = len(material_ranges)
        item["mixed_range_semantics"] = len(set(range_reasons)) > 1

    resolved_modeled_junction_edge_count = (
        _resolve_modeled_junction_review_units(
            review_units, raw_review_items, source_records, valid_walls))
    resolved_l_junction_closure_edge_count = (
        _resolve_l_junction_closure_review_units(
            review_units, raw_review_items, source_records, valid_walls))
    resolved_endpoint_adjustment_count = (
        _resolve_endpoint_adjustment_review_units(
            review_units, raw_review_items, source_records, valid_walls))
    short_wall_pair_audit = _audit_short_wall_pair_groups(review_units)
    short_wall_proposal_audit = _classify_short_wall_proposals(
        short_wall_pair_audit, review_units, source_records, valid_walls)
    logical_review_unit_count = sum(
        item.get("status") == "NEEDS_REVIEW" for item in review_units)
    review_unit_reason_counts = Counter(
        item["decision_reason"] for item in review_units)
    review_unit_bucket_counts = Counter(
        item["review_bucket"] for item in review_units)
    review_unit_status_counts = Counter(
        item["status"] for item in review_units)
    paired = [wall for wall in valid_walls if wall.get("paired")]
    singles = [wall for wall in valid_walls if not wall.get("paired")]
    represented_length = (
        2.0 * sum(math.dist(wall["start"], wall["end"]) for wall in paired) +
        sum(math.dist(wall["start"], wall["end"]) for wall in singles))
    ratio = min(1.0, represented_length / source_length) if source_length else 0.0
    result = {
        "audit_min_length_m": min_length_m,
        "modeling_min_length_m": 0.5,
        "raw_source_segment_count": raw_source_segment_count,
        "ignored_degenerate_source_segment_count": (
            ignored_degenerate_source_segment_count),
        "source_segment_count": len(source_records),
        "short_source_segment_count": short_source_segment_count,
        "source_face_length_m": round(source_length, 3),
        "represented_face_length_m": round(represented_length, 3),
        "face_length_coverage": round(ratio, 4),
        "exact_mapped_face_length_m": round(exact_covered_length, 3),
        "exact_face_length_coverage": round(
            exact_covered_length / source_length, 4) if source_length else 0.0,
        "gate_source_face_length_m": round(gate_source_length, 3),
        "gate_exact_mapped_face_length_m": round(
            gate_exact_covered_length, 3),
        "gate_exact_face_length_coverage": round(
            gate_exact_covered_length / gate_source_length, 4
        ) if gate_source_length else 0.0,
        "raw_source_review_count": len(raw_review_items),
        "raw_needs_review_source_segment_count": sum(
            item.get("status") == "NEEDS_REVIEW"
            for item in raw_review_items),
        "rejected_by_rule_source_segment_count": sum(
            item.get("status") == "REJECTED_BY_RULE"
            for item in raw_review_items),
        "resolved_by_profile_source_segment_count": sum(
            item.get("status") == "RESOLVED_BY_PROFILE"
            for item in raw_review_items),
        "review_unit_schema": "buildmate.wall-source-review-unit/1.0",
        "review_units": review_units,
        "logical_review_unit_count": logical_review_unit_count,
        "review_unit_reason_counts": dict(sorted(
            review_unit_reason_counts.items())),
        "review_unit_bucket_counts": dict(sorted(
            review_unit_bucket_counts.items())),
        "review_unit_status_counts": dict(sorted(
            review_unit_status_counts.items())),
        "resolved_modeled_junction_edge_count": (
            resolved_modeled_junction_edge_count),
        "resolved_l_junction_closure_edge_count": (
            resolved_l_junction_closure_edge_count),
        "resolved_endpoint_adjustment_count": (
            resolved_endpoint_adjustment_count),
        "resolved_junction_edge_count": (
            resolved_modeled_junction_edge_count +
            resolved_l_junction_closure_edge_count),
        "resolved_endpoint_adjustment_rule": {
            "classification": "EVIDENCE_RESOLUTION_ONLY",
            "requires_paired_wall": True,
            "requires_heal_wall_junction_adjustment": True,
            "maximum_range_length_m": 0.5,
            "endpoint_tolerance_m": (
                _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M),
            "model_geometry_created": False,
            "auto_action": "NONE",
        },
        "modeled_junction_edge_rule": {
            "classification": "EVIDENCE_RESOLUTION_ONLY",
            "endpoint_tolerance_m": (
                _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M),
            "length_tolerance_m": (
                _MODELED_JUNCTION_LENGTH_TOLERANCE_M),
            "angle_tolerance_deg": (
                _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG),
            "auto_action": "NONE",
        },
        "l_junction_closure_edge_rule": {
            "classification": "EVIDENCE_RESOLUTION_ONLY",
            "endpoint_tolerance_m": (
                _MODELED_JUNCTION_ENDPOINT_TOLERANCE_M),
            "length_tolerance_m": (
                _MODELED_JUNCTION_LENGTH_TOLERANCE_M),
            "angle_tolerance_deg": (
                _MODELED_JUNCTION_ANGLE_TOLERANCE_DEG),
            "maximum_source_length_m": 0.5,
            "requires_exact_source_interval_endpoint": True,
            "auto_action": "NONE",
        },
        "short_wall_pair_audit": short_wall_pair_audit,
        "short_wall_proposal_audit": short_wall_proposal_audit,
        "mixed_range_semantic_source_count": sum(
            item.get("mixed_range_semantics") is True
            for item in raw_review_items),
        "inferred_opening_bridge_supported_review_unit_count": sum(
            bool(item.get("opening_bridge_supported"))
            for item in review_units),
        "inferred_opening_bridge_rule": {
            "classification": "TOPOLOGY_EVIDENCE_ONLY",
            "changes_wall_source_review_bucket": False,
            "suppresses_wall_pair_or_proposal_analysis": False,
            "auto_action": "NONE",
        },
        "strict_topology_end_cap_count": strict_topology_end_cap_count,
        "resolved_strict_topology_end_cap_count": sum(
            item.get("status") ==
            "RESOLVED_STRICT_TOPOLOGY_END_CAP"
            for item in review_units),
        "resolved_strict_topology_end_cap_face_length_m": round(
            strict_end_cap_face_length, 3),
        "strict_topology_end_cap_rule": {
            "classification": "EVIDENCE_RESOLUTION_ONLY",
            "candidate_length_range_m": [
                _STRICT_END_CAP_MIN_LENGTH_M,
                _STRICT_END_CAP_MAX_LENGTH_M,
            ],
            "endpoint_tolerance_m": (
                _STRICT_END_CAP_ENDPOINT_TOLERANCE_M),
            "minimum_support_length_m": (
                _STRICT_END_CAP_MIN_SUPPORT_LENGTH_M),
            "minimum_support_length_ratio": (
                _STRICT_END_CAP_SUPPORT_LENGTH_RATIO),
            "parallel_tolerance_deg": (
                _STRICT_END_CAP_ANGLE_TOLERANCE_DEG),
            "perpendicular_tolerance_deg": (
                _STRICT_END_CAP_ANGLE_TOLERANCE_DEG),
            "same_half_plane_tolerance_deg": (
                _STRICT_END_CAP_ANGLE_TOLERANCE_DEG),
            "model_geometry_created": False,
            "auto_action": "NONE",
        },
        "fully_mapped_source_segment_count": (
            len(source_records) - len(uncovered) - len(partial)),
        "partially_mapped_source_segment_count": len(partial),
        "uncovered_source_segment_count": len(uncovered),
        "uncovered_source_segments": sorted(
            uncovered, key=lambda item: item["length_m"], reverse=True),
        "partially_mapped_source_segments": sorted(
            partial, key=lambda item: item["length_m"], reverse=True),
        "paired_wall_count": len(paired),
        "single_wall_count": len(singles),
    }
    if closed_profile_audit is not None:
        result["closed_profile_association"] = closed_profile_association
        result["closed_wall_profile_audit"] = {
            **(closed_profile_audit
               if isinstance(closed_profile_audit, dict) else {}),
            "association": closed_profile_association,
        }
    return result


def walls_bounds(walls: list[dict]) -> tuple[float, float, float, float] | None:
    if not walls:
        return None
    xs = [value for wall in walls for value in
          (float(wall["start"][0]), float(wall["end"][0]))]
    ys = [value for wall in walls for value in
          (float(wall["start"][1]), float(wall["end"][1]))]
    return min(xs), min(ys), max(xs), max(ys)


_VECTOR_WALL_EXCLUDE = (
    "FURT", "FURN", "家具", "SANT", "KITCHEN", "KE-KJ", "KIT-KJ",
    "DOOR", "COLU", "COLUMN", "STAIR", "ANNO", "DIM", "TEXT", "GRID",
    "FLOR", "HOLE", "墙洞", "HATC", "CONC-H", "STEL", "EQUI", "EQUIP",
    "集水坑", "排水沟", "POOL", "SITE", "CAR", "HIDDEN", "FINI", "GLAZ",
    "AREA", "ROAD", "TILE", "SOIL", "标高", "降板", "设备", "消防",
)
_VECTOR_WALL_HINTS = ("WALL", "PART", "MASON", "墙", "砌")


def audit_vector_wall_candidates(records, ox: float, oy: float,
                                 rot_deg: float, gcx: float, gcy: float,
                                 support_walls: list[dict] | None = None,
                                 columns: list[dict] | None = None) -> dict:
    """Audit non-semantic double lines without treating every pair as a wall.

    CAD layers are untrusted. Pairs on furniture, openings, sanitary equipment,
    hatches, grids and other known detail layers are reported as rejected. Only
    a wall-like layer whose candidate has both endpoints supported by the known
    physical network is promoted; all other plausible pairs remain REVIEW.
    """
    support_walls = support_walls or []
    columns = columns or []
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)

    def transform(point):
        x, y = float(point[0]) - gcx, float(point[1]) - gcy
        rx, ry = x * c - y * s + gcx, x * s + y * c + gcy
        return ((rx - ox) / 1000.0, (ry - oy) / 1000.0)

    groups: dict[str, list[dict]] = {}
    entity_counts: dict[str, int] = {}
    for record in records:
        entity, layer, _provenance = _record_parts(record)
        if wall_geometry_group(layer) is not None:
            continue
        leaf = (layer or "").upper().strip().rsplit("$0$", 1)[-1]
        entity_counts[leaf] = entity_counts.get(leaf, 0) + 1
        segments = []
        if entity.dxftype() == "LINE":
            segments.append((entity.dxf.start, entity.dxf.end))
        elif entity.dxftype() == "LWPOLYLINE":
            points = list(entity.get_points("xy"))
            segments.extend(zip(points, points[1:]))
            if entity.closed and len(points) > 2:
                segments.append((points[-1], points[0]))
        for start, end in segments:
            line = _line_record(transform(start), transform(end))
            if line and line["length"] >= 0.5:
                groups.setdefault(leaf, []).append(line)

    rejected_layers = {}
    pair_candidates = []
    for leaf, lines in groups.items():
        if any(token in leaf for token in _VECTOR_WALL_EXCLUDE):
            rejected_layers[leaf] = {"reason": "known_non_wall_layer",
                                     "entity_count": entity_counts.get(leaf, 0)}
            continue
        if len(lines) > 2000:
            rejected_layers[leaf] = {"reason": "excessive_detail_density",
                                     "entity_count": entity_counts.get(leaf, 0)}
            continue
        for first_index, first in enumerate(lines):
            for second in lines[first_index + 1:]:
                pair = _pair_geometry(first, second)
                if pair is None or pair["distance"] > 0.40:
                    continue
                pair_candidates.append({
                    "layer": leaf,
                    "start": [round(value, 3) for value in pair["start"]],
                    "end": [round(value, 3) for value in pair["end"]],
                    "thickness": round(pair["distance"] * 1000.0, 1),
                    "paired": True,
                    "wall_group": "A",
                })

    # Remove geometrically duplicated pairings before topology qualification.
    unique = {}
    for wall in pair_candidates:
        first = tuple(wall["start"])
        second = tuple(wall["end"])
        key = (wall["layer"], min(first, second), max(first, second), wall["thickness"])
        unique[key] = wall
    pair_candidates = list(unique.values())

    promoted, review = [], []
    for index, candidate in enumerate(pair_candidates):
        candidate["id"] = f"vector_candidate_{index}"
        metrics = topology_with_support([candidate], support_walls, columns)
        candidate["endpoint_coverage"] = metrics["endpoint_coverage"]
        has_hint = any(token in candidate["layer"] for token in _VECTOR_WALL_HINTS)
        if has_hint and metrics["fully_connected_rate"] == 1.0:
            candidate["confidence"] = 0.70
            candidate["source"] = ["drawing", "unknown-layer-double-line",
                                   "topology-corroborated"]
            candidate["warnings"] = ["非标准墙图层，已由双线和两端拓扑共同确认"]
            promoted.append(candidate)
        else:
            candidate["decision"] = ("no_wall_semantic_hint" if not has_hint
                                     else "insufficient_topology_support")
            review.append(candidate)
    return {
        "scanned_entity_count": sum(entity_counts.values()),
        "scanned_layer_count": len(entity_counts),
        "rejected_layers": rejected_layers,
        "pair_candidate_count": len(pair_candidates),
        "promoted_count": len(promoted),
        "review_count": len(review),
        "promoted_walls": promoted,
        "review_candidates": review,
    }


def infer_opening_bridges(walls: list[dict], min_gap_m: float = 0.30,
                          max_gap_m: float = 2.20,
                          angle_tolerance_deg: float = 3.0,
                          offset_tolerance_m: float = 0.12) -> list[dict]:
    """Infer door/opening gaps only between facing collinear wall endpoints.

    Bridges are topology evidence, never wall model elements. This lets room
    polygonization cross a genuine door gap without converting that opening
    into solid wall geometry.
    """
    endpoints = []
    for wall_index, wall in enumerate(walls):
        record = _line_record(tuple(wall["start"]), tuple(wall["end"]))
        if record is None:
            continue
        for key in ("start", "end"):
            endpoints.append({"wall_index": wall_index, "key": key,
                              "point": list(wall[key]), "line": record})
    candidates = []
    for first_index, first in enumerate(endpoints):
        for second_index in range(first_index + 1, len(endpoints)):
            second = endpoints[second_index]
            if first["wall_index"] == second["wall_index"]:
                continue
            if _angle_delta(first["line"]["angle"], second["line"]["angle"]) > angle_tolerance_deg:
                continue
            distance = math.dist(first["point"], second["point"])
            if not min_gap_m <= distance <= max_gap_m:
                continue
            bridge = _line_record(tuple(first["point"]), tuple(second["point"]))
            if bridge is None:
                continue
            if (_angle_delta(bridge["angle"], first["line"]["angle"]) > angle_tolerance_deg or
                    _angle_delta(bridge["angle"], second["line"]["angle"]) > angle_tolerance_deg):
                continue
            # Endpoint-to-opposite-line lateral distance rejects parallel walls
            # whose ends merely happen to be near each other diagonally.
            if (_point_segment_distance(first["point"], second["line"]["p1"],
                                        second["line"]["p2"]) >
                    distance + offset_tolerance_m):
                continue
            candidates.append((distance, first_index, second_index, first, second))
    used = set()
    bridges = []
    for distance, first_index, second_index, first, second in sorted(candidates):
        if first_index in used or second_index in used:
            continue
        used.update((first_index, second_index))
        bridges.append({
            "id": f"opening_bridge_{len(bridges)}",
            "start": [round(value, 3) for value in first["point"]],
            "end": [round(value, 3) for value in second["point"]],
            "thickness": 0,
            "kind": "inferred_opening",
            "gap_m": round(distance, 3),
            "confidence": 0.70,
            "source": ["wall-endpoints", "collinear-gap"],
        })
    return bridges


def cv_room_enclosure_audit(walls: list[dict], opening_bridges: list[dict] | None = None,
                            resolution_m: float = 0.10,
                            minimum_area_m2: float = 4.0) -> dict:
    """Independently count enclosed regions on a rasterized wall network."""
    opening_bridges = opening_bridges or []
    network = list(walls) + list(opening_bridges)
    bounds = walls_bounds(network)
    if bounds is None or resolution_m <= 0:
        return {"status": "NO_GEOMETRY", "enclosed_region_count": 0,
                "areas_m2": []}
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"status": "OPENCV_NOT_INSTALLED", "enclosed_region_count": 0,
                "areas_m2": []}
    min_x, min_y, max_x, max_y = bounds
    margin = 5
    width = int(math.ceil((max_x - min_x) / resolution_m)) + margin * 2 + 1
    height = int(math.ceil((max_y - min_y) / resolution_m)) + margin * 2 + 1
    if width <= 0 or height <= 0 or width * height > 20_000_000:
        return {"status": "RASTER_LIMIT", "enclosed_region_count": 0,
                "areas_m2": [], "image_size": [width, height]}
    mask = np.zeros((height, width), dtype=np.uint8)

    def pixel(point):
        return (int(round((float(point[0]) - min_x) / resolution_m)) + margin,
                int(round((max_y - float(point[1])) / resolution_m)) + margin)

    for wall in network:
        thickness_m = max(resolution_m, float(wall.get("thickness", 0)) / 1000.0)
        pixels = max(1, int(round(thickness_m / resolution_m)))
        cv2.line(mask, pixel(wall["start"]), pixel(wall["end"]), 255,
                 pixels, cv2.LINE_8)
    free = np.where(mask == 0, 255, 0).astype(np.uint8)
    labels_count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        free, connectivity=4)
    areas = []
    for label in range(1, labels_count):
        x, y, component_width, component_height, pixel_area = stats[label]
        touches_border = (x == 0 or y == 0 or x + component_width >= width or
                          y + component_height >= height)
        area_m2 = float(pixel_area) * resolution_m * resolution_m
        if not touches_border and area_m2 >= minimum_area_m2:
            areas.append(round(area_m2, 3))
    return {
        "status": "OK",
        "enclosed_region_count": len(areas),
        "areas_m2": sorted(areas, reverse=True),
        "resolution_m": resolution_m,
        "image_size": [width, height],
    }


def topology_with_support(primary_walls: list[dict], support_walls: list[dict] | None = None,
                          columns: list[dict] | None = None,
                          opening_bridges: list[dict] | None = None,
                          tolerance_m: float = 0.30,
                          column_margin_m: float = 0.30) -> dict:
    """Measure architectural endpoints against the complete physical network.

    Architectural partitions legitimately terminate on structural walls or
    columns, so an architecture-only graph understates connectivity. Room
    polygons are also reported from noded A+S centerlines when Shapely is
    available; failure to polygonize is explicit rather than silently ignored.
    """
    support_walls = support_walls or []
    columns = columns or []
    opening_bridges = opening_bridges or []
    connected = 0
    fully_connected = 0
    dangling = []
    all_walls = list(primary_walls) + list(support_walls) + list(opening_bridges)
    for index, wall in enumerate(primary_walls):
        hits = []
        for point in (wall["start"], wall["end"]):
            hit = False
            for other in all_walls:
                if other is wall:
                    continue
                physical_tol = min(0.8, max(
                    tolerance_m,
                    (float(wall.get("thickness", 0)) +
                     float(other.get("thickness", 0))) / 2000.0 + 0.05))
                if _point_segment_distance(point, other["start"], other["end"]) <= physical_tol:
                    hit = True
                    break
            if not hit:
                for column in columns:
                    center = column.get("center", [0, 0])
                    size = column.get("size", [0.8, 0.8])
                    if (abs(point[0] - center[0]) <= float(size[0]) / 2 + column_margin_m and
                            abs(point[1] - center[1]) <= float(size[1]) / 2 + column_margin_m):
                        hit = True
                        break
            hits.append(hit)
            connected += int(hit)
        fully_connected += int(all(hits))
        if not all(hits):
            dangling.append({"id": wall.get("id", f"wall_{index}"), "ends": hits})

    polygon_count = 0
    polygon_area_m2 = 0.0
    polygonize_status = "UNAVAILABLE"
    try:
        from shapely.geometry import LineString
        from shapely.ops import polygonize_full, unary_union

        lines = [LineString([wall["start"], wall["end"]]) for wall in all_walls
                 if math.dist(wall["start"], wall["end"]) > 1e-9]
        if lines:
            polygons, _cuts, _dangles, _invalid = polygonize_full(unary_union(lines))
            polygon_count = len(polygons.geoms)
            polygon_area_m2 = sum(float(item.area) for item in polygons.geoms)
        polygonize_status = "OK"
    except ImportError:
        polygonize_status = "SHAPELY_NOT_INSTALLED"
    except (TypeError, ValueError) as exc:
        polygonize_status = f"ERROR: {exc}"

    total = len(primary_walls)
    return {
        "wall_count": total,
        "support_wall_count": len(support_walls),
        "opening_bridge_count": len(opening_bridges),
        "endpoint_coverage": connected / (2 * total) if total else 0.0,
        "fully_connected_rate": fully_connected / total if total else 0.0,
        "dangling": dangling,
        "room_polygon_count": polygon_count,
        "room_polygon_area_m2": round(polygon_area_m2, 3),
        "polygonize_status": polygonize_status,
    }


def classify_dangling_endpoints(dangling: list[dict],
                                primary_walls: list[dict],
                                support_walls: list[dict] | None = None,
                                opening_bridges: list[dict] | None = None,
                                source_coverage: dict | None = None,
                                source_tolerance_m: float = 0.35,
                                nearby_tolerance_m: float = 1.0,
                                door_vectors: list[dict] | None = None) -> dict:
    """Classify unresolved wall endpoints without changing wall geometry.

    Inferred opening bridges are topology evidence only.  A door is supported
    only when an endpoint is also close to an explicit door-layer DXF vector.
    Exact end caps may explain a dangling endpoint without moving or connecting
    it; all other candidates remain review-only.
    """
    support_walls = support_walls or []
    opening_bridges = opening_bridges or []
    source_coverage = source_coverage or {}
    door_vectors = door_vectors or []
    walls_by_id = {
        wall.get("id", f"wall_{index}"): wall
        for index, wall in enumerate(primary_walls)
    }
    source_ranges = []
    resolved_profile_ranges = []
    for source in (
            list(source_coverage.get("uncovered_source_segments", [])) +
            list(source_coverage.get("partially_mapped_source_segments", []))):
        status = source.get("status")
        if status not in (None, "NEEDS_REVIEW", "RESOLVED_BY_PROFILE"):
            continue
        for part in source.get("unmapped_ranges", []):
            if float(part.get("length_m", 0.0)) < 0.01:
                continue
            if status == "RESOLVED_BY_PROFILE":
                resolved_profile_ranges.append((source, part))
            else:
                source_ranges.append((source, part))

    network = (
        [("architectural_wall", item) for item in primary_walls] +
        [("structural_wall", item) for item in support_walls] +
        [("opening_bridge", item) for item in opening_bridges]
    )

    def segment_projection(point, start, end):
        dx = float(end[0]) - float(start[0])
        dy = float(end[1]) - float(start[1])
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            target = [float(start[0]), float(start[1])]
            return target, 0.0, math.dist(point, target), 0.0
        ratio = ((float(point[0]) - float(start[0])) * dx +
                 (float(point[1]) - float(start[1])) * dy) / length_sq
        ratio = max(0.0, min(1.0, ratio))
        target = [float(start[0]) + ratio * dx,
                  float(start[1]) + ratio * dy]
        return target, ratio, math.dist(point, target), math.sqrt(length_sq)

    def unit_vector(start, end):
        dx = float(end[0]) - float(start[0])
        dy = float(end[1]) - float(start[1])
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            return None
        return [dx / length, dy / length]

    def explicit_door_layer(layer):
        leaf = str(layer or "").upper().strip().rsplit("$0$", 1)[-1]
        return "DOOR" in leaf or "门" in leaf

    def source_match_payload(distance, source, part):
        return {
            "candidate_id": source.get("candidate_id"),
            "entity_handle": source.get("entity_handle"),
            "layer": source.get("layer"),
            "drawing_identity": source.get("drawing_identity"),
            "source_occurrence_id": source.get("source_occurrence_id"),
            "placed_entity_id": source.get("placed_entity_id"),
            "structural_occurrence_id": source.get(
                "structural_occurrence_id"),
            "source_segment_id": source.get("source_segment_id"),
            "source_record_index": source.get("source_record_index"),
            "segment_index": source.get("segment_index"),
            "identity_is_complete": source.get("identity_is_complete"),
            "identity_limitations": copy.deepcopy(
                source.get("identity_limitations", [])),
            "placement_path": copy.deepcopy(source.get("placement_path")),
            "source_status": source.get("status"),
            "source_decision_reason": source.get("decision_reason"),
            "parent_profile_id": source.get("parent_profile_id"),
            "edge_role": source.get("edge_role"),
            "profile_association_status": source.get(
                "profile_association_status"),
            "support_evidence": copy.deepcopy(
                source.get("support_evidence")),
            "strict_topology_evidence": copy.deepcopy(
                source.get("strict_topology_evidence")),
            "range_start": list(part["start"]),
            "range_end": list(part["end"]),
            "distance_m": round(distance, 4),
            "opening_bridge_supported": bool(
                part.get("opening_bridge_supported")),
            "topology_evidence_only": bool(
                part.get("opening_bridge_supported")),
        }

    def exact_profile_cap_match(source_match, wall):
        return bool(
            source_match and
            source_match.get("source_status") == "RESOLVED_BY_PROFILE" and
            source_match.get("source_decision_reason") ==
            "approved_closed_wall_strip_end_cap" and
            source_match.get("profile_association_status") == "EXACT" and
            source_match.get("edge_role") == "end_cap" and
            source_match.get("parent_profile_id") and
            source_match.get("parent_profile_id") ==
            wall.get("source_candidate_id") and
            wall.get("geometry_source") == "DXF_CLOSED_WALL_STRIP" and
            source_match.get("identity_is_complete") is True and
            not source_match.get("identity_limitations") and
            float(source_match.get("distance_m", math.inf)) <= 0.005)

    def exact_paired_wall_cap_match(source_match, wall):
        if not (
                source_match and wall.get("paired") is True and
                source_match.get("source_status") == "NEEDS_REVIEW" and
                source_match.get("source_decision_reason") ==
                "strict_topology_end_cap" and
                source_match.get("identity_is_complete") is True and
                not source_match.get("identity_limitations") and
                float(source_match.get("distance_m", math.inf)) <= 0.005):
            return False
        wall_source_ids = list(wall.get("source_segment_ids") or [])
        support = source_match.get("support_evidence")
        strict = source_match.get("strict_topology_evidence") or {}
        if (len(wall_source_ids) != 2 or len(set(wall_source_ids)) != 2 or
                not isinstance(support, list) or len(support) != 2 or
                strict.get("support_count") != 2):
            return False
        support_ids = [item.get("source_segment_id") for item in support
                       if isinstance(item, dict)]
        if (len(support_ids) != 2 or None in support_ids or
                set(support_ids) != set(wall_source_ids)):
            return False
        drawing_identity = source_match.get("drawing_identity")
        structural_occurrence_id = source_match.get(
            "structural_occurrence_id")
        required_support_fields = (
            "drawing_identity", "source_occurrence_id", "placed_entity_id",
            "structural_occurrence_id", "source_segment_id", "entity_handle")
        return all(
            all(item.get(key) for key in required_support_fields) and
            item.get("drawing_identity") == drawing_identity and
            item.get("structural_occurrence_id") == structural_occurrence_id
            for item in support)

    valid_door_vectors = []
    for vector in door_vectors:
        if (not isinstance(vector, dict) or
                vector.get("geometry_source") != "DXF_VECTOR" or
                not explicit_door_layer(vector.get("layer"))):
            continue
        start, end = vector.get("start"), vector.get("end")
        if (not isinstance(start, (list, tuple)) or len(start) < 2 or
                not isinstance(end, (list, tuple)) or len(end) < 2):
            continue
        valid_door_vectors.append(vector)

    candidates = []
    for item in dangling:
        wall_id = item.get("id")
        wall = walls_by_id.get(wall_id)
        if wall is None:
            continue
        for endpoint_index, is_connected in enumerate(item.get("ends", [])):
            if is_connected or endpoint_index > 1:
                continue
            endpoint_name = "start" if endpoint_index == 0 else "end"
            point = list(wall[endpoint_name])
            other_point = wall["end" if endpoint_index == 0 else "start"]
            outward_tangent = unit_vector(other_point, point)
            nearest_resolved_profile = None
            for source, part in resolved_profile_ranges:
                if (source.get("parent_profile_id") !=
                        wall.get("source_candidate_id")):
                    continue
                distance = _point_segment_distance(
                    point, part["start"], part["end"])
                if (nearest_resolved_profile is None or
                        distance < nearest_resolved_profile[0]):
                    nearest_resolved_profile = (distance, source, part)
            nearest_source = None
            for source, part in source_ranges:
                distance = _point_segment_distance(
                    point, part["start"], part["end"])
                if (nearest_source is None or
                        distance < nearest_source[0]):
                    nearest_source = (distance, source, part)

            nearest_network = None
            for kind, other in network:
                if other is wall:
                    continue
                target, ratio, distance, length = segment_projection(
                    point, other["start"], other["end"])
                if (nearest_network is None or
                        distance < nearest_network[0]):
                    nearest_network = (
                        distance, kind, other, target, ratio, length)

            nearest_door = None
            for door in valid_door_vectors:
                target, ratio, distance, _length = segment_projection(
                    point, door["start"], door["end"])
                if nearest_door is None or distance < nearest_door[0]:
                    nearest_door = (distance, door, target, ratio)

            door_match = None
            if nearest_door and nearest_door[0] <= source_tolerance_m:
                distance, door, target, ratio = nearest_door
                door_match = {
                    "entity_handle": door.get("entity_handle"),
                    "layer": door.get("layer"),
                    "distance_m": round(distance, 4),
                    "closest_point": [round(value, 4) for value in target],
                    "projection_ratio": round(ratio, 4),
                    "geometry_source": "DXF_VECTOR",
                }

            source_match = None
            if (nearest_resolved_profile and
                    nearest_resolved_profile[0] <= 0.005):
                distance, source, part = nearest_resolved_profile
                source_match = source_match_payload(
                    distance, source, part)
            elif nearest_source and nearest_source[0] <= source_tolerance_m:
                distance, source, part = nearest_source
                source_match = source_match_payload(
                    distance, source, part)

            network_match = None
            network_within_tolerance = bool(
                nearest_network and
                nearest_network[0] <= nearby_tolerance_m)
            if nearest_network:
                distance, kind, other, target, ratio, length = nearest_network
                endpoint_margin = min(0.05, length * 0.1)
                along = ratio * length
                junction_position = (
                    "interior" if (length > 2 * endpoint_margin and
                                   endpoint_margin < along <
                                   length - endpoint_margin)
                    else "endpoint")
                other_tangent = unit_vector(other["start"], other["end"])
                angle_delta = None
                if outward_tangent and other_tangent:
                    dot = abs(sum(a * b for a, b in
                                  zip(outward_tangent, other_tangent)))
                    angle_delta = math.degrees(math.acos(
                        max(-1.0, min(1.0, dot))))
                oblique_structural = bool(
                    kind == "structural_wall" and
                    angle_delta is not None and
                    min(angle_delta, abs(90.0 - angle_delta)) > 5.0)
                network_match = {
                    "kind": kind,
                    "element_id": other.get("id"),
                    "distance_m": round(distance, 4),
                    "closest_point": [round(value, 4) for value in target],
                    "projection_ratio": round(ratio, 4),
                    "junction_position": junction_position,
                    "interior_junction": junction_position == "interior",
                    "angle_delta_deg": (round(angle_delta, 3)
                                        if angle_delta is not None else None),
                    "oblique_structural_wall": oblique_structural,
                    "topology_evidence_only": kind == "opening_bridge",
                    "snap_eligible": False,
                }

            opening_topology_evidence = bool(
                (source_match and source_match["topology_evidence_only"]) or
                (network_within_tolerance and network_match and
                 network_match["kind"] == "opening_bridge"))
            door_supported = door_match is not None
            resolution_type = None
            if exact_profile_cap_match(source_match, wall):
                status = "RESOLVED_INTENTIONAL_CAP"
                reason = "approved_closed_wall_strip_end_cap_endpoint"
                resolution_type = "APPROVED_CLOSED_PROFILE_END_CAP"
            elif exact_paired_wall_cap_match(source_match, wall):
                status = "RESOLVED_INTENTIONAL_CAP"
                reason = "exact_paired_wall_end_cap_endpoint"
                resolution_type = "EXACT_PAIRED_WALL_END_CAP"
            elif door_supported:
                status = "NEEDS_REVIEW"
                reason = "door_vector_supported_endpoint"
            elif source_match and source_match["topology_evidence_only"]:
                status = "NEEDS_REVIEW"
                reason = "opening_topology_evidence_endpoint"
            elif source_match:
                status = "NEEDS_REVIEW"
                reason = "source_supported_omission_endpoint"
            elif (network_within_tolerance and network_match and
                  network_match["kind"] != "opening_bridge" and
                  network_match["interior_junction"]):
                status = "NEEDS_REVIEW"
                reason = "interior_junction"
            elif network_within_tolerance and network_match:
                status = "NEEDS_REVIEW"
                reason = ("opening_topology_evidence_endpoint" if
                          network_match["kind"] == "opening_bridge" else
                          "nearby_network_gap")
            else:
                status = "NEEDS_REVIEW"
                reason = "unresolved_free_endpoint"

            blockers = []
            if status == "NEEDS_REVIEW":
                blockers.append("human_review_required")
                if outward_tangent is None:
                    blockers.append("degenerate_wall_direction")
                if opening_topology_evidence and not door_supported:
                    blockers.extend([
                        "opening_bridge_is_topology_evidence_only",
                        "explicit_door_vector_missing",
                    ])
                if (network_match and
                        network_match["oblique_structural_wall"] and
                        network_within_tolerance):
                    blockers.append("oblique_structural_wall_no_snap")
                if (network_match and network_match["interior_junction"] and
                        network_within_tolerance):
                    blockers.append("interior_junction_requires_review")
            signature = json.dumps(
                [wall_id, endpoint_name,
                 [round(float(value), 4) for value in point]],
                ensure_ascii=False)
            candidates.append({
                "candidate_id": "wall_endpoint_" + hashlib.sha256(
                    signature.encode("utf-8")).hexdigest()[:16],
                "status": status,
                "geometry_source": "DXF_VECTOR",
                "wall_id": wall_id,
                "endpoint": endpoint_name,
                "point": [round(float(value), 4) for value in point],
                "outward_tangent": ([round(value, 6)
                                      for value in outward_tangent]
                                     if outward_tangent else None),
                "paired_wall": bool(wall.get("paired")),
                "decision_reason": reason,
                "resolution_type": resolution_type,
                "source_match": source_match,
                "nearest_network": network_match,
                "door_vector_supported_endpoint": door_supported,
                "door_vector_match": door_match,
                "opening_bridge_topology_evidence": opening_topology_evidence,
                "nearest_endpoint_candidate": None,
                "mutual_nearest_cluster": None,
                "auto_action": "NONE",
                "blockers": blockers,
            })

    review_candidates = [candidate for candidate in candidates
                         if candidate["status"] == "NEEDS_REVIEW"]
    nearest_by_id = {}
    for candidate in review_candidates:
        alternatives = [
            (math.dist(candidate["point"], other["point"]),
             other["candidate_id"], other)
            for other in review_candidates
            if other["wall_id"] != candidate["wall_id"]
        ]
        if alternatives:
            nearest_by_id[candidate["candidate_id"]] = min(
                alternatives, key=lambda item: (item[0], item[1]))

    clusters = []
    clustered_ids = set()
    candidates_by_id = {
        candidate["candidate_id"]: candidate for candidate in candidates
    }
    for candidate in review_candidates:
        nearest = nearest_by_id.get(candidate["candidate_id"])
        if nearest is None:
            continue
        distance, other_id, other = nearest
        reverse = nearest_by_id.get(other_id)
        mutual = bool(
            reverse and reverse[1] == candidate["candidate_id"] and
            distance <= nearby_tolerance_m)
        candidate["nearest_endpoint_candidate"] = {
            "candidate_id": other_id,
            "wall_id": other["wall_id"],
            "endpoint": other["endpoint"],
            "distance_m": round(distance, 4),
            "mutual_nearest": mutual,
        }
        if (not mutual or candidate["candidate_id"] in clustered_ids or
                other_id in clustered_ids):
            continue

        first, second = sorted(
            (candidate, other), key=lambda item: item["candidate_id"])
        delta = [second["point"][0] - first["point"][0],
                 second["point"][1] - first["point"][1]]
        pair_distance = math.hypot(*delta)
        connection = ([delta[0] / pair_distance, delta[1] / pair_distance]
                      if pair_distance > 1e-12 else None)
        first_tangent = first["outward_tangent"]
        second_tangent = second["outward_tangent"]
        facing = False
        direction_delta = None
        lateral_offset = None
        if connection and first_tangent and second_tangent:
            first_forward = sum(a * b for a, b in
                                zip(first_tangent, connection))
            second_forward = -sum(a * b for a, b in
                                  zip(second_tangent, connection))
            facing = first_forward >= 0.966 and second_forward >= 0.966
            tangent_dot = abs(sum(a * b for a, b in
                                  zip(first_tangent, second_tangent)))
            direction_delta = math.degrees(math.acos(
                max(-1.0, min(1.0, tangent_dot))))
            lateral_offset = max(
                abs(first_tangent[0] * delta[1] -
                    first_tangent[1] * delta[0]),
                abs(second_tangent[0] * delta[1] -
                    second_tangent[1] * delta[0]))
        aligned = bool(
            facing and direction_delta is not None and
            direction_delta <= 5.0 and lateral_offset is not None and
            lateral_offset <= 0.08)
        member_ids = [first["candidate_id"], second["candidate_id"]]
        cluster_signature = json.dumps(member_ids, ensure_ascii=False)
        cluster = {
            "cluster_id": "endpoint_cluster_" + hashlib.sha256(
                cluster_signature.encode("utf-8")).hexdigest()[:16],
            "candidate_ids": member_ids,
            "distance_m": round(pair_distance, 4),
            "outward_facing": facing,
            "direction_delta_deg": (round(direction_delta, 3)
                                    if direction_delta is not None else None),
            "lateral_offset_m": (round(lateral_offset, 4)
                                  if lateral_offset is not None else None),
            "direct_connection_supported": aligned,
            "topology_evidence_only": True,
            "auto_action": "NONE",
        }
        clusters.append(cluster)
        clustered_ids.update(member_ids)
        for member_id in member_ids:
            member = candidates_by_id[member_id]
            member["mutual_nearest_cluster"] = cluster
            if not facing:
                member["blockers"].append(
                    "mutual_nearest_pair_not_outward_facing")
            if direction_delta is None or direction_delta > 5.0:
                member["blockers"].append(
                    "mutual_nearest_pair_not_collinear")
            if lateral_offset is None or lateral_offset > 0.08:
                member["blockers"].append(
                    "mutual_nearest_pair_laterally_offset")
            if aligned:
                member["blockers"].append(
                    "mutual_nearest_pair_requires_semantic_review")

    reason_counts = Counter(
        candidate["decision_reason"] for candidate in candidates)
    blocker_counts = Counter(
        blocker for candidate in candidates for blocker in candidate["blockers"])
    status_counts = Counter(candidate["status"] for candidate in candidates)
    needs_review_count = status_counts.get("NEEDS_REVIEW", 0)
    resolved_intentional_cap_count = status_counts.get(
        "RESOLVED_INTENTIONAL_CAP", 0)
    return {
        "status": "REVIEW" if needs_review_count else "PASS",
        "auto_action": "NONE",
        "candidate_count": len(candidates),
        "needs_review_count": needs_review_count,
        "resolved_intentional_cap_count": resolved_intentional_cap_count,
        "status_counts": dict(sorted(status_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "door_vector_input_count": len(door_vectors),
        "explicit_door_vector_count": len(valid_door_vectors),
        "mutual_nearest_cluster_count": len(clusters),
        "mutual_nearest_clusters": clusters,
        "candidates": candidates,
        "note": ("Candidates are diagnostic only; no wall endpoint was moved "
                 "or connected automatically. Exact intentional caps explain "
                 "an endpoint but do not increase physical connectivity. "
                 "Inferred opening bridges are topology evidence, not door "
                 "semantics."),
    }


def topology_metrics(walls: list[dict], tolerance_m: float = 0.30) -> dict:
    """计算墙体物理连接率；一面墙必须两端都连接才算 fully_connected。

    双线墙的中心线常在墙面收口处短半个墙厚，因此连接容差按两墙半厚之和自适应，
    并限制在 0.8m 内；这判断的是墙实体相交，不是无条件放宽 Gate。
    """
    connected_endpoints = 0
    fully_connected = 0
    dangling = []
    for i, wall in enumerate(walls):
        hits = []
        for endpoint in (wall["start"], wall["end"]):
            hit = False
            for j, other in enumerate(walls):
                if j == i:
                    continue
                physical_tol = min(0.8, max(
                    tolerance_m,
                    (float(wall.get("thickness", 0)) +
                     float(other.get("thickness", 0))) / 2000.0 + 0.05))
                if _point_segment_distance(endpoint, other["start"], other["end"]) <= physical_tol:
                    hit = True
                    break
            hits.append(hit)
            connected_endpoints += int(hit)
        if all(hits):
            fully_connected += 1
        else:
            dangling.append({"id": wall.get("id", f"wall_{i}"), "ends": hits})
    total = len(walls)
    return {
        "wall_count": total,
        "endpoint_coverage": connected_endpoints / (2 * total) if total else 0.0,
        "fully_connected_rate": fully_connected / total if total else 0.0,
        "dangling": dangling,
    }


def merge_collinear_walls(walls: list[dict], gap_m: float = 0.35,
                          offset_m: float = 0.06, angle_deg: float = 1.0) -> list[dict]:
    """合并同厚、共线且相接/轻微断开的墙段，恢复连续结构墙中心线。"""
    records = []
    for wall in walls:
        rec = _line_record(tuple(wall["start"]), tuple(wall["end"]))
        if rec:
            rec["wall"] = wall
            records.append(rec)
    groups: list[list[dict]] = []
    for rec in records:
        placed = False
        for group in groups:
            base = group[0]
            if abs(float(rec["wall"].get("thickness", 0)) -
                   float(base["wall"].get("thickness", 0))) > 20:
                continue
            rec_layers = set(rec["wall"].get("source_layers") or [])
            base_layers = set(base["wall"].get("source_layers") or [])
            if rec_layers and base_layers and rec_layers.isdisjoint(base_layers):
                continue
            if _angle_delta(rec["angle"], base["angle"]) > angle_deg:
                continue
            # Group by perpendicular baseline distance. Point-to-segment
            # distance also includes the longitudinal gap and therefore made
            # ``gap_m`` ineffective for genuinely separated wall fragments.
            baseline_error = max(abs(
                (point[0] - base["p1"][0]) * base["n"][0] +
                (point[1] - base["p1"][1]) * base["n"][1])
                for point in (rec["p1"], rec["p2"]))
            if baseline_error > offset_m:
                continue
            group.append(rec)
            placed = True
            break
        if not placed:
            groups.append([rec])

    merged = []
    for group in groups:
        base = group[0]
        ox, oy = base["p1"]
        ux, uy = base["u"]
        intervals = []
        for input_index, rec in enumerate(group):
            vals = [((p[0] - ox) * ux + (p[1] - oy) * uy) for p in (rec["p1"], rec["p2"])]
            intervals.append({
                "start": min(vals), "end": max(vals),
                "record": rec, "input_index": input_index,
            })
        intervals.sort(key=lambda item: (
            item["start"], item["end"], item["input_index"]))
        runs = []
        for interval in intervals:
            start, end = interval["start"], interval["end"]
            if runs and start <= runs[-1]["end"] + gap_m:
                runs[-1]["end"] = max(runs[-1]["end"], end)
                runs[-1]["contributors"].append(interval)
            else:
                runs.append({
                    "start": start, "end": end,
                    "contributors": [interval],
                })
        for run in runs:
            start, end = run["start"], run["end"]
            contributing_walls = [
                item["record"]["wall"] for item in run["contributors"]]
            source_refs = _stable_source_ref_union(contributing_walls)
            source_ids = _source_ids(source_refs)
            source_layers = sorted({
                str(layer)
                for contributor in contributing_walls
                for layer in (contributor.get("source_layers") or [])
                if str(layer)
            })
            wall = _copy_wall_lineage(base["wall"])
            wall["start"] = [round(ox + start * ux, 3), round(oy + start * uy, 3)]
            wall["end"] = [round(ox + end * ux, 3), round(oy + end * uy, 3)]
            # A merged run is paired only when at least one contributing
            # segment has measured opposing-face support.  This keeps a
            # terminal-only recovery reviewable if it cannot join a paired
            # run, while a joined terminal extension inherits the supported
            # wall's modeling status.
            wall["paired"] = any(
                bool(contributor.get("paired"))
                for contributor in contributing_walls)
            if source_layers:
                wall["source_layers"] = source_layers
            wall["source_segment_refs"] = source_refs
            wall["source_segment_ids"] = source_ids
            for field in ("endpoint_adjustments", "geometry_adjustments"):
                values = _stable_metadata_union(contributing_walls, field)
                if values:
                    wall[field] = values
                else:
                    wall.pop(field, None)
            derivation = _stable_metadata_union(
                contributing_walls, "derivation")
            parent_wall_ids = []
            for contributor in contributing_walls:
                wall_id = contributor.get("id")
                if wall_id and wall_id not in parent_wall_ids:
                    parent_wall_ids.append(wall_id)
            derivation.append({
                "operation": "merge_collinear_walls",
                "parent_wall_ids": parent_wall_ids,
                "parent_source_segment_ids": source_ids,
                "contributing_wall_count": len(contributing_walls),
                "output_run_interval_m": [round(start, 6), round(end, 6)],
            })
            wall["derivation"] = derivation
            terminal_contributors = sum(
                bool(contributor.get("terminal_face_recovery"))
                for contributor in contributing_walls)
            if terminal_contributors:
                wall["terminal_face_recovery"] = True
                wall["terminal_face_recovery_count"] = terminal_contributors
                if any(contributor.get("paired")
                       for contributor in contributing_walls):
                    wall["geometry_source"] = (
                        "DXF_VECTOR_TERMINAL_FACE_EXTENSION_MERGED")
            merged.append(wall)
    return merged


def restore_architectural_wall_continuity(
        walls: list[dict], gap_m: float = 2.20,
        compact_max_length_m: float = 0.65,
        compact_max_aspect_ratio: float = 1.5,
        merge_terminal_extensions: bool = False) -> tuple[list[dict], dict]:
    """Restore gross architectural walls while excluding compact column shapes."""
    paired = []
    proposals = []
    terminal_extensions = []
    excluded_compact = []
    for source_wall in walls:
        wall = copy.deepcopy(source_wall)
        if not wall.get("paired"):
            if (merge_terminal_extensions and
                    wall.get("terminal_face_recovery")):
                terminal_extensions.append(wall)
            else:
                proposals.append(wall)
            continue
        length_m = math.dist(wall["start"], wall["end"])
        thickness_m = float(wall.get("thickness") or 0.0) / 1000.0
        aspect_ratio = (length_m / thickness_m
                        if thickness_m > 1e-9 else math.inf)
        if (length_m <= compact_max_length_m and
                aspect_ratio <= compact_max_aspect_ratio):
            excluded_compact.append({
                "start": list(wall["start"]),
                "end": list(wall["end"]),
                "length_m": round(length_m, 6),
                "thickness_mm": float(wall.get("thickness") or 0.0),
                "aspect_ratio": round(aspect_ratio, 6),
                "source_layers": list(wall.get("source_layers") or []),
                "source_segment_ids": list(
                    wall.get("source_segment_ids") or []),
                "reason": "compact_column_like_geometry",
            })
            continue
        paired.append(wall)

    merged_inputs = paired + terminal_extensions
    merged_all = merge_collinear_walls(merged_inputs, gap_m=gap_m)
    restored = [wall for wall in merged_all if wall.get("paired")]
    unmerged_terminal_extensions = [
        wall for wall in merged_all if not wall.get("paired")]
    return restored + proposals + unmerged_terminal_extensions, {
        "mode": "GROSS_WALL_IGNORE_OPENINGS_AND_CONSTRUCTION_COLUMNS",
        "max_bridged_gap_m": gap_m,
        "input_wall_count": len(walls),
        "input_paired_wall_count": len(paired) + len(excluded_compact),
        "restored_paired_wall_count": len(restored),
        "unpaired_review_wall_count": len(proposals),
        "merged_paired_wall_count": max(0, len(paired) - len(restored)),
        "terminal_extension_input_count": len(terminal_extensions),
        "terminal_extension_merged_count": sum(
            1 for wall in restored if wall.get("terminal_face_recovery")),
        "terminal_extension_unmerged_count": len(unmerged_terminal_extensions),
        "excluded_compact_column_like_count": len(excluded_compact),
        "excluded_compact_column_like_candidates": excluded_compact,
        "compact_column_rule": {
            "max_length_m": compact_max_length_m,
            "max_length_to_thickness_ratio": compact_max_aspect_ratio,
        },
        "preserves_turns": True,
        "preserves_thickness_changes": True,
    }


def heal_wall_junctions(walls: list[dict], max_tolerance_m: float = 0.80) -> list[dict]:
    """沿墙自身方向延伸端点，修复双线转中线产生的半墙厚缺口。

    旧实现把端点直接投影到任意邻墙，横向分量也会一起移动；短墙因此会旋转，
    随后的正交化又会平移整面墙。这里仅处理可靠的水平/垂直连接，并且只改
    当前墙的纵向坐标。
    """
    healed = []
    for source_wall in walls:
        item = _copy_wall_lineage(source_wall)
        item["start"] = list(source_wall["start"])
        item["end"] = list(source_wall["end"])
        healed.append(item)

    for index, wall in enumerate(healed):
        own = _line_record(tuple(wall["start"]), tuple(wall["end"]))
        if own is None:
            continue
        own_axis = ("H" if _angle_delta(own["angle"], 0.0) <= 2.0 else
                    "V" if _angle_delta(own["angle"], 90.0) <= 2.0 else None)
        if own_axis is None:
            continue
        for key in ("start", "end"):
            point = wall[key]
            best = None
            for other_index, other in enumerate(healed):
                if other_index == index:
                    continue
                other_rec = _line_record(tuple(other["start"]), tuple(other["end"]))
                if other_rec is None:
                    continue
                other_axis = ("H" if _angle_delta(other_rec["angle"], 0.0) <= 2.0 else
                              "V" if _angle_delta(other_rec["angle"], 90.0) <= 2.0 else None)
                if other_axis is None or other_axis == own_axis:
                    continue
                tol = min(max_tolerance_m, max(
                    0.20,
                    (float(wall.get("thickness", 0)) +
                     float(other.get("thickness", 0))) / 2000.0 + 0.40))
                if own_axis == "H":
                    target = [other_rec["p1"][0], point[1]]
                    lo, hi = sorted((other["start"][1], other["end"][1]))
                    within_other = lo - tol <= point[1] <= hi + tol
                    distance = abs(target[0] - point[0])
                else:
                    target = [point[0], other_rec["p1"][1]]
                    lo, hi = sorted((other["start"][0], other["end"][0]))
                    within_other = lo - tol <= point[0] <= hi + tol
                    distance = abs(target[1] - point[1])
                if not within_other:
                    continue
                opposite = wall["end" if key == "start" else "start"]
                if math.dist(target, opposite) < 0.5:
                    continue
                if distance <= tol and (best is None or distance < best[0]):
                    best = (distance, target, other_index)
            if best is not None:
                before = [float(point[0]), float(point[1])]
                after = [round(best[1][0], 3), round(best[1][1], 3)]
                wall[key] = after
                if before == after:
                    continue
                constraint_wall = healed[best[2]]
                constraint_refs = _stable_source_ref_union(
                    [constraint_wall])
                constraint_source_ids = _source_ids(constraint_refs)
                if not constraint_source_ids:
                    constraint_source_ids = list(dict.fromkeys(
                        constraint_wall.get("source_segment_ids") or []))
                own_source_ids = _source_ids(
                    wall.get("source_segment_refs") or [])
                if not own_source_ids:
                    own_source_ids = list(dict.fromkeys(
                        wall.get("source_segment_ids") or []))
                adjustment = {
                    "operation": "heal_wall_junctions",
                    "endpoint": key,
                    "before": [round(value, 6) for value in before],
                    "after": list(after),
                    "distance_m": round(float(best[0]), 6),
                    "constraint_wall_id": constraint_wall.get("id"),
                    "constraint_source_segment_ids": constraint_source_ids,
                    "constraint_source_refs": constraint_refs,
                }
                wall.setdefault("endpoint_adjustments", []).append(
                    adjustment)
                wall.setdefault("derivation", []).append({
                    "operation": "heal_wall_junctions",
                    "endpoint": key,
                    "parent_source_segment_ids": own_source_ids,
                    "constraint_source_segment_ids": constraint_source_ids,
                })
    return [w for w in healed if math.dist(w["start"], w["end"]) >= 0.5]


def orthogonalize_walls(walls: list[dict], angle_tolerance_deg: float = 2.0) -> list[dict]:
    """仅将接近水平或垂直的墙扶正，保留图纸中真实的斜墙。

    轴网已经扶正时，DXF 转换、端点吸附等步骤可能遗留很小的角度误差；
    这里最多修正 ``angle_tolerance_deg``，避免把坡道、斜向剪力墙等设计意图改坏。
    """
    if angle_tolerance_deg < 0:
        raise ValueError("angle_tolerance_deg must be non-negative")
    result = []
    for wall in walls:
        item = _copy_wall_lineage(wall)
        item["start"] = list(wall["start"])
        item["end"] = list(wall["end"])
        before_start = list(item["start"])
        before_end = list(item["end"])
        x1, y1 = item["start"]
        x2, y2 = item["end"]
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
        horizontal_error = _angle_delta(angle, 0.0)
        vertical_error = _angle_delta(angle, 90.0)
        if min(horizontal_error, vertical_error) > angle_tolerance_deg:
            if math.dist(item["start"], item["end"]) >= 0.5:
                result.append(item)
            continue
        if horizontal_error <= vertical_error:
            center_y = round((y1 + y2) / 2.0, 3)
            item["start"] = [round(x1, 3), center_y]
            item["end"] = [round(x2, 3), center_y]
        else:
            center_x = round((x1 + x2) / 2.0, 3)
            item["start"] = [center_x, round(y1, 3)]
            item["end"] = [center_x, round(y2, 3)]
        if (item["start"] != before_start or item["end"] != before_end):
            source_ids = _source_ids(
                item.get("source_segment_refs") or [])
            if not source_ids:
                source_ids = list(dict.fromkeys(
                    item.get("source_segment_ids") or []))
            adjustment = {
                "operation": "orthogonalize_walls",
                "before": {
                    "start": before_start,
                    "end": before_end,
                },
                "after": {
                    "start": list(item["start"]),
                    "end": list(item["end"]),
                },
                "angle_before_deg": round(angle, 6),
            }
            item.setdefault("geometry_adjustments", []).append(adjustment)
            item.setdefault("derivation", []).append({
                "operation": "orthogonalize_walls",
                "parent_source_segment_ids": source_ids,
                "geometry_adjustment": copy.deepcopy(adjustment),
            })
        if math.dist(item["start"], item["end"]) >= 0.5:
            result.append(item)
    return result


def structural_topology_metrics(walls: list[dict], columns: list[dict],
                                wall_tolerance_m: float = 0.30,
                                column_margin_m: float = 0.30) -> dict:
    """结构体系连接率：墙端点连接邻墙或落入柱截面附近均视为有效连接。"""
    connected = 0
    fully_connected = 0
    dangling = []
    for index, wall in enumerate(walls):
        endpoint_hits = []
        for point in (wall["start"], wall["end"]):
            hit = False
            for other_index, other in enumerate(walls):
                if other_index == index:
                    continue
                tolerance = min(0.8, max(
                    wall_tolerance_m,
                    (float(wall.get("thickness", 0)) +
                     float(other.get("thickness", 0))) / 2000.0 + 0.05))
                if _point_segment_distance(point, other["start"], other["end"]) <= tolerance:
                    hit = True
                    break
            if not hit:
                for column in columns:
                    center = column.get("center", [0, 0])
                    size = column.get("size", [0.8, 0.8])
                    if (abs(point[0] - center[0]) <= size[0] / 2 + column_margin_m and
                            abs(point[1] - center[1]) <= size[1] / 2 + column_margin_m):
                        hit = True
                        break
            endpoint_hits.append(hit)
            connected += int(hit)
        fully_connected += int(all(endpoint_hits))
        if not all(endpoint_hits):
            dangling.append({"id": wall.get("id", "wall_%d" % index),
                             "ends": endpoint_hits})
    total = len(walls)
    return {
        "wall_count": total,
        "endpoint_coverage": connected / (2 * total) if total else 0.0,
        "fully_connected_rate": fully_connected / total if total else 0.0,
        "dangling": dangling,
    }
