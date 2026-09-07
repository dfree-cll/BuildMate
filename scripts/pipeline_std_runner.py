"""以仓库内精确墙算法增强标准 BIM 管线。"""
import copy
import os
import sys
from collections import defaultdict
import json
import math
from pathlib import Path
import re

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts", "bim_pipeline"))
DEFAULT_YOLO_MODEL = Path(
    ROOT, "data", "runtime", "models", "floor_plan_yolo",
    "floor_plan_yolov8_best.pt")

import pipeline_std as pipeline  # noqa: E402
from backend.engines.column_provenance import (  # noqa: E402
    enrich_columns_with_provenance)
from backend.engines.wall_symbol_semantics import (  # noqa: E402
    classify_door_leaf_swing_profiles)
from backend.engines.short_wall_replay import (  # noqa: E402
    bind_short_wall_gate_base, replay_approved_short_wall_bundle)
from backend.engines.wall_geometry import (  # noqa: E402
    audit_closed_wall_strips, audit_vector_wall_candidates,
    classify_dangling_endpoints,
    cv_room_enclosure_audit, extract_precise_walls, filter_wall_records_to_region,
    heal_wall_junctions, infer_opening_bridges, merge_collinear_walls,
    orthogonalize_walls, structural_topology_metrics, topology_with_support,
    recover_short_wall_group_spans, restore_architectural_wall_continuity,
    wall_geometry_group,
    wall_source_coverage, walls_bounds)


ARCHITECTURAL_WALL_CONTINUITY_GAP_M = 4.00
# A wall centerline produced from two faces can stop half a wall thickness
# short of a perpendicular wall.  Heal only that physical junction-sized gap;
# larger gaps remain continuity/review evidence instead of invented geometry.
ARCHITECTURAL_WALL_JUNCTION_TOLERANCE_M = 0.20


def _record_parts(record):
    return record[0], record[1], record[2] if len(record) > 2 else {}


def _filter_wall_records_by_source_layers(records):
    """Apply explicit source-layer exclusions before wall pairing."""
    raw_patterns = [str(value).strip() for value in (
        getattr(pipeline, "WALL_SOURCE_EXCLUDE_PATTERNS", []) or [])
                    if str(value).strip()]
    patterns = [value.casefold() for value in raw_patterns]
    if not patterns:
        return list(records), [], {
            "patterns": [], "input_count": len(records),
            "excluded_count": 0, "kept_count": len(records),
            "excluded_layer_counts": {}, "matched_pattern_counts": {},
            "mode": "no_explicit_source_filter",
        }
    kept, excluded = [], []
    excluded_layer_counts = defaultdict(int)
    matched_pattern_counts = defaultdict(int)
    for record in records:
        _entity, layer, provenance = _record_parts(record)
        searchable = [str(layer)]
        if isinstance(provenance, dict):
            searchable.extend([
                str(provenance.get("geometry_layer") or ""),
                str(provenance.get("source_layer") or ""),
            ])
        folded_searchable = [value.casefold() for value in searchable]
        matched_patterns = [
            raw for raw, pattern in zip(raw_patterns, patterns)
            if any(pattern in value for value in folded_searchable)
        ]
        if matched_patterns:
            excluded.append(record)
            excluded_layer_counts[str(layer)] += 1
            for pattern in matched_patterns:
                matched_pattern_counts[pattern] += 1
        else:
            kept.append(record)
    return kept, excluded, {
        "patterns": raw_patterns,
        "input_count": len(records), "excluded_count": len(excluded),
        "kept_count": len(kept),
        "excluded_layer_counts": dict(sorted(
            excluded_layer_counts.items(),
            key=lambda item: (-item[1], item[0]))),
        "matched_pattern_counts": dict(sorted(
            matched_pattern_counts.items(),
            key=lambda item: (-item[1], item[0]))),
        "mode": "explicit_source_filter",
    }


def _extract_door_vector_evidence(records, s1, bounds=None, margin_m=8.0,
                                  source_status="OK", source_error=None):
    """Convert placed door-layer DXF vectors into review-only local evidence."""
    from scripts.geometry_first_clean import (
        entity_segments, explicit_door_layer, placed_segment_id)

    rad = math.radians(float(s1.get("rot_deg", 0.0)))
    cosine, sine = math.cos(rad), math.sin(rad)
    ox, oy = [float(value) for value in s1.get("origin", [0.0, 0.0])]
    gcx, gcy = float(s1.get("gcx", 0.0)), float(s1.get("gcy", 0.0))

    def transform(point):
        x, y = float(point[0]) - gcx, float(point[1]) - gcy
        rx = x * cosine - y * sine + gcx
        ry = x * sine + y * cosine + gcy
        return [(rx - ox) / 1000.0, (ry - oy) / 1000.0]

    vectors = []
    raw_segment_count = 0
    excluded_segment_count = 0
    degenerate_segment_count = 0
    invalid_semantic_record_count = 0
    for entity, layer, provenance in map(_record_parts, records or []):
        if not explicit_door_layer(layer):
            invalid_semantic_record_count += 1
            continue
        segment_ids = list((provenance or {}).get("segment_ids") or [])
        for segment_index, segment in enumerate(entity_segments(entity)):
            raw_segment_count += 1
            source_start = [float(segment[0]), float(segment[1])]
            source_end = [float(segment[2]), float(segment[3])]
            if not all(math.isfinite(value)
                       for value in source_start + source_end):
                degenerate_segment_count += 1
                continue
            start, end = transform(source_start), transform(source_end)
            length = math.dist(start, end)
            if length <= 1e-6:
                degenerate_segment_count += 1
                continue
            if bounds is not None:
                min_x, min_y, max_x, max_y = bounds
                midpoint = [(start[0] + end[0]) / 2.0,
                            (start[1] + end[1]) / 2.0]
                if not (min_x - margin_m <= midpoint[0] <= max_x + margin_m and
                        min_y - margin_m <= midpoint[1] <= max_y + margin_m):
                    excluded_segment_count += 1
                    continue
            source_segment_id = (
                segment_ids[segment_index]
                if segment_index < len(segment_ids) else
                placed_segment_id(
                    provenance or {}, segment_index, "door_vector_segment"))
            placement_path = list(
                (provenance or {}).get("placement_path") or [])
            source_handle = (provenance or {}).get("source_entity_handle")
            identity_complete = bool(
                (provenance or {}).get("drawing_identity") and
                source_handle and
                (provenance or {}).get("source_occurrence_id") and
                (provenance or {}).get("placed_entity_id") and
                placement_path and all(
                    isinstance(item, dict) and item.get("insert_handle")
                    for item in placement_path))
            vectors.append({
                "door_vector_id": "door_vector_" + source_segment_id,
                "status": "SEMANTIC_EVIDENCE_ONLY",
                "auto_action": "NONE",
                "geometry_source": "DXF_VECTOR",
                "semantic_role": "EXPLICIT_DOOR_VECTOR",
                "layer": layer,
                "geometry_layer": (provenance or {}).get(
                    "geometry_layer", layer),
                "entity_type": entity.dxftype(),
                "entity_handle": source_handle,
                "drawing_identity": (provenance or {}).get(
                    "drawing_identity"),
                "source_occurrence_id": (provenance or {}).get(
                    "source_occurrence_id"),
                "placed_entity_id": (provenance or {}).get(
                    "placed_entity_id"),
                "structural_occurrence_id": (provenance or {}).get(
                    "structural_occurrence_id"),
                "source_segment_id": source_segment_id,
                "segment_index": segment_index,
                "placement_path": placement_path,
                "identity_is_complete": identity_complete,
                "source_start_dxf_mm": [round(value, 6)
                                        for value in source_start],
                "source_end_dxf_mm": [round(value, 6)
                                      for value in source_end],
                "start": [round(value, 6) for value in start],
                "end": [round(value, 6) for value in end],
                "length_m": round(length, 6),
                "coordinate_system": "PROJECT_LOCAL_M",
                "curve_derivation": (
                    "DXF_ARC_CHORD" if entity.dxftype() == "ARC" else
                    "EXACT_DXF_SEGMENT"),
            })
    status = ("ERROR" if source_status == "ERROR" else
              "OK" if vectors else "NO_EXPLICIT_DOOR_VECTOR")
    return {
        "status": status,
        "semantic_evidence_only": True,
        "geometry_authority": "DXF_VECTOR",
        "auto_action": "NONE",
        "source_error": source_error,
        "source_record_count": len(records or []),
        "raw_segment_count": raw_segment_count,
        "explicit_door_vector_count": len(vectors),
        "excluded_out_of_region_segment_count": excluded_segment_count,
        "degenerate_segment_count": degenerate_segment_count,
        "invalid_semantic_record_count": invalid_semantic_record_count,
        "identity_incomplete_count": sum(
            not item["identity_is_complete"] for item in vectors),
        "vectors": vectors,
        "note": ("Explicit door-layer DXF vectors are semantic evidence only; "
                 "they do not create walls, alter endpoints, lower a Gate, or "
                 "trigger Revit."),
    }


def _associate_endpoint_door_vectors(endpoint_review, door_vectors,
                                     tolerance_m=1e-4):
    """Join endpoint matches back to one exact placed DXF vector segment."""
    exact_count = 0
    ambiguous_count = 0
    missing_count = 0
    for candidate in endpoint_review.get("candidates", []):
        match = candidate.get("door_vector_match")
        if not isinstance(match, dict):
            continue
        point = candidate.get("point")
        closest = match.get("closest_point")
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            missing_count += 1
            match["association_status"] = "MISSING"
            continue
        matches = []
        for vector in door_vectors:
            if (vector.get("entity_handle") != match.get("entity_handle") or
                    vector.get("layer") != match.get("layer")):
                continue
            start, end = vector.get("start"), vector.get("end")
            if not start or not end:
                continue
            dx, dy = end[0] - start[0], end[1] - start[1]
            length_sq = dx * dx + dy * dy
            ratio = (0.0 if length_sq <= 1e-12 else max(0.0, min(
                1.0, ((point[0] - start[0]) * dx +
                      (point[1] - start[1]) * dy) / length_sq)))
            target = [start[0] + ratio * dx, start[1] + ratio * dy]
            distance = math.dist(point[:2], target)
            if abs(distance - float(match.get("distance_m", distance))) > tolerance_m:
                continue
            if (isinstance(closest, (list, tuple)) and len(closest) >= 2 and
                    math.dist(target, closest[:2]) > tolerance_m):
                continue
            matches.append(vector)
        if len(matches) == 1:
            vector = matches[0]
            match.update({
                "association_status": "EXACT",
                "door_vector_id": vector.get("door_vector_id"),
                "source_segment_id": vector.get("source_segment_id"),
                "drawing_identity": vector.get("drawing_identity"),
                "source_occurrence_id": vector.get("source_occurrence_id"),
                "placed_entity_id": vector.get("placed_entity_id"),
                "identity_is_complete": vector.get("identity_is_complete"),
            })
            exact_count += 1
        elif matches:
            match["association_status"] = "AMBIGUOUS"
            match["matching_door_vector_ids"] = sorted(
                str(item.get("door_vector_id")) for item in matches)
            candidate.setdefault("blockers", []).append(
                "door_vector_exact_association_ambiguous")
            ambiguous_count += 1
        else:
            match["association_status"] = "MISSING"
            candidate.setdefault("blockers", []).append(
                "door_vector_exact_association_missing")
            missing_count += 1
    endpoint_review["door_vector_association"] = {
        "status": ("COMPLETE" if not ambiguous_count and not missing_count
                   else "ERROR"),
        "exact_count": exact_count,
        "ambiguous_count": ambiguous_count,
        "missing_count": missing_count,
    }
    return endpoint_review


