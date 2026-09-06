"""Audit the current Step0 drawing-cleaning rules without modifying the source."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import ezdxf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_YOLO_MODEL = (
    PROJECT_ROOT / "data" / "runtime" / "models" /
    "floor_plan_yolo" / "floor_plan_yolov8_best.pt"
)
# The bundled checkpoint records train_args.imgsz=640.  Half-tile overlap
# keeps long walls visible when a crop boundary crosses them.
DEFAULT_YOLO_TILE_SIZE = 640
DEFAULT_YOLO_TILE_OVERLAP = 320

from backend.engines.yolo_drawing import (
    correlate_cv_candidates,
    run_yolo_drawing_audit,
)
from backend.engines.drawing_audit_cache import (
    audit_cache_key,
    build_audit_cache_identity,
    capture_environment,
    collect_dependency_versions,
    read_audit_cache,
    write_audit_cache,
)


KEEP = (
    "S-COLU", "S-WALL", "A-WALL", "S-STEL", "A-GRID", "A-ANNO",
    "DIM", "TEXT", "轴网", "S-ANNO", "GRID", "COLUMN", "COLU", "A-PART", "WALL",
    "S-BEAM", "S-SLAB", "S-STAIR",
)
PROTECTED = ("A-GRID", "轴网", "A-ANNO", "DIM", "TEXT")
DROP = (
    "家具", "厨洗", "卫生间", "A-CAR", "A-STAIR", "A-FIRE", "EQUIP",
    "A-TECH", "A-FURT", "F-FURN", "KITCHEN", "KJ", "A-FLOR-OVHD",
    "消火栓", "3T_BAR", "A-DOOR", "精装", "SANT", "设备",
    "集水坑", "潜污泵", "墙洞", "夹层",
)
GEOMETRY_TYPES = {
    "LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE",
    "SPLINE", "HATCH", "SOLID", "TRACE", "3DFACE", "INSERT",
}
STRUCTURAL_HINTS = (
    "WALL", "COLU", "COLUMN", "BEAM", "GRID", "SLAB", "STAIR",
    "墙", "柱", "梁", "板", "轴",
)
WALL_SEMANTIC_HINTS = ("WALL", "PART", "MASON", "墙", "砌")
KNOWN_NON_WALL_LAYER_RULES = {
    "floor_or_slab_boundary": (
        "SLAB", "FLOOR", "FLOR", "降板", "板边", "底板",
    ),
    "parking_or_site_marking": (
        "A-CAR", "PARK", "停车", "ROAD", "SITE",
    ),
    "annotation_or_grid": (
        "ANNO", "DIM", "TEXT", "GRID", "轴网", "标注",
    ),
}

AUDIT_CACHE_ENVIRONMENT = (
    "DRAWING_AUDIT_CACHE_DIR",
    "DRAWING_CLEANING_AUDIT_DIR",
    "DRAWING_FULL_CV_AUDIT",
    "DRAWING_YOLO_CONFIDENCE",
    "DRAWING_YOLO_MODEL_PATH",
    "DRAWING_YOLO_REQUIRED",
    "DRAWING_YOLO_TILE_OVERLAP",
    "DRAWING_YOLO_TILE_SIZE",
    "YOLO_CONFIG_DIR",
)
AUDIT_DEPENDENCIES = (
    "ezdxf", "numpy", "opencv-python", "opencv-python-headless", "Pillow",
    "torch", "torchvision", "ultralytics",
)
AUDIT_ARTIFACT_FILES = {
    "original_preview": "01_original.png",
    "cleaned_preview": "02_cleaned.png",
    "comparison_preview": "03_comparison.png",
    "cv_auxiliary": "04_cv_auxiliary.png",
    "cv_review_overlay": "06_original_cleaned_cv.png",
    "vector_wall_reference": "07_vector_wall_reference.png",
}


def classify_layer(layer: str) -> str:
    upper = (layer or "").upper()
    leaf = upper.rsplit("$0$", 1)[-1]
    if not any(token.upper() in leaf for token in KEEP):
        return "unclassified"
    if any(token in leaf for token in ("S-WALL", "A-WALL", "A-PART")) or leaf == "WALL":
        return "kept_wall_geometry"
    if any(token.upper() in leaf for token in PROTECTED):
        return "kept_protected"
    if any(token.upper() in leaf for token in DROP):
        return "dropped_by_rule"
    return "kept_candidate"


def known_non_wall_reason(layer: str) -> str | None:
    """Return a general semantic exclusion reason for a non-wall layer."""
    leaf = str(layer or "").upper().rsplit("$0$", 1)[-1]
    if any(token in leaf for token in WALL_SEMANTIC_HINTS):
        return None
    return next((
        reason for reason, tokens in KNOWN_NON_WALL_LAYER_RULES.items()
        if any(token in leaf for token in tokens)
    ), None)


def is_structurally_named_non_wall_layer(layer: str) -> bool:
    leaf = str(layer or "").upper().rsplit("$0$", 1)[-1]
    return (known_non_wall_reason(layer) is not None and
            any(token in leaf for token in STRUCTURAL_HINTS))


def is_suspicious_layer(layer: str) -> bool:
    upper = (layer or "").upper()
    leaf = upper.rsplit("$0$", 1)[-1]
    return (classify_layer(layer) == "unclassified" and
            known_non_wall_reason(layer) is None and
            not any(token.upper() in upper for token in DROP) and
            any(token in leaf for token in STRUCTURAL_HINTS))


def line_key(entity, layer: str):
    if entity.dxftype() != "LINE":
        return None
    start = entity.dxf.start
    end = entity.dxf.end
    first = (layer, round(start.x, 1), round(start.y, 1),
             round(end.x, 1), round(end.y, 1))
    reverse = (layer, first[3], first[4], first[1], first[2])
    return min(first, reverse)


def approximate_length(entity) -> float:
    kind = entity.dxftype()
    try:
        if kind == "LINE":
            return entity.dxf.start.distance(entity.dxf.end)
        if kind == "LWPOLYLINE":
            points = list(entity.get_points("xy"))
            return sum(math.hypot(b[0] - a[0], b[1] - a[1])
                       for a, b in zip(points, points[1:]))
        if kind == "CIRCLE":
            return 2.0 * math.pi * abs(float(entity.dxf.radius))
        if kind == "ARC":
            sweep = (float(entity.dxf.end_angle) -
                     float(entity.dxf.start_angle)) % 360.0
            return abs(float(entity.dxf.radius)) * math.radians(sweep)
    except Exception:
        return 0.0
    return 0.0


def _entity_source_identity(entity) -> dict:
    origin = getattr(entity, "origin_of_copy", None) or entity
    origin_dxf = getattr(origin, "dxf", None)
    block_reference = getattr(entity, "source_block_reference", None)
    block_origin = (getattr(block_reference, "origin_of_copy", None) or
                    block_reference)
    block_dxf = getattr(block_origin, "dxf", None)
    return {
        "entity_handle": getattr(origin_dxf, "handle", None),
        "entity_type": entity.dxftype(),
        "source_entity_type": (
            origin.dxftype() if hasattr(origin, "dxftype") else entity.dxftype()),
        "source_block_handle": getattr(block_dxf, "handle", None),
        "source_block_name": getattr(block_dxf, "name", None),
    }


def _angle_delta_degrees(first: tuple[float, float],
                         second: tuple[float, float]) -> float:
    first_length = math.hypot(*first)
    second_length = math.hypot(*second)
    if first_length <= 1e-9 or second_length <= 1e-9:
        return 180.0
    dot = abs((first[0] * second[0] + first[1] * second[1]) /
              (first_length * second_length))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def associate_cv_candidates(
    candidates: list[dict],
    vector_segments: list[dict],
    max_distance_px: float = 5.0,
    max_angle_deg: float = 4.0,
    minimum_overlap_ratio: float = 0.20,
) -> dict:
    """Associate raster omissions with placed DXF segments without promotion."""
    associated = []
    for candidate_index, candidate in enumerate(candidates):
        start = tuple(float(value) for value in candidate.get("start_px", [])[:2])
        end = tuple(float(value) for value in candidate.get("end_px", [])[:2])
        if len(start) != 2 or len(end) != 2:
            continue
        direction = (end[0] - start[0], end[1] - start[1])
        length = math.hypot(*direction)
        if length <= 1e-9:
            continue
        unit = (direction[0] / length, direction[1] / length)
        matches = []
        for segment in vector_segments:
            segment_start = tuple(float(value) for value in segment["start_px"])
            segment_end = tuple(float(value) for value in segment["end_px"])
            segment_direction = (segment_end[0] - segment_start[0],
                                 segment_end[1] - segment_start[1])
            angle_delta = _angle_delta_degrees(direction, segment_direction)
            if angle_delta > max_angle_deg:
                continue
            midpoint = ((segment_start[0] + segment_end[0]) / 2.0,
                        (segment_start[1] + segment_end[1]) / 2.0)
            relative_midpoint = (midpoint[0] - start[0],
                                 midpoint[1] - start[1])
            perpendicular_distance = abs(
                relative_midpoint[0] * unit[1] -
                relative_midpoint[1] * unit[0])
            if perpendicular_distance > max_distance_px:
                continue
            projections = [
                (point[0] - start[0]) * unit[0] +
                (point[1] - start[1]) * unit[1]
                for point in (segment_start, segment_end)
            ]
            overlap = max(
                0.0, min(length, max(projections)) -
                max(0.0, min(projections)))
            overlap_ratio = overlap / length
            if overlap_ratio < minimum_overlap_ratio:
                continue
            matches.append({
                **{key: value for key, value in segment.items()
                   if key not in {"start_px", "end_px"}},
                "start_px": list(segment_start),
                "end_px": list(segment_end),
                "pixel_distance": round(perpendicular_distance, 3),
                "angle_delta_deg": round(angle_delta, 3),
                "candidate_overlap_ratio": round(overlap_ratio, 4),
            })
        matches.sort(key=lambda item: (
            item["pixel_distance"], item["angle_delta_deg"],
            -item["candidate_overlap_ratio"]))
        enriched = dict(candidate)
        enriched["candidate_id"] = f"cv_missing_{candidate_index:03d}"
        enriched["matched_entity_count"] = len(matches)
        enriched["has_exact_vector_source"] = bool(matches)
        enriched["matched_dxf_segments"] = matches[:12]
        enriched["geometry_authority"] = False
        associated.append(enriched)
    return {
        "candidate_count": len(associated),
        "associated_candidate_count": sum(
            1 for item in associated if item["has_exact_vector_source"]),
        "unassociated_candidate_count": sum(
            1 for item in associated if not item["has_exact_vector_source"]),
        "candidates": associated,
        "note": ("Associations identify exact placed DXF evidence; they do not "
                 "promote candidates to walls."),
    }


def build_dxf_review_candidates(associated_cv_candidates: list[dict],
                                yolo_supported_cv_ids: set[str] | None = None
                                ) -> dict:
    """Collapse duplicate raster detections into stable placed-DXF candidates."""
    yolo_supported_cv_ids = yolo_supported_cv_ids or set()
    grouped: dict[tuple, dict] = {}
    for cv_candidate in associated_cv_candidates:
        cv_candidate_id = cv_candidate.get("candidate_id")
        for segment in cv_candidate.get("matched_dxf_segments", []):
            start = tuple(round(float(value), 4)
                          for value in segment.get("start_dxf", []))
            end = tuple(round(float(value), 4)
                        for value in segment.get("end_dxf", []))
            if len(start) != 2 or len(end) != 2:
                continue
            first, second = sorted((start, end))
            key = (
                segment.get("entity_handle"), segment.get("layer"),
                first, second,
            )
            if key not in grouped:
                rejection_reason = known_non_wall_reason(
                    segment.get("layer") or "")
                status = ("REJECTED_BY_RULE" if rejection_reason else
                          "NEEDS_REVIEW")
                signature = json.dumps(key, ensure_ascii=False, sort_keys=True)
                stable_id = hashlib.sha256(
                    signature.encode("utf-8")).hexdigest()[:16]
                grouped[key] = {
                    "candidate_id": f"dxf_line_{stable_id}",
                    "status": status,
                    "geometry_source": "DXF_VECTOR",
                    "wall_semantics_confirmed": False,
                    "decision_reason": rejection_reason,
                    "entity_handle": segment.get("entity_handle"),
                    "entity_type": segment.get("entity_type"),
                    "layer": segment.get("layer"),
                    "source_block_handle": segment.get("source_block_handle"),
                    "source_block_name": segment.get("source_block_name"),
                    "start_dxf": list(first),
                    "end_dxf": list(second),
                    "evidence": {
                        "cv_candidate_ids": [],
                        "yolo_supported": False,
                    },
                }
            evidence = grouped[key]["evidence"]
            if cv_candidate_id and cv_candidate_id not in evidence["cv_candidate_ids"]:
                evidence["cv_candidate_ids"].append(cv_candidate_id)
            if cv_candidate_id in yolo_supported_cv_ids:
                evidence["yolo_supported"] = True
    review_candidates = sorted(
        grouped.values(), key=lambda item: item["candidate_id"])
    for candidate in review_candidates:
        candidate["evidence"]["cv_candidate_ids"].sort()
    needs_review_count = sum(
        1 for item in review_candidates if item["status"] == "NEEDS_REVIEW")
    rejected_count = sum(
        1 for item in review_candidates if item["status"] == "REJECTED_BY_RULE")
    return {
        "status": "NEEDS_REVIEW" if needs_review_count else "PASS",
        "candidate_count": len(review_candidates),
        "needs_review_count": needs_review_count,
        "rejected_by_rule_count": rejected_count,
        "yolo_supported_candidate_count": sum(
            1 for item in review_candidates
            if item["evidence"]["yolo_supported"]),
        "candidates": review_candidates,
        "note": ("Coordinates come from placed DXF vectors. A reviewer or "
                 "validated constraint rule must confirm wall semantics."),
    }


def render_previews(document, output_dir: Path) -> dict:
    try:
        from array import array
        from PIL import Image, ImageDraw
        from ezdxf.disassemble import recursive_decompose

        coordinates = array("d")
        category_flags = bytearray()
        wall_flags = bytearray()
        source_metadata = []
        selected_suspicious_layers = set()
        bounds = [float("inf"), float("inf"),
                  float("-inf"), float("-inf")]

        def append_segment(x1, y1, x2, y2, category, is_wall, metadata,
                           include_in_bounds=True):
            coordinates.extend((x1, y1, x2, y2))
            category_flags.append(category)
            wall_flags.append(is_wall)
            source_metadata.append(metadata)
            if category == 1 and include_in_bounds:
                bounds[0] = min(bounds[0], x1, x2)
                bounds[1] = min(bounds[1], y1, y2)
                bounds[2] = max(bounds[2], x1, x2)
                bounds[3] = max(bounds[3], y1, y2)

        # AVE_* inserts are rendering metadata placed hundreds of kilometres
        # away from the actual plans. Including them collapses the building to
        # a pixel, so preview only the drawing inserts and direct entities.
        from scripts.geometry_first_clean import select_main_plan
        selected_plan, plan_selection = select_main_plan(document)
        drawing_entities = [selected_plan]

        for top_level in drawing_entities:
            for entity in recursive_decompose([top_level]):
                layer = getattr(entity.dxf, "layer", "")
                reason = classify_layer(layer)
                kept = reason.startswith("kept_")
                category = (1 if kept else 2 if is_suspicious_layer(layer)
                            else 3 if is_structurally_named_non_wall_layer(layer)
                            else 0)
                if category == 2:
                    selected_suspicious_layers.add(str(layer))
                is_wall = reason == "kept_wall_geometry"
                metadata = {**_entity_source_identity(entity), "layer": layer}
                try:
                    if entity.dxftype() == "LINE":
                        start, end = entity.dxf.start, entity.dxf.end
                        append_segment(
                            start.x, start.y, end.x, end.y, category, is_wall,
                            metadata)
                    elif entity.dxftype() == "LWPOLYLINE":
                        points = list(entity.get_points("xy"))
                        pairs = list(zip(points, points[1:]))
                        if entity.closed and len(points) > 2:
                            pairs.append((points[-1], points[0]))
                        for first, second in pairs:
                            # LibreDWG can emit malformed/mirrored OCS points
                            # for some converted polylines. LINE entities are
                            # stable enough to establish the plan extents; the
                            # polyline still renders when it falls inside them.
                            append_segment(first[0], first[1],
                                           second[0], second[1], category,
                                           is_wall, metadata, False)
                except Exception:
                    continue
        if not category_flags or not all(math.isfinite(value) for value in bounds):
            raise ValueError("no renderable kept linework")
        width, height, margin = 2400, 1400, 30
        span_x = max(bounds[2] - bounds[0], 1.0)
        span_y = max(bounds[3] - bounds[1], 1.0)
        scale = min((width - margin * 2) / span_x,
                    (height - margin * 2) / span_y)

        def point(x, y):
            return (margin + (x - bounds[0]) * scale,
                    height - margin - (y - bounds[1]) * scale)

        original = Image.new("RGB", (width, height), (24, 30, 36))
        cleaned = Image.new("RGB", (width, height), (24, 30, 36))
        comparison = Image.new("RGB", (width, height), (24, 30, 36))
        original_draw = ImageDraw.Draw(original)
        cleaned_draw = ImageDraw.Draw(cleaned)
        comparison_draw = ImageDraw.Draw(comparison)
        cv_kept = Image.new("L", (width, height), 0)
        cv_suspicious = Image.new("L", (width, height), 0)
        wall_reference = Image.new("L", (width, height), 0)
        cv_kept_draw = ImageDraw.Draw(cv_kept)
        cv_suspicious_draw = ImageDraw.Draw(cv_suspicious)
        wall_reference_draw = ImageDraw.Draw(wall_reference)
        suspicious_segments = []
        known_non_wall_segments = []
        for index, category in enumerate(category_flags):
            offset = index * 4
            x1, y1, x2, y2 = coordinates[offset:offset + 4]
            if (max(x1, x2) < bounds[0] or min(x1, x2) > bounds[2] or
                    max(y1, y2) < bounds[1] or min(y1, y2) > bounds[3]):
                continue
            segment = (point(x1, y1), point(x2, y2))
            original_draw.line(segment, fill=(190, 198, 205), width=1)
            if wall_flags[index]:
                wall_reference_draw.line(segment, fill=255, width=3)
            if category == 1:
                cleaned_draw.line(segment, fill=(30, 230, 200), width=1)
                comparison_draw.line(segment, fill=(30, 230, 120), width=1)
                cv_kept_draw.line(segment, fill=255, width=1)
            else:
                comparison_draw.line(segment, fill=(90, 45, 45), width=1)
                if category == 2:
                    cv_suspicious_draw.line(segment, fill=255, width=1)
                    suspicious_segments.append({
                        **source_metadata[index],
                        "start_px": [round(value, 3) for value in segment[0]],
                        "end_px": [round(value, 3) for value in segment[1]],
                        "start_dxf": [round(x1, 4), round(y1, 4)],
                        "end_dxf": [round(x2, 4), round(y2, 4)],
                    })
                elif (category == 3 and
                      source_metadata[index].get("source_entity_type") != "HATCH"):
                    known_non_wall_segments.append({
                        **source_metadata[index],
                        "start_px": [round(value, 3) for value in segment[0]],
                        "end_px": [round(value, 3) for value in segment[1]],
                        "start_dxf": [round(x1, 4), round(y1, 4)],
                        "end_dxf": [round(x2, 4), round(y2, 4)],
                    })
        original.save(output_dir / "01_original.png")
        cleaned.save(output_dir / "02_cleaned.png")
        comparison.save(output_dir / "03_comparison.png")
        wall_reference.save(output_dir / "07_vector_wall_reference.png")
        cv_result = cv_auxiliary_audit(cv_kept, cv_suspicious, output_dir)
        vector_associations = associate_cv_candidates(
            cv_result.get("candidates", []), suspicious_segments)
        cv_result["candidates"] = vector_associations["candidates"]
        cv_result["vector_association"] = {
            key: value for key, value in vector_associations.items()
            if key != "candidates"
        }
        return {
            "error": None,
            "cv_auxiliary": cv_result,
            "selected_plan_suspicious_layers": sorted(
                selected_suspicious_layers),
            "known_non_wall_segments": known_non_wall_segments,
            "original_image": str(output_dir / "01_original.png"),
            "wall_reference_mask": str(
                output_dir / "07_vector_wall_reference.png"),
            "render_transform": {
                "bounds": [round(value, 4) for value in bounds],
                "width": width,
                "height": height,
                "margin": margin,
                "scale": scale,
            },
            "plan_selection": plan_selection,
        }
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "cv_auxiliary": None,
            "selected_plan_suspicious_layers": [],
            "known_non_wall_segments": None,
            "original_image": None,
            "wall_reference_mask": None,
            "render_transform": None,
            "plan_selection": None,
        }


def render_wall_source_review_overlay(walls: list[dict],
                                      source_coverage: dict,
                                      output_path: Path) -> dict:
    """Render exact semantic-wall gaps over the extracted wall centre-lines."""
    from PIL import Image, ImageDraw

    candidates = (
        list(source_coverage.get("uncovered_source_segments", [])) +
        list(source_coverage.get("partially_mapped_source_segments", []))
    )
    if not candidates and not walls:
        raise ValueError("no wall or semantic-source geometry to render")

    columns = 4
    rows = max(1, math.ceil(len(candidates) / columns))
    width, header, gap = 2400, 105, 10
    height = max(700, header + rows * 420 + gap)
    panel_width = (width - gap * (columns + 1)) / columns
    panel_height = (height - header - gap * (rows + 1)) / rows
    image = Image.new("RGB", (width, height), (20, 24, 30))
    draw = ImageDraw.Draw(image)

    def review_bucket(candidate: dict) -> str:
        if (candidate.get("status") == "RESOLVED_BY_PROFILE" or
                candidate.get("decision_reason") ==
                "approved_closed_wall_strip_end_cap"):
            return "resolved_profile_edge"
        if (candidate.get("status") == "REJECTED_BY_RULE" or
                candidate.get("decision_reason") ==
                "exact_door_leaf_swing_topology"):
            return "known_non_wall"
        if (candidate.get("review_bucket") ==
                "STRICT_TOPOLOGY_END_CAP" or
                candidate.get("decision_reason") ==
                "strict_topology_end_cap"):
            return "strict_topology_end_cap"
        if candidate.get("decision_reason") == "possible_opening_gap":
            return "possible_opening"
        return "possible_omission"

    bucket_counts = Counter(review_bucket(item) for item in candidates)
    opening_count = bucket_counts["possible_opening"]
    strict_end_cap_count = bucket_counts["strict_topology_end_cap"]
    resolved_strict_end_cap_count = int(source_coverage.get(
        "resolved_strict_topology_end_cap_count", 0))
    omission_count = bucket_counts["possible_omission"]
    known_non_wall_count = bucket_counts["known_non_wall"]
    resolved_profile_edge_count = bucket_counts["resolved_profile_edge"]
    review_units = list(source_coverage.get("review_units", []))
    resolved_junction_range_count = sum(
        item.get("status") == "RESOLVED_MODELED_JUNCTION_EDGE"
        for item in review_units)
    needs_review_unit_count = int(source_coverage.get(
        "logical_review_unit_count",
        sum(item.get("status") == "NEEDS_REVIEW"
            for item in review_units)))
    mixed_range_source_count = int(source_coverage.get(
        "mixed_range_semantic_source_count", 0))
    strict_end_cap_colour = (186, 104, 255)
    known_non_wall_colour = (82, 210, 142)
    resolved_profile_edge_colour = (78, 156, 232)
    for index, candidate in enumerate(candidates):
        row, column = divmod(index, columns)
        panel_left = gap + column * (panel_width + gap)
        panel_top = header + gap + row * (panel_height + gap)
        panel_right = panel_left + panel_width
        panel_bottom = panel_top + panel_height
        draw.rectangle((panel_left, panel_top, panel_right, panel_bottom),
                       fill=(25, 30, 37), outline=(62, 73, 84), width=2)

        candidate_points = [candidate["start"], candidate["end"]]
        candidate_points.extend(
            point
            for part in candidate.get("unmapped_ranges", [])
            for point in (part["start"], part["end"])
        )
        xs = [float(point[0]) for point in candidate_points]
        ys = [float(point[1]) for point in candidate_points]
        center_x, center_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        span_x = max(max(xs) - min(xs) + 3.0, 4.0)
        span_y = max(max(ys) - min(ys) + 3.0, 4.0)
        plot_width, plot_height = panel_width - 24, panel_height - 58
        scale = min(plot_width / span_x, plot_height / span_y)

        def point(value):
            return (
                panel_left + panel_width / 2 +
                (float(value[0]) - center_x) * scale,
                panel_top + 44 + plot_height / 2 -
                (float(value[1]) - center_y) * scale,
            )

        min_x, max_x = center_x - span_x / 2, center_x + span_x / 2
        min_y, max_y = center_y - span_y / 2, center_y + span_y / 2
        for wall in walls:
            wall_xs = [float(wall["start"][0]), float(wall["end"][0])]
            wall_ys = [float(wall["start"][1]), float(wall["end"][1])]
            if (max(wall_xs) < min_x or min(wall_xs) > max_x or
                    max(wall_ys) < min_y or min(wall_ys) > max_y):
                continue
            draw.line((point(wall["start"]), point(wall["end"])),
                      fill=(82, 102, 112), width=3)

        bucket = review_bucket(candidate)
        source_colour = (
            strict_end_cap_colour if bucket == "strict_topology_end_cap"
            else known_non_wall_colour if bucket == "known_non_wall"
            else resolved_profile_edge_colour
            if bucket == "resolved_profile_edge"
            else (236, 180, 58))
        draw.line((point(candidate["start"]), point(candidate["end"])),
                  fill=source_colour, width=5)
        material_ranges = [
            (range_index, part)
            for range_index, part in enumerate(
                candidate.get("unmapped_ranges", []))
            if float(part.get("length_m", 0.0)) >= 0.01
        ]
        range_summaries = {
            int(item.get("range_index")): item
            for item in candidate.get("range_review_units", [])
            if isinstance(item, dict) and
            isinstance(item.get("range_index"), int)
        }
        for range_index, part in material_ranges:
            supported = bool(part.get("opening_bridge_supported"))
            range_summary = range_summaries.get(range_index) or {}
            if (range_summary.get("status") ==
                    "RESOLVED_MODELED_JUNCTION_EDGE"):
                colour = (52, 190, 184)
            elif bucket == "strict_topology_end_cap":
                colour = strict_end_cap_colour
            elif bucket == "known_non_wall":
                colour = known_non_wall_colour
            elif bucket == "resolved_profile_edge":
                colour = resolved_profile_edge_colour
            else:
                colour = (55, 205, 238) if supported else (255, 76, 72)
            first, second = point(part["start"]), point(part["end"])
            draw.line((first, second), fill=colour, width=8)
            midpoint = ((first[0] + second[0]) / 2.0,
                        (first[1] + second[1]) / 2.0)
            radius = 7
            draw.ellipse((midpoint[0] - radius, midpoint[1] - radius,
                          midpoint[0] + radius, midpoint[1] + radius),
                         outline=colour, width=3)
        label = str(candidate.get("entity_handle") or
                    candidate.get("candidate_id", "")[-8:])
        material_summaries = [
            range_summaries.get(range_index) or {}
            for range_index, _part in material_ranges
        ]
        if (material_summaries and all(
                item.get("status") == "RESOLVED_MODELED_JUNCTION_EDGE"
                for item in material_summaries)):
            decision = "RESOLVED MODELED JUNCTION EDGE"
        elif (candidate.get("mixed_range_semantics") or
              len({item.get("decision_reason")
                   for item in material_summaries if item}) > 1):
            decision = "MIXED RANGE REVIEW"
        else:
            decision = {
                "possible_opening": "POSSIBLE OPENING",
                "strict_topology_end_cap": (
                    "STRICT END CAP | EVIDENCE RESOLVED"),
                "known_non_wall": "KNOWN NON-WALL | DOOR LEAF",
                "resolved_profile_edge": "RESOLVED WALL-STRIP END CAP",
                "possible_omission": "POSSIBLE OMISSION",
            }[bucket]
        draw.text((panel_left + 12, panel_top + 9),
                  f"{label}  |  {decision}  |  mapped {candidate.get('mapped_ratio', 0):.0%}",
                  fill=(245, 245, 245))

    draw.rectangle((20, 16, 1250, 92), fill=(10, 13, 17))
    draw.text((32, 26),
              "Semantic wall source review sheet | each panel is locally zoomed",
              fill=(235, 235, 235))
    draw.text((32, 49),
              "gray: final wall | yellow: exact source | cyan: inferred opening proximity | "
              "purple: strict end cap (evidence resolved) | green: known non-wall | "
              "blue: resolved profile cap | teal: resolved junction edge | "
              "red: omission",
              fill=(235, 235, 235))
    draw.text((32, 72),
              f"exact DXF handles | opening {opening_count} | strict end cap "
              f"{strict_end_cap_count} (resolved {resolved_strict_end_cap_count}) | "
              f"known non-wall {known_non_wall_count} | "
              f"resolved profile {resolved_profile_edge_count} | "
              f"omission {omission_count} | range units {len(review_units)} | "
              f"junction resolved {resolved_junction_range_count} | "
              f"review {needs_review_unit_count} | mixed {mixed_range_source_count}",
              fill=(180, 190, 200))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return {
        "status": "OK",
        "path": str(output_path),
        "candidate_count": len(candidates),
        "possible_opening_count": opening_count,
        "strict_topology_end_cap_count": strict_end_cap_count,
        "resolved_strict_topology_end_cap_count": (
            resolved_strict_end_cap_count),
        "known_non_wall_count": known_non_wall_count,
        "resolved_profile_edge_count": resolved_profile_edge_count,
        "possible_omission_count": omission_count,
        "review_unit_count": len(review_units),
        "logical_review_unit_count": needs_review_unit_count,
        "needs_review_unit_count": needs_review_unit_count,
        "resolved_modeled_junction_edge_count": (
            resolved_junction_range_count),
        "mixed_range_semantic_source_count": mixed_range_source_count,
        "review_unit_reason_counts": source_coverage.get(
            "review_unit_reason_counts", {}),
        "review_unit_bucket_counts": source_coverage.get(
            "review_unit_bucket_counts", {}),
        "review_bucket_counts": {
            "possible_opening": opening_count,
            "strict_topology_end_cap": strict_end_cap_count,
            "known_non_wall": known_non_wall_count,
            "resolved_profile_edge": resolved_profile_edge_count,
            "possible_omission": omission_count,
        },
        "legend": {
            "gray": "final wall",
            "yellow": "exact DXF source",
            "cyan": "inferred opening proximity; topology evidence only",
            "purple": (
                "strict topology end cap; evidence resolved; no geometry action"),
            "green": "exact door leaf/swing topology; known non-wall; retained",
            "blue": "approved closed wall-strip end cap; retained and resolved",
            "teal": "exact modeled junction edge; retained and resolved",
            "red": "possible omission",
        },
    }


def render_short_wall_proposal_overlay(walls: list[dict],
                                       proposal_audit: dict,
                                       output_path: Path) -> dict:
    """Render support-audited short-wall proposals without changing geometry."""
    from PIL import Image, ImageDraw

    proposals = list(proposal_audit.get("proposals") or [])
    insufficient = list(
        proposal_audit.get("insufficient_support_candidates") or [])
    candidates = proposals + insufficient
    if not candidates:
        raise ValueError("no short-wall proposal evidence to render")

    columns = 3
    rows = max(1, math.ceil(len(candidates) / columns))
    width, header, gap = 2400, 118, 12
    height = max(720, header + rows * 430 + gap)
    panel_width = (width - gap * (columns + 1)) / columns
    panel_height = (height - header - gap * (rows + 1)) / rows
    image = Image.new("RGB", (width, height), (18, 22, 28))
    draw = ImageDraw.Draw(image)

    proposal_colours = {
        "strict": (72, 205, 132),
        "ordinary": (70, 150, 245),
        "ambiguous": (244, 164, 66),
        "single": (244, 210, 82),
        "upgrade": (190, 105, 245),
        "insufficient": (246, 78, 74),
    }
    support_colours = {
        "EXACT_STRICT_CAP": (72, 205, 132),
        "EXACT_SOURCE_CAP": (72, 205, 132),
        "EXACT_PAIRED_WALL_JUNCTION": (55, 205, 238),
        "AMBIGUOUS_SUPPORT": (244, 164, 66),
        "SINGLE_WALL_JUNCTION": (244, 210, 82),
        "UNSUPPORTED": (246, 78, 74),
    }
    wall_by_id = {str(wall.get("id")): wall for wall in walls
                  if wall.get("id")}

    def proposal_kind(candidate):
        if candidate.get("status") == "INSUFFICIENT_SUPPORT":
            return "insufficient"
        if candidate.get("status") == "PROPOSED_WALL_UPGRADE":
            return "upgrade"
        if candidate.get("approval_status") == (
                "ELIGIBLE_FOR_STRICT_APPROVAL"):
            return "strict"
        blockers = set(candidate.get("blockers") or [])
        if "endpoint_supported_by_single_wall_only" in blockers:
            return "single"
        if blockers:
            return "ambiguous"
        return "ordinary"

    def dashed_line(first, second, colour, width_px=7, dash_px=15):
        dx, dy = second[0] - first[0], second[1] - first[1]
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            return
        ux, uy = dx / length, dy / length
        cursor = 0.0
        while cursor < length:
            end = min(length, cursor + dash_px)
            draw.line((first[0] + cursor * ux, first[1] + cursor * uy,
                       first[0] + end * ux, first[1] + end * uy),
                      fill=colour, width=width_px)
            cursor += dash_px * 1.65

    for index, candidate in enumerate(candidates):
        row, column = divmod(index, columns)
        left = gap + column * (panel_width + gap)
        top = header + gap + row * (panel_height + gap)
        right = left + panel_width
        bottom = top + panel_height
        draw.rectangle((left, top, right, bottom), fill=(25, 30, 37),
                       outline=(62, 73, 84), width=2)

        points = [candidate.get("start"), candidate.get("end")]
        points.extend(
            face_point
            for endpoint in candidate.get("endpoint_support", [])
            for face_point in endpoint.get("face_points", []))
        replacement = candidate.get("replacement_evidence") or {}
        replaced_wall = wall_by_id.get(str(
            replacement.get("existing_wall_id")))
        if replaced_wall:
            points.extend([replaced_wall.get("start"),
                           replaced_wall.get("end")])
        points = [point for point in points
                  if isinstance(point, (list, tuple)) and len(point) >= 2]
        if len(points) < 2:
            continue
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        center_x, center_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        span_x = max(max(xs) - min(xs) + 1.2, 1.8)
        span_y = max(max(ys) - min(ys) + 1.2, 1.8)
        plot_width, plot_height = panel_width - 30, panel_height - 92
        scale = min(plot_width / span_x, plot_height / span_y)

        def point(value):
            return (
                left + panel_width / 2 +
                (float(value[0]) - center_x) * scale,
                top + 67 + plot_height / 2 -
                (float(value[1]) - center_y) * scale,
            )

        min_x, max_x = center_x - span_x / 2, center_x + span_x / 2
        min_y, max_y = center_y - span_y / 2, center_y + span_y / 2
        for wall in walls:
            start, end = wall.get("start"), wall.get("end")
            if (not isinstance(start, (list, tuple)) or len(start) < 2 or
                    not isinstance(end, (list, tuple)) or len(end) < 2):
                continue
            wall_xs = [float(start[0]), float(end[0])]
            wall_ys = [float(start[1]), float(end[1])]
            if (max(wall_xs) < min_x or min(wall_xs) > max_x or
                    max(wall_ys) < min_y or min(wall_ys) > max_y):
                continue
            colour = ((132, 78, 155) if replaced_wall is wall
                      else (78, 91, 102))
            draw.line((point(start), point(end)), fill=colour,
                      width=5 if replaced_wall is wall else 3)

        kind = proposal_kind(candidate)
        colour = proposal_colours[kind]
        first, second = point(candidate["start"]), point(candidate["end"])
        if kind == "insufficient":
            dashed_line(first, second, colour)
        else:
            draw.line((first, second), fill=colour, width=8)
        for endpoint in candidate.get("endpoint_support", []):
            endpoint_point = endpoint.get("point")
            if not isinstance(endpoint_point, (list, tuple)):
                continue
            center = point(endpoint_point)
            endpoint_colour = support_colours.get(
                endpoint.get("status"), (210, 210, 210))
            radius = 9
            draw.ellipse((center[0] - radius, center[1] - radius,
                          center[0] + radius, center[1] + radius),
                         fill=endpoint_colour, outline=(245, 245, 245),
                         width=2)

        handles = "+".join(str(value) for value in
                           candidate.get("source_entity_handles", []))
        label = {
            "strict": "STRICT CANDIDATE",
            "ordinary": "PROPOSED",
            "ambiguous": "AMBIGUOUS | BLOCKED",
            "single": "SINGLE WALL | BLOCKED",
            "upgrade": "UPGRADE | REPLACE",
            "insufficient": "INSUFFICIENT SUPPORT",
        }[kind]
        draw.text((left + 12, top + 10),
                  f"{handles or candidate.get('proposal_id', '')[-10:]} | {label}",
                  fill=(245, 245, 245))
        endpoint_label = "+".join(
            str(item.get("status"))
            for item in candidate.get("endpoint_support", []))
        draw.text((left + 12, top + 33),
                  f"L {candidate.get('length_m', 0):.3f}m | "
                  f"T {candidate.get('thickness_mm', 0):.0f}mm | "
                  f"{endpoint_label}", fill=(190, 202, 214))
        if replacement.get("existing_wall_id"):
            draw.text((left + 12, top + 54),
                      f"atomic replace: {replacement['existing_wall_id']} | "
                      "model unchanged", fill=proposal_colours["upgrade"])

    strict_count = int(proposal_audit.get(
        "strict_approval_eligible_count", 0))
    upgrade_count = int(proposal_audit.get(
        "single_wall_upgrade_proposal_count", 0))
    proposal_kinds = [proposal_kind(candidate) for candidate in candidates]
    ordinary_count = proposal_kinds.count("ordinary")
    ambiguous_count = proposal_kinds.count("ambiguous")
    single_count = proposal_kinds.count("single")
    draw.rectangle((20, 14, width - 20, 101), fill=(10, 13, 17))
    draw.text((34, 25),
              "Short-wall proposal review | exact DXF coordinates | review only",
              fill=(240, 240, 240))
    draw.text((34, 49),
              "green: strict | blue: proposed | orange: ambiguous | "
              "yellow: single-wall support | purple: replace | red: insufficient",
              fill=(205, 215, 225))
    draw.text((34, 73),
              f"proposals {len(proposals)} | strict candidates {strict_count} | "
              f"ordinary {ordinary_count} | ambiguous {ambiguous_count} | "
              f"single support {single_count} | upgrades {upgrade_count} | "
              f"insufficient {len(insufficient)} | auto actions 0",
              fill=(178, 192, 205))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return {
        "status": "OK",
        "path": str(output_path),
        "proposal_count": len(proposals),
        "strict_candidate_count": strict_count,
        "ordinary_proposal_count": ordinary_count,
        "ambiguous_support_count": ambiguous_count,
        "single_wall_support_count": single_count,
        "single_wall_upgrade_count": upgrade_count,
        "insufficient_support_count": len(insufficient),
        "auto_model_action_count": 0,
        "legend": {
            "green": "strict approval candidate; no model action",
            "blue": "supported proposal; review required",
            "orange": "ambiguous endpoint support; blocked",
            "yellow": "single-wall endpoint support only; blocked",
            "purple": "atomic replacement required; old model retained",
            "dashed_red": "insufficient endpoint support",
        },
    }


def render_wall_endpoint_review_overlay(walls: list[dict],
                                        endpoint_review: dict,
                                        output_path: Path) -> dict:
    """Render locally zoomed panels for unresolved wall endpoints."""
    from PIL import Image, ImageDraw

    candidates = list(endpoint_review.get("candidates", []))
    if not candidates:
        raise ValueError("no unresolved wall endpoints to render")
    columns = 5
    rows = math.ceil(len(candidates) / columns)
    width, header, gap = 2400, 100, 8
    height = header + rows * 300 + gap
    panel_width = (width - gap * (columns + 1)) / columns
    panel_height = (height - header - gap * (rows + 1)) / rows
    image = Image.new("RGB", (width, height), (20, 24, 30))
    draw = ImageDraw.Draw(image)
    colours = {
        "approved_closed_wall_strip_end_cap_endpoint": (64, 142, 245),
        "exact_paired_wall_end_cap_endpoint": (72, 210, 142),
        "possible_opening_endpoint": (55, 205, 238),
        "opening_topology_evidence_endpoint": (55, 205, 238),
        "door_vector_supported_endpoint": (72, 210, 142),
        "source_supported_omission_endpoint": (255, 76, 72),
        "interior_junction": (255, 177, 66),
        "nearby_network_gap": (255, 177, 66),
        "unresolved_free_endpoint": (210, 100, 230),
    }

    for index, candidate in enumerate(candidates):
        row, column = divmod(index, columns)
        left = gap + column * (panel_width + gap)
        top = header + gap + row * (panel_height + gap)
        right, bottom = left + panel_width, top + panel_height
        draw.rectangle((left, top, right, bottom), fill=(25, 30, 37),
                       outline=(62, 73, 84), width=2)
        center = candidate["point"]
        network = candidate.get("nearest_network") or {}
        target = network.get("closest_point")
        context_radius = 2.2
        if target and float(network.get("distance_m", 0.0)) <= 4.0:
            context_radius = max(
                context_radius,
                abs(float(target[0]) - float(center[0])) + 0.8,
                abs(float(target[1]) - float(center[1])) + 0.8,
            )
        plot_size = min(panel_width - 22, panel_height - 58)
        scale = plot_size / (2 * context_radius)

        def point(value):
            return (
                left + panel_width / 2 +
                (float(value[0]) - float(center[0])) * scale,
                top + 43 + (panel_height - 52) / 2 -
                (float(value[1]) - float(center[1])) * scale,
            )

        for wall in walls:
            xs = [float(wall["start"][0]), float(wall["end"][0])]
            ys = [float(wall["start"][1]), float(wall["end"][1])]
            if (max(xs) < float(center[0]) - context_radius or
                    min(xs) > float(center[0]) + context_radius or
                    max(ys) < float(center[1]) - context_radius or
                    min(ys) > float(center[1]) + context_radius):
                continue
            wall_colour = ((78, 116, 126) if wall.get("id", "").startswith("wall_")
                           else (88, 92, 116))
            draw.line((point(wall["start"]), point(wall["end"])),
                      fill=wall_colour, width=3)

        colour = colours.get(candidate.get("decision_reason"), (230, 230, 230))
        center_px = point(center)
        if target and float(network.get("distance_m", 0.0)) <= 4.0:
            draw.line((center_px, point(target)), fill=colour, width=2)
        source = candidate.get("source_match") or {}
        if source:
            draw.line((point(source["range_start"]), point(source["range_end"])),
                      fill=colour, width=7)
        radius = 7
        draw.ellipse((center_px[0] - radius, center_px[1] - radius,
                      center_px[0] + radius, center_px[1] + radius),
                     fill=colour, outline=(245, 245, 245), width=2)
        reason = candidate.get("decision_reason", "unknown").replace("_", " ")
        distance = network.get("distance_m")
        suffix = f" | nearest {distance:.2f}m" if isinstance(distance, (int, float)) else ""
        draw.text((left + 10, top + 8),
                  f"{candidate.get('wall_id')}:{candidate.get('endpoint')} | {reason}{suffix}",
                  fill=(240, 240, 240))

    draw.rectangle((20, 14, 1120, 88), fill=(10, 13, 17))
    draw.text((32, 24), "Wall endpoint audit | each panel is locally zoomed",
              fill=(235, 235, 235))
    draw.text((32, 47),
              "blue/green: exact intentional cap | orange: network/junction | purple: free | cyan: opening | red: omission",
              fill=(235, 235, 235))
    draw.text((32, 69),
              f"raw {len(candidates)} | resolved cap {endpoint_review.get('resolved_intentional_cap_count', 0)} | review {endpoint_review.get('needs_review_count', len(candidates))} | no endpoint moved",
              fill=(180, 190, 200))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return {
        "status": "OK",
        "path": str(output_path),
        "candidate_count": len(candidates),
        "needs_review_count": endpoint_review.get(
            "needs_review_count", len(candidates)),
        "resolved_intentional_cap_count": endpoint_review.get(
            "resolved_intentional_cap_count", 0),
        "reason_counts": endpoint_review.get("reason_counts", {}),
    }


def render_closed_wall_profile_review_overlay(walls: list[dict],
                                              profile_audit: dict,
                                              output_path: Path) -> dict:
    """Render occurrence-local closed wall-strip evidence without editing it."""
    from PIL import Image, ImageDraw

    candidates = list(profile_audit.get("candidates", []))
    if not candidates:
        raise ValueError("no closed wall-strip candidates to render")

    columns = 3
    rows = math.ceil(len(candidates) / columns)
    width, header, gap = 2400, 118, 10
    height = header + rows * 470 + gap
    panel_width = (width - gap * (columns + 1)) / columns
    panel_height = (height - header - gap * (rows + 1)) / rows
    image = Image.new("RGB", (width, height), (20, 24, 30))
    draw = ImageDraw.Draw(image)
    approved_count = sum(
        item.get("status") == "APPROVED_BY_RULE" for item in candidates)
    review_count = sum(
        item.get("status") == "NEEDS_REVIEW" for item in candidates)
    rejected_count = sum(
        item.get("status") == "REJECTED_BY_RULE" for item in candidates)
    face_count = sum(
        edge.get("edge_role") == "wall_face"
        for item in candidates for edge in item.get("child_evidence", []))
    cap_count = sum(
        edge.get("edge_role") == "end_cap"
        for item in candidates for edge in item.get("child_evidence", []))

    def draw_dashed_line(first, second, *, fill, width_px=3, dash_px=12):
        dx, dy = second[0] - first[0], second[1] - first[1]
        length = math.hypot(dx, dy)
        if length <= 0:
            return
        ux, uy = dx / length, dy / length
        position = 0.0
        while position < length:
            end_position = min(position + dash_px, length)
            draw.line((
                (first[0] + ux * position, first[1] + uy * position),
                (first[0] + ux * end_position,
                 first[1] + uy * end_position),
            ), fill=fill, width=width_px)
            position += dash_px * 2

    for index, candidate in enumerate(candidates):
        row, column = divmod(index, columns)
        left = gap + column * (panel_width + gap)
        top = header + gap + row * (panel_height + gap)
        right, bottom = left + panel_width, top + panel_height
        status = candidate.get("status")
        is_approved = status == "APPROVED_BY_RULE"
        status_colour = ((72, 210, 142) if is_approved else
                         (82, 170, 238) if status == "REJECTED_BY_RULE" else
                         (255, 92, 84))
        draw.rectangle((left, top, right, bottom), fill=(25, 30, 37),
                       outline=status_colour, width=5)

        edges = list(candidate.get("child_evidence", []))
        geometry_points = [
            point
            for edge in edges
            for point in (edge.get("start"), edge.get("end"))
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        centerline = candidate.get("centerline") or {}
        geometry_points.extend(
            point for point in (centerline.get("start"), centerline.get("end"))
            if isinstance(point, (list, tuple)) and len(point) >= 2)
        if not geometry_points:
            geometry_points = [
                point for point in candidate.get("points", [])
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]

        plot_top = top + 92
        plot_bottom = bottom - 58
        if geometry_points:
            xs = [float(point[0]) for point in geometry_points]
            ys = [float(point[1]) for point in geometry_points]
            center_x = (min(xs) + max(xs)) / 2.0
            center_y = (min(ys) + max(ys)) / 2.0
            span_x = max(max(xs) - min(xs), 0.25)
            span_y = max(max(ys) - min(ys), 0.25)
            context_x = span_x * 0.18 + 0.20
            context_y = span_y * 0.35 + 0.20
            min_x, max_x = min(xs) - context_x, max(xs) + context_x
            min_y, max_y = min(ys) - context_y, max(ys) + context_y
            scale = min(
                (panel_width - 34) / (max_x - min_x),
                (plot_bottom - plot_top) / (max_y - min_y),
            )

            def point(value):
                return (
                    left + panel_width / 2.0 +
                    (float(value[0]) - center_x) * scale,
                    (plot_top + plot_bottom) / 2.0 -
                    (float(value[1]) - center_y) * scale,
                )

            for wall in walls:
                wall_start, wall_end = wall.get("start"), wall.get("end")
                if not wall_start or not wall_end:
                    continue
                wall_xs = [float(wall_start[0]), float(wall_end[0])]
                wall_ys = [float(wall_start[1]), float(wall_end[1])]
                if (max(wall_xs) < min_x or min(wall_xs) > max_x or
                        max(wall_ys) < min_y or min(wall_ys) > max_y):
                    continue
                draw.line((point(wall_start), point(wall_end)),
                          fill=(76, 86, 96), width=3)

            role_colours = {
                "wall_face": (55, 205, 238),
                "end_cap": (255, 177, 66),
            }
            for edge in edges:
                edge_start, edge_end = edge.get("start"), edge.get("end")
                if not edge_start or not edge_end:
                    continue
                role = edge.get("edge_role", "unknown")
                colour = role_colours.get(role, (220, 220, 220))
                first, second = point(edge_start), point(edge_end)
                draw.line((first, second), fill=colour, width=8)
                midpoint = ((first[0] + second[0]) / 2.0,
                            (first[1] + second[1]) / 2.0)
                marker = "F" if role == "wall_face" else (
                    "C" if role == "end_cap" else "?")
                draw.text((midpoint[0] + 4, midpoint[1] + 4), marker,
                          fill=colour)
            if centerline.get("start") and centerline.get("end"):
                draw_dashed_line(point(centerline["start"]),
                                 point(centerline["end"]),
                                 fill=(245, 245, 245), width_px=4)
        else:
            draw.text((left + 20, plot_top + 20),
                      "NO EXACT DXF EDGE GEOMETRY", fill=(255, 92, 84))

        handle = (candidate.get("entity_handle") or
                  candidate.get("candidate_id", "unknown"))
        status = candidate.get("status", "UNKNOWN")
        identity = ("COMPLETE" if candidate.get("identity_is_complete")
                    else "INCOMPLETE")
        linetype = candidate.get("linetype_evidence") or {}
        effective_linetype = linetype.get("effective_linetype") or "UNKNOWN"
        blockers = candidate.get("blockers") or []
        blocker_text = ", ".join(str(item) for item in blockers) or "none"
        candidate_face_count = sum(
            edge.get("edge_role") == "wall_face" for edge in edges)
        candidate_cap_count = sum(
            edge.get("edge_role") == "end_cap" for edge in edges)
        draw.rectangle((left + 10, top + 8, left + 190, top + 35),
                       fill=status_colour)
        draw.text((left + 18, top + 15), status, fill=(10, 13, 17))
        draw.text((left + 204, top + 13), f"handle {handle}",
                  fill=(242, 242, 242))
        draw.text((left + 14, top + 43),
                  f"{candidate.get('thickness_mm', 0):.1f} mm | "
                  f"linetype {effective_linetype} | identity {identity}",
                  fill=(195, 205, 215))
        draw.text((left + 14, top + 66), f"blockers: {blocker_text}",
                  fill=status_colour)
        limitations = candidate.get("identity_limitations") or []
        footer = (f"exact DXF edges: {candidate_face_count} wall_face / "
                  f"{candidate_cap_count} end_cap | identity limits: "
                  f"{', '.join(str(item) for item in limitations) or 'none'}")
        draw.text((left + 14, bottom - 38), footer, fill=(180, 190, 200))

    draw.rectangle((20, 14, 1760, 104), fill=(10, 13, 17))
    draw.text((32, 24),
              "Closed wall-strip review | exact DXF occurrence evidence",
              fill=(235, 235, 235))
    draw.text((32, 49),
              "cyan: wall_face | orange: end_cap | white dash: centerline | green/red: decision",
              fill=(235, 235, 235))
    draw.text((32, 74),
              f"candidates {len(candidates)} | approved {approved_count} | "
              f"rejected {rejected_count} | review {review_count} | "
              f"faces {face_count} | caps {cap_count}",
              fill=(180, 190, 200))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return {
        "status": "OK",
        "path": str(output_path),
        "candidate_count": len(candidates),
        "approved_by_rule_count": approved_count,
        "needs_review_count": review_count,
        "rejected_by_rule_count": rejected_count,
        "review_count": review_count,
        "wall_face_count": face_count,
        "end_cap_count": cap_count,
        "cap_count": cap_count,
    }


def render_irregular_profile_review_overlay(columns: list[dict],
                                            structural_walls: list[dict],
                                            profile_audit: dict,
                                            output_path: Path) -> dict:
    """Render exact non-rectangular hatch profiles in selected-plan context."""
    from PIL import Image, ImageDraw

    candidates = list(profile_audit.get("candidates", []))
    if not candidates:
        raise ValueError("no irregular structural hatch profiles to render")
    columns_per_row = 3
    rows = math.ceil(len(candidates) / columns_per_row)
    width, header, gap = 2400, 108, 10
    height = header + rows * 470 + gap
    panel_width = (width - gap * (columns_per_row + 1)) / columns_per_row
    panel_height = (height - header - gap * (rows + 1)) / rows
    image = Image.new("RGB", (width, height), (20, 24, 30))
    draw = ImageDraw.Draw(image)
    colours = {
        "matched_structural_wall_hatch": (55, 205, 238),
        "spatially_overlaps_extracted_column": (255, 177, 66),
        "unresolved_irregular_structural_hatch": (220, 88, 210),
    }

    for index, candidate in enumerate(candidates):
        row, column_index = divmod(index, columns_per_row)
        left = gap + column_index * (panel_width + gap)
        top = header + gap + row * (panel_height + gap)
        right, bottom = left + panel_width, top + panel_height
        draw.rectangle((left, top, right, bottom), fill=(25, 30, 37),
                       outline=(62, 73, 84), width=2)
        points = candidate.get("points_local_m") or []
        if len(points) < 3:
            continue
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        center_x, center_y = ((min(xs) + max(xs)) / 2.0,
                              (min(ys) + max(ys)) / 2.0)
        context_radius = max(max(xs) - min(xs), max(ys) - min(ys), 2.0) / 2.0 + 1.2
        plot_width, plot_height = panel_width - 26, panel_height - 82
        scale = min(plot_width, plot_height) / (2.0 * context_radius)

        def point(value):
            return (
                left + panel_width / 2.0 +
                (float(value[0]) - center_x) * scale,
                top + 58 + plot_height / 2.0 -
                (float(value[1]) - center_y) * scale,
            )

        min_x, max_x = center_x - context_radius, center_x + context_radius
        min_y, max_y = center_y - context_radius, center_y + context_radius
        for wall in structural_walls:
            wall_xs = [float(wall["start"][0]), float(wall["end"][0])]
            wall_ys = [float(wall["start"][1]), float(wall["end"][1])]
            if (max(wall_xs) < min_x or min(wall_xs) > max_x or
                    max(wall_ys) < min_y or min(wall_ys) > max_y):
                continue
            draw.line((point(wall["start"]), point(wall["end"])),
                      fill=(92, 112, 122), width=5)
        for column in columns:
            column_center = column.get("center", [0.0, 0.0])
            size = column.get("size", [0.0, 0.0])
            column_bbox = [
                float(column_center[0]) - float(size[0]) / 2.0,
                float(column_center[1]) - float(size[1]) / 2.0,
                float(column_center[0]) + float(size[0]) / 2.0,
                float(column_center[1]) + float(size[1]) / 2.0,
            ]
            if (column_bbox[2] < min_x or column_bbox[0] > max_x or
                    column_bbox[3] < min_y or column_bbox[1] > max_y):
                continue
            draw.rectangle((point([column_bbox[0], column_bbox[3]]),
                            point([column_bbox[2], column_bbox[1]])),
                           outline=(104, 220, 120), width=3)
        reason = candidate.get("decision_reason", "unknown")
        colour = colours.get(reason, (235, 235, 235))
        polygon = [point(value) for value in points]
        draw.polygon(polygon, outline=colour)
        draw.line(polygon + [polygon[0]], fill=colour, width=7)
        label = candidate.get("entity_handle") or candidate.get("candidate_id", "")[-8:]
        draw.text((left + 12, top + 9),
                  f"{label} | {reason.replace('_', ' ')}",
                  fill=(242, 242, 242))
        draw.text((left + 12, top + 31),
                  f"{candidate.get('width_mm', 0):.0f} x {candidate.get('depth_mm', 0):.0f} mm"
                  f" | wall support {candidate.get('structural_wall_support_ratio', 0):.0%}",
                  fill=(190, 200, 210))

    counts = profile_audit.get("reason_counts", {})
    draw.rectangle((20, 14, 1360, 96), fill=(10, 13, 17))
    draw.text((32, 24),
              "Selected-plan non-rectangular S-WALL-HATC review | exact DXF vectors",
              fill=(235, 235, 235))
    draw.text((32, 49),
              "cyan: wall-hatch match | orange: column overlap | purple: unresolved",
              fill=(235, 235, 235))
    draw.text((32, 73),
              f"raw {profile_audit.get('raw_candidate_count', 0)} | selected unique {len(candidates)} | {counts}",
              fill=(180, 190, 200))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return {
        "status": "OK",
        "path": str(output_path),
        "candidate_count": len(candidates),
        "reason_counts": counts,
    }


def cv_auxiliary_audit(kept_image, suspicious_image, output_dir: Path) -> dict:
    """Overlay cleaned geometry with relevant original-only geometry and scan it."""
    import cv2
    import numpy as np

    kept = np.asarray(kept_image, dtype=np.uint8)
    suspicious = np.asarray(suspicious_image, dtype=np.uint8)
    difference = np.where((suspicious > 0) & (kept == 0), 255, 0).astype(np.uint8)
    height, width = kept.shape
    minimum_length = max(30, int(min(width, height) * 0.025))
    lines = cv2.HoughLinesP(
        difference, 1, np.pi / 720.0, threshold=18,
        minLineLength=minimum_length, maxLineGap=8,
    )
    nearby_kept = cv2.dilate(
        kept, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17)))
    adjacency_pixels = int(np.count_nonzero(
        (difference > 0) & (nearby_kept > 0)))
    suspicious_pixels = int(np.count_nonzero(difference))
    candidates = []
    overlay = np.zeros((height, width, 3), dtype=np.uint8)
    # Cleaned result is deliberately dim; relevant original-only geometry is
    # bright. This makes omissions visible without furniture/text noise.
    overlay[kept > 0] = (58, 70, 62)
    overlay[difference > 0] = (20, 225, 255)
    if lines is not None:
        for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
            probe = np.zeros_like(kept)
            cv2.line(probe, (x1, y1), (x2, y2), 255, 3)
            touches_kept = bool(np.any((probe > 0) & (nearby_kept > 0)))
            if not touches_kept:
                continue
            length = float(math.hypot(x2 - x1, y2 - y1))
            candidates.append({
                "start_px": [int(x1), int(y1)],
                "end_px": [int(x2), int(y2)],
                "length_px": round(length, 1),
            })
            cv2.line(overlay, (x1, y1), (x2, y2), (20, 20, 255), 2,
                     cv2.LINE_AA)
    cv2.imwrite(str(output_dir / "04_cv_auxiliary.png"), overlay)
    cv2.imwrite(str(output_dir / "06_original_cleaned_cv.png"), overlay)
    original_relevant = int(np.count_nonzero((kept > 0) | (suspicious > 0)))
    return {
        "status": "REVIEW" if candidates else "PASS",
        "minimum_candidate_length_px": minimum_length,
        "suspicious_structural_pixels": suspicious_pixels,
        "pixels_adjacent_to_kept_structure": adjacency_pixels,
        "candidate_missing_line_count": len(candidates),
        "relevant_original_pixels": original_relevant,
        "cleaned_pixels": int(np.count_nonzero(kept)),
        "original_only_ratio": round(
            suspicious_pixels / original_relevant, 4) if original_relevant else 0.0,
        "candidates": sorted(
            candidates, key=lambda item: item["length_px"], reverse=True)[:100],
        "overlay": str(output_dir / "06_original_cleaned_cv.png"),
        "legend": {
            "dim_gray_green": "cleaned result",
            "bright_yellow": "relevant original geometry missing after cleaning",
            "red": "CV long-line omission candidate",
        },
    }


def _environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _environment_number(name: str, default, cast):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_yolo_configuration() -> tuple[Path | None, dict]:
    model_value = os.environ.get("DRAWING_YOLO_MODEL_PATH", "").strip()
    model_path = (Path(model_value) if model_value else
                  DEFAULT_YOLO_MODEL if DEFAULT_YOLO_MODEL.is_file() else None)
    return model_path, {
        "confidence": _environment_number(
            "DRAWING_YOLO_CONFIDENCE", 0.10, float),
        "tile_size": _environment_number(
            "DRAWING_YOLO_TILE_SIZE", DEFAULT_YOLO_TILE_SIZE, int),
        "overlap": _environment_number(
            "DRAWING_YOLO_TILE_OVERLAP", DEFAULT_YOLO_TILE_OVERLAP, int),
        "nms_iou": 0.35,
        "device": "cpu",
        "maximum_detections_per_tile": 1000,
    }


def _build_cache_identity(source: Path, model_path: Path | None,
                          yolo_params: dict) -> dict:
    code_paths = (
        Path(__file__),
        PROJECT_ROOT / "scripts" / "geometry_first_clean.py",
        PROJECT_ROOT / "backend" / "engines" / "drawing_audit_cache.py",
        PROJECT_ROOT / "backend" / "engines" / "yolo_drawing.py",
    )
    cv_params = {
        "render_size_px": [2400, 1400],
        "render_margin_px": 30,
        "hough_rho_px": 1,
        "hough_theta_divisor": 720,
        "hough_threshold": 18,
        "minimum_line_ratio": 0.025,
        "minimum_line_floor_px": 30,
        "maximum_line_gap_px": 8,
        "kept_adjacency_kernel_px": [17, 17],
        "candidate_probe_width_px": 3,
        "maximum_candidates": 100,
    }
    cache_model = (model_path if model_path is not None and model_path.is_file()
                   else None)
    environment = capture_environment(AUDIT_CACHE_ENVIRONMENT)
    if environment.get("YOLO_CONFIG_DIR") is None:
        # yolo_drawing applies this same deterministic default before loading
        # Ultralytics. Recording the effective value avoids a false miss after
        # the first inference sets the process environment.
        environment["YOLO_CONFIG_DIR"] = str(
            PROJECT_ROOT / "data" / "runtime")
    return build_audit_cache_identity(
        source,
        code_paths=code_paths,
        yolo_model_path=cache_model,
        cv_params=cv_params,
        yolo_params=yolo_params,
        dependency_versions=collect_dependency_versions(AUDIT_DEPENDENCIES),
        environment_config=environment,
    )


def _contains_failed_execution(value) -> bool:
    failed = {"CANCELLED", "CRASHED", "ERROR", "EXCEPTION", "FAILED", "TIMEOUT"}
    if isinstance(value, dict):
        return any(_contains_failed_execution(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_failed_execution(item) for item in value)
    return isinstance(value, str) and value.upper() in failed


def _cacheable_evidence(result: dict) -> bool:
    if result.get("preview_error"):
        return False
    if _contains_failed_execution(result):
        return False
    yolo_status = (result.get("yolo_wall_audit") or {}).get(
        "inference_status", "NOT_RUN")
    return yolo_status in {"OK", "DISABLED"}


def _audit_artifacts(output_dir: Path, result: dict) -> dict[str, Path]:
    artifacts = {
        name: output_dir / filename
        for name, filename in AUDIT_ARTIFACT_FILES.items()
    }
    if ((result.get("yolo_wall_audit") or {}).get("inference_status") == "OK"):
        artifacts["yolo_detection_overlay"] = (
            output_dir / "08_yolo_detection_overlay.png")
    return artifacts


def _compute_audit_evidence(source: Path, output_dir: Path,
                            document) -> dict:
    source = Path(source).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    reason_counts = Counter()
    type_counts = Counter()
    layer_rows = defaultdict(lambda: {
        "total": 0, "kept": 0, "dropped": 0, "geometry": 0,
        "approx_length": 0.0, "types": Counter(), "reason": "",
    })
    duplicate_keys = set()
    duplicates = 0
    total = 0
    kept_before_duplicates = 0
    text_count = 0
    for block in document.blocks:
        for entity in block:
            total += 1
            kind = entity.dxftype()
            layer = getattr(entity.dxf, "layer", "") or "<NO_LAYER>"
            reason = classify_layer(layer)
            row = layer_rows[layer]
            row["total"] += 1
            row["reason"] = reason
            row["types"][kind] += 1
            type_counts[kind] += 1
            if kind in ("TEXT", "MTEXT"):
                text_count += 1
            if kind in GEOMETRY_TYPES:
                row["geometry"] += 1
                row["approx_length"] += approximate_length(entity)
            if reason.startswith("kept_"):
                kept_before_duplicates += 1
                key = line_key(entity, layer)
                if key is not None and key in duplicate_keys:
                    duplicates += 1
                    row["dropped"] += 1
                    reason_counts["duplicate_line"] += 1
                    continue
                if key is not None:
                    duplicate_keys.add(key)
                row["kept"] += 1
            else:
                row["dropped"] += 1
            reason_counts[reason] += 1

    kept = kept_before_duplicates - duplicates
    suspicious = []
    known_non_wall_exclusions = []
    layers = []
    for name, row in layer_rows.items():
        exclusion_reason = known_non_wall_reason(name)
        item = {
            "layer": name,
            "reason": row["reason"],
            "total": row["total"],
            "kept": row["kept"],
            "dropped": row["dropped"],
            "geometry": row["geometry"],
            "approx_length": round(row["approx_length"], 2),
            "types": dict(row["types"].most_common()),
            "review_status": (
                "REJECTED_BY_RULE" if exclusion_reason else None),
            "decision_reason": exclusion_reason,
        }
        layers.append(item)
        if row["geometry"] and is_suspicious_layer(name):
            suspicious.append(item)
        elif row["geometry"] and exclusion_reason:
            known_non_wall_exclusions.append(item)
    layers.sort(key=lambda item: item["total"], reverse=True)
    suspicious.sort(key=lambda item: item["total"], reverse=True)
    known_non_wall_exclusions.sort(
        key=lambda item: item["total"], reverse=True)

    line_x = []
    for block in document.blocks:
        for entity in block:
            if entity.dxftype() == "LINE":
                line_x.append(float(entity.dxf.start.x))
                if len(line_x) >= 50:
                    break
        if len(line_x) >= 50:
            break
    unit = "UNKNOWN" if line_x and max(line_x) < 1000 else "mm"
    warnings = []
    if unit == "UNKNOWN":
        warnings.append("coordinate magnitude below 1000; unit is ambiguous")
    if suspicious:
        warnings.append("unclassified layers contain structural-looking names")

    result = {
        "source": str(source),
        "source_sha256": _file_sha256(source),
        "source_size_bytes": source.stat().st_size,
        "dxf_version": document.dxfversion,
        "current_step0": {
            "hardcoded_confidence": 0.9,
            "unit": unit,
            "total_entities_in_all_blocks": total,
            "kept_before_duplicates": kept_before_duplicates,
            "duplicate_lines_removed": duplicates,
            "kept_after_duplicates": kept,
            "dropped_or_unclassified": total - kept,
            "kept_ratio": round(kept / total, 4) if total else 0.0,
            "text_entities": text_count,
            "reason_counts": dict(reason_counts),
        },
        "audit": {
            "layer_count": len(document.layers),
            "block_count": len(document.blocks),
            "modelspace_entity_count": len(document.modelspace()),
            "entity_types": dict(type_counts.most_common()),
            "suspicious_unclassified_layers": suspicious,
            "known_non_wall_exclusions": known_non_wall_exclusions,
            "warnings": warnings,
            "layers": layers,
        },
    }
    preview = render_previews(document, output_dir)
    selected_suspicious_names = set(
        preview.get("selected_plan_suspicious_layers") or [])
    selected_suspicious = [
        item for item in suspicious
        if item.get("layer") in selected_suspicious_names
    ]
    unplaced_suspicious = [
        item for item in suspicious
        if item.get("layer") not in selected_suspicious_names
    ]
    result["audit"]["all_block_suspicious_unclassified_layers"] = suspicious
    result["audit"]["suspicious_unclassified_layers"] = selected_suspicious
    result["audit"]["unplaced_suspicious_unclassified_layers"] = (
        unplaced_suspicious)
    result["audit"]["warnings"] = [
        warning for warning in result["audit"]["warnings"]
        if warning != "unclassified layers contain structural-looking names"
    ]
    if selected_suspicious:
        result["audit"]["warnings"].append(
            "selected plan contains unclassified structural-looking layers")
    result["preview_error"] = preview["error"]
    result["cv_auxiliary"] = preview["cv_auxiliary"]
    result["plan_selection"] = preview["plan_selection"]
    model_path, yolo_params = _resolved_yolo_configuration()
    if preview["original_image"]:
        yolo_audit = run_yolo_drawing_audit(
            Path(preview["original_image"]),
            model_path,
            output_overlay=output_dir / "08_yolo_detection_overlay.png",
            reference_mask_path=(
                Path(preview["wall_reference_mask"])
                if preview["wall_reference_mask"] else None),
            confidence=yolo_params["confidence"],
            tile_size=yolo_params["tile_size"],
            overlap=yolo_params["overlap"],
        )
    else:
        yolo_audit = {
            "inference_status": "NOT_RUN",
            "geometry_authority": False,
            "reason": "drawing preview was not available",
        }
    result["yolo_wall_audit"] = yolo_audit
    result["yolo_cv_fusion"] = correlate_cv_candidates(
        yolo_audit.get("detections", []),
        (result["cv_auxiliary"] or {}).get("candidates", []),
    )
    yolo_supported_cv_ids = {
        item.get("candidate_id")
        for item in result["yolo_cv_fusion"].get("candidates", [])
        if item.get("candidate_id")
    }
    result["dxf_review_candidates"] = build_dxf_review_candidates(
        (result["cv_auxiliary"] or {}).get("candidates", []),
        yolo_supported_cv_ids,
    )
    result["known_non_wall_vector_candidates"] = build_dxf_review_candidates([
        {"candidate_id": None, "matched_dxf_segments": [segment]}
        for segment in (preview.get("known_non_wall_segments") or [])
    ])
    result["render_transform"] = preview["render_transform"]
    return result


def _build_cleaning_decision(result: dict) -> dict:
    suspicious = ((result.get("audit") or {}).get(
        "suspicious_unclassified_layers") or [])
    review_reasons = []
    if suspicious:
        review_reasons.append(
            f"{len(suspicious)} structurally named layers were not kept")
    unassociated_cv_count = int(
        ((result["cv_auxiliary"] or {}).get("vector_association") or {}).get(
            "unassociated_candidate_count", 0))
    if unassociated_cv_count:
        review_reasons.append(
            f"{unassociated_cv_count} CV exclusions could not be mapped to DXF")
    dxf_review_count = int(
        (result.get("dxf_review_candidates") or {}).get(
            "needs_review_count", 0))
    if dxf_review_count:
        review_reasons.append(
            f"{dxf_review_count} exact DXF line candidates need semantic review")
    if result.get("preview_error"):
        review_reasons.append("visual cross-check could not be completed")
    model_path, _yolo_params = _resolved_yolo_configuration()
    yolo_required = _environment_flag("DRAWING_YOLO_REQUIRED")
    yolo_audit = result.get("yolo_wall_audit") or {}
    yolo_status = yolo_audit.get("inference_status")
    yolo_advisories = []
    if model_path is not None and yolo_status != "OK":
        yolo_advisories.append(f"YOLO inference did not complete ({yolo_status})")
    elif yolo_status == "OK" and (
            yolo_audit.get("quality") or {}).get("status") != "PASS":
        yolo_advisories.append(
            "YOLO wall detections did not pass vector-reference validation")
    elif yolo_required and model_path is None:
        yolo_advisories.append("YOLO audit is required but no model is configured")
    # YOLO supplies semantic evidence only. Its failure blocks the cleaning
    # Gate only when the deployment explicitly requires that evidence source.
    if yolo_required:
        review_reasons.extend(yolo_advisories)
    return {
        "status": "REVIEW" if review_reasons else "PASS",
        "allow_modeling": not review_reasons,
        "reasons": review_reasons,
        "advisories": yolo_advisories,
        "yolo_required": yolo_required,
        "plain_language": (
            "Do not start modeling until the highlighted exclusions are reviewed."
            if review_reasons else
            "No blocking omission was found by the rule and CV checks."
        ),
    }


def _cache_evidence(result: dict) -> dict:
    """Remove runtime decisions and ambiguous formal-model path fields."""
    evidence = {
        key: value for key, value in result.items()
        if key not in {"audit_cache", "cleaning_decision"}
    }
    yolo = dict(evidence.get("yolo_wall_audit") or {})
    yolo.pop("model_path", None)
    evidence["yolo_wall_audit"] = yolo
    return evidence


def _restore_runtime_evidence(evidence: dict,
                              model_path: Path | None) -> dict:
    result = dict(evidence)
    yolo = dict(result.get("yolo_wall_audit") or {})
    if yolo.get("inference_status") == "OK" and model_path is not None:
        yolo["model_path"] = str(model_path)
    result["yolo_wall_audit"] = yolo
    return result


def audit(source: Path, output_dir: Path, document=None) -> dict:
    """Audit one DXF, caching evidence while recalculating every decision."""
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path, yolo_params = _resolved_yolo_configuration()
    configured_cache = os.environ.get("DRAWING_AUDIT_CACHE_DIR", "").strip()
    cache_dir = (Path(configured_cache).resolve() if configured_cache else
                 output_dir / ".audit_cache")
    identity = None
    cache_key = None
    cache_note = None
    cached = None
    try:
        identity = _build_cache_identity(source, model_path, yolo_params)
        cache_key = audit_cache_key(identity)
        cached = read_audit_cache(cache_dir, identity)
    except (OSError, TypeError, ValueError) as exc:
        cache_note = f"cache identity unavailable: {str(exc)[:180]}"

    if cached is not None:
        result = _restore_runtime_evidence(cached["evidence"], model_path)
        cache_state = "HIT"
    else:
        if document is None:
            document = ezdxf.readfile(source)
        result = _compute_audit_evidence(source, output_dir, document)
        cache_state = "MISS"
        if identity is not None:
            try:
                evidence = _cache_evidence(result)
                if _cacheable_evidence(evidence):
                    write_audit_cache(
                        cache_dir, identity, evidence,
                        _audit_artifacts(output_dir, evidence))
                else:
                    # Replace any prior record for this exact identity with a
                    # fail-closed marker. Partial evidence is never reused.
                    write_audit_cache(
                        cache_dir, identity,
                        {"audit_status": "ERROR"}, {})
                    cache_note = "incomplete audit evidence was not cached"
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                cache_note = f"cache write skipped: {str(exc)[:180]}"

    # Deliberately recomputed on both HIT and MISS. The cache never contains a
    # cleaning authorization, Quality Gate, model.json, or Revit state.
    result["cleaning_decision"] = _build_cleaning_decision(result)
    result["audit_cache"] = {
        "state": cache_state,
        "cache_key": cache_key,
        "evidence_only": True,
        "decision_recomputed": True,
        "note": cache_note,
    }
    (output_dir / "cleaning_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    result = audit(args.source, args.output_dir)
    print(json.dumps({
        "current_step0": result["current_step0"],
        "warnings": result["audit"]["warnings"],
        "suspicious_layers": len(
            result["audit"]["suspicious_unclassified_layers"]),
        "cleaning_decision": result["cleaning_decision"],
        "preview_error": result["preview_error"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
