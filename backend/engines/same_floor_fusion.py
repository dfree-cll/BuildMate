"""Fuse architecture and structure review IRs for one physical floor.

The output is deliberately review-only.  This module performs no file I/O and
does not emit the executable ``model.json`` schema used by the Revit pipeline.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from backend.engines.wall_segmentation_equivalence import (
    prove_wall_segmentation_equivalence,
)


REVIEW_IR_SCHEMA = "buildmate.review-ir/1.0"
FUSION_SCHEMA = "buildmate.same-floor-fusion-review/1.0"
ALIGNMENT_TOLERANCE_M = 0.001
SIZE_TOLERANCE_M = 0.001
AXIS_TOLERANCE_M = 0.001
THICKNESS_TOLERANCE_MM = 1.0
_MATRIX_TOLERANCE = 1e-8
_GRID_CONTRACT_TOLERANCE_MM = 0.001
_ROTATION_TOLERANCE_DEG = 1e-6
_COLUMN_CENTER_PROVENANCE_TOLERANCE_M = 0.005
_RECTANGLE_ANGLE_TOLERANCE_DEG = 2.0
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_FLOOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _point(value: Any, name: str) -> tuple[float, float]:
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or
            len(value) != 2):
        raise ValueError(f"{name} must contain exactly two coordinates")
    return (
        _finite_number(value[0], f"{name}[0]"),
        _finite_number(value[1], f"{name}[1]"),
    )


def _source(ir: Mapping[str, Any], discipline: str) -> dict[str, Any]:
    if ir.get("schema_version") != REVIEW_IR_SCHEMA:
        raise ValueError(f"{discipline} input is not {REVIEW_IR_SCHEMA}")
    if ir.get("artifact_role") != "REVIEW_ONLY":
        raise ValueError(f"{discipline} input must be REVIEW_ONLY")
    source = _mapping(ir.get("source"), f"{discipline}.source")
    path = source.get("path")
    sha256 = str(source.get("sha256") or "").lower()
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{discipline}.source.path is required")
    if not _SHA256_PATTERN.fullmatch(sha256):
        raise ValueError(f"{discipline}.source.sha256 is invalid")
    return {
        "source_id": f"{discipline}-{sha256[:12]}",
        "discipline": discipline,
        "path": path,
        "sha256": sha256,
    }


def _coordinate_contract(ir: Mapping[str, Any], discipline: str) -> dict[str, Any]:
    coordinate = _mapping(
        ir.get("coordinate_system"), f"{discipline}.coordinate_system")
    if coordinate.get("unit") != "m":
        raise ValueError(f"{discipline} coordinate unit must be m")
    return {
        "unit": "m",
        "origin": _point(
            coordinate.get("origin_dxf_mm"),
            f"{discipline}.coordinate_system.origin_dxf_mm",
        ),
        "rotation_deg": _finite_number(
            coordinate.get("rotation_deg"),
            f"{discipline}.coordinate_system.rotation_deg",
        ),
        "grid_center": _point(
            coordinate.get("grid_center_dxf_mm"),
            f"{discipline}.coordinate_system.grid_center_dxf_mm",
        ),
    }


def _rotate(point: tuple[float, float], angle_deg: float,
            center: tuple[float, float]) -> tuple[float, float]:
    radians = math.radians(angle_deg)
    cosine, sine = math.cos(radians), math.sin(radians)
    x = point[0] - center[0]
    y = point[1] - center[1]
    return (
        x * cosine - y * sine + center[0],
        x * sine + y * cosine + center[1],
    )


def _transform_point(point: tuple[float, float], source: Mapping[str, Any],
                     target: Mapping[str, Any]) -> tuple[float, float]:
    source_rotated_mm = (
        point[0] * 1000.0 + source["origin"][0],
        point[1] * 1000.0 + source["origin"][1],
    )
    world_mm = _rotate(
        source_rotated_mm,
        -source["rotation_deg"],
        source["grid_center"],
    )
    target_rotated_mm = _rotate(
        world_mm,
        target["rotation_deg"],
        target["grid_center"],
    )
    return (
        (target_rotated_mm[0] - target["origin"][0]) / 1000.0,
        (target_rotated_mm[1] - target["origin"][1]) / 1000.0,
    )


def _affine_matrix(source: Mapping[str, Any],
                   target: Mapping[str, Any]) -> list[list[float]]:
    origin = _transform_point((0.0, 0.0), source, target)
    x_basis = _transform_point((1.0, 0.0), source, target)
    y_basis = _transform_point((0.0, 1.0), source, target)
    return [
        [x_basis[0] - origin[0], y_basis[0] - origin[0], origin[0]],
        [x_basis[1] - origin[1], y_basis[1] - origin[1], origin[1]],
        [0.0, 0.0, 1.0],
    ]


def _rounded(value: float, digits: int = 9) -> float:
    result = round(float(value), digits)
    return 0.0 if result == 0 else result


def _rounded_point(point: tuple[float, float]) -> list[float]:
    return [_rounded(point[0]), _rounded(point[1])]


def _distance(left: tuple[float, float],
              right: tuple[float, float]) -> float:
    return math.hypot(left[0] - right[0], left[1] - right[1])


def _geometry_list(ir: Mapping[str, Any], key: str,
                   discipline: str) -> list[Mapping[str, Any]]:
    geometry = _mapping(ir.get("geometry"), f"{discipline}.geometry")
    values = geometry.get(key)
    if not isinstance(values, list):
        raise ValueError(f"{discipline}.geometry.{key} must be a list")
    if not all(isinstance(value, Mapping) for value in values):
        raise ValueError(f"{discipline}.geometry.{key} contains invalid items")
    return values


def _identity_matches(value: Any, sha256: str) -> bool:
    return str(value or "").lower() == f"sha256:{sha256}"


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_placement_path(value: Any, name: str) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty placement path")
    for index, marker in enumerate(value):
        marker = _mapping(marker, f"{name}[{index}]")
        if (not _nonempty_text(marker.get("insert_handle")) or
                not _nonempty_text(
                    marker.get("block_name") or marker.get("name"))):
            raise ValueError(f"{name}[{index}] has incomplete insert identity")
        transform = _mapping(
            marker.get("cumulative_transform"),
            f"{name}[{index}].cumulative_transform",
        )
        matrix = transform.get("affine_2d")
        if (not isinstance(matrix, list) or len(matrix) != 3 or
                any(not isinstance(row, list) or len(row) != 3
                    for row in matrix)):
            raise ValueError(f"{name}[{index}] has an invalid affine transform")
        for row_index, row in enumerate(matrix):
            for column_index, value in enumerate(row):
                _finite_number(
                    value,
                    f"{name}[{index}].affine_2d[{row_index}]"
                    f"[{column_index}]",
                )


def _validate_rectangular_column_geometry(
    item: Mapping[str, Any],
    profile: Mapping[str, Any],
    references: list[Mapping[str, Any]],
    name: str,
) -> None:
    if len(references) != 4:
        raise ValueError(f"{name} rectangular profile must have four edges")
    clusters: list[tuple[float, float]] = []

    def cluster_index(point: tuple[float, float]) -> int:
        for index, existing in enumerate(clusters):
            if _distance(point, existing) <= SIZE_TOLERANCE_M:
                return index
        clusters.append(point)
        return len(clusters) - 1

    edges = []
    for index, reference in enumerate(references):
        start = _point(
            reference.get("start_local_m"),
            f"{name}.source_segment_refs[{index}].start_local_m",
        )
        end = _point(
            reference.get("end_local_m"),
            f"{name}.source_segment_refs[{index}].end_local_m",
        )
        start_index = cluster_index(start)
        end_index = cluster_index(end)
        if start_index == end_index:
            raise ValueError(f"{name} contains a zero-length profile edge")
        edges.append((start_index, end_index))
    if len(clusters) != 4:
        raise ValueError(f"{name} profile edges do not form four corners")

    adjacency = {index: [] for index in range(4)}
    for start_index, end_index in edges:
        adjacency[start_index].append(end_index)
        adjacency[end_index].append(start_index)
    if any(len(neighbours) != 2 for neighbours in adjacency.values()):
        raise ValueError(f"{name} profile edges do not form one closed loop")

    ordered_indexes = [0]
    previous = None
    current = 0
    while len(ordered_indexes) < 4:
        candidates = [index for index in adjacency[current]
                      if index != previous]
        if not candidates:
            raise ValueError(f"{name} profile edges do not form one closed loop")
        following = candidates[0]
        if following in ordered_indexes:
            raise ValueError(f"{name} profile edges close before four corners")
        ordered_indexes.append(following)
        previous, current = current, following
    if ordered_indexes[0] not in adjacency[ordered_indexes[-1]]:
        raise ValueError(f"{name} profile edges do not form one closed loop")

    vertices = [clusters[index] for index in ordered_indexes]
    vectors = [
        (
            vertices[(index + 1) % 4][0] - vertices[index][0],
            vertices[(index + 1) % 4][1] - vertices[index][1],
        )
        for index in range(4)
    ]
    lengths = [math.hypot(*vector) for vector in vectors]
    if any(length <= SIZE_TOLERANCE_M for length in lengths):
        raise ValueError(f"{name} contains a degenerate profile edge")
    perpendicular_limit = math.sin(
        math.radians(_RECTANGLE_ANGLE_TOLERANCE_DEG))
    for index, vector in enumerate(vectors):
        axis_deviation = min(abs(vector[0]), abs(vector[1])) / lengths[index]
        if axis_deviation > perpendicular_limit:
            raise ValueError(
                f"{name} source profile is not axis-aligned in local coordinates")
        following = vectors[(index + 1) % 4]
        normalized_dot = abs(
            vector[0] * following[0] + vector[1] * following[1]
        ) / (lengths[index] * lengths[(index + 1) % 4])
        if normalized_dot > perpendicular_limit:
            raise ValueError(f"{name} source profile is not rectangular")
    if (abs(lengths[0] - lengths[2]) > SIZE_TOLERANCE_M or
            abs(lengths[1] - lengths[3]) > SIZE_TOLERANCE_M):
        raise ValueError(f"{name} opposite profile edges have different lengths")

    xs = [point[0] for point in vertices]
    ys = [point[1] for point in vertices]
    reconstructed_center = (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
    )
    reconstructed_size = (max(xs) - min(xs), max(ys) - min(ys))
    profile_center = _point(
        profile.get("center_local_m"),
        f"{name}.source_profile_ref.center_local_m")
    profile_size = _point(
        profile.get("size_local_m"),
        f"{name}.source_profile_ref.size_local_m")
    column_center, column_size = _column_geometry(item, name)
    if _distance(reconstructed_center, profile_center) > SIZE_TOLERANCE_M:
        raise ValueError(f"{name} profile center disagrees with its edges")
    if max(
        abs(reconstructed_size[0] - profile_size[0]),
        abs(reconstructed_size[1] - profile_size[1]),
    ) > SIZE_TOLERANCE_M:
        raise ValueError(f"{name} profile size disagrees with its edges")
    if (_distance(column_center, profile_center) >
            _COLUMN_CENTER_PROVENANCE_TOLERANCE_M):
        raise ValueError(f"{name} modeled center disagrees with source profile")
    if max(
        abs(column_size[0] - profile_size[0]),
        abs(column_size[1] - profile_size[1]),
    ) > SIZE_TOLERANCE_M:
        raise ValueError(f"{name} modeled size disagrees with source profile")


def _validate_provenance(item: Mapping[str, Any], sha256: str, name: str,
                         *, column: bool = False) -> None:
    references = item.get("source_segment_refs")
    if not isinstance(references, list) or not references:
        raise ValueError(f"{name} has no source segment references")
    if not all(
        isinstance(reference, Mapping) and
        _identity_matches(reference.get("drawing_identity"), sha256)
        for reference in references
    ):
        raise ValueError(f"{name} source segment identity is incomplete")
    segment_ids = []
    for index, reference in enumerate(references):
        required_ids = (
            "source_occurrence_id", "placed_entity_id", "source_segment_id")
        if not all(_nonempty_text(reference.get(key)) for key in required_ids):
            raise ValueError(f"{name} source reference {index} is incomplete")
        segment_ids.append(reference["source_segment_id"])
    declared_segment_ids = item.get("source_segment_ids")
    if ((not column or declared_segment_ids is not None) and
            (not isinstance(declared_segment_ids, list) or
             set(declared_segment_ids) != set(segment_ids))):
        raise ValueError(f"{name} source segment IDs do not match references")

    if column:
        profile = item.get("source_profile_ref")
        if (not isinstance(profile, Mapping) or
                not _identity_matches(profile.get("drawing_identity"), sha256)):
            raise ValueError(f"{name} source profile identity is incomplete")
        profile_ids = (
            "selected_structural_occurrence_id",
            "source_structural_occurrence_id",
            "source_occurrence_id",
            "placed_entity_id",
            "source_entity_handle",
        )
        if not all(_nonempty_text(profile.get(key)) for key in profile_ids):
            raise ValueError(f"{name} source profile identity is incomplete")
        if (profile.get("entity_closed") is not True or
                profile.get("vertex_count") != 4):
            raise ValueError(f"{name} is not a verified rectangular profile")
        profile_segment_ids = profile.get("source_segment_ids")
        if (not isinstance(profile_segment_ids, list) or
                set(profile_segment_ids) != set(segment_ids)):
            raise ValueError(f"{name} profile edges do not match source references")
        _validate_placement_path(
            profile.get("placement_path"), f"{name}.source_profile_ref.placement_path")
        for index, reference in enumerate(references):
            for key in profile_ids[:-1]:
                if reference.get(key) != profile.get(key):
                    raise ValueError(
                        f"{name} source reference {index} disagrees with profile")
            if reference.get("source_entity_handle") != profile.get(
                    "source_entity_handle"):
                raise ValueError(
                    f"{name} source reference {index} disagrees with profile")
        _validate_rectangular_column_geometry(
            item, profile, references, name)
        return

    wall_reference_keys = set()
    for index, reference in enumerate(references):
        required_ids = ("structural_occurrence_id", "entity_handle")
        if not all(_nonempty_text(reference.get(key)) for key in required_ids):
            raise ValueError(f"{name} source reference {index} is incomplete")
        if (reference.get("identity_is_complete") is not True or
                reference.get("identity_limitations") not in ([], ())):
            raise ValueError(f"{name} source reference {index} identity is incomplete")
        for key in ("source_record_index", "segment_index"):
            value = reference.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} source reference {index} has invalid {key}")
        interval = reference.get("source_interval_m")
        if (not isinstance(interval, Sequence) or
                isinstance(interval, (str, bytes)) or len(interval) != 2):
            raise ValueError(f"{name} source reference {index} interval is invalid")
        interval_start = _finite_number(
            interval[0], f"{name}.source_segment_refs[{index}].interval[0]")
        interval_end = _finite_number(
            interval[1], f"{name}.source_segment_refs[{index}].interval[1]")
        if interval_end < interval_start:
            raise ValueError(f"{name} source reference {index} interval is invalid")
        reference_key = (
            reference["source_segment_id"],
            round(interval_start, 9),
            round(interval_end, 9),
        )
        if reference_key in wall_reference_keys:
            raise ValueError(f"{name} contains a duplicate source interval")
        wall_reference_keys.add(reference_key)
        _validate_placement_path(
            reference.get("placement_path"),
            f"{name}.source_segment_refs[{index}].placement_path",
        )


def _column_geometry(column: Mapping[str, Any], name: str
                     ) -> tuple[tuple[float, float], tuple[float, float]]:
    center = _point(column.get("center"), f"{name}.center")
    size = _point(column.get("size"), f"{name}.size")
    if size[0] <= 0 or size[1] <= 0:
        raise ValueError(f"{name}.size must be positive")
    return center, size


def _match_columns(architecture_columns: list[Mapping[str, Any]],
                   structure_columns: list[Mapping[str, Any]],
                   structure_coordinate: Mapping[str, Any],
                   architecture_coordinate: Mapping[str, Any]
                   ) -> tuple[list[tuple[Mapping[str, Any], Mapping[str, Any],
                                         tuple[float, float], float]], float]:
    if not architecture_columns or len(architecture_columns) != len(structure_columns):
        raise ValueError("column counts must be equal and non-zero")

    transformed: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for index, column in enumerate(structure_columns):
        center, size = _column_geometry(column, f"structure column {index}")
        transformed.append((
            _transform_point(center, structure_coordinate,
                             architecture_coordinate),
            size,
        ))

    matches = []
    used_structure_indexes = set()
    max_residual = 0.0
    for architecture_index, architecture_column in enumerate(architecture_columns):
        center, size = _column_geometry(
            architecture_column, f"architecture column {architecture_index}")
        candidates = []
        for structure_index, (transformed_center, structure_size) in enumerate(
                transformed):
            residual = _distance(center, transformed_center)
            size_residual = max(
                abs(size[0] - structure_size[0]),
                abs(size[1] - structure_size[1]),
            )
            if (residual <= ALIGNMENT_TOLERANCE_M and
                    size_residual <= SIZE_TOLERANCE_M):
                candidates.append((structure_index, residual))
        if len(candidates) != 1:
            raise ValueError(
                "column alignment is not one-to-one: architecture column "
                f"{architecture_index} has {len(candidates)} matches")
        structure_index, residual = candidates[0]
        if structure_index in used_structure_indexes:
            raise ValueError("column alignment maps multiple columns to one source")
        used_structure_indexes.add(structure_index)
        max_residual = max(max_residual, residual)
        matches.append((
            architecture_column,
            structure_columns[structure_index],
            transformed[structure_index][0],
            residual,
        ))
    if len(used_structure_indexes) != len(structure_columns):
        raise ValueError("column alignment left unmatched structure columns")
    return matches, max_residual


def _millimetres(value_m: float) -> int:
    return int(round(value_m * 1000.0))


def _stable_id(floor_code: str, kind: str,
               geometry_key: Mapping[str, Any]) -> str:
    payload = {
        "floor": floor_code,
        "kind": kind,
        "geometry": geometry_key,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    return f"{floor_code}-{kind}-{digest}"


def _ordered_endpoints(start: tuple[float, float], end: tuple[float, float]
                       ) -> tuple[tuple[float, float], tuple[float, float]]:
    return (start, end) if start <= end else (end, start)


def _column_id(floor_code: str, center: tuple[float, float],
               size: tuple[float, float]) -> str:
    return _stable_id(floor_code, "COLUMN", {
        "center_mm": [_millimetres(center[0]), _millimetres(center[1])],
        "size_mm": [_millimetres(size[0]), _millimetres(size[1])],
    })


def _wall_id(floor_code: str, kind: str, start: tuple[float, float],
             end: tuple[float, float], thickness_mm: float) -> str:
    start, end = _ordered_endpoints(start, end)
    return _stable_id(floor_code, kind, {
        "endpoints_mm": [
            [_millimetres(start[0]), _millimetres(start[1])],
            [_millimetres(end[0]), _millimetres(end[1])],
        ],
        "thickness_mm": int(round(thickness_mm)),
    })


def _selected_plan_name(ir: Mapping[str, Any], discipline: str) -> str:
    plan_selection = _mapping(
        ir.get("plan_selection"), f"{discipline}.plan_selection")
    selected = _mapping(
        plan_selection.get("selected"), f"{discipline}.plan_selection.selected")
    name = selected.get("block_name") or selected.get("name")
    if not _nonempty_text(name):
        raise ValueError(f"{discipline} selected plan name is missing")
    return str(name)


def _floor_number(value: str, name: str) -> int:
    match = re.search(
        r"(?i)B0*(\d+)(?=层|[^A-Za-z0-9]|$)", value)
    if not match:
        raise ValueError(f"{name} does not declare a basement floor")
    return int(match.group(1))


def _elevation_range(value: str) -> tuple[float, float] | None:
    match = re.search(
        r"\(\s*(-?\d+(?:\.\d+)?)\s*[~～]\s*"
        r"(-?\d+(?:\.\d+)?)\s*m?\s*\)",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    start = float(match.group(1))
    end = float(match.group(2))
    return (min(start, end), max(start, end))


def _is_identity_affine(matrix: Any) -> bool:
    expected = (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    if (not isinstance(matrix, list) or len(matrix) != 3 or
            any(not isinstance(row, list) or len(row) != 3
                for row in matrix)):
        return False
    for row_index, row in enumerate(matrix):
        for column_index, value in enumerate(row):
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return False
            if (not math.isfinite(number) or
                    abs(number - expected[row_index][column_index]) >
                    _MATRIX_TOLERANCE):
                return False
    return True


def _nested_elevation_marker(
    placement_path: Any,
    selected_handle: str,
    name: str,
) -> tuple[Mapping[str, Any], tuple[float, float], list[Any]] | None:
    _validate_placement_path(placement_path, name)
    selected_indexes = [
        index for index, marker in enumerate(placement_path)
        if marker.get("insert_handle") == selected_handle
    ]
    if len(selected_indexes) != 1:
        raise ValueError(
            f"{name} does not contain the selected architecture plan exactly once")
    selected_index = selected_indexes[0]
    for marker_index in range(selected_index + 1, len(placement_path)):
        marker = _mapping(
            placement_path[marker_index], f"{name}[{marker_index}]")
        marker_name = str(marker.get("block_name") or marker.get("name") or "")
        elevation = _elevation_range(marker_name)
        if elevation is None:
            continue
        transform = _mapping(
            marker.get("cumulative_transform"),
            f"{name}[{marker_index}].cumulative_transform",
        )
        if not _is_identity_affine(transform.get("affine_2d")):
            raise ValueError(
                "architecture nested elevation marker is not identity-aligned")
        return marker, elevation, copy.deepcopy(
            placement_path[:marker_index + 1])
    return None


def _architecture_nested_elevation_reference(
        architecture_ir: Mapping[str, Any]) -> dict[str, Any] | None:
    """Verify one elevation-bearing structural reference nested in the plan.

    This is deliberately narrower than treating the architecture plan as an
    elevation declaration.  Every formal column profile and every structural
    wall source reference must descend through the same identity-aligned block
    occurrence, and the architecture traceability Gate must already agree with
    the geometry counts.
    """
    plan_selection = _mapping(
        architecture_ir.get("plan_selection"), "architecture.plan_selection")
    selected = _mapping(
        plan_selection.get("selected"),
        "architecture.plan_selection.selected",
    )
    selected_handle = selected.get("insert_handle")
    if not _nonempty_text(selected_handle):
        return None
    selected_handle = str(selected_handle)

    traceability_passed, _traceability_reasons = _traceability_gate(
        architecture_ir, "architecture")
    if not traceability_passed:
        return None

    source = _mapping(architecture_ir.get("source"), "architecture.source")
    source_sha256 = str(source.get("sha256") or "").lower()
    if not _SHA256_PATTERN.fullmatch(source_sha256):
        raise ValueError("architecture.source.sha256 is invalid")

    evidence_records: list[dict[str, Any]] = []
    columns = _geometry_list(architecture_ir, "columns", "architecture")
    for index, column in enumerate(columns):
        profile = _mapping(
            column.get("source_profile_ref"),
            f"architecture column {index}.source_profile_ref",
        )
        evidence_records.append({
            "kind": "COLUMN_PROFILE",
            "drawing_identity": profile.get("drawing_identity"),
            "occurrence_id": profile.get(
                "selected_structural_occurrence_id"),
            "placement_path": profile.get("placement_path"),
            "name": f"architecture column {index}.source_profile_ref.placement_path",
        })

    structural_walls = _geometry_list(
        architecture_ir, "structural_walls", "architecture")
    structural_wall_reference_count = 0
    for wall_index, wall in enumerate(structural_walls):
        references = wall.get("source_segment_refs")
        if not isinstance(references, list) or not references:
            raise ValueError(
                f"architecture structural wall {wall_index} has no source references")
        for reference_index, reference in enumerate(references):
            reference = _mapping(
                reference,
                "architecture structural wall "
                f"{wall_index}.source_segment_refs[{reference_index}]",
            )
            structural_wall_reference_count += 1
            evidence_records.append({
                "kind": "STRUCTURAL_WALL_REFERENCE",
                "drawing_identity": reference.get("drawing_identity"),
                "occurrence_id": reference.get("structural_occurrence_id"),
                "placement_path": reference.get("placement_path"),
                "name": "architecture structural wall "
                f"{wall_index}.source_segment_refs[{reference_index}]"
                ".placement_path",
            })
    if not evidence_records:
        return None

    observed = []
    for record in evidence_records:
        marker = _nested_elevation_marker(
            record["placement_path"], selected_handle, record["name"])
        if marker is None:
            continue
        if not _identity_matches(record["drawing_identity"], source_sha256):
            raise ValueError(
                "architecture nested elevation evidence has a source identity "
                "mismatch")
        occurrence_id = record["occurrence_id"]
        if not _nonempty_text(occurrence_id):
            raise ValueError(
                "architecture nested elevation evidence has no structural "
                "occurrence identity")
        marker_value, elevation, path = marker
        observed.append({
            "marker_name": str(
                marker_value.get("block_name") or marker_value.get("name")),
            "marker_insert_handle": str(marker_value.get("insert_handle")),
            "elevation_range_m": elevation,
            "structural_occurrence_id": str(occurrence_id),
            "placement_path": path,
        })
    if not observed:
        return None
    if len(observed) != len(evidence_records):
        raise ValueError(
            "architecture nested elevation marker is not present on every "
            "formal structural reference")

    signatures = {
        (
            record["marker_name"],
            record["marker_insert_handle"],
            record["elevation_range_m"],
            record["structural_occurrence_id"],
        )
        for record in observed
    }
    if len(signatures) != 1:
        raise ValueError(
            "architecture nested elevation references are internally inconsistent")
    representative = observed[0]
    return {
        "status": "VERIFIED",
        "source_path": source.get("path"),
        "source_sha256": source_sha256,
        "selected_plan_insert_handle": selected_handle,
        "marker_insert_handle": representative["marker_insert_handle"],
        "marker_name": representative["marker_name"],
        "structural_occurrence_id": representative[
            "structural_occurrence_id"],
        "elevation_range_m": list(representative["elevation_range_m"]),
        "parser": "BLOCK_NAME_RANGE",
        "scope": "STRUCTURAL_WALL_COLUMN_VERTICAL_RANGE",
        "column_profile_count": len(columns),
        "structural_wall_count": len(structural_walls),
        "structural_wall_reference_count": structural_wall_reference_count,
        "placement_path": representative["placement_path"],
    }


def _same_floor_contract(architecture_ir: Mapping[str, Any],
                         structure_ir: Mapping[str, Any],
                         floor_code: str) -> dict[str, Any]:
    requested_floor = _floor_number(floor_code, "floor_code")
    architecture_name = _selected_plan_name(architecture_ir, "architecture")
    structure_name = _selected_plan_name(structure_ir, "structure")
    architecture_floor = _floor_number(
        architecture_name, "architecture selected plan")
    structure_floor = _floor_number(structure_name, "structure selected plan")
    if not (requested_floor == architecture_floor == structure_floor):
        raise ValueError(
            "requested floor does not match both selected drawing plans")

    architecture_elevation = _elevation_range(architecture_name)
    structure_elevation = _elevation_range(structure_name)
    architecture_nested_reference = (
        _architecture_nested_elevation_reference(architecture_ir))
    nested_elevation = (
        tuple(architecture_nested_reference["elevation_range_m"])
        if architecture_nested_reference else None)
    if nested_elevation:
        nested_floor = _floor_number(
            architecture_nested_reference["marker_name"],
            "architecture nested elevation marker",
        )
        if nested_floor != requested_floor:
            raise ValueError(
                "architecture nested elevation marker declares a different floor")

    if architecture_elevation and structure_elevation:
        if max(
            abs(architecture_elevation[0] - structure_elevation[0]),
            abs(architecture_elevation[1] - structure_elevation[1]),
        ) > ALIGNMENT_TOLERANCE_M:
            raise ValueError("selected plans declare different elevation ranges")
    if architecture_elevation and nested_elevation:
        if max(
            abs(architecture_elevation[0] - nested_elevation[0]),
            abs(architecture_elevation[1] - nested_elevation[1]),
        ) > ALIGNMENT_TOLERANCE_M:
            raise ValueError(
                "architecture selected plan and nested reference declare "
                "different elevation ranges")
    if structure_elevation and nested_elevation:
        if max(
            abs(structure_elevation[0] - nested_elevation[0]),
            abs(structure_elevation[1] - nested_elevation[1]),
        ) > ALIGNMENT_TOLERANCE_M:
            raise ValueError(
                "structure plan and architecture nested reference declare "
                "different elevation ranges")

    if architecture_elevation and structure_elevation:
        elevation = architecture_elevation
        evidence = "BOTH_SELECTED_PLANS"
        status = "PASS"
    elif structure_elevation and nested_elevation:
        elevation = structure_elevation
        evidence = "STRUCTURE_PLAN_PLUS_ARCH_NESTED_REFERENCE"
        status = "PASS"
    else:
        elevation = architecture_elevation or structure_elevation or nested_elevation
        evidence = (
            "ONE_SELECTED_PLAN" if architecture_elevation or structure_elevation
            else "ARCH_NESTED_REFERENCE_ONLY" if nested_elevation
            else "NOT_DECLARED")
        status = "PARTIAL" if elevation else "FAIL"
    result = {
        "status": status,
        "code": f"B{requested_floor:02d}",
        "architecture_selected_plan": architecture_name,
        "structure_selected_plan": structure_name,
        "elevation_range_m": list(elevation) if elevation else None,
        "elevation_evidence": evidence,
    }
    if architecture_nested_reference:
        result["architecture_nested_reference"] = (
            architecture_nested_reference)
    return result


def _traceability_gate(ir: Mapping[str, Any], discipline: str
                       ) -> tuple[bool, list[str]]:
    reasons = []
    traceability = _mapping(
        ir.get("traceability"), f"{discipline}.traceability")
    source = _mapping(ir.get("source"), f"{discipline}.source")
    traceability_source = _mapping(
        traceability.get("source"), f"{discipline}.traceability.source")
    expected_sha = str(source.get("sha256") or "").lower()
    if traceability.get("schema_version") != (
            "buildmate.wall-column-traceability/1.0"):
        reasons.append("traceability schema is invalid")
    if (traceability.get("status") != "COMPLETE" or
            traceability.get("gate_passed") is not True):
        reasons.append("traceability summary is incomplete")
    if (traceability_source.get("identity_status") != "MATCH" or
            str(traceability_source.get("sha256") or "").lower() != expected_sha or
            not _identity_matches(
                traceability_source.get("drawing_identity"), expected_sha) or
            traceability_source.get("path") != source.get("path")):
        reasons.append("traceability source identity does not match input source")
    if traceability.get("issue_count") != 0:
        reasons.append("traceability reports issues")
    issues = traceability.get("issues")
    if not isinstance(issues, list) or issues:
        reasons.append("traceability issue list is not empty")
    for key in (
        "identity_incomplete_ref_count",
        "drawing_identity_mismatch_count",
        "occurrence_mismatch_count",
        "lineage_incomplete_count",
    ):
        if traceability.get(key) != 0:
            reasons.append(f"traceability {key} is non-zero")

    geometry = _mapping(ir.get("geometry"), f"{discipline}.geometry")
    groups = _mapping(
        traceability.get("groups"), f"{discipline}.traceability.groups")
    for group_name, geometry_key in (
        ("columns", "columns"),
        ("structural_walls", "structural_walls"),
        ("architectural_walls", "architectural_walls"),
    ):
        elements = geometry.get(geometry_key)
        if not isinstance(elements, list):
            raise ValueError(f"{discipline}.geometry.{geometry_key} must be a list")
        group = _mapping(
            groups.get(group_name),
            f"{discipline}.traceability.groups.{group_name}",
        )
        expected_count = len(elements)
        expected_statuses = ({"COMPLETE"} if expected_count else
                             {"COMPLETE", "NOT_APPLICABLE"})
        expected_reference_count = sum(
            len(element.get("source_segment_refs") or [])
            for element in elements if isinstance(element, Mapping))
        if group.get("status") not in expected_statuses:
            reasons.append(f"traceability group {group_name} is incomplete")
        if (group.get("element_count") != expected_count or
                group.get("traceable_element_count") != expected_count or
                group.get("incomplete_element_count") != 0 or
                group.get("source_segment_ref_count") != expected_reference_count):
            reasons.append(f"traceability group {group_name} counts disagree")
        group_issues = group.get("issues")
        if not isinstance(group_issues, list) or group_issues:
            reasons.append(f"traceability group {group_name} reports issues")
        if group_name == "columns":
            if (group.get("source_profile_ref_count") != expected_count or
                    group.get("one_to_one_complete") is not True or
                    group.get("drawing_identity_mismatch_count") != 0):
                reasons.append("traceability group columns is not one-to-one")
        else:
            for key in (
                "identity_incomplete_ref_count",
                "drawing_identity_mismatch_count",
                "occurrence_mismatch_count",
                "lineage_incomplete_count",
            ):
                if group.get(key) != 0:
                    reasons.append(
                        f"traceability group {group_name} {key} is non-zero")
    return not reasons, list(dict.fromkeys(reasons))


def _input_gate(ir: Mapping[str, Any], discipline: str) -> tuple[bool, dict[str, Any]]:
    gate = _mapping(ir.get("quality_gate"), f"{discipline}.quality_gate")
    structure_status = (_mapping(
        gate.get("structure"), f"{discipline}.quality_gate.structure")
                        .get("status"))
    architecture_status = (_mapping(
        gate.get("architecture"), f"{discipline}.quality_gate.architecture")
                           .get("status"))
    raw_reasons = gate.get("blocking_reasons")
    if not isinstance(raw_reasons, list) or not all(
            isinstance(reason, str) and reason for reason in raw_reasons):
        raise ValueError(f"{discipline}.quality_gate.blocking_reasons is invalid")
    traceability_passed, traceability_reasons = _traceability_gate(
        ir, discipline)
    artifact_status = ir.get("artifact_status")
    allow_modeling = gate.get("allow_modeling") is True
    required_architecture_status = (
        architecture_status == "PASS" if discipline == "architecture"
        else architecture_status in {"PASS", "N/A"})
    passed = bool(
        artifact_status == "READY_FOR_PREVIEW" and
        allow_modeling and not raw_reasons and traceability_passed and
        structure_status == "PASS" and
        required_architecture_status
    )
    reasons = list(raw_reasons)
    if allow_modeling and artifact_status != "READY_FOR_PREVIEW":
        reasons.append(
            "allow_modeling conflicts with the input artifact status")
    if allow_modeling and raw_reasons:
        reasons.append("allow_modeling conflicts with input blocking reasons")
    if artifact_status == "READY_FOR_PREVIEW" and not allow_modeling:
        reasons.append(
            "READY_FOR_PREVIEW conflicts with allow_modeling=false")
    if not traceability_passed:
        reasons.extend(traceability_reasons)
    if structure_status != "PASS" and not raw_reasons:
        reasons.append(f"structure Gate is {structure_status or 'MISSING'}")
    if not required_architecture_status and not raw_reasons:
        reasons.append(
            f"architecture Gate is {architecture_status or 'MISSING'}")
    if not reasons and not passed:
        reasons.append("input Gate did not pass")
    reasons = list(dict.fromkeys(reasons))
    return passed, {
        "status": "PASS" if passed else "FAIL",
        "artifact_status": artifact_status,
        "allow_modeling": allow_modeling,
        "structure": structure_status,
        "architecture": architecture_status,
        "traceability": "PASS" if traceability_passed else "FAIL",
        "blocking_reasons": reasons,
    }


def _axis_values(grid: Mapping[str, Any], key: str,
                 name: str) -> list[tuple[float, str | None]]:
    values = grid.get(key)
    if not isinstance(values, list) or not values:
        raise ValueError(f"{name}.{key} must be a non-empty list")
    result = []
    for index, axis in enumerate(values):
        axis = _mapping(axis, f"{name}.{key}[{index}]")
        label = axis.get("label")
        result.append((
            _finite_number(axis.get("coord"), f"{name}.{key}[{index}].coord"),
            str(label) if label is not None else None,
        ))
    return result


def _validate_grid_contract(grid: Mapping[str, Any],
                            coordinate: Mapping[str, Any], name: str) -> None:
    origin = _point(grid.get("origin_mm"), f"{name}.origin_mm")
    meta = _mapping(grid.get("meta"), f"{name}.meta")
    rotation = _finite_number(meta.get("rot_deg"), f"{name}.meta.rot_deg")
    grid_center = (
        _finite_number(meta.get("gcx"), f"{name}.meta.gcx"),
        _finite_number(meta.get("gcy"), f"{name}.meta.gcy"),
    )
    if _distance(origin, coordinate["origin"]) > _GRID_CONTRACT_TOLERANCE_MM:
        raise ValueError(f"{name} origin does not match its review IR")
    if abs(rotation - coordinate["rotation_deg"]) > _ROTATION_TOLERANCE_DEG:
        raise ValueError(f"{name} rotation does not match its review IR")
    if (_distance(grid_center, coordinate["grid_center"]) >
            _GRID_CONTRACT_TOLERANCE_MM):
        raise ValueError(f"{name} grid center does not match its review IR")


def _ensure_unique_axis_coordinates(axes: list[tuple[float, str | None]],
                                    name: str) -> None:
    ordered = sorted(coordinate for coordinate, _label in axes)
    if any(
        right - left <= AXIS_TOLERANCE_M
        for left, right in zip(ordered, ordered[1:])
    ):
        raise ValueError(f"{name} contains duplicate axis coordinates")


def _fuse_grid(architecture_grid: Mapping[str, Any],
               structure_grid: Mapping[str, Any],
               affine: list[list[float]], floor_code: str
               ) -> tuple[dict[str, Any], dict[str, Any]]:
    a, b, tx = affine[0]
    c, d, ty = affine[1]
    if (abs(a - 1.0) > _MATRIX_TOLERANCE or
            abs(d - 1.0) > _MATRIX_TOLERANCE or
            abs(b) > _MATRIX_TOLERANCE or
            abs(c) > _MATRIX_TOLERANCE):
        raise ValueError("grid fusion requires matching drawing rotation")

    architecture_x = _axis_values(architecture_grid, "x_axes", "architecture_grid")
    architecture_y = _axis_values(architecture_grid, "y_axes", "architecture_grid")
    structure_x_raw = _axis_values(structure_grid, "x_axes", "structure_grid")
    structure_y_raw = _axis_values(structure_grid, "y_axes", "structure_grid")
    structure_x = [(a * coordinate + tx, label)
                   for coordinate, label in structure_x_raw]
    structure_y = [(d * coordinate + ty, label)
                   for coordinate, label in structure_y_raw]
    _ensure_unique_axis_coordinates(structure_x, "structure_grid.x_axes")
    _ensure_unique_axis_coordinates(structure_y, "structure_grid.y_axes")

    def matched_count(subset: list[tuple[float, str | None]],
                      full: list[tuple[float, str | None]], name: str) -> int:
        count = 0
        for coordinate, _label in subset:
            matches = [candidate for candidate, _source_label in full
                       if abs(candidate - coordinate) <= AXIS_TOLERANCE_M]
            if len(matches) != 1:
                raise ValueError(f"{name} is not a unique subset of structure grid")
            count += 1
        return count

    x_match_count = matched_count(architecture_x, structure_x, "architecture x grid")
    y_match_count = matched_count(architecture_y, structure_y, "architecture y grid")

    def output_axes(axes: list[tuple[float, str | None]], kind: str) -> list[dict]:
        output = []
        for coordinate, label in sorted(axes):
            output.append({
                "id": _stable_id(floor_code, f"GRID_{kind}", {
                    "coord_mm": _millimetres(coordinate),
                }),
                "coord": _rounded(coordinate),
                "label": label,
                "label_status": "UNVERIFIED_EXTRACTED_ORDER",
            })
        return output

    return ({
        "authority": "structure",
        "x_axes": output_axes(structure_x, "X"),
        "y_axes": output_axes(structure_y, "Y"),
    }, {
        "status": "PASS",
        "architecture_x_axis_count": len(architecture_x),
        "architecture_y_axis_count": len(architecture_y),
        "canonical_x_axis_count": len(structure_x),
        "canonical_y_axis_count": len(structure_y),
        "matched_architecture_x_axis_count": x_match_count,
        "matched_architecture_y_axis_count": y_match_count,
    })


def _wall_match_residual(left: Mapping[str, Any],
                         right: Mapping[str, Any]) -> float | None:
    if abs(left["thickness"] - right["thickness"]) > THICKNESS_TOLERANCE_MM:
        return None
    direct = max(
        _distance(left["start"], right["start"]),
        _distance(left["end"], right["end"]),
    )
    reverse = max(
        _distance(left["start"], right["end"]),
        _distance(left["end"], right["start"]),
    )
    residual = min(direct, reverse)
    return residual if residual <= ALIGNMENT_TOLERANCE_M else None


def _match_wall_views(structure_views: list[dict[str, Any]],
                      architecture_views: list[dict[str, Any]]
                      ) -> tuple[list[tuple[int, int, float]], list[int], list[int]]:
    structure_candidates: list[list[tuple[int, float]]] = []
    architecture_candidates: list[list[tuple[int, float]]] = [
        [] for _view in architecture_views]
    for structure_index, structure_view in enumerate(structure_views):
        candidates = []
        for architecture_index, architecture_view in enumerate(architecture_views):
            residual = _wall_match_residual(structure_view, architecture_view)
            if residual is None:
                continue
            candidates.append((architecture_index, residual))
            architecture_candidates[architecture_index].append(
                (structure_index, residual))
        structure_candidates.append(candidates)

    matches = []
    matched_structure = set()
    matched_architecture = set()
    for structure_index, candidates in enumerate(structure_candidates):
        if len(candidates) != 1:
            continue
        architecture_index, residual = candidates[0]
        reverse_candidates = architecture_candidates[architecture_index]
        if (len(reverse_candidates) != 1 or
                reverse_candidates[0][0] != structure_index):
            continue
        matches.append((structure_index, architecture_index, residual))
        matched_structure.add(structure_index)
        matched_architecture.add(architecture_index)
    return (
        matches,
        [index for index in range(len(structure_views))
         if index not in matched_structure],
        [index for index in range(len(architecture_views))
         if index not in matched_architecture],
    )


def _wall_view_candidate(floor_code: str, discipline: str,
                         source: Mapping[str, Any], view: Mapping[str, Any]
                         ) -> dict[str, Any]:
    wall = view["source_wall"]
    identifier = _stable_id(floor_code, "STRUCTURAL_WALL_VIEW", {
        "discipline": discipline,
        "source_sha256": source["sha256"],
        "source_element_id": wall.get("id"),
        "endpoints_mm": [
            [_millimetres(view["start"][0]), _millimetres(view["start"][1])],
            [_millimetres(view["end"][0]), _millimetres(view["end"][1])],
        ],
        "thickness_mm": int(round(view["thickness"])),
    })
    return {
        "id": identifier,
        "discipline": discipline,
        "source_id": source["source_id"],
        "source_element_id": wall.get("id"),
        "start": _rounded_point(view["start"]),
        "end": _rounded_point(view["end"]),
        "thickness": view["thickness"],
        "source_segment_refs": copy.deepcopy(
            wall.get("source_segment_refs") or []),
    }


def _source_alias(source: Mapping[str, Any], item: Mapping[str, Any],
                  *, role: str, source_center: tuple[float, float] | None = None,
                  transformed_center: tuple[float, float] | None = None
                  ) -> dict[str, Any]:
    alias = {
        "source_id": source["source_id"],
        "role": role,
        "source_element_id": item.get("id"),
        "source_profile_ref": copy.deepcopy(item.get("source_profile_ref")),
        "source_segment_refs": copy.deepcopy(item.get("source_segment_refs") or []),
    }
    if source_center is not None:
        alias["source_center"] = _rounded_point(source_center)
    if transformed_center is not None:
        alias["canonical_center"] = _rounded_point(transformed_center)
    if item.get("grid_ref") is not None:
        alias["source_grid_ref"] = item.get("grid_ref")
    return alias


def fuse_same_floor_review_ir(
    architecture_ir: Mapping[str, Any],
    architecture_grid: Mapping[str, Any],
    structure_ir: Mapping[str, Any],
    structure_grid: Mapping[str, Any],
    *,
    floor_code: str,
) -> dict[str, Any]:
    """Return a deterministic, review-only same-floor fusion artifact.

    Invalid source identity, provenance, coordinate contracts, or ambiguous
    alignment raise ``ValueError``.  A legitimate failed input quality Gate is
    represented as a blocked review artifact rather than being hidden.
    """
    architecture_ir = _mapping(architecture_ir, "architecture_ir")
    structure_ir = _mapping(structure_ir, "structure_ir")
    architecture_grid = _mapping(architecture_grid, "architecture_grid")
    structure_grid = _mapping(structure_grid, "structure_grid")
    if not isinstance(floor_code, str) or not _FLOOR_PATTERN.fullmatch(floor_code):
        raise ValueError("floor_code contains unsupported characters")
    floor_code = floor_code.upper()
    floor_contract = _same_floor_contract(
        architecture_ir, structure_ir, floor_code)
    floor_code = floor_contract["code"]

    architecture_source = _source(architecture_ir, "architecture")
    structure_source = _source(structure_ir, "structure")
    if architecture_source["sha256"] == structure_source["sha256"]:
        raise ValueError("architecture and structure sources must be distinct")

    architecture_coordinate = _coordinate_contract(
        architecture_ir, "architecture")
    structure_coordinate = _coordinate_contract(structure_ir, "structure")
    _validate_grid_contract(
        architecture_grid, architecture_coordinate, "architecture_grid")
    _validate_grid_contract(
        structure_grid, structure_coordinate, "structure_grid")
    affine = _affine_matrix(structure_coordinate, architecture_coordinate)

    architecture_columns = _geometry_list(
        architecture_ir, "columns", "architecture")
    structure_columns = _geometry_list(structure_ir, "columns", "structure")
    for index, column in enumerate(architecture_columns):
        _validate_provenance(
            column, architecture_source["sha256"],
            f"architecture column {index}", column=True)
    for index, column in enumerate(structure_columns):
        _validate_provenance(
            column, structure_source["sha256"],
            f"structure column {index}", column=True)
    column_matches, max_column_residual = _match_columns(
        architecture_columns,
        structure_columns,
        structure_coordinate,
        architecture_coordinate,
    )

    columns = []
    for architecture_column, structure_column, transformed_center, residual in column_matches:
        center, size = _column_geometry(architecture_column, "architecture column")
        structure_center, _structure_size = _column_geometry(
            structure_column, "structure column")
        item = {
            "id": _column_id(floor_code, center, size),
            "center": _rounded_point(center),
            "size": _rounded_point(size),
            "mark": structure_column.get("mark"),
            "grid_ref": structure_column.get("grid_ref"),
            "confidence": min(
                _finite_number(architecture_column.get("confidence", 0.0),
                               "architecture column confidence"),
                _finite_number(structure_column.get("confidence", 0.0),
                               "structure column confidence"),
            ),
            "fusion": {
                "match_status": "EXACT",
                "residual_m": _rounded(residual, 12),
                "geometry_authority": architecture_source["source_id"],
                "attribute_authority": structure_source["source_id"],
            },
            "source_aliases": [
                _source_alias(
                    architecture_source, architecture_column,
                    role="CANONICAL_GEOMETRY", source_center=center,
                    transformed_center=center,
                ),
                _source_alias(
                    structure_source, structure_column,
                    role="STRUCTURE_ATTRIBUTES", source_center=structure_center,
                    transformed_center=transformed_center,
                ),
            ],
        }
        columns.append(item)
    columns.sort(key=lambda item: item["id"])

    structure_walls_input = _geometry_list(
        structure_ir, "structural_walls", "structure")
    architecture_structural_views = _geometry_list(
        architecture_ir, "structural_walls", "architecture")
    structural_walls = []
    structure_wall_views = []
    for index, wall in enumerate(structure_walls_input):
        _validate_provenance(
            wall, structure_source["sha256"], f"structure wall {index}")
        start = _transform_point(
            _point(wall.get("start"), f"structure wall {index}.start"),
            structure_coordinate, architecture_coordinate)
        end = _transform_point(
            _point(wall.get("end"), f"structure wall {index}.end"),
            structure_coordinate, architecture_coordinate)
        start, end = _ordered_endpoints(start, end)
        thickness = _finite_number(
            wall.get("thickness"), f"structure wall {index}.thickness")
        if thickness <= 0:
            raise ValueError(f"structure wall {index}.thickness must be positive")
        output_wall = {
            "id": _wall_id(
                floor_code, "STRUCTURAL_WALL", start, end, thickness),
            "start": _rounded_point(start),
            "end": _rounded_point(end),
            "thickness": thickness,
            "confidence": wall.get("confidence"),
            "geometry_source": wall.get("geometry_source"),
            "fusion": {
                "geometry_authority": structure_source["source_id"],
                "corroboration_status": "UNMATCHED_STRUCTURE_VIEW",
                "architecture_duplicate_view_suppressed": False,
            },
            "source_aliases": [{
                "source_id": structure_source["source_id"],
                "role": "STRUCTURAL_GEOMETRY",
                "source_element_id": wall.get("id"),
                "source_start": list(wall.get("start")),
                "source_end": list(wall.get("end")),
                "source_segment_refs": copy.deepcopy(
                    wall.get("source_segment_refs") or []),
                "derivation": copy.deepcopy(wall.get("derivation") or []),
            }],
        }
        structural_walls.append(output_wall)
        structure_wall_views.append({
            "start": start,
            "end": end,
            "thickness": thickness,
            "source_wall": wall,
            "output_wall": output_wall,
        })

    architecture_wall_views = []
    for index, wall in enumerate(architecture_structural_views):
        _validate_provenance(
            wall, architecture_source["sha256"],
            f"architecture structural wall {index}")
        start = _point(
            wall.get("start"), f"architecture structural wall {index}.start")
        end = _point(
            wall.get("end"), f"architecture structural wall {index}.end")
        start, end = _ordered_endpoints(start, end)
        thickness = _finite_number(
            wall.get("thickness"),
            f"architecture structural wall {index}.thickness")
        if thickness <= 0:
            raise ValueError(
                f"architecture structural wall {index}.thickness must be positive")
        architecture_wall_views.append({
            "start": start,
            "end": end,
            "thickness": thickness,
            "source_wall": wall,
        })

    wall_matches, exact_unmatched_structure_indexes, \
        exact_unmatched_architecture_indexes = _match_wall_views(
            structure_wall_views, architecture_wall_views)
    max_wall_residual = 0.0
    for structure_index, architecture_index, residual in wall_matches:
        max_wall_residual = max(max_wall_residual, residual)
        structure_view = structure_wall_views[structure_index]
        architecture_view = architecture_wall_views[architecture_index]
        output_wall = structure_view["output_wall"]
        output_wall["fusion"].update({
            "corroboration_status": "EXACT_MATCH",
            "architecture_duplicate_view_suppressed": True,
            "corroboration_residual_m": _rounded(residual, 12),
        })
        architecture_wall = architecture_view["source_wall"]
        output_wall["source_aliases"].append({
            "source_id": architecture_source["source_id"],
            "role": "ARCHITECTURE_CORROBORATION",
            "source_element_id": architecture_wall.get("id"),
            "source_start": list(architecture_wall.get("start")),
            "source_end": list(architecture_wall.get("end")),
            "source_segment_refs": copy.deepcopy(
                architecture_wall.get("source_segment_refs") or []),
            "derivation": copy.deepcopy(
                architecture_wall.get("derivation") or []),
        })

    segmentation_result = prove_wall_segmentation_equivalence(
        [structure_wall_views[index]
         for index in exact_unmatched_structure_indexes],
        [architecture_wall_views[index]
         for index in exact_unmatched_architecture_indexes],
    )
    segmentation_equivalence_groups = []
    for group in segmentation_result["groups"]:
        structure_indexes = [
            exact_unmatched_structure_indexes[index]
            for index in group["structure_indices"]
        ]
        architecture_indexes = [
            exact_unmatched_architecture_indexes[index]
            for index in group["architecture_indices"]
        ]
        for structure_index in structure_indexes:
            structure_wall_views[structure_index]["output_wall"]["fusion"].update({
                "corroboration_status": "SEGMENTATION_EQUIVALENT_GROUP",
                "architecture_duplicate_view_suppressed": True,
                "segmentation_equivalence_group_id": group["id"],
            })

        def group_members(indexes: list[int], discipline: str) -> list[dict]:
            views = (structure_wall_views if discipline == "structure"
                     else architecture_wall_views)
            source = (structure_source if discipline == "structure"
                      else architecture_source)
            members = []
            for index in indexes:
                view = views[index]
                wall = view["source_wall"]
                member = {
                    "source_id": source["source_id"],
                    "source_element_id": wall.get("id"),
                    "start": _rounded_point(view["start"]),
                    "end": _rounded_point(view["end"]),
                    "thickness": view["thickness"],
                    "source_segment_refs": copy.deepcopy(
                        wall.get("source_segment_refs") or []),
                    "derivation": copy.deepcopy(
                        wall.get("derivation") or []),
                }
                if discipline == "structure":
                    member["canonical_output_wall_id"] = view[
                        "output_wall"]["id"]
                members.append(member)
            members.sort(key=lambda item: (
                str(item.get("source_element_id") or ""),
                item["start"], item["end"],
            ))
            return members

        segmentation_equivalence_groups.append({
            "id": group["id"],
            "status": group["status"],
            "relation": group["relation"],
            "evidence": copy.deepcopy(group["evidence"]),
            "structure_members": group_members(
                structure_indexes, "structure"),
            "architecture_members": group_members(
                architecture_indexes, "architecture"),
        })
    segmentation_equivalence_groups.sort(key=lambda item: item["id"])
    unmatched_structure_indexes = [
        exact_unmatched_structure_indexes[index]
        for index in segmentation_result["unmatched_structure_indices"]
    ]
    unmatched_architecture_indexes = [
        exact_unmatched_architecture_indexes[index]
        for index in segmentation_result["unmatched_architecture_indices"]
    ]

    unmatched_structure_wall_views = [
        _wall_view_candidate(
            floor_code, "structure", structure_source,
            structure_wall_views[index])
        for index in unmatched_structure_indexes
    ]
    unmatched_architecture_wall_views = [
        _wall_view_candidate(
            floor_code, "architecture", architecture_source,
            architecture_wall_views[index])
        for index in unmatched_architecture_indexes
    ]
    unmatched_structure_wall_views.sort(key=lambda item: item["id"])
    unmatched_architecture_wall_views.sort(key=lambda item: item["id"])
    structural_walls.sort(key=lambda item: item["id"])

    architecture_walls_input = _geometry_list(
        architecture_ir, "architectural_walls", "architecture")
    architectural_walls = []
    for index, wall in enumerate(architecture_walls_input):
        _validate_provenance(
            wall, architecture_source["sha256"],
            f"architecture wall {index}")
        start = _point(wall.get("start"), f"architecture wall {index}.start")
        end = _point(wall.get("end"), f"architecture wall {index}.end")
        start, end = _ordered_endpoints(start, end)
        thickness = _finite_number(
            wall.get("thickness"), f"architecture wall {index}.thickness")
        if thickness <= 0:
            raise ValueError(f"architecture wall {index}.thickness must be positive")
        architectural_walls.append({
            "id": _wall_id(
                floor_code, "ARCHITECTURAL_WALL", start, end, thickness),
            "start": _rounded_point(start),
            "end": _rounded_point(end),
            "thickness": thickness,
            "confidence": wall.get("confidence"),
            "geometry_source": wall.get("geometry_source"),
            "fusion": {
                "geometry_authority": architecture_source["source_id"],
            },
            "source_aliases": [{
                "source_id": architecture_source["source_id"],
                "role": "ARCHITECTURAL_GEOMETRY",
                "source_element_id": wall.get("id"),
                "source_segment_refs": copy.deepcopy(
                    wall.get("source_segment_refs") or []),
                "derivation": copy.deepcopy(wall.get("derivation") or []),
            }],
        })
    architectural_walls.sort(key=lambda item: item["id"])

    beams_input = _geometry_list(structure_ir, "beams", "structure")
    beam_candidates = []
    for index, beam in enumerate(beams_input):
        start = _transform_point(
            _point(beam.get("start"), f"structure beam {index}.start"),
            structure_coordinate, architecture_coordinate)
        end = _transform_point(
            _point(beam.get("end"), f"structure beam {index}.end"),
            structure_coordinate, architecture_coordinate)
        start, end = _ordered_endpoints(start, end)
        candidate_id = _stable_id(floor_code, "BEAM_CANDIDATE", {
            "endpoints_mm": [
                [_millimetres(start[0]), _millimetres(start[1])],
                [_millimetres(end[0]), _millimetres(end[1])],
            ],
            "source_element_id": beam.get("id"),
            "source_sha256": structure_source["sha256"],
        })
        beam_candidates.append({
            "id": candidate_id,
            "start": _rounded_point(start),
            "end": _rounded_point(end),
            "source_id": structure_source["source_id"],
            "source_element_id": beam.get("id"),
            "source_start": list(beam.get("start")),
            "source_end": list(beam.get("end")),
            "source": copy.deepcopy(beam.get("source")),
            "warnings": copy.deepcopy(beam.get("warnings") or []),
        })
    beam_candidates.sort(key=lambda item: item["id"])

    grid, grid_evidence = _fuse_grid(
        architecture_grid, structure_grid, affine, floor_code)

    all_ids = [item["id"] for group in (
        columns, structural_walls, architectural_walls,
        grid["x_axes"], grid["y_axes"], beam_candidates,
        unmatched_structure_wall_views, unmatched_architecture_wall_views,
    ) for item in group]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("fusion produced duplicate stable IDs")

    architecture_gate_passed, architecture_gate = _input_gate(
        architecture_ir, "architecture")
    structure_gate_passed, structure_gate = _input_gate(
        structure_ir, "structure")
    blockers = []
    if not architecture_gate_passed:
        reasons = architecture_gate["blocking_reasons"] or [
            "architecture input Gate did not pass"]
        blockers.extend(f"architecture: {reason}" for reason in reasons)
    if not structure_gate_passed:
        reasons = structure_gate["blocking_reasons"] or [
            "structure input Gate did not pass"]
        blockers.extend(f"structure: {reason}" for reason in reasons)
    wall_alignment_passed = bool(
        not unmatched_structure_indexes and
        not unmatched_architecture_indexes)
    if not wall_alignment_passed:
        blockers.append(
            "structural wall cross-source alignment: "
            f"{len(unmatched_structure_indexes)} structure and "
            f"{len(unmatched_architecture_indexes)} architecture views "
            "require segmentation review")
    floor_contract_passed = floor_contract["status"] == "PASS"
    if not floor_contract_passed:
        blockers.append(
            "same-floor elevation evidence is "
            f"{floor_contract['elevation_evidence']}; independent architecture "
            "and structure elevation evidence must agree")
    blockers = list(dict.fromkeys(blockers))
    eligible = bool(
        architecture_gate_passed and structure_gate_passed and
        wall_alignment_passed and floor_contract_passed)

    architecture_source["input_gate"] = architecture_gate
    structure_source["input_gate"] = structure_gate
    architecture_source["coordinate_system"] = copy.deepcopy(
        architecture_ir.get("coordinate_system"))
    structure_source["coordinate_system"] = copy.deepcopy(
        structure_ir.get("coordinate_system"))
    affine_output = [[_rounded(value, 12) for value in row] for row in affine]
    return {
        "schema_version": FUSION_SCHEMA,
        "artifact_role": "REVIEW_ONLY",
        "artifact_status": "READY_FOR_PREVIEW" if eligible else "BLOCKED",
        "floor": floor_contract,
        "sources": [architecture_source, structure_source],
        "canonical_coordinate_system": {
            "unit": "m",
            "authority_source_id": architecture_source["source_id"],
            "origin_dxf_mm": list(architecture_coordinate["origin"]),
            "rotation_deg": architecture_coordinate["rotation_deg"],
            "grid_center_dxf_mm": list(architecture_coordinate["grid_center"]),
        },
        "alignments": [{
            "source_id": structure_source["source_id"],
            "target_source_id": architecture_source["source_id"],
            "source_to_canonical_affine_m": affine_output,
            "status": "PASS" if wall_alignment_passed else "PARTIAL",
            "coordinate_alignment_status": "PASS",
            "structural_wall_alignment_status": (
                "PASS" if wall_alignment_passed else "FAIL"),
            "evidence": {
                "column_count": len(column_matches),
                "unique_match_count": len(column_matches),
                "max_center_residual_m": _rounded(max_column_residual, 12),
                "structural_wall_exact_match_count": len(wall_matches),
                "structural_wall_segmentation_equivalence_group_count": len(
                    segmentation_equivalence_groups),
                "structural_wall_unmatched_structure_count": len(
                    unmatched_structure_indexes),
                "structural_wall_unmatched_architecture_count": len(
                    unmatched_architecture_indexes),
                "max_structural_wall_match_residual_m": _rounded(
                    max_wall_residual, 12),
                **grid_evidence,
            },
        }],
        "geometry": {
            "grid": grid,
            "columns": columns,
            "structural_walls": structural_walls,
            "architectural_walls": architectural_walls,
            "beams": [],
        },
        "discipline_reviews": {
            "architecture": {
                "coordinate_system": copy.deepcopy(
                    architecture_ir.get("coordinate_system")),
                "plan_selection": copy.deepcopy(
                    architecture_ir.get("plan_selection")),
                "review_candidates": copy.deepcopy(
                    architecture_ir.get("review_candidates") or {}),
                "artifacts": copy.deepcopy(
                    architecture_ir.get("artifacts") or {}),
            },
            "structure": {
                "coordinate_system": copy.deepcopy(
                    structure_ir.get("coordinate_system")),
                "plan_selection": copy.deepcopy(
                    structure_ir.get("plan_selection")),
                "review_candidates": copy.deepcopy(
                    structure_ir.get("review_candidates") or {}),
                "artifacts": copy.deepcopy(
                    structure_ir.get("artifacts") or {}),
            },
        },
        "review_candidates": {
            "structural_wall_view_differences": {
                "status": "PASS" if wall_alignment_passed else "REVIEW",
                "exact_match_count": len(wall_matches),
                "segmentation_equivalence_group_count": len(
                    segmentation_equivalence_groups),
                "segmentation_equivalence_groups": (
                    segmentation_equivalence_groups),
                "unmatched_structure_count": len(
                    unmatched_structure_wall_views),
                "unmatched_architecture_count": len(
                    unmatched_architecture_wall_views),
                "reason": (
                    "One-to-one matches and strictly proven unambiguous 1-to-N "
                    "segmentation equivalence groups are collapsed; all other "
                    "differences remain review-only."),
                "unmatched_structure_views": unmatched_structure_wall_views,
                "unmatched_architecture_views": unmatched_architecture_wall_views,
            },
            "unverified_structure_beams": {
                "count": len(beam_candidates),
                "reason": (
                    "The wall-column drawing does not provide a complete beam "
                    "semantic and section provenance contract."),
                "items": beam_candidates,
            },
        },
        "quality_gate": {
            "allow_modeling": False,
            "eligible_for_model_compilation": eligible,
            "blocking_reasons": blockers,
            "advisories": ([
                f"{len(beam_candidates)} unverified structure beam candidates "
                "were excluded from formal geometry."
            ] if beam_candidates else []),
            "checks": {
                "source_identity": "PASS",
                "same_floor_contract": floor_contract["status"],
                "coordinate_contract": "PASS",
                "column_alignment": "PASS",
                "structural_wall_cross_source_alignment": (
                    "PASS" if wall_alignment_passed else "FAIL"),
                "grid_alignment": "PASS",
                "formal_geometry_provenance": "PASS",
                "stable_id_uniqueness": "PASS",
                "architecture_input_gate": architecture_gate["status"],
                "structure_input_gate": structure_gate["status"],
            },
        },
        "metrics": {
            "input_column_representations": (
                len(architecture_columns) + len(structure_columns)),
            "fused_columns": len(columns),
            "duplicate_column_representations_collapsed": len(structure_columns),
            "input_structural_wall_representations": (
                len(architecture_structural_views) + len(structure_walls_input)),
            "suppressed_architecture_structural_wall_representations": (
                len(wall_matches) + sum(
                    len(group["architecture_members"])
                    for group in segmentation_equivalence_groups)),
            "exact_structural_wall_cross_source_matches": len(wall_matches),
            "structural_wall_segmentation_equivalence_groups": len(
                segmentation_equivalence_groups),
            "unmatched_structure_wall_views": len(
                unmatched_structure_indexes),
            "unmatched_architecture_wall_views": len(
                unmatched_architecture_indexes),
            "fused_structural_walls": len(structural_walls),
            "architectural_walls": len(architectural_walls),
            "formal_beams": 0,
            "review_beam_candidates": len(beam_candidates),
            "grid_sets": 1,
            "unique_element_ids": True,
        },
    }