def _environment_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def audited_step0(document):
    """Attach a full-render CV/YOLO audit to the exact DXF being cleaned."""
    cleaned = _original_step0(document)
    model_configured = (
        bool(os.environ.get("DRAWING_YOLO_MODEL_PATH", "").strip()) or
        DEFAULT_YOLO_MODEL.is_file()
    )
    audit_enabled = (
        model_configured or
        _environment_flag("DRAWING_YOLO_REQUIRED") or
        _environment_flag("DRAWING_FULL_CV_AUDIT", default=True)
    )
    if not audit_enabled:
        cleaned["full_drawing_audit_status"] = "NOT_RUN"
        return cleaned

    source = Path(document.filename or "")
    if not source.is_file():
        cleaned.update({
            "full_drawing_audit_status": "ERROR",
            "cleaning_decision": {
                "status": "REVIEW",
                "allow_modeling": False,
                "reasons": ["full drawing audit could not resolve the source DXF"],
            },
        })
        return cleaned

    configured_output = os.environ.get(
        "DRAWING_CLEANING_AUDIT_DIR", "").strip()
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.stem)
    output_dir = (Path(configured_output) if configured_output else
                  Path(ROOT) / "data" / "runtime" / "cleaning" / safe_stem)
    try:
        from scripts.audit_drawing_cleaning import audit
        audit_result = audit(source, output_dir, document=document)
    except Exception as exc:
        cleaned.update({
            "full_drawing_audit_status": "ERROR",
            "cleaning_decision": {
                "status": "REVIEW",
                "allow_modeling": False,
                "reasons": [f"full drawing audit failed: {str(exc)[:240]}"],
            },
        })
        return cleaned

    cleaned.update({
        "full_drawing_audit_status": "OK",
        "full_drawing_audit_dir": str(output_dir),
        "source_identity": {
            "path": audit_result.get("source"),
            "sha256": audit_result.get("source_sha256"),
            "size_bytes": audit_result.get("source_size_bytes"),
        },
        "full_cv_audit": audit_result.get("cv_auxiliary"),
        "drawing_audit_cache": audit_result.get("audit_cache"),
        "yolo_wall_audit": audit_result.get("yolo_wall_audit"),
        "yolo_cv_fusion": audit_result.get("yolo_cv_fusion"),
        "dxf_review_candidates": audit_result.get("dxf_review_candidates"),
        "known_non_wall_vector_candidates": audit_result.get(
            "known_non_wall_vector_candidates"),
        "cleaning_decision": audit_result.get("cleaning_decision"),
    })
    return cleaned


def _grid_bounds(s1):
    x_values = [float(item["coord"]) for item in s1["grid"].get("x_axes", [])]
    y_values = [float(item["coord"]) for item in s1["grid"].get("y_axes", [])]
    if not x_values or not y_values:
        return None
    return min(x_values), min(y_values), max(x_values), max(y_values)


def _select_structural_occurrence(records, s1):
    groups = defaultdict(list)
    for record in records:
        _entity, layer, provenance = _record_parts(record)
        if wall_geometry_group(layer) != "S":
            continue
        occurrence = provenance.get("structural_occurrence_id") or "legacy-unplaced"
        groups[occurrence].append(record)

    bounds = _grid_bounds(s1)
    candidates = []
    for occurrence, source in groups.items():
        walls = extract_precise_walls(
            source, s1["origin"][0], s1["origin"][1], s1["rot_deg"],
            s1["gcx"], s1["gcy"], wall_groups=("S",))
        paired = [wall for wall in walls if wall.get("paired")]
        total_length = sum(
            ((wall["end"][0] - wall["start"][0]) ** 2 +
             (wall["end"][1] - wall["start"][1]) ** 2) ** 0.5
            for wall in paired)
        inside_length = 0.0
        if bounds:
            min_x, min_y, max_x, max_y = bounds
            for wall in paired:
                mid_x = (wall["start"][0] + wall["end"][0]) / 2.0
                mid_y = (wall["start"][1] + wall["end"][1]) / 2.0
                if (min_x - 3.0 <= mid_x <= max_x + 3.0 and
                        min_y - 3.0 <= mid_y <= max_y + 3.0):
                    inside_length += ((wall["end"][0] - wall["start"][0]) ** 2 +
                                      (wall["end"][1] - wall["start"][1]) ** 2) ** 0.5
        inside_rate = inside_length / total_length if total_length else 0.0
        topology = structural_topology_metrics(paired, s1.get("cols", []))
        # Coordinate agreement dominates; column/wall support breaks ties
        # between repeated translated instances that partly overlap the grid.
        score = inside_rate * 0.75 + topology["endpoint_coverage"] * 0.25
        candidates.append({
            "occurrence_id": occurrence,
            "source_records": source,
            "source_count": len(source),
            "paired_count": len(paired),
            "inside_rate": inside_rate,
            "column_wall_endpoint_coverage": topology["endpoint_coverage"],
            "score": score,
        })
    candidates.sort(key=lambda item: (item["score"], item["inside_rate"],
                                      item["paired_count"]), reverse=True)
    if not candidates:
        return [], {"valid": False, "reason": "主平面无 S-WALL 实例", "candidates": []}
    selected = candidates[0]
    second_score = candidates[1]["score"] if len(candidates) > 1 else -1.0
    valid = bool(
        selected["paired_count"] > 0 and selected["inside_rate"] >= 0.60 and
        (len(candidates) == 1 or selected["score"] - second_score >= 0.03))
    diagnostics = {
        "valid": valid,
        "selected_occurrence_id": selected["occurrence_id"],
        "score_gap": round(selected["score"] - second_score, 4),
        "reason": ("与轴网/柱坐标唯一对齐" if valid else
                   "墙柱实例与轴网/柱未形成唯一可靠对齐"),
        "candidates": [{key: (round(value, 4) if isinstance(value, float) else value)
                        for key, value in item.items() if key != "source_records"}
                       for item in candidates],
    }
    return selected["source_records"] if valid else [], diagnostics


def classify_irregular_profiles(profile_audit, s1, columns, structural_walls,
                                selected_occurrence_id=None):
    """Attach exact plan-coordinate evidence without guessing column semantics."""
    result = dict(profile_audit or {})
    rad = math.radians(float(s1.get("rot_deg", 0.0)))
    cosine, sine = math.cos(rad), math.sin(rad)
    ox, oy = [float(value) for value in s1.get("origin", [0.0, 0.0])]
    gcx, gcy = float(s1.get("gcx", 0.0)), float(s1.get("gcy", 0.0))

    def transform(point):
        x, y = float(point[0]) - gcx, float(point[1]) - gcy
        rx = x * cosine - y * sine + gcx
        ry = x * sine + y * cosine + gcy
        return [(rx - ox) / 1000.0, (ry - oy) / 1000.0]

    def point_segment_distance(point, start, end):
        dx = float(end[0]) - float(start[0])
        dy = float(end[1]) - float(start[1])
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            return math.dist(point, start)
        ratio = ((float(point[0]) - float(start[0])) * dx +
                 (float(point[1]) - float(start[1])) * dy) / length_sq
        ratio = max(0.0, min(1.0, ratio))
        projection = [float(start[0]) + ratio * dx,
                      float(start[1]) + ratio * dy]
        return math.dist(point, projection)

    classified = []
    raw_candidates = list(result.get("candidates", []))
    occurrence_candidates = [
        item for item in raw_candidates
        if (not selected_occurrence_id or
            not (item.get("structural_occurrence_id") or
                 item.get("source_occurrence_id")) or
            (item.get("structural_occurrence_id") or
             item.get("source_occurrence_id")) == selected_occurrence_id)
    ]
    seen = {}
    deduplicated = []
    for source in occurrence_candidates:
        source_points = source.get("points_dxf_mm") or []
        center = source.get("center_dxf_mm")
        if not center and source_points:
            center = [
                sum(float(point[0]) for point in source_points) / len(source_points),
                sum(float(point[1]) for point in source_points) / len(source_points),
            ]
        center = center or [0.0, 0.0]
        source_identity = source.get("entity_handle") or tuple(sorted(
            (round(float(point[0]), 1), round(float(point[1]), 1))
            for point in source_points))
        duplicate_key = (
            (source.get("structural_occurrence_id") or
             source.get("source_occurrence_id")),
            source.get("placed_entity_id") or source_identity,
            round(float(center[0]), 1), round(float(center[1]), 1),
        )
        if duplicate_key in seen:
            existing = seen[duplicate_key]
            existing["duplicate_evidence_count"] += 1
            handle = source.get("source_block_handle")
            if handle and handle not in existing["source_block_handles"]:
                existing["source_block_handles"].append(handle)
            continue
        item = dict(source)
        item["duplicate_evidence_count"] = 1
        item["source_block_handles"] = [source["source_block_handle"]] if (
            source.get("source_block_handle")) else []
        seen[duplicate_key] = item
        deduplicated.append(item)

    for item in deduplicated:
        item["geometry_source"] = "DXF_VECTOR"
        points = [transform(point) for point in item.get("points_dxf_mm", [])]
        if len(points) < 3:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        center = [sum(xs) / len(xs), sum(ys) / len(ys)]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        item["center_local_m"] = [round(value, 4) for value in center]
        item["points_local_m"] = [
            [round(value, 4) for value in point] for point in points]
        item["bbox_local_m"] = [round(value, 4) for value in bbox]

        column_matches = []
        for column_index, column in enumerate(columns):
            column_center = column.get("center", [0.0, 0.0])
            size = column.get("size", [0.0, 0.0])
            column_bbox = [
                float(column_center[0]) - float(size[0]) / 2.0,
                float(column_center[1]) - float(size[1]) / 2.0,
                float(column_center[0]) + float(size[0]) / 2.0,
                float(column_center[1]) + float(size[1]) / 2.0,
            ]
            overlap = not (
                bbox[2] < column_bbox[0] or bbox[0] > column_bbox[2] or
                bbox[3] < column_bbox[1] or bbox[1] > column_bbox[3])
            if overlap:
                column_matches.append((
                    math.dist(center, column_center), column_index, column))
        column_matches.sort(key=lambda match: match[0])

        sample_points = list(points)
        sample_points.extend([
            [(first[0] + second[0]) / 2.0,
             (first[1] + second[1]) / 2.0]
            for first, second in zip(points, points[1:] + points[:1])
        ])
        wall_matches = []
        for wall in structural_walls:
            distances = [point_segment_distance(
                point, wall["start"], wall["end"]) for point in sample_points]
            allowed = float(wall.get("thickness", 0.0)) / 2000.0 + 0.35
            supported = sum(distance <= allowed for distance in distances)
            if supported:
                wall_matches.append((supported, min(distances), wall))
        wall_matches.sort(key=lambda match: (-match[0], match[1]))
        supported_points = set()
        for point_index, point in enumerate(sample_points):
            if any(point_segment_distance(
                    point, wall["start"], wall["end"]) <=
                   float(wall.get("thickness", 0.0)) / 2000.0 + 0.35
                   for wall in structural_walls):
                supported_points.add(point_index)
        wall_support_ratio = (len(supported_points) / len(sample_points)
                              if sample_points else 0.0)
        item["structural_wall_support_ratio"] = round(wall_support_ratio, 4)
        item["structural_wall_matches"] = [{
            "wall_id": match[2].get("id"),
            "supported_sample_count": match[0],
            "minimum_distance_m": round(match[1], 4),
        } for match in wall_matches[:4]]

        if wall_support_ratio >= 0.50:
            item["decision_reason"] = "matched_structural_wall_hatch"
            item["status"] = "REJECTED_BY_RULE"
        elif column_matches:
            distance, column_index, column = column_matches[0]
            item["decision_reason"] = "spatially_overlaps_extracted_column"
            item["column_match"] = {
                "column_id": column.get("id") or f"column_{column_index}",
                "distance_m": round(distance, 4),
            }
            item["status"] = "NEEDS_REVIEW"
        else:
            item["decision_reason"] = "unresolved_irregular_structural_hatch"
            item["status"] = "NEEDS_REVIEW"
        classified.append(item)

    reason_counts = defaultdict(int)
    for item in classified:
        reason_counts[item["decision_reason"]] += 1
    needs_review_count = sum(
        item.get("status") == "NEEDS_REVIEW" for item in classified)
    rejected_count = sum(
        item.get("status") == "REJECTED_BY_RULE" for item in classified)
    result.update({
        "status": "REVIEW" if needs_review_count else "PASS",
        "raw_candidate_count": len(raw_candidates),
        "selected_occurrence_id": selected_occurrence_id,
        "selected_occurrence_raw_candidate_count": len(occurrence_candidates),
        "excluded_other_occurrence_candidate_count": (
            len(raw_candidates) - len(occurrence_candidates)),
        "deduplicated_candidate_count": len(classified),
        "candidate_count": len(classified),
        "needs_review_count": needs_review_count,
        "rejected_by_rule_count": rejected_count,
        "reason_counts": dict(sorted(reason_counts.items())),
        "candidates": classified,
        "note": ("Only the selected structural occurrence is retained and "
                 "duplicate block references are collapsed. Profiles supported "
                 "by extracted structural walls are rejected as column "
                 "candidates; unresolved profiles remain review-only."),
    })
    return result


