"""Conservative semantic classification for exact wall-layer symbols.

This module only enriches review evidence.  It never creates wall geometry and
does not depend on layer names or source handles for a semantic decision.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Iterable


_MIN_LEAF_LENGTH_M = 0.60
_MAX_LEAF_LENGTH_M = 1.20
_MIN_LEAF_THICKNESS_MM = 15.0
_MAX_LEAF_THICKNESS_MM = 80.0
_RADIUS_TOLERANCE_M = 0.002
_SWEEP_TOLERANCE_DEG = 1.0
_POINT_TOLERANCE_M = 0.002
_RECTANGLE_TOLERANCE_M = 0.002
_RECTANGLE_ANGLE_TOLERANCE_DEG = 1.0
_WALL_ENDPOINT_TOLERANCE_M = 0.15


def _point(value: Any) -> tuple[float, float] | None:
    try:
        point = float(value[0]), float(value[1])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return point if all(math.isfinite(item) for item in point) else None


def _coordinate_transform(s1: dict) -> Any:
    origin = _point(s1.get("origin"))
    if origin is None:
        return None
    try:
        rotation = float(s1.get("rot_deg", 0.0))
        gcx = float(s1.get("gcx", origin[0]))
        gcy = float(s1.get("gcy", origin[1]))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (rotation, gcx, gcy)):
        return None
    cosine = math.cos(math.radians(rotation))
    sine = math.sin(math.radians(rotation))

    def transform(value: Any) -> tuple[float, float] | None:
        source = _point(value)
        if source is None:
            return None
        x, y = source[0] - gcx, source[1] - gcy
        rotated_x = x * cosine - y * sine + gcx
        rotated_y = x * sine + y * cosine + gcy
        return ((rotated_x - origin[0]) / 1000.0,
                (rotated_y - origin[1]) / 1000.0)

    return transform


def _profile_identity_complete(profile: dict) -> bool:
    segment_ids = profile.get("source_segment_ids")
    valid_segment_ids = (
        isinstance(segment_ids, list) and bool(segment_ids) and
        all(isinstance(value, str) and bool(value) for value in segment_ids)
    )
    required = (
        profile.get("identity_is_complete") is True,
        bool(profile.get("entity_handle")),
        bool(profile.get("drawing_identity")),
        bool(profile.get("source_occurrence_id")),
        bool(profile.get("placed_entity_id")),
        bool(profile.get("structural_occurrence_id")),
        valid_segment_ids,
        valid_segment_ids and len(segment_ids) == len(set(segment_ids)),
    )
    return all(required)


def _rectangle_metrics(profile: dict) -> dict | None:
    values = profile.get("points")
    if not isinstance(values, list) or len(values) != 4:
        return None
    points = [_point(value) for value in values]
    if any(point is None for point in points):
        return None
    points = [point for point in points if point is not None]
    edges = [
        (points[(index + 1) % 4][0] - points[index][0],
         points[(index + 1) % 4][1] - points[index][1])
        for index in range(4)
    ]
    lengths = [math.hypot(*edge) for edge in edges]
    if min(lengths) <= 1e-9:
        return None
    length = max(lengths)
    thickness = min(lengths)
    length_tolerance = max(_RECTANGLE_TOLERANCE_M, length * 0.005)
    thickness_tolerance = max(_RECTANGLE_TOLERANCE_M,
                              thickness * 0.05)
    if (abs(lengths[0] - lengths[2]) > length_tolerance or
            abs(lengths[1] - lengths[3]) > thickness_tolerance):
        return None
    maximum_cosine = math.sin(math.radians(
        _RECTANGLE_ANGLE_TOLERANCE_DEG))
    for index in range(4):
        first = edges[index - 1]
        second = edges[index]
        cosine = abs((first[0] * second[0] + first[1] * second[1]) /
                     (lengths[index - 1] * lengths[index]))
        if cosine > maximum_cosine:
            return None
    return {
        "points": points,
        "edge_lengths": lengths,
        "length_m": length,
        "thickness_mm": thickness * 1000.0,
    }


def _arc_identity(provenance: dict) -> dict | None:
    segment_ids = provenance.get("segment_ids")
    required = (
        provenance.get("drawing_identity"),
        provenance.get("source_entity_handle"),
        provenance.get("source_occurrence_id"),
        provenance.get("placed_entity_id"),
        provenance.get("structural_occurrence_id"),
    )
    if (not all(required) or not isinstance(segment_ids, list) or
            len(segment_ids) != 1 or not segment_ids[0]):
        return None
    return {
        "drawing_identity": provenance["drawing_identity"],
        "entity_handle": provenance["source_entity_handle"],
        "source_occurrence_id": provenance["source_occurrence_id"],
        "placed_entity_id": provenance["placed_entity_id"],
        "structural_occurrence_id": provenance["structural_occurrence_id"],
        "source_segment_id": segment_ids[0],
        "placement_path": deepcopy(provenance.get("placement_path") or []),
    }


def _prepare_arc(record: Any, transform: Any) -> dict | None:
    if not isinstance(record, (list, tuple)) or len(record) < 3:
        return None
    entity, layer, provenance = record[0], record[1], record[2]
    if (getattr(entity, "dxftype", lambda: None)() != "ARC" or
            not isinstance(provenance, dict)):
        return None
    identity = _arc_identity(provenance)
    if identity is None:
        return None
    try:
        center_dxf = entity.dxf.center
        center_source = float(center_dxf.x), float(center_dxf.y)
        radius_dxf = float(entity.dxf.radius)
        start_angle = float(entity.dxf.start_angle)
        end_angle = float(entity.dxf.end_angle)
    except (AttributeError, TypeError, ValueError):
        return None
    if (not all(math.isfinite(value) for value in (
            radius_dxf, start_angle, end_angle)) or radius_dxf <= 0.0):
        return None
    endpoints_source = []
    for angle in (start_angle, end_angle):
        radians = math.radians(angle)
        endpoints_source.append((
            center_source[0] + radius_dxf * math.cos(radians),
            center_source[1] + radius_dxf * math.sin(radians),
        ))
    center = transform(center_source)
    endpoints = [transform(point) for point in endpoints_source]
    if center is None or any(point is None for point in endpoints):
        return None
    endpoints = [point for point in endpoints if point is not None]
    radius_m = sum(math.dist(center, point) for point in endpoints) / 2.0
    sweep_deg = (end_angle - start_angle) % 360.0
    try:
        entity_linetype = str(getattr(entity.dxf, "linetype", "") or "")
    except (AttributeError, TypeError, ValueError):
        entity_linetype = ""
    return {
        **identity,
        "layer": str(layer or ""),
        "entity_linetype": entity_linetype,
        "center": center,
        "endpoints": endpoints,
        "radius_m": radius_m,
        "sweep_deg": sweep_deg,
    }


def _match_arc(rectangle: dict, arc: dict) -> dict | None:
    length = rectangle["length_m"]
    if abs(arc["radius_m"] - length) > max(
            _RADIUS_TOLERANCE_M, length * 0.005):
        return None
    if abs(arc["sweep_deg"] - 90.0) > _SWEEP_TOLERANCE_DEG:
        return None
    points = rectangle["points"]
    hinge_index, hinge_error = min(enumerate(
        math.dist(arc["center"], point) for point in points),
        key=lambda item: item[1])
    if hinge_error > _POINT_TOLERANCE_M:
        return None
    neighbor_indices = ((hinge_index - 1) % 4, (hinge_index + 1) % 4)
    free_candidates = [
        index for index in neighbor_indices
        if abs(math.dist(points[hinge_index], points[index]) - length) <=
        max(_RECTANGLE_TOLERANCE_M, length * 0.005)
    ]
    if len(free_candidates) != 1:
        return None
    free_index = free_candidates[0]
    free_endpoint_error = min(
        math.dist(endpoint, points[free_index])
        for endpoint in arc["endpoints"])
    if free_endpoint_error > _POINT_TOLERANCE_M:
        return None
    return {
        "hinge_corner_index": hinge_index,
        "free_corner_index": free_index,
        "hinge_error_m": hinge_error,
        "free_endpoint_error_m": free_endpoint_error,
        "arc_radius_m": arc["radius_m"],
        "arc_sweep_deg": arc["sweep_deg"],
    }


def _point_segment_distance(point: tuple[float, float], start: Any,
                            end: Any) -> float | None:
    first, second = _point(start), _point(end)
    if first is None or second is None:
        return None
    dx, dy = second[0] - first[0], second[1] - first[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.dist(point, first)
    ratio = max(0.0, min(1.0, (
        (point[0] - first[0]) * dx + (point[1] - first[1]) * dy
    ) / length_sq))
    projection = first[0] + ratio * dx, first[1] + ratio * dy
    return math.dist(point, projection)


def _wall_endpoint_support(profile: dict, walls: list[dict]) -> dict:
    centerline = profile.get("centerline") or {}
    endpoints = [_point(centerline.get(name)) for name in ("start", "end")]
    if any(point is None for point in endpoints):
        return {"both_ends_supported": True, "endpoint_distances_m": []}
    endpoints = [point for point in endpoints if point is not None]
    distances = []
    for endpoint in endpoints:
        choices = []
        for wall in walls:
            if (not isinstance(wall, dict) or
                    wall.get("source_candidate_id") ==
                    profile.get("candidate_id")):
                continue
            distance = _point_segment_distance(
                endpoint, wall.get("start"), wall.get("end"))
            if distance is not None:
                choices.append(distance)
        distances.append(min(choices) if choices else None)
    return {
        "both_ends_supported": all(
            distance is not None and
            distance <= _WALL_ENDPOINT_TOLERANCE_M
            for distance in distances),
        "endpoint_distances_m": [
            round(distance, 6) if distance is not None else None
            for distance in distances
        ],
        "endpoint_tolerance_m": _WALL_ENDPOINT_TOLERANCE_M,
    }


def _statistics(profile_count: int) -> dict:
    return {
        "status": "COMPLETE",
        "profile_count": profile_count,
        "eligible_profile_count": 0,
        "matched_profile_count": 0,
        "missing_arc_match_count": 0,
        "ambiguous_arc_match_count": 0,
        "identity_incomplete_profile_count": 0,
        "invalid_profile_geometry_count": 0,
        "out_of_range_profile_count": 0,
        "two_ended_wall_network_blocked_count": 0,
        "arc_record_count": 0,
        "valid_arc_record_count": 0,
        "invalid_arc_record_count": 0,
        "matches": [],
        "errors": [],
    }


def classify_door_leaf_swing_profiles(
        closed_profile_audit: dict,
        arc_records: Iterable[Any],
        s1: dict,
        wall_network: Iterable[dict] | None = None) -> tuple[dict, dict]:
    """Return a copied audit enriched with exact door-leaf/swing evidence.

    A classification requires one and only one exact ARC in the same drawing
    and structural occurrence.  Missing, ambiguous, incomplete, or wall-like
    evidence remains ``NEEDS_REVIEW``.
    """
    if not isinstance(closed_profile_audit, dict):
        statistics = _statistics(0)
        statistics.update({"status": "ERROR",
                           "errors": ["invalid_closed_profile_audit"]})
        return {}, statistics
    enriched = deepcopy(closed_profile_audit)
    candidates = enriched.get("candidates")
    profile_count = len(candidates) if isinstance(candidates, list) else 0
    statistics = _statistics(profile_count)
    if not isinstance(candidates, list):
        statistics.update({"status": "ERROR",
                           "errors": ["invalid_candidate_list"]})
        return enriched, statistics
    if (not isinstance(s1, dict) or
            (transform := _coordinate_transform(s1)) is None):
        statistics.update({"status": "ERROR",
                           "errors": ["invalid_coordinate_system"]})
        return enriched, statistics
    if wall_network is None:
        walls = []
    else:
        try:
            walls = list(wall_network)
        except TypeError:
            statistics.update({"status": "ERROR",
                               "errors": ["invalid_wall_network"]})
            return enriched, statistics
        if any(not isinstance(wall, dict) or
               _point(wall.get("start")) is None or
               _point(wall.get("end")) is None for wall in walls):
            statistics.update({"status": "ERROR",
                               "errors": ["invalid_wall_network"]})
            return enriched, statistics
    try:
        source_arcs = list(arc_records or [])
    except TypeError:
        statistics.update({"status": "ERROR",
                           "errors": ["invalid_arc_records"]})
        return enriched, statistics
    statistics["arc_record_count"] = len(source_arcs)
    arcs = []
    for record in source_arcs:
        prepared = _prepare_arc(record, transform)
        if prepared is None:
            statistics["invalid_arc_record_count"] += 1
        else:
            arcs.append(prepared)
    statistics["valid_arc_record_count"] = len(arcs)

    for profile in candidates:
        if not isinstance(profile, dict) or profile.get("status") != "NEEDS_REVIEW":
            continue
        if not _profile_identity_complete(profile):
            statistics["identity_incomplete_profile_count"] += 1
            continue
        rectangle = _rectangle_metrics(profile)
        if rectangle is None:
            statistics["invalid_profile_geometry_count"] += 1
            continue
        if not (_MIN_LEAF_LENGTH_M <= rectangle["length_m"] <=
                _MAX_LEAF_LENGTH_M and
                _MIN_LEAF_THICKNESS_MM <= rectangle["thickness_mm"] <=
                _MAX_LEAF_THICKNESS_MM):
            statistics["out_of_range_profile_count"] += 1
            continue
        statistics["eligible_profile_count"] += 1
        matches = []
        for arc in arcs:
            if (arc["drawing_identity"] != profile["drawing_identity"] or
                    arc["structural_occurrence_id"] !=
                    profile["structural_occurrence_id"]):
                continue
            geometry_match = _match_arc(rectangle, arc)
            if geometry_match is not None:
                matches.append((arc, geometry_match))
        if not matches:
            statistics["missing_arc_match_count"] += 1
            continue
        if len(matches) != 1:
            statistics["ambiguous_arc_match_count"] += 1
            continue
        wall_support = _wall_endpoint_support(profile, walls)
        if wall_support["both_ends_supported"]:
            statistics["two_ended_wall_network_blocked_count"] += 1
            continue
        arc, geometry_match = matches[0]
        profile["prior_status"] = profile.get("status")
        profile["prior_decision_reason"] = profile.get("decision_reason")
        profile["status"] = "REJECTED_BY_RULE"
        profile["decision_reason"] = "exact_door_leaf_swing_topology"
        profile["review_bucket"] = "EXACT_DOOR_LEAF_SWING"
        profile["auto_action"] = "NONE"
        profile["wall_semantics_confirmed"] = False
        evidence = {
            "geometry_source": "DXF_VECTOR",
            "classification": "DOOR_LEAF_SWING_SYMBOL",
            "profile_candidate_id": profile.get("candidate_id"),
            "profile_entity_handle": profile.get("entity_handle"),
            "profile_source_segment_ids": list(
                profile.get("source_segment_ids") or []),
            "arc_entity_handle": arc["entity_handle"],
            "arc_source_occurrence_id": arc["source_occurrence_id"],
            "arc_placed_entity_id": arc["placed_entity_id"],
            "arc_source_segment_id": arc["source_segment_id"],
            "drawing_identity": arc["drawing_identity"],
            "structural_occurrence_id": arc["structural_occurrence_id"],
            "arc_layer": arc["layer"],
            "arc_entity_linetype": arc["entity_linetype"],
            "profile_length_m": round(rectangle["length_m"], 6),
            "profile_thickness_mm": round(
                rectangle["thickness_mm"], 3),
            "arc_radius_m": round(geometry_match["arc_radius_m"], 6),
            "arc_sweep_deg": round(geometry_match["arc_sweep_deg"], 6),
            "hinge_corner_index": geometry_match["hinge_corner_index"],
            "free_corner_index": geometry_match["free_corner_index"],
            "hinge_error_m": round(geometry_match["hinge_error_m"], 9),
            "free_endpoint_error_m": round(
                geometry_match["free_endpoint_error_m"], 9),
            "wall_network_support": wall_support,
            "auto_action": "NONE",
        }
        profile["door_leaf_swing_evidence"] = evidence
        statistics["matched_profile_count"] += 1
        statistics["matches"].append({
            "profile_candidate_id": profile.get("candidate_id"),
            "profile_entity_handle": profile.get("entity_handle"),
            "arc_entity_handle": arc["entity_handle"],
            "arc_source_segment_id": arc["source_segment_id"],
        })

    enriched["candidate_count"] = len(candidates)
    enriched["approved_by_rule_count"] = sum(
        candidate.get("status") == "APPROVED_BY_RULE"
        for candidate in candidates if isinstance(candidate, dict))
    enriched["needs_review_count"] = sum(
        candidate.get("status") == "NEEDS_REVIEW"
        for candidate in candidates if isinstance(candidate, dict))
    enriched["rejected_by_rule_count"] = sum(
        candidate.get("status") == "REJECTED_BY_RULE"
        for candidate in candidates if isinstance(candidate, dict))
    enriched["status"] = (
        "REVIEW" if enriched["needs_review_count"] else "PASS")
    return enriched, statistics


__all__ = ["classify_door_leaf_swing_profiles"]
