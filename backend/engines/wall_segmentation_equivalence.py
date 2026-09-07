"""Conservative proof of cross-drawing wall segmentation equivalence.

The public function in this module is deliberately pure: it only compares
leftover structural-wall views and returns review evidence.  Coordinates are
metres and wall thicknesses are millimetres, matching the review IR contract.
Only an unambiguous one-to-many (or many-to-one) collinear split can pass.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


ANGLE_TOLERANCE_DEG = 0.01
DISTANCE_TOLERANCE_M = 0.001
THICKNESS_TOLERANCE_MM = 1.0
MIN_POSITIVE_OVERLAP_M = 0.001

_FLOAT_EPSILON = 1e-12
_MIN_SEGMENT_LENGTH_M = 1e-9


@dataclass(frozen=True)
class _Segment:
    index: int
    start: tuple[float, float]
    end: tuple[float, float]
    thickness_mm: float
    unit: tuple[float, float]


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
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


def _segments(values: Any, name: str) -> list[_Segment]:
    if (not isinstance(values, Sequence) or
            isinstance(values, (str, bytes))):
        raise ValueError(f"{name} must be a list of wall views")
    output = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise ValueError(f"{name}[{index}] must be an object")
        start = _point(value.get("start"), f"{name}[{index}].start")
        end = _point(value.get("end"), f"{name}[{index}].end")
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= _MIN_SEGMENT_LENGTH_M:
            raise ValueError(f"{name}[{index}] must have positive length")
        thickness = _finite_number(
            value.get("thickness"), f"{name}[{index}].thickness")
        if thickness <= 0.0:
            raise ValueError(f"{name}[{index}].thickness must be positive")
        output.append(_Segment(
            index=index,
            start=start,
            end=end,
            thickness_mm=thickness,
            unit=(dx / length, dy / length),
        ))
    return output


def _canonical_unit(segment: _Segment) -> tuple[float, float]:
    ux, uy = segment.unit
    if ux < -_FLOAT_EPSILON or (
            abs(ux) <= _FLOAT_EPSILON and uy < 0.0):
        return -ux, -uy
    return ux, uy


def _angle_delta_deg(left: _Segment, right: _Segment) -> float:
    dot = abs(left.unit[0] * right.unit[0] +
              left.unit[1] * right.unit[1])
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _point_to_line_distance(point: tuple[float, float],
                            line: _Segment) -> float:
    relative_x = point[0] - line.start[0]
    relative_y = point[1] - line.start[1]
    return abs(relative_x * -line.unit[1] + relative_y * line.unit[0])


def _normal_residual(left: _Segment, right: _Segment) -> float:
    # Checking both infinite centre lines keeps the predicate symmetric even
    # for the small non-zero angle that the evidence contract permits.
    return max(
        _point_to_line_distance(left.start, right),
        _point_to_line_distance(left.end, right),
        _point_to_line_distance(right.start, left),
        _point_to_line_distance(right.end, left),
    )


def _projected_interval(segment: _Segment,
                        axis: tuple[float, float]) -> tuple[float, float]:
    first = segment.start[0] * axis[0] + segment.start[1] * axis[1]
    second = segment.end[0] * axis[0] + segment.end[1] * axis[1]
    return (first, second) if first <= second else (second, first)


def _interval_overlap(left: tuple[float, float],
                      right: tuple[float, float]) -> float:
    return min(left[1], right[1]) - max(left[0], right[0])


def _pair_overlap(left: _Segment, right: _Segment) -> float:
    left_axis = _canonical_unit(left)
    right_axis = _canonical_unit(right)
    return min(
        _interval_overlap(
            _projected_interval(left, left_axis),
            _projected_interval(right, left_axis),
        ),
        _interval_overlap(
            _projected_interval(left, right_axis),
            _projected_interval(right, right_axis),
        ),
    )


def _within(value: float, tolerance: float) -> bool:
    return value <= tolerance + _FLOAT_EPSILON


def _candidate_pair(left: _Segment, right: _Segment) -> bool:
    return bool(
        _within(_angle_delta_deg(left, right), ANGLE_TOLERANCE_DEG) and
        _within(_normal_residual(left, right), DISTANCE_TOLERANCE_M) and
        _within(
            abs(left.thickness_mm - right.thickness_mm),
            THICKNESS_TOLERANCE_MM,
        ) and
        _pair_overlap(left, right) > (
            MIN_POSITIVE_OVERLAP_M + _FLOAT_EPSILON)
    )


def _rounded(value: float) -> float:
    result = round(value, 12)
    return 0.0 if result == 0.0 else result


def _canonical_member(segment: _Segment) -> dict[str, Any]:
    endpoints = sorted((
        [_rounded(segment.start[0]), _rounded(segment.start[1])],
        [_rounded(segment.end[0]), _rounded(segment.end[1])],
    ))
    return {
        "endpoints_m": endpoints,
        "thickness_mm": _rounded(segment.thickness_mm),
    }


def _group_id(structure: Sequence[_Segment],
              architecture: Sequence[_Segment]) -> str:
    def members(values: Sequence[_Segment]) -> list[dict[str, Any]]:
        canonical = [_canonical_member(value) for value in values]
        return sorted(
            canonical,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            ),
        )

    payload = {
        "structure": members(structure),
        "architecture": members(architecture),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"wall-segmentation-equivalence-{digest[:20]}"


def _prove_component(structure: Sequence[_Segment],
                     architecture: Sequence[_Segment]
                     ) -> dict[str, Any] | None:
    if len(structure) == 1 and len(architecture) >= 2:
        singleton = structure[0]
        many = list(architecture)
        relation = "ONE_STRUCTURE_TO_MANY_ARCHITECTURE"
        singleton_discipline = "structure"
        many_discipline = "architecture"
    elif len(architecture) == 1 and len(structure) >= 2:
        singleton = architecture[0]
        many = list(structure)
        relation = "MANY_STRUCTURE_TO_ONE_ARCHITECTURE"
        singleton_discipline = "architecture"
        many_discipline = "structure"
    else:
        return None

    # Recheck the complete star here.  This makes the proof independent of
    # how the caller constructs candidate components.
    if not all(_candidate_pair(singleton, member) for member in many):
        return None

    all_members = [singleton, *many]
    thicknesses = [member.thickness_mm for member in all_members]
    thickness_delta = max(thicknesses) - min(thicknesses)
    if not _within(thickness_delta, THICKNESS_TOLERANCE_MM):
        return None

    angle_delta = max(_angle_delta_deg(singleton, member) for member in many)
    normal_residual = max(_normal_residual(singleton, member) for member in many)
    if (not _within(angle_delta, ANGLE_TOLERANCE_DEG) or
            not _within(normal_residual, DISTANCE_TOLERANCE_M)):
        return None

    axis = _canonical_unit(singleton)
    singleton_interval = _projected_interval(singleton, axis)
    many_intervals = sorted(
        ((_projected_interval(member, axis), member) for member in many),
        key=lambda item: (item[0][0], item[0][1],
                          json.dumps(_canonical_member(item[1]),
                                     sort_keys=True)),
    )
    overlaps = [
        _interval_overlap(singleton_interval, interval)
        for interval, _member in many_intervals
    ]
    if any(overlap <= MIN_POSITIVE_OVERLAP_M + _FLOAT_EPSILON
           for overlap in overlaps):
        return None

    boundary_residuals = (
        abs(many_intervals[0][0][0] - singleton_interval[0]),
        abs(many_intervals[-1][0][1] - singleton_interval[1]),
    )
    boundary_residual = max(boundary_residuals)
    if not _within(boundary_residual, DISTANCE_TOLERANCE_M):
        return None

    seam_residuals = []
    for (previous, _previous_member), (current, _current_member) in zip(
            many_intervals, many_intervals[1:]):
        signed_gap = current[0] - previous[1]
        # Positive is a gap, negative is an overlap.  Either is tolerated only
        # up to 1 mm; larger overlaps are material double coverage.
        if not _within(abs(signed_gap), DISTANCE_TOLERANCE_M):
            return None
        seam_residuals.append(signed_gap)

    max_seam_residual = max(
        (abs(value) for value in seam_residuals), default=0.0)
    structure_indices = sorted(member.index for member in structure)
    architecture_indices = sorted(member.index for member in architecture)
    many_indices_in_axis_order = [
        member.index for _interval, member in many_intervals]
    return {
        "id": _group_id(structure, architecture),
        "status": "PROVEN",
        "relation": relation,
        "structure_indices": structure_indices,
        "architecture_indices": architecture_indices,
        "singleton_discipline": singleton_discipline,
        "singleton_index": singleton.index,
        "many_discipline": many_discipline,
        "many_indices_in_axis_order": many_indices_in_axis_order,
        "criteria": {
            "angle_tolerance_deg_inclusive": ANGLE_TOLERANCE_DEG,
            "distance_tolerance_m_inclusive": DISTANCE_TOLERANCE_M,
            "thickness_tolerance_mm_inclusive": THICKNESS_TOLERANCE_MM,
            "minimum_positive_overlap_m_exclusive": (
                MIN_POSITIVE_OVERLAP_M),
        },
        "evidence": {
            "max_angle_delta_deg": _rounded(angle_delta),
            "max_normal_residual_m": _rounded(normal_residual),
            "max_thickness_delta_mm": _rounded(thickness_delta),
            "max_boundary_residual_m": _rounded(boundary_residual),
            "max_seam_gap_or_overlap_m": _rounded(max_seam_residual),
            "minimum_positive_overlap_m": _rounded(min(overlaps)),
        },
    }


def prove_wall_segmentation_equivalence(
    structure_views: Sequence[Mapping[str, Any]],
    architecture_views: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prove unambiguous 1-to-N wall-view segmentation equivalence.

    The returned indices refer to the two input sequences.  Any wall belonging
    to a 1-to-1, N-to-M, branched, gapped, overlapping, or otherwise ambiguous
    candidate component remains unmatched.  Group identifiers are based only
    on canonical geometry, so member order and endpoint direction do not
    affect them.
    """
    structure = _segments(structure_views, "structure_views")
    architecture = _segments(architecture_views, "architecture_views")

    adjacency: dict[tuple[str, int], set[tuple[str, int]]] = defaultdict(set)
    for structure_segment in structure:
        for architecture_segment in architecture:
            if not _candidate_pair(structure_segment, architecture_segment):
                continue
            structure_node = ("structure", structure_segment.index)
            architecture_node = ("architecture", architecture_segment.index)
            adjacency[structure_node].add(architecture_node)
            adjacency[architecture_node].add(structure_node)

    components = []
    visited: set[tuple[str, int]] = set()
    for first in sorted(adjacency):
        if first in visited:
            continue
        pending = [first]
        component: set[tuple[str, int]] = set()
        while pending:
            node = pending.pop()
            if node in component:
                continue
            component.add(node)
            pending.extend(sorted(adjacency[node] - component, reverse=True))
        visited.update(component)
        components.append(component)

    accepted_structure: set[int] = set()
    accepted_architecture: set[int] = set()
    groups = []
    for component in components:
        structure_indices = sorted(
            index for discipline, index in component
            if discipline == "structure")
        architecture_indices = sorted(
            index for discipline, index in component
            if discipline == "architecture")
        if not ((len(structure_indices) == 1 and
                 len(architecture_indices) >= 2) or
                (len(architecture_indices) == 1 and
                 len(structure_indices) >= 2)):
            # This rejects duplicate alternatives and candidate-graph T
            # branches: proof is allowed only when exactly one side is a
            # singleton and every opposite member points back to it.
            continue
        singleton_node = (
            ("structure", structure_indices[0])
            if len(structure_indices) == 1
            else ("architecture", architecture_indices[0])
        )
        many_nodes = {
            node for node in component if node != singleton_node
        }
        if (adjacency[singleton_node] != many_nodes or
                any(adjacency[node] != {singleton_node}
                    for node in many_nodes)):
            continue
        group = _prove_component(
            [structure[index] for index in structure_indices],
            [architecture[index] for index in architecture_indices],
        )
        if group is None:
            continue
        groups.append(group)
        accepted_structure.update(structure_indices)
        accepted_architecture.update(architecture_indices)

    groups.sort(key=lambda item: item["id"])
    return {
        "groups": groups,
        "unmatched_structure_indices": [
            index for index in range(len(structure))
            if index not in accepted_structure
        ],
        "unmatched_architecture_indices": [
            index for index in range(len(architecture))
            if index not in accepted_architecture
        ],
    }