def structural_step2(cleaned, s1):
    """结构墙柱图：全部可靠 S-WALL 双线中线归入结构墙，并限定本层轴网范围。"""
    out = _original_step2(cleaned, s1)
    records = (cleaned.get("wall_source_records", [])
               if isinstance(cleaned, dict) else cleaned)
    selected_records, selection = _select_structural_occurrence(records, s1)
    selected_occurrence_id = (
        selection.get("selected_occurrence_id") if selection.get("valid")
        else "")
    columns, column_provenance_audit = enrich_columns_with_provenance(
        out.get("cols", []),
        (cleaned.get("vector_source_records", [])
         if isinstance(cleaned, dict) else []),
        origin_dxf_mm=s1.get("origin", []),
        rotation_deg=s1.get("rot_deg", 0.0),
        grid_center_dxf_mm=[s1.get("gcx"), s1.get("gcy")],
        selected_structural_occurrence_id=selected_occurrence_id,
    )
    out["cols"] = columns
    out["column_provenance_audit"] = column_provenance_audit
    walls = extract_precise_walls(
        selected_records, s1["origin"][0], s1["origin"][1], s1["rot_deg"], s1["gcx"], s1["gcy"],
        wall_groups=("S",))
    # The selected S-WALL block may contain a second grid or an extension.
    # Grid bounds are positioning evidence, not a deletion boundary.
    walls = [w for w in walls if w.get("paired")]
    walls = orthogonalize_walls(walls)
    # 合并会产生新的端点，单轮吸附不足；迭代三轮至拓扑基本稳定。
    for _ in range(3):
        walls = merge_collinear_walls(walls)
        walls = heal_wall_junctions(walls)
    walls = merge_collinear_walls(walls)
    # 节点吸附可能移动单个端点；最终锁定正交方向后不再改动端点。
    walls = orthogonalize_walls(walls)
    for i, wall in enumerate(walls):
        wall["id"] = "shear_%d" % i
        wall["confidence"] = pipeline.CONF["HIGH"]
        wall["source"] = ["drawing", "S-WALL", "double-line-center"]
        wall["warnings"] = []
    out["shear_walls"] = walls
    out["structural_topology"] = structural_topology_metrics(walls, out["cols"])
    out["wall_occurrence_selection"] = selection
    out["source_drawing_identity"] = (
        cleaned.get("source_drawing_identity")
        if isinstance(cleaned, dict) else None)
    profile_audit = classify_irregular_profiles(
        cleaned.get("irregular_profile_audit", {}), s1, out["cols"], walls,
        selection.get("selected_occurrence_id") if selection.get("valid") else None)
    profile_audit["extracted_column_count"] = len(out["cols"])
    profile_audit["extracted_profile_column_count"] = sum(
        bool(column.get("profile")) for column in out["cols"])
    profile_audit["extracted_rectangular_column_count"] = sum(
        not column.get("profile") for column in out["cols"])
    audit_dir = (cleaned.get("full_drawing_audit_dir")
                 if isinstance(cleaned, dict) else None)
    if audit_dir and profile_audit.get("candidates"):
        try:
            from scripts.audit_drawing_cleaning import (
                render_irregular_profile_review_overlay)
            overlay = render_irregular_profile_review_overlay(
                out["cols"], walls, profile_audit,
                Path(audit_dir) / "12_irregular_profile_review.png")
            profile_audit["review_overlay"] = overlay["path"]
        except (OSError, ValueError) as exc:
            profile_audit["review_overlay_error"] = str(exc)[:300]
    out["irregular_profile_audit"] = profile_audit
    if isinstance(cleaned, dict):
        cleaned["irregular_profile_audit"] = profile_audit
    out["conflicts"] = []
    out["traceability"] = _geometry_traceability_audit(
        cleaned if isinstance(cleaned, dict) else {}, out,
        {"architecture_applicable": False, "walls": []})
    return out


def _architecture_wall_analysis(
        seed_walls, architecture_records, vector_records,
        closed_profile_seed, cleaned, s1, s2, door_vector_evidence,
        audit_dir=None):
    """Recompute every architecture-derived audit from one fresh wall set."""
    walls = copy.deepcopy(seed_walls)
    vector_audit = audit_vector_wall_candidates(
        vector_records, s1["origin"][0], s1["origin"][1], s1["rot_deg"],
        s1["gcx"], s1["gcy"], walls + s2.get("shear_walls", []),
        s2.get("cols", []))
    # A topology-confirmed pair is useful review evidence, but it is not
    # sufficient for formal quantity/model output when its source lineage is
    # absent.  Keep those candidates in the audit and require complete source
    # refs before promoting them into the wall network; otherwise they create
    # false concrete walls and fail the traceability gate.
    formally_promoted = []
    excluded_promotions = []
    for candidate in vector_audit.get("promoted_walls", []):
        if (candidate.get("source_segment_refs") and
                candidate.get("source_segment_ids")):
            formally_promoted.append(copy.deepcopy(candidate))
        else:
            excluded_promotions.append(str(candidate.get("id") or ""))
    vector_audit["formal_model_promoted_count"] = len(formally_promoted)
    vector_audit["formal_model_excluded_count"] = len(excluded_promotions)
    vector_audit["formal_model_excluded_ids"] = excluded_promotions
    walls.extend(formally_promoted)
    normal_index = 0
    for wall in walls:
        if wall.get("approved_proposal_id"):
            wall.setdefault("confidence", pipeline.CONF["HIGH"])
            wall.setdefault("source", [
                "drawing", "architectural-wall-layer",
                "approved-short-wall-replay"])
            wall.setdefault("warnings", [])
            continue
        wall["id"] = "wall_%d" % normal_index
        normal_index += 1

    closed_profile_audit, wall_symbol_semantics = (
        classify_door_leaf_swing_profiles(
            closed_profile_seed,
            (cleaned.get("wall_symbol_source_records", [])
             if isinstance(cleaned, dict) else []),
            s1, wall_network=walls + s2.get("shear_walls", [])))
    closed_profile_audit["door_leaf_swing_audit"] = wall_symbol_semantics
    opening_bridges = infer_opening_bridges(walls)
    source_coverage = wall_source_coverage(
        architecture_records, walls, s1["origin"][0], s1["origin"][1],
        s1["rot_deg"], s1["gcx"], s1["gcy"],
        opening_bridges=opening_bridges,
        closed_profile_audit=closed_profile_audit)
    closed_profile_audit = dict(
        source_coverage.get("closed_wall_profile_audit") or
        closed_profile_audit)
    if ("association" not in closed_profile_audit and
            source_coverage.get("closed_profile_association") is not None):
        closed_profile_audit["association"] = source_coverage[
            "closed_profile_association"]

    if audit_dir and closed_profile_audit.get("candidate_count"):
        try:
            from scripts.audit_drawing_cleaning import (
                render_closed_wall_profile_review_overlay)
            closed_profile_audit["review_overlay"] = (
                render_closed_wall_profile_review_overlay(
                    walls, closed_profile_audit,
                    Path(audit_dir) / "13_closed_wall_profile_review.png"))
            source_coverage["closed_wall_profile_audit"] = (
                closed_profile_audit)
        except (OSError, ValueError) as exc:
            closed_profile_audit["review_overlay"] = {
                "status": "ERROR", "error": str(exc)[:240]}
    if audit_dir:
        try:
            from scripts.audit_drawing_cleaning import (
                render_wall_source_review_overlay)
            source_coverage["review_overlay"] = (
                render_wall_source_review_overlay(
                    walls, source_coverage,
                    Path(audit_dir) / "09_semantic_wall_source_review.png"))
        except Exception as exc:
            source_coverage["review_overlay"] = {
                "status": "ERROR", "error": str(exc)[:240]}
    short_wall_proposal_audit = dict(
        source_coverage.get("short_wall_proposal_audit") or {})
    if (audit_dir and short_wall_proposal_audit.get("component_count", 0)):
        try:
            from scripts.audit_drawing_cleaning import (
                render_short_wall_proposal_overlay)
            short_wall_proposal_audit["review_overlay"] = (
                render_short_wall_proposal_overlay(
                    walls, short_wall_proposal_audit,
                    Path(audit_dir) / "15_short_wall_proposal_review.png"))
        except Exception as exc:
            short_wall_proposal_audit["review_overlay"] = {
                "status": "ERROR", "error": str(exc)[:240]}
        source_coverage["short_wall_proposal_audit"] = (
            short_wall_proposal_audit)

    metrics = topology_with_support(
        walls, s2.get("shear_walls", []), s2.get("cols", []),
        opening_bridges)
    endpoint_review = classify_dangling_endpoints(
        metrics["dangling"], walls, s2.get("shear_walls", []),
        opening_bridges, source_coverage,
        door_vectors=door_vector_evidence["vectors"])
    endpoint_review = _associate_endpoint_door_vectors(
        endpoint_review, door_vector_evidence["vectors"])
    if audit_dir and endpoint_review.get("candidate_count"):
        try:
            from scripts.audit_drawing_cleaning import (
                render_wall_endpoint_review_overlay)
            endpoint_review["review_overlay"] = (
                render_wall_endpoint_review_overlay(
                    walls + s2.get("shear_walls", []), endpoint_review,
                    Path(audit_dir) / "11_wall_endpoint_review.png"))
        except Exception as exc:
            endpoint_review["review_overlay"] = {
                "status": "ERROR", "error": str(exc)[:240]}
    cv_topology = cv_room_enclosure_audit(
        walls + s2.get("shear_walls", []), opening_bridges)
    return {
        "walls": walls,
        "closed_rate": metrics["fully_connected_rate"],
        "endpoint_coverage": metrics["endpoint_coverage"],
        "dangling": metrics["dangling"],
        "endpoint_review": endpoint_review,
        "door_vector_evidence": door_vector_evidence,
        "wall_symbol_semantics": wall_symbol_semantics,
        "room_polygon_count": metrics["room_polygon_count"],
        "room_polygon_area_m2": metrics["room_polygon_area_m2"],
        "polygonize_status": metrics["polygonize_status"],
        "opening_bridges": opening_bridges,
        "cv_topology_audit": cv_topology,
        "vector_candidate_audit": vector_audit,
        "source_coverage": source_coverage,
        "closed_wall_profile_audit": closed_profile_audit,
    }


def _architecture_occurrence_scope(records, plan_selection):
    """Bind architectural evidence to one role-specific placed occurrence."""
    selected_plan = ((plan_selection or {}).get("selected")
                     if isinstance(plan_selection, dict) else None)
    selected_plan_handle = (str(selected_plan.get("insert_handle") or "")
                            if isinstance(selected_plan, dict) else "")
    occurrence_ids = set()
    missing_occurrence_count = 0
    selected_plan_root_mismatch_count = 0
    for record in records:
        _entity, _layer, provenance = _record_parts(record)
        occurrence_id = str(
            (provenance or {}).get("structural_occurrence_id") or "")
        if occurrence_id:
            occurrence_ids.add(occurrence_id)
        else:
            missing_occurrence_count += 1
        placement_path = (provenance or {}).get("placement_path")
        root_handle = (
            str(placement_path[0].get("insert_handle") or "")
            if isinstance(placement_path, list) and placement_path and
            isinstance(placement_path[0], dict) else "")
        if (not selected_plan_handle or
                root_handle != selected_plan_handle):
            selected_plan_root_mismatch_count += 1
    occurrence_ids = sorted(occurrence_ids)
    valid = bool(
        records and selected_plan_handle and len(occurrence_ids) == 1 and
        missing_occurrence_count == 0 and
        selected_plan_root_mismatch_count == 0)
    return {
        "valid": valid,
        "selected_occurrence_id": (
            occurrence_ids[0] if len(occurrence_ids) == 1 else None),
        "occurrence_ids": occurrence_ids,
        "source_record_count": len(records),
        "missing_occurrence_count": missing_occurrence_count,
        "selected_plan_insert_handle": selected_plan_handle or None,
        "selected_plan_root_mismatch_count":
            selected_plan_root_mismatch_count,
        "reason": (
            "建筑墙证据唯一归属当前主平面实例" if valid else
            "建筑墙证据未唯一归属当前主平面实例"),
    }


def _short_wall_gate_base_review_ir(cleaned, s1, s2, analysis):
    source = dict((cleaned.get("source_identity") or {})
                  if isinstance(cleaned, dict) else {})
    if not source.get("sha256") and isinstance(cleaned, dict):
        source["sha256"] = cleaned.get("source_drawing_identity")
    selection = dict(s2.get("wall_occurrence_selection") or {})
    selected_occurrence = selection.get("selected_occurrence_id")
    architecture_selection = dict(
        analysis.get("architecture_occurrence_selection") or {})
    review_ir = {
        "schema_version": "buildmate.review-ir/1.0",
        "source": source,
        "plan_selection": (cleaned.get("plan_selection")
                           if isinstance(cleaned, dict) else None),
        "coordinate_system": {
            "unit": "m", "origin_dxf_mm": s1.get("origin"),
            "rotation_deg": s1.get("rot_deg"),
            "grid_center_dxf_mm": [s1.get("gcx"), s1.get("gcy")],
        },
        "traceability": {
            "selected_structural_occurrence_id": selected_occurrence,
            "selected_architectural_occurrence_id":
                architecture_selection.get("selected_occurrence_id"),
        },
        "wall_occurrence_selection": selection,
        "architecture_occurrence_selection": architecture_selection,
        "geometry": {
            "architectural_walls": copy.deepcopy(analysis.get("walls", [])),
        },
        "review_candidates": {
            "semantic_wall_source_units": copy.deepcopy(
                (analysis.get("source_coverage") or {}).get(
                    "review_units", [])),
            "short_wall_proposals": copy.deepcopy(
                (analysis.get("source_coverage") or {}).get(
                    "short_wall_proposal_audit", {})),
        },
    }
    return bind_short_wall_gate_base(review_ir)


def _load_explicit_short_wall_review_ir(path_value):
    path = Path(str(path_value))
    try:
        if not path.is_file():
            return None, ["approved_review_ir_not_a_file"]
        if path.stat().st_size > 128 * 1024 * 1024:
            return None, ["approved_review_ir_too_large"]
        with path.open(encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError, RecursionError):
        return None, ["approved_review_ir_unreadable"]
    if not isinstance(payload, dict):
        return None, ["approved_review_ir_invalid"]
    return payload, []


def _restore_architectural_wall_continuity(walls):
    """Restore whole walls across openings/columns without promoting guesses.

    Paired faces carry measured centerline and thickness.  A terminal face
    extension is allowed to merge back into its supported paired run; other
    unpaired single-line proposals stay reviewable. Collinear merging keeps
    real turns and thickness changes as separate wall instances.
    """
    gap_m = float(getattr(
        pipeline, "ARCHITECTURAL_WALL_CONTINUITY_GAP_M",
        ARCHITECTURAL_WALL_CONTINUITY_GAP_M))
    return restore_architectural_wall_continuity(
        walls, gap_m=gap_m, merge_terminal_extensions=True)


def precise_step4(cleaned, s1, s2):
    prefix = s1.get("prefix") or ""
    structural_only = ("墙柱" in prefix or "wall_col" in prefix.lower() or
                       "wall-column" in prefix.lower())
    records = (cleaned.get("wall_source_records", [])
               if isinstance(cleaned, dict) else cleaned)
    architecture_records = [
        record for record in records
        if wall_geometry_group(_record_parts(record)[1]) == "A"]
    architecture_records, source_layer_excluded_records, source_filter = (
        _filter_wall_records_by_source_layers(architecture_records))
    # Use the structural wall envelope as the primary geometry scope. The
    # grid envelope can be narrower than the actual plan (especially at
    # perimeter/service rooms), silently discarding valid partition walls
    # before pairing. Fall back to the grid envelope when no structural bounds
    # are available.
    selected_bounds = walls_bounds(s2.get("shear_walls", [])) or _grid_bounds(s1)
    door_vector_evidence = _extract_door_vector_evidence(
        (cleaned.get("door_vector_source_records", [])
         if isinstance(cleaned, dict) else []),
        s1, selected_bounds,
        source_status=(cleaned.get("door_vector_source_status", "OK")
                       if isinstance(cleaned, dict) else "OK"),
        source_error=(cleaned.get("door_vector_source_error")
                      if isinstance(cleaned, dict) else None))
    if selected_bounds:
        architecture_records, region_excluded_records = filter_wall_records_to_region(
            architecture_records, s1["origin"][0], s1["origin"][1],
            s1["rot_deg"], s1["gcx"], s1["gcy"], selected_bounds,
            margin_m=8.0)
    else:
        region_excluded_records = list(architecture_records)
        architecture_records = []
    excluded_records = source_layer_excluded_records + region_excluded_records
    architecture_source_count = len(architecture_records)
    architecture_occurrence_selection = _architecture_occurrence_scope(
        architecture_records,
        (cleaned.get("plan_selection")
         if isinstance(cleaned, dict) else None))
    closed_profile_seed = audit_closed_wall_strips(
        architecture_records, s1["origin"][0], s1["origin"][1],
        s1["rot_deg"], s1["gcx"], s1["gcy"], wall_groups=("A",))
    primary_walls = extract_precise_walls(
        architecture_records, s1["origin"][0], s1["origin"][1],
        s1["rot_deg"], s1["gcx"], s1["gcy"], wall_groups=("A",),
        recover_terminal_remainders=True)
    primary_walls, continuity_restoration = (
        _restore_architectural_wall_continuity(primary_walls))
    primary_walls = heal_wall_junctions(
        primary_walls,
        max_tolerance_m=ARCHITECTURAL_WALL_JUNCTION_TOLERANCE_M)
    for index, wall in enumerate(primary_walls):
        wall["id"] = "wall_%d" % index
        wall["confidence"] = (pipeline.CONF["HIGH"] if wall.get("paired")
                              else pipeline.CONF["MEDIUM"])
        wall["source"] = [
            "drawing", "architectural-wall-layer", "overlap-paired-v2"]
        wall["warnings"] = ([] if wall.get("paired") else
                            ["未配对单线, 厚度取配对众数"])

    vector_records = (cleaned.get("vector_source_records", [])
                      if isinstance(cleaned, dict) else [])
    vector_records, vector_source_layer_excluded_records, vector_source_filter = (
        _filter_wall_records_by_source_layers(vector_records))
    if selected_bounds:
        vector_records, vector_region_excluded = filter_wall_records_to_region(
            vector_records, s1["origin"][0], s1["origin"][1],
            s1["rot_deg"], s1["gcx"], s1["gcy"], selected_bounds,
            margin_m=8.0)
    else:
        vector_region_excluded = list(vector_records)
        vector_records = []
    vector_excluded = (vector_source_layer_excluded_records +
                       vector_region_excluded)
    audit_dir = (cleaned.get("full_drawing_audit_dir")
                 if isinstance(cleaned, dict) else None)
    base_analysis = _architecture_wall_analysis(
        primary_walls, architecture_records, vector_records,
        closed_profile_seed, cleaned, s1, s2, door_vector_evidence,
        audit_dir=audit_dir)
    base_analysis["architecture_occurrence_selection"] = copy.deepcopy(
        architecture_occurrence_selection)
    final_analysis = base_analysis
    # A converted wall can be split into short paired face runs around a
    # doorway. Recover only spans with a unique measured end-cap on both
    # sides; unsupported/ambiguous evidence remains review-only.
    fragmented_face_recovery = recover_short_wall_group_spans(
        base_analysis.get("source_coverage") or {},
        max_gap_m=ARCHITECTURAL_WALL_CONTINUITY_GAP_M,
        existing_walls=primary_walls,
        door_vectors=(base_analysis.get("door_vector_evidence") or {}).get(
            "vectors", []))
    if fragmented_face_recovery.get("candidates"):
        recovered_walls = list(primary_walls) + [
            copy.deepcopy(item) for item in
            fragmented_face_recovery["candidates"]]
        recovered_walls, recovered_continuity = (
            _restore_architectural_wall_continuity(recovered_walls))
        recovered_walls = heal_wall_junctions(
            recovered_walls,
            max_tolerance_m=ARCHITECTURAL_WALL_JUNCTION_TOLERANCE_M)
        final_analysis = _architecture_wall_analysis(
            recovered_walls, architecture_records, vector_records,
            closed_profile_seed, cleaned, s1, s2, door_vector_evidence,
            audit_dir=audit_dir)
        final_analysis["architecture_continuity_restoration"] = (
            recovered_continuity)
    final_analysis["fragmented_face_recovery"] = copy.deepcopy(
        fragmented_face_recovery)
    gate_base_ir = None
    gate_base_error = None
    try:
        gate_base_ir = _short_wall_gate_base_review_ir(
            cleaned, s1, s2, base_analysis)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        gate_base_error = str(exc)[:240]

    configured_review_ir = getattr(
        pipeline, "SHORT_WALL_REVIEW_IR", None)
    replay_state = None
    if configured_review_ir:
        if gate_base_ir is None:
            replay_state = {
                "status": "BLOCKED", "errors": ["current_gate_base_invalid"]}
        else:
            approved_bundle, load_errors = (
                _load_explicit_short_wall_review_ir(configured_review_ir))
            if load_errors:
                replay_state = {"status": "BLOCKED", "errors": load_errors}
            else:
                replay_state = replay_approved_short_wall_bundle(
                    gate_base_ir, approved_bundle)
        if replay_state.get("status") == "REPLAYED":
            additions = []
            for approved_wall in replay_state.get("approved_additions", []):
                wall = copy.deepcopy(approved_wall)
                application = wall.pop("review_application", None)
                wall.pop("formal_model_eligible", None)
                wall.pop("revit_execution_status", None)
                wall["approval_replay"] = {
                    "gate_base_sha256": replay_state.get(
                        "gate_base_sha256"),
                    "proposal_id": wall.get("approved_proposal_id"),
                    "decision_id": ((application or {}).get("decision") or
                                    {}).get("decision_id"),
                }
                wall.setdefault("derivation", []).append({
                    "operation": "approved_short_wall_gate_replay",
                    "proposal_id": wall.get("approved_proposal_id"),
                    "gate_base_sha256": replay_state.get(
                        "gate_base_sha256"),
                    "parent_source_segment_ids": list(
                        wall.get("source_segment_ids") or []),
                })
                additions.append(wall)
            final_analysis = _architecture_wall_analysis(
                list(primary_walls) + additions,
                architecture_records, vector_records, closed_profile_seed,
                cleaned, s1, s2, door_vector_evidence,
                audit_dir=audit_dir)
            final_analysis["architecture_occurrence_selection"] = (
                copy.deepcopy(architecture_occurrence_selection))

    output_gate_base_ir = gate_base_ir
    if (replay_state and replay_state.get("status") == "REPLAYED"):
        try:
            output_gate_base_ir = _short_wall_gate_base_review_ir(
                cleaned, s1, s2, final_analysis)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            output_gate_base_ir = None
            gate_base_error = str(exc)[:240]

    result = dict(final_analysis)
    result.update({
        "architecture_source_count": architecture_source_count,
        "architecture_excluded_source_count": len(excluded_records),
        "architecture_source_layer_filter": source_filter,
        "vector_region_excluded_source_count": len(vector_excluded),
        "vector_source_layer_filter": vector_source_filter,
        "wall_source_mode": (cleaned.get("wall_source_mode")
                             if isinstance(cleaned, dict) else "legacy"),
        "plan_selection": (cleaned.get("plan_selection")
                           if isinstance(cleaned, dict) else None),
        "wall_occurrence_selection": s2.get("wall_occurrence_selection"),
        "architecture_occurrence_selection": copy.deepcopy(
            architecture_occurrence_selection),
        "architecture_continuity_restoration": final_analysis.get(
            "architecture_continuity_restoration", continuity_restoration),
        "fragmented_face_recovery": copy.deepcopy(
            final_analysis.get("fragmented_face_recovery") or
            fragmented_face_recovery),
        "full_drawing_audit_status": (
            cleaned.get("full_drawing_audit_status", "NOT_RUN")
            if isinstance(cleaned, dict) else "NOT_RUN"),
        "full_cv_audit": (cleaned.get("full_cv_audit")
                          if isinstance(cleaned, dict) else None),
        "yolo_wall_audit": (cleaned.get("yolo_wall_audit")
                            if isinstance(cleaned, dict) else None),
        "yolo_cv_fusion": (cleaned.get("yolo_cv_fusion")
                           if isinstance(cleaned, dict) else None),
        "dxf_review_candidates": (
            cleaned.get("dxf_review_candidates")
            if isinstance(cleaned, dict) else None),
        "known_non_wall_vector_candidates": (
            cleaned.get("known_non_wall_vector_candidates")
            if isinstance(cleaned, dict) else None),
        "cleaning_decision": (cleaned.get("cleaning_decision")
                              if isinstance(cleaned, dict) else None),
        "source_drawing_identity": (
            cleaned.get("source_drawing_identity")
            if isinstance(cleaned, dict) else None),
        "architecture_applicable": (
            bool(architecture_source_count) or not structural_only),
    })
    if output_gate_base_ir is not None:
        result["short_wall_gate_base"] = copy.deepcopy(
            output_gate_base_ir.get("short_wall_gate_base"))
    elif gate_base_error:
        result["short_wall_gate_base_error"] = gate_base_error
    if configured_review_ir:
        result["short_wall_replay"] = {
            "requested": True,
            "status": replay_state.get("status", "BLOCKED"),
            "gate_base_sha256": replay_state.get("gate_base_sha256"),
            "replayed_proposal_ids": list(
                replay_state.get("replayed_proposal_ids") or []),
            "decision_ids": [
                str(((item.get("decision") or {}).get("decision_id") or ""))
                for item in replay_state.get("applications", [])
                if isinstance(item, dict)
            ],
            "errors": list(replay_state.get("errors") or []),
        }
    result["traceability"] = _geometry_traceability_audit(
        cleaned if isinstance(cleaned, dict) else {}, s2, result)
    return result


def _normalised_sha256(value):
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


def _placement_path_complete(path):
    if not isinstance(path, list) or not path:
        return False
    for marker in path:
        if (not isinstance(marker, dict) or
                not marker.get("insert_handle") or
                not (marker.get("block_name") or marker.get("name")) or
                "array_index" not in marker):
            return False
        transform = marker.get("cumulative_transform")
        matrix = (transform.get("affine_2d")
                  if isinstance(transform, dict) else None)
        if (not isinstance(matrix, (list, tuple)) or len(matrix) != 3 or
                any(not isinstance(row, (list, tuple)) or len(row) != 3
                    for row in matrix)):
            return False
        try:
            if any(not math.isfinite(float(value))
                   for row in matrix for value in row):
                return False
        except (TypeError, ValueError, OverflowError):
            return False
    return True


def _wall_source_ref_limitations(reference, expected_drawing_identity=None,
                                 expected_occurrence_id=None):
    if not isinstance(reference, dict):
        return ["invalid_source_ref"]
    limitations = []
    for key in ("drawing_identity", "source_occurrence_id",
                "placed_entity_id", "structural_occurrence_id",
                "source_segment_id", "entity_handle"):
        if not reference.get(key):
            limitations.append(f"{key}_missing")
    if not isinstance(reference.get("source_record_index"), int):
        limitations.append("source_record_index_missing")
    if not isinstance(reference.get("segment_index"), int):
        limitations.append("segment_index_missing")
    interval = reference.get("source_interval_m")
    try:
        interval_valid = bool(
            isinstance(interval, (list, tuple)) and len(interval) == 2 and
            all(math.isfinite(float(value)) for value in interval) and
            float(interval[0]) >= 0.0 and
            float(interval[1]) + 1e-9 >= float(interval[0]))
    except (TypeError, ValueError, OverflowError):
        interval_valid = False
    if not interval_valid:
        limitations.append("source_interval_m_invalid")
    if not _placement_path_complete(reference.get("placement_path")):
        limitations.append("placement_path_incomplete")
    if (reference.get("identity_is_complete") is not True or
            reference.get("identity_limitations") not in ([], ())):
        limitations.append("declared_identity_incomplete")
    expected_sha = _normalised_sha256(expected_drawing_identity)
    actual_sha = _normalised_sha256(reference.get("drawing_identity"))
    if expected_sha and actual_sha != expected_sha:
        limitations.append("drawing_identity_mismatch")
    if (expected_occurrence_id and
            reference.get("structural_occurrence_id") !=
            expected_occurrence_id):
        limitations.append("structural_occurrence_id_mismatch")
    return limitations


def _wall_traceability_audit(walls, *, role,
                             expected_drawing_identity=None,
                             expected_occurrence_id=None,
                             require_derivation=False):
    elements = list(walls or [])
    issues = []
    traceable_count = 0
    source_ref_count = 0
    incomplete_ref_count = 0
    drawing_mismatch_count = 0
    occurrence_mismatch_count = 0
    lineage_incomplete_count = 0
    for wall_index, wall in enumerate(elements):
        wall_id = wall.get("id", f"{role}_{wall_index}") if isinstance(
            wall, dict) else f"{role}_{wall_index}"
        wall_issues = []
        references = (wall.get("source_segment_refs")
                      if isinstance(wall, dict) else None)
        if not isinstance(references, list) or not references:
            wall_issues.append("source_segment_refs_missing")
            references = []
        source_ref_count += len(references)
        reference_ids = []
        for reference_index, reference in enumerate(references):
            limitations = _wall_source_ref_limitations(
                reference, expected_drawing_identity,
                expected_occurrence_id)
            if isinstance(reference, dict) and reference.get(
                    "source_segment_id") not in reference_ids:
                reference_ids.append(reference.get("source_segment_id"))
            if limitations:
                incomplete_ref_count += 1
                drawing_mismatch_count += (
                    "drawing_identity_mismatch" in limitations)
                occurrence_mismatch_count += (
                    "structural_occurrence_id_mismatch" in limitations)
                wall_issues.append({
                    "source_ref_index": reference_index,
                    "limitations": limitations,
                })
        declared_ids = (wall.get("source_segment_ids")
                        if isinstance(wall, dict) else None)
        if declared_ids != reference_ids:
            wall_issues.append("source_segment_ids_mismatch")
        if require_derivation:
            derivation = wall.get("derivation") if isinstance(wall, dict) else None
            if (not isinstance(derivation, list) or not derivation or
                    any(not isinstance(item, dict) or not item.get("operation") or
                        not isinstance(item.get("parent_source_segment_ids"), list) or
                        not item.get("parent_source_segment_ids")
                        for item in derivation)):
                wall_issues.append("derivation_incomplete")
                lineage_incomplete_count += 1
        for adjustment_index, adjustment in enumerate(
                (wall.get("endpoint_adjustments") or [])
                if isinstance(wall, dict) else []):
            constraint_ids = adjustment.get("constraint_source_segment_ids")
            constraint_refs = adjustment.get("constraint_source_refs")
            if not constraint_ids:
                continue
            actual_constraint_ids = []
            constraint_limitations = []
            for reference in constraint_refs or []:
                if isinstance(reference, dict) and reference.get(
                        "source_segment_id") not in actual_constraint_ids:
                    actual_constraint_ids.append(reference.get(
                        "source_segment_id"))
                constraint_limitations.extend(
                    _wall_source_ref_limitations(
                        reference, expected_drawing_identity,
                        expected_occurrence_id))
            if (not isinstance(constraint_refs, list) or
                    constraint_ids != actual_constraint_ids or
                    constraint_limitations):
                wall_issues.append({
                    "endpoint_adjustment_index": adjustment_index,
                    "limitations": ["constraint_source_refs_incomplete"] +
                    sorted(set(constraint_limitations)),
                })
                lineage_incomplete_count += 1
        if wall_issues:
            issues.append({"element_index": wall_index,
                           "element_id": wall_id,
                           "issues": wall_issues})
        else:
            traceable_count += 1
    complete = bool(elements and traceable_count == len(elements))
    return {
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "element_count": len(elements),
        "traceable_element_count": traceable_count,
        "incomplete_element_count": len(elements) - traceable_count,
        "source_segment_ref_count": source_ref_count,
        "identity_incomplete_ref_count": incomplete_ref_count,
        "drawing_identity_mismatch_count": drawing_mismatch_count,
        "occurrence_mismatch_count": occurrence_mismatch_count,
        "lineage_incomplete_count": lineage_incomplete_count,
        "issues": issues,
    }


def _column_traceability_audit(s2, expected_drawing_identity=None):
    columns = list(s2.get("cols", []) or [])
    matching = s2.get("column_provenance_audit") or {}
    selected_occurrence_id = (
        (s2.get("wall_occurrence_selection") or {}).get(
            "selected_occurrence_id"))
    issues = []
    traceable_count = 0
    segment_ref_count = 0
    drawing_mismatch_count = 0
    for index, column in enumerate(columns):
        limitations = []
        profile = column.get("source_profile_ref") or {}
        segments = column.get("source_segment_refs") or []
        required = ("drawing_identity", "selected_structural_occurrence_id",
                    "source_structural_occurrence_id", "source_occurrence_id",
                    "placed_entity_id", "source_entity_handle")
        limitations.extend(f"{key}_missing" for key in required
                           if not profile.get(key))
        if not _placement_path_complete(profile.get("placement_path")):
            limitations.append("placement_path_incomplete")
        if (selected_occurrence_id and
                profile.get("selected_structural_occurrence_id") !=
                selected_occurrence_id):
            limitations.append("selected_structural_occurrence_id_mismatch")
        expected_sha = _normalised_sha256(expected_drawing_identity)
        if (expected_sha and _normalised_sha256(
                profile.get("drawing_identity")) != expected_sha):
            limitations.append("drawing_identity_mismatch")
            drawing_mismatch_count += 1
        profile_ids = profile.get("source_segment_ids")
        segment_ids = [item.get("source_segment_id")
                       for item in segments if isinstance(item, dict)]
        segment_ref_count += len(segments) if isinstance(segments, list) else 0
        if (not isinstance(profile_ids, list) or len(profile_ids) != 4 or
                len(set(profile_ids)) != 4 or
                not isinstance(segments, list) or len(segments) != 4 or
                segment_ids != profile_ids):
            limitations.append("four_exact_source_segment_refs_required")
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                limitations.append("invalid_source_segment_ref")
                continue
            for key in ("drawing_identity", "source_structural_occurrence_id",
                        "source_occurrence_id", "placed_entity_id",
                        "source_entity_handle", "source_segment_id"):
                if not segment.get(key):
                    limitations.append(f"segment_{key}_missing")
            if (expected_sha and _normalised_sha256(
                    segment.get("drawing_identity")) != expected_sha):
                limitations.append("segment_drawing_identity_mismatch")
                drawing_mismatch_count += 1
        if limitations:
            issues.append({"element_index": index,
                           "element_id": column.get("id", f"column_{index}"),
                           "limitations": sorted(set(limitations))})
        else:
            traceable_count += 1
    one_to_one = bool(
        columns and matching.get("status") == "PASS" and
        matching.get("one_to_one_complete") is True and
        matching.get("matched_count") == len(columns) and
        matching.get("missing_count") == 0 and
        matching.get("ambiguous_count") == 0 and
        matching.get("identity_incomplete_count") == 0)
    complete = bool(one_to_one and traceable_count == len(columns))
    return {
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "element_count": len(columns),
        "traceable_element_count": traceable_count,
        "incomplete_element_count": len(columns) - traceable_count,
        "source_profile_ref_count": sum(
            bool(column.get("source_profile_ref")) for column in columns),
        "source_segment_ref_count": segment_ref_count,
        "one_to_one_complete": one_to_one,
        "drawing_identity_mismatch_count": drawing_mismatch_count,
        "issues": issues,
    }


def _geometry_traceability_audit(s0, s2, s4):
    source = s0.get("source_identity") or {}
    source_sha = _normalised_sha256(source.get("sha256"))
    drawing_identity = (s0.get("source_drawing_identity") or
                        s2.get("source_drawing_identity") or
                        s4.get("source_drawing_identity"))
    drawing_sha = _normalised_sha256(drawing_identity)
    identity_matches = bool(source_sha and drawing_sha == source_sha)
    selected_occurrence_id = (
        (s2.get("wall_occurrence_selection") or {}).get(
            "selected_occurrence_id"))
    columns = _column_traceability_audit(s2, drawing_identity)
    structural = _wall_traceability_audit(
        s2.get("shear_walls", []), role="structural_wall",
        expected_drawing_identity=drawing_identity,
        expected_occurrence_id=selected_occurrence_id,
        require_derivation=True)
    architecture_applicable = bool(s4.get("architecture_applicable", True))
    architecture_selection = (
        s4.get("architecture_occurrence_selection") or {})
    selected_architecture_occurrence_id = (
        architecture_selection.get("selected_occurrence_id"))
    architecture_selection_valid = bool(
        architecture_selection.get("valid") is True and
        str(selected_architecture_occurrence_id or "").strip())
    if architecture_applicable:
        architecture = _wall_traceability_audit(
            s4.get("walls", []), role="architectural_wall",
            expected_drawing_identity=drawing_identity,
            expected_occurrence_id=(
                selected_architecture_occurrence_id
                if architecture_selection_valid else None))
        architecture["occurrence_selection_valid"] = (
            architecture_selection_valid)
        if not architecture_selection_valid:
            architecture["status"] = "INCOMPLETE"
            architecture.setdefault("issues", []).append({
                "element_id": "architecture_occurrence_selection",
                "issues": [{
                    "limitations": [
                        "architecture_occurrence_selection_invalid"],
                }],
            })
    else:
        architecture = {
            "status": "NOT_APPLICABLE", "element_count": 0,
            "traceable_element_count": 0, "incomplete_element_count": 0,
            "source_segment_ref_count": 0,
            "identity_incomplete_ref_count": 0,
            "drawing_identity_mismatch_count": 0,
            "occurrence_mismatch_count": 0,
            "lineage_incomplete_count": 0, "issues": [],
        }
    groups = {
        "columns": columns,
        "structural_walls": structural,
        "architectural_walls": architecture,
    }
    required_statuses = [columns["status"], structural["status"]]
    if architecture_applicable:
        required_statuses.append(architecture["status"])
    gate_passed = bool(
        identity_matches and all(status == "COMPLETE"
                                 for status in required_statuses))
    issues = [
        {"group": name, "issues": audit.get("issues", [])}
        for name, audit in groups.items()
        if audit.get("status") == "INCOMPLETE"
    ]
    if not identity_matches:
        issues.insert(0, {"group": "source",
                          "issues": ["drawing_identity_mismatch"]})
    return {
        "schema_version": "buildmate.wall-column-traceability/1.0",
        "scope": ["columns", "structural_walls",
                  "architectural_walls"],
        "status": "COMPLETE" if gate_passed else "INCOMPLETE",
        "gate_passed": gate_passed,
        "geometry_authority": "DXF_VECTOR",
        "source": {
            "path": source.get("path") or s0.get("drawing_id"),
            "sha256": source_sha,
            "drawing_identity": drawing_identity,
            "identity_status": "MATCH" if identity_matches else "MISMATCH",
        },
        "selected_structural_occurrence_id": selected_occurrence_id,
        "selected_architectural_occurrence_id": (
            selected_architecture_occurrence_id),
        "architecture_applicable": architecture_applicable,
        "groups": groups,
        "identity_incomplete_ref_count": sum(
            audit.get("identity_incomplete_ref_count", 0)
            for audit in groups.values()),
        "drawing_identity_mismatch_count": sum(
            audit.get("drawing_identity_mismatch_count", 0)
            for audit in groups.values()) + (0 if identity_matches else 1),
        "occurrence_mismatch_count": sum(
            audit.get("occurrence_mismatch_count", 0)
            for audit in groups.values()),
        "lineage_incomplete_count": sum(
            audit.get("lineage_incomplete_count", 0)
            for audit in groups.values()),
        "issue_count": sum(len(item.get("issues", []))
                           for item in issues),
        "issues": issues,
    }


def _closed_wall_profile_gate_state(s4):
    """Validate the closed-profile audit and its exact wall association."""
    coverage = s4.get("source_coverage") or {}
    audit = (s4.get("closed_wall_profile_audit") or
             coverage.get("closed_wall_profile_audit") or {})
    association = (audit.get("association") or
                   coverage.get("closed_profile_association") or {})
    candidates = audit.get("candidates")
    audit_ran = bool(
        isinstance(candidates, list) and
        audit.get("status") in {"PASS", "REVIEW"} and
        all(key in audit for key in (
            "candidate_count", "approved_by_rule_count",
            "needs_review_count", "identity_incomplete_count")))
    audit_status_pass = audit.get("status") == "PASS"

    def safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    needs_review = safe_int(audit.get("needs_review_count", -1))
    identity_incomplete = safe_int(
        audit.get("identity_incomplete_count", -1))
    approved_count = safe_int(audit.get("approved_by_rule_count", -1))
    candidate_count = safe_int(audit.get("candidate_count", -1))
    association_complete = association.get("status") == "COMPLETE"

    associated_profile_count = safe_int(
        association.get("profile_count", -1))
    child_evidence_count = safe_int(
        association.get("child_evidence_count", -1))
    associated_source_segment_count = safe_int(
        association.get("associated_source_segment_count", -1))
    source_missing_count = safe_int(association.get("missing_count", -1))
    source_duplicate_count = safe_int(
        association.get("duplicate_count", -1))

    approved_profile_count = safe_int(
        association.get("approved_profile_count", -1))
    approved_model_wall_count = safe_int(
        association.get("approved_model_wall_count", -1))
    one_to_one_count = safe_int(
        association.get("approved_wall_one_to_one_count", -1))
    missing_count = safe_int(
        association.get("approved_wall_missing_count", -1))
    duplicate_count = safe_int(
        association.get("approved_wall_duplicate_count", -1))

    approved_ids = {
        item.get("candidate_id") for item in candidates or []
        if item.get("status") == "APPROVED_BY_RULE" and
        item.get("candidate_id")
    }
    model_profile_ids = [
        wall.get("source_candidate_id") for wall in s4.get("walls", [])
        if wall.get("geometry_source") == "DXF_CLOSED_WALL_STRIP"
    ]
    in_memory_one_to_one = bool(
        audit_ran and len(candidates or []) == candidate_count and
        len(approved_ids) == approved_count and
        len(model_profile_ids) == approved_count and
        len(set(model_profile_ids)) == len(model_profile_ids) and
        set(model_profile_ids) == approved_ids)
    association_one_to_one = bool(
        association_complete and approved_count >= 0 and
        associated_profile_count == candidate_count and
        child_evidence_count == associated_source_segment_count and
        source_missing_count == 0 and source_duplicate_count == 0 and
        approved_profile_count == approved_count and
        approved_model_wall_count == approved_count and
        one_to_one_count == approved_count and
        missing_count == 0 and duplicate_count == 0)
    return {
        "audit": audit,
        "association": association,
        "audit_ran": audit_ran,
        "audit_status_pass": audit_status_pass,
        "needs_review_count": needs_review,
        "identity_incomplete_count": identity_incomplete,
        "approved_count": approved_count,
        "candidate_count": candidate_count,
        "association_complete": association_complete,
        "association_one_to_one": association_one_to_one,
        "in_memory_one_to_one": in_memory_one_to_one,
        "pass": bool(
            audit_ran and audit_status_pass and needs_review == 0 and
            identity_incomplete == 0 and
            association_one_to_one and in_memory_one_to_one),
    }


def scoped_gate2(s4):
    if not s4.get("architecture_applicable", True):
        return "N/A", ["结构墙柱图：建筑空间闭合 Gate 不适用"]
    checks = []
    replay_state = s4.get("short_wall_replay") or {}
    replay_requested = replay_state.get("requested") is True
    replay_passed = (not replay_requested or
                     replay_state.get("status") == "REPLAYED")
    if replay_requested:
        checks.append(
            "短墙人工审批重放 " +
            ("OK" if replay_passed else "FAIL") + " (" +
            str(replay_state.get("status", "MISSING")) + ")")
    traceability = s4.get("traceability") or {}
    architecture_traceability = (
        (traceability.get("groups") or {}).get("architectural_walls") or {})
    architecture_traceable = bool(
        traceability.get("source", {}).get("identity_status") == "MATCH" and
        architecture_traceability.get("status") == "COMPLETE")
    checks.append(
        f"建筑墙精确来源追溯 "
        f"{architecture_traceability.get('traceable_element_count', 'MISSING')}/"
        f"{architecture_traceability.get('element_count', 'MISSING')} "
        f"({'OK' if architecture_traceable else 'FAIL'})")
    valid_plan = bool(s4.get("plan_selection"))
    checks.append(f"主平面唯一选择 ({'OK' if valid_plan else 'FAIL'})")
    occurrence = s4.get("wall_occurrence_selection") or {}
    checks.append(f"墙柱实例坐标对齐 ({'OK' if occurrence.get('valid') else 'FAIL'})")
    architecture_occurrence = (
        s4.get("architecture_occurrence_selection") or {})
    architecture_occurrence_valid = bool(
        architecture_occurrence.get("valid") is True and
        architecture_occurrence.get("selected_occurrence_id"))
    checks.append(
        "建筑墙实例唯一归属当前主平面 " +
        ("(OK)" if architecture_occurrence_valid else "(FAIL)"))
    source_count = int(s4.get("architecture_source_count", 0))
    checks.append(f"主布局建筑墙源实体 {source_count} ({'OK' if source_count else 'FAIL'})")
    coverage_report = s4.get("source_coverage") or {}
    source_coverage = float(coverage_report.get(
        "gate_exact_face_length_coverage",
        coverage_report.get(
            "exact_face_length_coverage",
            coverage_report.get("face_length_coverage", 0.0))))
    checks.append(
        f"建筑墙源线精确覆盖率 {source_coverage:.0%} "
        f"({'OK' if source_coverage >= 0.95 else 'FAIL'})")
    uncovered_source = int(
        coverage_report.get("uncovered_source_segment_count", 0))
    partial_source = int(
        coverage_report.get("partially_mapped_source_segment_count", 0))

    def review_count(value, default=-1):
        try:
            count = int(value)
        except (TypeError, ValueError):
            return default
        return count if count >= 0 else default

    raw_source_review_count = review_count(coverage_report.get(
        "raw_source_review_count", uncovered_source + partial_source))
    logical_source_review_value = coverage_report.get(
        "logical_review_unit_count")
    logical_source_review_count = review_count(logical_source_review_value)
    checks.append(
        f"墙源复核: 原始线段 {raw_source_review_count} / "
        f"逻辑审核单元 "
        f"{logical_source_review_count if logical_source_review_count >= 0 else 'MISSING'} "
        f"({'OK' if logical_source_review_count == 0 else 'REVIEW'})")
    short_wall_pair_audit = coverage_report.get(
        "short_wall_pair_audit") or {}
    short_wall_pair_groups = review_count(
        short_wall_pair_audit.get("pair_group_count"), 0)
    short_wall_raw_pairs = review_count(
        short_wall_pair_audit.get("raw_pair_count"), 0)
    checks.append(
        f"短墙双面证据: 原始配对 {short_wall_raw_pairs} / "
        f"逻辑面组 {short_wall_pair_groups} / 自动批准 0 "
        f"({'OK' if short_wall_pair_groups == 0 else 'REVIEW'})")
    short_wall_proposals = coverage_report.get(
        "short_wall_proposal_audit") or {}
    reported_short_wall_count = review_count(
        short_wall_proposals.get("proposed_short_wall_count"), 0)
    proposal_items = short_wall_proposals.get("proposals") or []
    if not isinstance(proposal_items, list):
        proposal_items = []
    proposed_short_walls = max(
        reported_short_wall_count, len(proposal_items))
    unsupported_short_walls = review_count(
        short_wall_proposals.get("insufficient_support_count"), 0)
    strict_short_walls = review_count(
        short_wall_proposals.get("strict_approval_eligible_count"), 0)
    reported_upgrade_count = review_count(
        short_wall_proposals.get(
            "single_wall_upgrade_proposal_count"), 0)
    upgrade_proposal_count = max(
        reported_upgrade_count,
        sum(
            isinstance(item, dict) and
            item.get("status") == "PROPOSED_WALL_UPGRADE"
            for item in proposal_items))
    audit_items = [short_wall_proposals, *proposal_items]
    insufficient_items = short_wall_proposals.get(
        "insufficient_support_candidates") or []
    if isinstance(insufficient_items, list):
        audit_items.extend(insufficient_items)
    atomic_replacement_blocker_count = 0
    for item in audit_items:
        if not isinstance(item, dict):
            continue
        blockers = item.get("blockers") or []
        if isinstance(blockers, str):
            blockers = [blockers]
        if (isinstance(blockers, (list, tuple, set)) and
                "atomic_single_wall_replacement_not_implemented" in blockers):
            atomic_replacement_blocker_count += 1
    short_wall_modeling_blocked = bool(
        proposed_short_walls != 0 or
        unsupported_short_walls != 0 or
        upgrade_proposal_count != 0 or
        atomic_replacement_blocker_count != 0)
    checks.append(
        f"短墙拓扑提案: 候选 {proposed_short_walls} / "
        f"双封帽严格候选 {strict_short_walls} / "
        f"支撑不足 {unsupported_short_walls} / 自动建模 0 "
        f"({'OK' if proposed_short_walls == 0 and unsupported_short_walls == 0 else 'REVIEW'})")
    checks.append(
        f"短墙正式建模阻断: 提案 {proposed_short_walls} / "
        f"支撑不足 {unsupported_short_walls} / "
        f"墙升级 {upgrade_proposal_count} / "
        f"原子替换未实现 {atomic_replacement_blocker_count} "
        f"({'FAIL' if short_wall_modeling_blocked else 'OK'})")
    profile_state = _closed_wall_profile_gate_state(s4)
    profile_audit = profile_state["audit"]
    checks.append(
        "闭合墙轮廓审核已运行 " +
        ("(OK)" if profile_state["audit_ran"] else "(FAIL)"))
    checks.append(
        f"闭合墙轮廓审核结果 {profile_audit.get('status', 'MISSING')} "
        f"({'OK' if profile_state['audit_status_pass'] else 'FAIL'})")
    checks.append(
        f"闭合墙轮廓: 原始子边 "
        f"{profile_state['association'].get('child_evidence_count', 'MISSING')} / "
        f"逻辑轮廓 {profile_audit.get('candidate_count', 'MISSING')} / "
        f"待复核 {profile_state['needs_review_count']} / "
        f"身份不完整 {profile_state['identity_incomplete_count']} "
        f"({'OK' if profile_state['needs_review_count'] == 0 and profile_state['identity_incomplete_count'] == 0 else 'REVIEW'})")
    checks.append(
        f"闭合墙轮廓与模型墙关联 "
        f"{profile_state['association'].get('status', 'MISSING')} / "
        f"一一对应 {profile_state['association'].get('approved_wall_one_to_one_count', 'MISSING')} "
        f"({'OK' if profile_state['association_complete'] and profile_state['association_one_to_one'] and profile_state['in_memory_one_to_one'] else 'FAIL'})")
    endpoint_rate = float(s4.get("endpoint_coverage", 0.0))
    fully_rate = float(s4.get("closed_rate", 0.0))
    checks.append(f"墙端点物理连接率 {endpoint_rate:.0%} ({'OK' if endpoint_rate >= 0.80 else 'FAIL'})")
    checks.append(f"墙双端连接率 {fully_rate:.0%} ({'OK' if fully_rate >= 0.70 else 'FAIL'})")
    endpoint_review = s4.get("endpoint_review") or {}
    endpoint_candidate_count = int(endpoint_review.get("candidate_count", 0))
    endpoint_review_count = int(endpoint_review.get(
        "needs_review_count", endpoint_candidate_count))
    endpoint_resolved_cap_count = int(endpoint_review.get(
        "resolved_intentional_cap_count", 0))
    checks.append(
        f"未连接墙端点: 原始 {endpoint_candidate_count} / "
        f"合法封帽 {endpoint_resolved_cap_count} / "
        f"待复核 {endpoint_review_count} "
        f"({'OK' if endpoint_review_count == 0 else 'REVIEW'})")
    room_count = int(s4.get("room_polygon_count", 0))
    checks.append(f"可成环房间 {room_count} ({'OK' if room_count > 0 else 'FAIL'})")
    cv_audit = s4.get("cv_topology_audit") or {}
    cv_ok = cv_audit.get("status") == "OK"
    checks.append(f"CV 封闭区复核 {cv_audit.get('enclosed_region_count', 0)} "
                  f"({'OK' if cv_ok else 'FAIL'})")
    vector_review = int((s4.get("vector_candidate_audit") or {}).get("review_count", 0))
    checks.append(f"未决矢量墙候选 {vector_review} ({'OK' if vector_review == 0 else 'REVIEW'})")
    full_audit_status = s4.get("full_drawing_audit_status", "NOT_RUN")
    cleaning_decision = s4.get("cleaning_decision") or {}
    full_audit_ok = (
        full_audit_status == "NOT_RUN" or
        (full_audit_status == "OK" and cleaning_decision.get("allow_modeling"))
    )
    if full_audit_status != "NOT_RUN":
        checks.append(
            f"原图清洗审计 {full_audit_status} "
            f"({'OK' if full_audit_status == 'OK' else 'FAIL'})")
        cv_missing = int(
            (s4.get("full_cv_audit") or {}).get(
                "candidate_missing_line_count", 0))
        exact_audit = s4.get("dxf_review_candidates") or {}
        exact_review_count = int(exact_audit.get(
            "needs_review_count", exact_audit.get("candidate_count", 0)))
        rejected_exact_count = int(exact_audit.get("rejected_by_rule_count", 0))
        unassociated_cv_count = int(
            ((s4.get("full_cv_audit") or {}).get("vector_association") or {}).get(
                "unassociated_candidate_count", 0))
        cv_resolved = exact_review_count == 0 and unassociated_cv_count == 0
        checks.append(
            f"原图CV线候选 {cv_missing} / DXF规则拒绝 {rejected_exact_count} / "
            f"未关联 {unassociated_cv_count} "
            f"({'OK' if cv_resolved else 'REVIEW'})")
        known_non_wall_audit = s4.get("known_non_wall_vector_candidates") or {}
        known_non_wall_count = int(
            known_non_wall_audit.get("rejected_by_rule_count", 0))
        checks.append(
            f"已保留的规则拒绝非墙矢量 {known_non_wall_count} (OK)")
        yolo_audit = s4.get("yolo_wall_audit") or {}
        yolo_status = yolo_audit.get("inference_status", "NOT_RUN")
        yolo_quality = (yolo_audit.get("quality") or {}).get(
            "status", "NOT_EVALUATED")
        if yolo_status != "DISABLED":
            yolo_required = bool(cleaning_decision.get("yolo_required"))
            yolo_pass = yolo_status == "OK" and yolo_quality == "PASS"
            checks.append(
                f"YOLO语义证据 {yolo_status}/{yolo_quality} "
                f"({'OK' if yolo_pass else 'FAIL' if yolo_required else 'ADVISORY'})")
        checks.append(
            f"未决精确DXF线候选 {exact_review_count} "
            f"({'OK' if exact_review_count == 0 else 'REVIEW'})")
    ok = (replay_passed and architecture_traceable and valid_plan and
          occurrence.get("valid") and architecture_occurrence_valid and
          source_count > 0 and
          source_coverage >= 0.95 and logical_source_review_count == 0 and
          not short_wall_modeling_blocked and
          profile_state["pass"] and
          endpoint_rate >= 0.80 and fully_rate >= 0.70 and
          endpoint_review_count == 0 and room_count > 0 and cv_ok and
          vector_review == 0 and full_audit_ok)
    return ("PASS" if ok else "FAIL"), checks


def strict_gate1(s1, s2, s3):
    status, checks = _original_gate1(s1, s2, s3)
    occurrence = s2.get("wall_occurrence_selection") or {}
    if not occurrence.get("valid"):
        checks.append("墙柱实例未与本层轴网/柱唯一对齐 (FAIL)")
        status = "FAIL"
    if not s2.get("shear_walls"):
        checks.append("主实例结构墙 0 段 (FAIL)")
        status = "FAIL"
    column_audit = s2.get("column_provenance_audit") or {}
    traceability = s2.get("traceability") or {}
    traceability_groups = traceability.get("groups") or {}
    column_traceability = traceability_groups.get("columns") or {}
    structural_traceability = traceability_groups.get(
        "structural_walls") or {}
    column_count = len(s2.get("cols", []))
    column_traceable = bool(
        column_count and column_audit.get("status") == "PASS" and
        column_audit.get("one_to_one_complete") is True and
        column_audit.get("matched_count") == column_count and
        column_audit.get("missing_count") == 0 and
        column_audit.get("ambiguous_count") == 0 and
        column_audit.get("identity_incomplete_count") == 0 and
        column_traceability.get("status") == "COMPLETE")
    checks.append(
        f"柱源轮廓一一追溯 {column_audit.get('matched_count', 'MISSING')}/"
        f"{column_count} ({'OK' if column_traceable else 'FAIL'})")
    if not column_traceable:
        status = "FAIL"
    structural_count = len(s2.get("shear_walls", []))
    structural_traceable = bool(
        traceability.get("source", {}).get("identity_status") == "MATCH" and
        structural_traceability.get("status") == "COMPLETE" and
        structural_traceability.get("traceable_element_count") ==
        structural_count)
    checks.append(
        f"结构墙精确来源追溯 "
        f"{structural_traceability.get('traceable_element_count', 'MISSING')}/"
        f"{structural_count} ({'OK' if structural_traceable else 'FAIL'})")
    if not structural_traceable:
        status = "FAIL"
    return status, checks


def build_review_bim_ir(steps, report):
    """Build a traceable, non-executable IR for failed or pending reviews."""
    s0, s1, s2, s3, g1, s4, g2, _s5, _s6 = steps
    coverage = s4.get("source_coverage") or {}
    semantic_candidates = (
        list(coverage.get("uncovered_source_segments", [])) +
        list(coverage.get("partially_mapped_source_segments", []))
    )
    final_decision = (report.get("清洗审计") or {}).get("最终判定") or {}
    traceability = s4.get("traceability") or s2.get("traceability") or {}
    artifact_ready = bool(
        final_decision.get("allow_modeling") and g1[0] == "PASS" and
        g2[0] in {"PASS", "N/A"} and
        traceability.get("status") == "COMPLETE" and
        traceability.get("gate_passed") is True)

    def selected(item, keys):
        return {key: item[key] for key in keys if key in item}

    review_ir = {
        "schema_version": "buildmate.review-ir/1.0",
        "artifact_role": "REVIEW_ONLY",
        "artifact_status": ("READY_FOR_PREVIEW" if artifact_ready
                            else "BLOCKED"),
        "source": s0.get("source_identity") or {
            "path": s0.get("drawing_id"), "sha256": None,
        },
        "geometry_authority": "DXF_VECTOR",
        "semantic_evidence_only": ["CV", "YOLO"],
        "traceability": traceability,
        "plan_selection": s4.get("plan_selection"),
        "wall_occurrence_selection": s4.get(
            "wall_occurrence_selection") or {},
        "architecture_occurrence_selection": s4.get(
            "architecture_occurrence_selection") or {},
        "coordinate_system": {
            "unit": "m",
            "origin_dxf_mm": s1.get("origin"),
            "rotation_deg": s1.get("rot_deg"),
            "grid_center_dxf_mm": [s1.get("gcx"), s1.get("gcy")],
        },
        "quality_gate": {
            "allow_modeling": bool(final_decision.get("allow_modeling")),
            "blocking_reasons": list(final_decision.get("reasons", [])),
            "structure": {"status": g1[0], "checks": list(g1[1])},
            "architecture": {"status": g2[0], "checks": list(g2[1])},
        },
        "geometry": {
            "columns": [selected(item, (
                "id", "center", "size", "profile", "mark", "grid_ref",
                "confidence", "source", "warnings", "source_profile_ref",
                "source_segment_refs"))
                for item in s2.get("cols", [])],
            "structural_walls": [selected(item, (
                "id", "start", "end", "thickness", "confidence", "source",
                "warnings", "geometry_source", "source_segment_ids",
                "source_segment_refs", "derivation",
                "endpoint_adjustments", "geometry_adjustments"))
                for item in s2.get("shear_walls", [])],
            "architectural_walls": [selected(item, (
                "id", "start", "end", "thickness", "thickness_mm", "length_m",
                "paired", "confidence",
                "source", "warnings", "geometry_source", "wall_group",
                "source_layers", "thickness_mm",
                "source_candidate_id", "profile_occurrence_id",
                "drawing_identity", "source_occurrence_id",
                "placed_entity_id", "structural_occurrence_id",
                "fragmented_face_recovery_id", "short_wall_recovery",
                "terminal_face_recovery", "terminal_face_recovery_count",
                "source_segment_ids", "source_segment_refs", "derivation",
                "endpoint_adjustments", "geometry_adjustments",
                "approved_proposal_id", "proposal_integrity_sha256",
                "logical_id", "revision", "revision_number", "revision_id",
                "application_trace", "approval_replay"))
                for item in s4.get("walls", [])],
            "beams": [selected(item, (
                "id", "start", "end", "confidence", "source", "warnings"))
                for item in s3.get("beams", [])],
        },
        "review_candidates": {
            "semantic_wall_sources": semantic_candidates,
            "semantic_wall_source_units": list(
                coverage.get("review_units", [])),
            "short_wall_pair_groups": (
                coverage.get("short_wall_pair_audit") or {}),
            "short_wall_proposals": (
                coverage.get("short_wall_proposal_audit") or {}),
            "closed_wall_profiles": (
                s4.get("closed_wall_profile_audit") or {}),
            "wall_endpoints": s4.get("endpoint_review") or {},
            "explicit_door_vectors": s4.get(
                "door_vector_evidence") or {},
            "wall_symbol_semantics": s4.get(
                "wall_symbol_semantics") or {},
            "nonstandard_vector_walls": s4.get("dxf_review_candidates") or {},
            "known_non_wall_vectors": (
                s4.get("known_non_wall_vector_candidates") or {}),
            "irregular_columns": s0.get("irregular_profile_audit") or {},
            "column_provenance": s2.get("column_provenance_audit") or {},
        },
        "artifacts": {
            "semantic_wall_source_overlay": coverage.get("review_overlay"),
            "closed_wall_profile_overlay": (
                (s4.get("closed_wall_profile_audit") or {}).get(
                    "review_overlay")),
            "wall_endpoint_overlay": (
                (s4.get("endpoint_review") or {}).get("review_overlay")),
            "short_wall_proposal_overlay": (
                (coverage.get("short_wall_proposal_audit") or {}).get(
                    "review_overlay")),
            "irregular_profile_overlay": (
                (s0.get("irregular_profile_audit") or {}).get("review_overlay")),
        },
    }
    if s4.get("short_wall_gate_base"):
        review_ir["short_wall_gate_base"] = copy.deepcopy(
            s4["short_wall_gate_base"])
    if s4.get("short_wall_replay"):
        review_ir["short_wall_replay"] = copy.deepcopy(
            s4["short_wall_replay"])
    return review_ir


def guarded_trigger_revit():
    """Fail closed when the persisted cleaning/modeling Gate is not approved."""
    status_path = Path(pipeline.JSON_IN) / "model_status.json"
    model_path = Path(pipeline.JSON_IN) / "model.json"
    try:
        with status_path.open(encoding="utf-8") as stream:
            status = json.load(stream)
        with model_path.open(encoding="utf-8") as stream:
            model = json.load(stream)
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": f"Revit Gate 输入不可用: {str(exc)[:180]}"}
    final_decision = (status.get("清洗审计") or {}).get("最终判定") or {}
    if final_decision.get("allow_modeling") is not True:
        reasons = final_decision.get("reasons") or ["清洗审计未明确批准建模"]
        return {"status": "error", "error": "Revit Gate 阻断: " + "; ".join(reasons)}
    cleaning_audit = status.get("清洗审计") or {}
    traceability = cleaning_audit.get("墙柱几何溯源") or {}
    if (traceability.get("status") != "COMPLETE" or
            traceability.get("gate_passed") is not True or
            (traceability.get("source") or {}).get(
                "identity_status") != "MATCH"):
        return {
            "status": "error",
            "error": "Revit Gate 阻断: 墙柱几何精确来源追溯未通过",
        }
    if cleaning_audit.get("建筑空间适用", True):
        profile_audit = cleaning_audit.get("闭合墙轮廓审核") or {}
        association = profile_audit.get("association") or {}

        def exact_count(name):
            value = association.get(name)
            return value if isinstance(value, int) and value >= 0 else None

        approved = profile_audit.get("approved_by_rule_count")
        candidate_count = profile_audit.get("candidate_count")
        profile_safe = bool(
            profile_audit.get("gate_passed") is True and
            profile_audit.get("status") == "PASS" and
            association.get("status") == "COMPLETE" and
            isinstance(approved, int) and approved >= 0 and
            isinstance(candidate_count, int) and candidate_count >= 0 and
            profile_audit.get("needs_review_count") == 0 and
            profile_audit.get("identity_incomplete_count") == 0 and
            exact_count("profile_count") == candidate_count and
            exact_count("child_evidence_count") == exact_count(
                "associated_source_segment_count") and
            exact_count("missing_count") == 0 and
            exact_count("duplicate_count") == 0 and
            exact_count("approved_profile_count") == approved and
            exact_count("approved_model_wall_count") == approved and
            exact_count("approved_wall_one_to_one_count") == approved and
            exact_count("approved_wall_missing_count") == 0 and
            exact_count("approved_wall_duplicate_count") == 0)
        if not profile_safe:
            return {
                "status": "error",
                "error": "Revit Gate 阻断: 闭合建筑墙轮廓审核或一一关联未通过",
            }
    gates = status.get("MODEL_STATUS") or {}
    required = ("Step0_数据清洗", "结构框架Gate", "建筑空间Gate")
    failed = [f"{name}={gates.get(name, 'MISSING')}"
              for name in required if gates.get(name) != "PASS"]
    if failed:
        return {"status": "error", "error": "Revit Gate 阻断: " + "; ".join(failed)}
    if not (model.get("model_elements") or []):
        return {"status": "error", "error": "Revit Gate 阻断: model_elements 为空"}
    return _original_trigger_revit()


def scoped_risk_report(steps):
    report = _original_risk_report(steps)
    s0, _s1, s2, _s3, g1, s4, g2, _s5, _s6 = steps
    profile_state = _closed_wall_profile_gate_state(s4)
    profile_report = dict(profile_state["audit"])
    profile_report["association"] = profile_state["association"]
    profile_report["gate_passed"] = profile_state["pass"]
    source_coverage = s4.get("source_coverage") or {}
    traceability = s4.get("traceability") or s2.get("traceability") or {}
    report["清洗审计"] = {
        "建筑空间适用": bool(s4.get("architecture_applicable", True)),
        "墙源模式": s0.get("wall_source_mode"),
        "主平面选择": s0.get("plan_selection"),
        "墙源实体": {
            "结构墙": s0.get("structural_source_entities", 0),
            "建筑墙": s0.get("architecture_source_entities", 0),
            "主布局建筑墙": s4.get("architecture_source_count", 0),
            "排除的异布局建筑墙": s4.get("architecture_excluded_source_count", 0),
        },
        "建筑墙来源图层过滤": s4.get("architecture_source_layer_filter", {}),
        "矢量墙来源图层过滤": s4.get("vector_source_layer_filter", {}),
        "整墙连续性还原": s4.get("architecture_continuity_restoration", {}),
        "墙柱实例选择": s2.get("wall_occurrence_selection"),
        "墙柱几何溯源": traceability,
        "柱源轮廓追溯": s2.get("column_provenance_audit", {}),
        "异形柱轮廓": s0.get("irregular_profile_audit", {}),
        "闭合墙轮廓审核": profile_report,
        "墙源复核计数": {
            "raw_source_review_count": source_coverage.get(
                "raw_source_review_count"),
            "raw_needs_review_source_segment_count": source_coverage.get(
                "raw_needs_review_source_segment_count"),
            "rejected_by_rule_source_segment_count": source_coverage.get(
                "rejected_by_rule_source_segment_count"),
            "resolved_by_profile_source_segment_count": source_coverage.get(
                "resolved_by_profile_source_segment_count"),
            "logical_review_unit_count": source_coverage.get(
                "logical_review_unit_count"),
        },
        "建筑墙拓扑": {
            "墙数": len(s4.get("walls", [])),
            "源线覆盖": s4.get("source_coverage", {}),
            "端点物理连接率": round(float(s4.get("endpoint_coverage", 0.0)), 4),
            "双端连接率": round(float(s4.get("closed_rate", 0.0)), 4),
            "推断门洞桥接数": len(s4.get("opening_bridges", [])),
            "成环房间数": s4.get("room_polygon_count", 0),
            "成环面积_m2": s4.get("room_polygon_area_m2", 0.0),
            "成环引擎": s4.get("polygonize_status"),
            "CV封闭区复核": s4.get("cv_topology_audit", {}),
            "墙端点复核": s4.get("endpoint_review", {}),
            "显式门DXF矢量证据": s4.get(
                "door_vector_evidence", {}),
            "墙图层门扇符号证据": s4.get(
                "wall_symbol_semantics", {}),
            "碎片墙面恢复": s4.get(
                "fragmented_face_recovery", {}),
        },
        "短墙审批重放": s4.get("short_wall_replay") or {
            "requested": False, "status": "NOT_REQUESTED"},
        "非标准图层矢量候选": s4.get("vector_candidate_audit", {}),
        "原图CV遗漏复核": s4.get("full_cv_audit"),
        "YOLO墙识别复核": s4.get("yolo_wall_audit"),
        "YOLO与CV交叉证据": s4.get("yolo_cv_fusion"),
        "精确DXF复核候选": s4.get("dxf_review_candidates"),
        "规则拒绝的非墙矢量": s4.get("known_non_wall_vector_candidates"),
    }
    cleaning_failures = []
    replay_state = s4.get("short_wall_replay") or {}
    if (replay_state.get("requested") is True and
            replay_state.get("status") != "REPLAYED"):
        errors = ", ".join(str(item) for item in
                           replay_state.get("errors") or [])
        cleaning_failures.append(
            "短墙审批重放未通过" + (f": {errors}" if errors else ""))
    if g1[0] == "FAIL":
        cleaning_failures.append("结构框架 Gate 未通过")
    if g2[0] == "FAIL":
        cleaning_failures.append("建筑墙拓扑 Gate 未通过")
    if (traceability.get("status") != "COMPLETE" or
            traceability.get("gate_passed") is not True):
        cleaning_failures.append("墙柱几何精确来源追溯未通过")
    if (s0.get("irregular_profile_audit") or {}).get("status") == "REVIEW":
        cleaning_failures.append("存在未完成语义匹配的非矩形结构填充轮廓")
    if (s4.get("architecture_applicable", True) and
            not profile_state["pass"]):
        cleaning_failures.append("闭合建筑墙轮廓审核或模型墙一一关联未通过")
    cleaning_decision = s4.get("cleaning_decision") or {}
    if cleaning_decision and not cleaning_decision.get("allow_modeling", False):
        cleaning_failures.extend(
            reason for reason in cleaning_decision.get("reasons", [])
            if reason not in cleaning_failures)
    report["清洗审计"]["最终判定"] = {
        "status": "FAIL" if cleaning_failures else "PASS",
        "allow_modeling": not cleaning_failures,
        "reasons": cleaning_failures,
    }
    if cleaning_failures:
        report["MODEL_STATUS"]["Step0_数据清洗"] = "FAIL"
    if not s4.get("architecture_applicable", True):
        report["MODEL_STATUS"]["Step4_建筑墙"] = "N/A"
        report["MODEL_STATUS"]["建筑空间Gate"] = "N/A"
        report["MODEL_STATUS"]["Step5_板门窗楼梯"] = "N/A(结构墙柱图)"
        report["数据质量"]["几何准确率"] = None
        report["数据质量"]["建筑空间适用"] = False
    review_ir = build_review_bim_ir(steps, report)
    review_dir = Path(s0.get("full_drawing_audit_dir") or pipeline.JSON_IN)
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(_s1.get("prefix") or "drawing"))
    review_path = review_dir / f"10_bim_ir_review_{safe_prefix}.json"
    try:
        review_dir.mkdir(parents=True, exist_ok=True)
        with review_path.open("w", encoding="utf-8") as stream:
            json.dump(review_ir, stream, ensure_ascii=False, indent=2)
        report["清洗审计"]["BIM_IR复核文件"] = str(review_path)
    except OSError as exc:
        report["清洗审计"]["BIM_IR复核错误"] = str(exc)[:240]
    with open(os.path.join(pipeline.JSON_IN, "model_status.json"), "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=1)
    return report


_original_gate2 = pipeline.gate2_arch
_original_gate1 = pipeline.gate1_structure
_original_risk_report = pipeline.step7_risk_report
_original_step0 = pipeline.step0_clean
_original_step2 = pipeline.step2_col_shear
_original_trigger_revit = pipeline.trigger_revit
pipeline.step0_clean = audited_step0
pipeline.step2_col_shear = structural_step2
pipeline.step4_arch_wall = precise_step4
pipeline.gate1_structure = strict_gate1
pipeline.gate2_arch = scoped_gate2
pipeline.step7_risk_report = scoped_risk_report
pipeline.trigger_revit = guarded_trigger_revit

if __name__ == "__main__":
    pipeline.main()
