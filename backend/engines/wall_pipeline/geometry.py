"""Deterministic wall evidence, geometry cleanup and topology audit."""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from backend.engines.wall_pipeline.contracts import (
    CoordinateConfig,
    EvidenceSourceRef,
    GridAxis,
    ReviewGate,
    SourceEntities,
    SourceEntity,
    SourceManifest,
    TopologyJunction,
    WallEvidence,
    WallEvidenceItem,
    WallModel,
    WallPipelineConfig,
    WallRecord,
    ColumnRecord,
    BeamRecord,
    OpeningRecord,
    ConstructionInfo,
    QuantityTakeoff,
)
from backend.engines.wall_pipeline.io import (
    canonical_sha256,
    file_sha256,
    read_artifact,
)
from backend.engines.wall_geometry import wall_geometry_group
from backend.engines.wall_pipeline.standards import validate_modeling_standard
from backend.engines.wall_pipeline.specifications import (
    TextRow,
    resolve_beam_elevation,
    extract as extract_specifications,
    resolve as resolve_specification,
)


_UNIT_TO_METRE = {
    "mm": 0.001, "cm": 0.01, "m": 1.0,
    "in": 0.0254, "ft": 0.3048, "pt": 0.0254 / 72.0,
}

_PDF_SCALE_RE = re.compile(
    r"(?<!\d)1\s*[:：]\s*(\d{1,4})(?!\d)", re.IGNORECASE
)
_PDF_PLAN_TITLE_TOKENS = (
    "平面图", "平面布置", "FLOOR PLAN", "PLAN",
)
_PDF_PRIMARY_PLAN_TOKENS = (
    "墙柱配筋", "柱墙配筋", "基础平面", "结构平面", "建筑平面",
)
_PDF_DETAIL_TITLE_TOKENS = (
    "大样", "详图", "节点", "剖面", "DETAIL", "SECTION",
)

# Revit 2020 rejects curves shorter than roughly 0.8 mm.  Keeping a 2 mm
# deterministic profile floor removes exported HATCH duplicate vertices while
# staying far below drawing/model tolerances and quantity precision.
_MIN_COLUMN_PROFILE_EDGE_M = 0.002

# Keep the two finite wall solids numerically disjoint after face-to-face
# trimming.  This is ten microns in project metres: below drawing precision,
# but large enough to avoid Shapely/Revit treating coincident floating-point
# faces as a positive-area overlap.
_JUNCTION_CLEARANCE_M = 1.0e-5
# A positive-area intersection below this value is numerical residue from
# rounded source coordinates at a trimmed junction, not a constructible clash
# (10 mm² is well below the drawing/model precision used by this pipeline).
_WALL_COLLISION_AREA_EPS_M2 = 1.0e-5


@dataclass(frozen=True)
class Segment:
    entity: SourceEntity
    index: int
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass
class CandidateWall:
    start: tuple[float, float]
    end: tuple[float, float]
    thickness_m: float
    evidence_ids: list[str]
    source_refs: list[EvidenceSourceRef]
    confidence: float


_SHEAR_WALL_LAYER_RE = re.compile(
    r"^(?:S[-_]WALL(?:[-_](?:CONC|RC|SW|S))?|剪力墙|抗震墙)$",
    re.IGNORECASE,
)
_ARCHITECTURAL_WALL_LAYER_RE = re.compile(
    r"^(?:A[-_]WALL(?:[-_](?:FIN|MASONRY|PARTITION))?|A[-_]PART(?:[-_]S)?|A[-_](?:MASONRY|后砌)|建筑墙|后砌|砌体|砌块|填充墙|隔墙)$",
    re.IGNORECASE,
)

_STRUCTURAL_WALL_MARK_RE = re.compile(
    r"^(?:Q|GWQ|SW|S[-_]WALL)\s*[-_]?\d+[A-Z]?$",
    re.IGNORECASE,
)
_ARCHITECTURAL_WALL_MARK_RE = re.compile(
    r"^(?:A|AW|W|A[-_]WALL|隔墙|建筑墙)\s*[-_]?\d*[A-Z]?$",
    re.IGNORECASE,
)


def _wall_structural_role(
    source_refs: Iterable[EvidenceSourceRef],
    manifest_sources: Iterable[ManifestSource],
) -> str:
    """Classify one wall only from traceable source evidence.

    Geometry cannot distinguish a 200 mm partition from a 200 mm shear wall.
    The deterministic classifier therefore uses the explicit leaf CAD layer;
    a manifest's structural sheet role is not wall-specific type evidence.
    Mixed evidence is kept unresolved so it is surfaced for review instead of
    turning a shear wall into a non-structural wall (or vice versa).
    """

    roles: set[str] = set()
    for reference in source_refs:
        leaf = str(reference.layer or "").rsplit("$0$", 1)[-1]
        leaf = leaf.rsplit("|", 1)[-1].strip()
        if not leaf:
            continue
        if _SHEAR_WALL_LAYER_RE.fullmatch(leaf):
            roles.add("shear_wall")
            continue
        if _ARCHITECTURAL_WALL_LAYER_RE.fullmatch(leaf):
            roles.add("architectural_wall")
            continue
        # A structural plan also contains architectural infill walls.  The
        # sheet title/role therefore cannot resolve an ambiguous A-WALL-S /
        # A-WALL-CONC or flattened layer.  Its wall-specific legend/annotation
        # must supply the missing semantics after geometry extraction.

    if roles == {"shear_wall"}:
        return "shear_wall"
    if roles == {"architectural_wall"}:
        return "architectural_wall"
    return "unresolved"


def _wall_specification_roles(mark: str | None, text: str | None) -> set[str]:
    """Classify only a wall's associated, explicit legend/annotation.

    A bare thickness such as ``墙厚400`` is dimensional evidence, not a
    structural designation.  It must not turn every wall on a structural
    sheet into a shear wall, nor turn every unmarked wall into a partition.
    Column marks (GBZ/KZ/etc.) are deliberately not wall-role evidence.
    """
    normalized_mark = str(mark or "").replace(" ", "").upper()
    normalized_text = str(text or "").upper()
    roles: set[str] = set()
    if _STRUCTURAL_WALL_MARK_RE.fullmatch(normalized_mark) or re.search(
        r"剪力墙|抗震墙|SHEAR\s*WALL", normalized_text
    ):
        roles.add("shear_wall")
    if _ARCHITECTURAL_WALL_MARK_RE.fullmatch(normalized_mark) or re.search(
        r"建筑墙|后砌|砌体|砌块|填充墙|隔墙|非承重|MASONRY|PARTITION",
        normalized_text,
    ):
        roles.add("architectural_wall")
    return roles


def _matches_layer(layer: str, includes: list[str], excludes: list[str]) -> bool:
    try:
        included = not includes or any(re.search(pattern, layer, re.IGNORECASE) for pattern in includes)
        excluded = any(re.search(pattern, layer, re.IGNORECASE) for pattern in excludes)
    except re.error as exc:
        raise ValueError(f"invalid layer regular expression: {exc}") from exc
    return included and not excluded


def _semantic_text_matches(
    entity: SourceEntity,
    includes: list[str],
    excludes: list[str],
    *,
    source_type: str,
) -> bool:
    """Allow explicit OCR labels to classify vector geometry.

    A PDF renderer often drops CAD text layers, so RapidOCR entities use a
    synthetic ``OCR`` layer.  The caller still applies a strict mark regex;
    this helper only makes the already-classified text eligible for pairing
    with an immutable vector profile/boundary.  OCR never supplies geometry.
    """

    if _matches_layer(entity.layer, includes, excludes):
        return True
    return (
        source_type == "pdf"
        and entity.kind == "text"
        and str(entity.style.get("source") or "").lower() == "rapidocr"
        and not any(
            re.search(pattern, entity.layer, re.IGNORECASE)
            for pattern in excludes
        )
    )


def _segments(entities: Iterable[SourceEntity], config: WallPipelineConfig) -> list[Segment]:
    result: list[Segment] = []
    for entity in entities:
        if entity.kind == "text" or not _matches_layer(
            entity.layer, config.wall.include_layers, config.wall.exclude_layers
        ):
            continue
        geometry = entity.geometry
        pairs = []
        if "start" in geometry and "end" in geometry:
            pairs.append((geometry["start"], geometry["end"]))
        else:
            points = geometry.get("points") or []
            pairs.extend(zip(points, points[1:]))
        for index, (start, end) in enumerate(pairs):
            try:
                first = (float(start[0]), float(start[1]))
                second = (float(end[0]), float(end[1]))
            except (IndexError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "invalid 2D geometry for source entity " + entity.entity_id
                ) from exc
            if not all(math.isfinite(value) for value in (*first, *second)):
                raise ValueError(
                    "non-finite 2D geometry for source entity " + entity.entity_id
                )
            if math.dist(first, second) > 1e-9:
                result.append(Segment(entity, index, first, second))
    return result


def _scoped_segments(
    entities: Iterable[SourceEntity],
    includes: list[str],
    excludes: list[str],
) -> list[Segment]:
    """Return vector segments for one deterministic layer scope."""

    result: list[Segment] = []
    for entity in entities:
        if entity.kind == "text" or not _matches_layer(
            entity.layer, includes, excludes
        ):
            continue
        geometry = entity.geometry
        if "start" in geometry and "end" in geometry:
            pairs = [(geometry["start"], geometry["end"])]
        else:
            points = geometry.get("points") or []
            pairs = list(zip(points, points[1:]))
        for index, (start, end) in enumerate(pairs):
            first = (float(start[0]), float(start[1]))
            second = (float(end[0]), float(end[1]))
            if math.dist(first, second) <= 1.0e-9:
                continue
            result.append(Segment(entity, index, first, second))
    return result


def _scale_to_m(
    coordinate: CoordinateConfig, source_units: dict[str, str], source_file_id: str,
    *, source_type: str, inferred_pdf_scale_to_m: float | None = None,
) -> float:
    if coordinate.scale_to_m is not None:
        return coordinate.scale_to_m
    if coordinate.calibration:
        values = [
            pair.model_distance_m / pair.source_distance
            for pair in coordinate.calibration
        ]
        return float(statistics.median(values))
    if source_type == "pdf" and inferred_pdf_scale_to_m is not None:
        return inferred_pdf_scale_to_m
    unit = coordinate.source_unit
    if unit == "auto":
        unit = source_units.get(source_file_id, "unitless")
    if source_type == "pdf":
        raise ValueError(
            "PDF model scale is not inferable from page points; configure "
            "coordinate.scale_to_m or coordinate.calibration"
        )
    if unit not in _UNIT_TO_METRE:
        raise ValueError(
            f"source unit is unresolved for {source_file_id}; configure coordinate.source_unit"
        )
    return _UNIT_TO_METRE[unit]


def _infer_pdf_scale(
    source_entities: SourceEntities, source_file_id: str,
) -> dict[str, Any] | None:
    """Infer the primary plan scale from source text evidence.

    A PDF point is a paper-space unit.  We therefore need the printed scale
    before any wall thickness or grid spacing can be interpreted in metres.
    Main-plan titles outrank detail/section scales, while repeated equal
    candidates provide a small deterministic tie-break.  Ambiguous top-ranked
    scales fail closed instead of silently selecting a convenient value.
    """

    text_entities = [
        entity for entity in source_entities.entities
        if entity.kind == "text" and entity.source_file_id == source_file_id
    ]
    candidates: list[dict[str, Any]] = []
    for entity in text_entities:
        text = str(entity.geometry.get("text") or "").strip()
        if not text:
            continue
        upper = text.upper()
        for match in _PDF_SCALE_RE.finditer(upper):
            denominator = int(match.group(1))
            if not 10 <= denominator <= 2000:
                continue
            context_parts = [upper]
            point = entity.geometry.get("point")
            if point and len(point) >= 2:
                for other in text_entities:
                    if other.entity_id == entity.entity_id:
                        continue
                    if other.frame_id != entity.frame_id or other.page_no != entity.page_no:
                        continue
                    other_point = other.geometry.get("point")
                    if not other_point or len(other_point) < 2:
                        continue
                    if math.hypot(
                        float(other_point[0]) - float(point[0]),
                        float(other_point[1]) - float(point[1]),
                    ) <= 180.0:
                        context_parts.append(
                            str(other.geometry.get("text") or "").upper()
                        )
            context = " ".join(context_parts)
            plan_title = any(
                token in context for token in _PDF_PLAN_TITLE_TOKENS
            )
            primary_plan = any(
                token in context for token in _PDF_PRIMARY_PLAN_TOKENS
            ) or bool(re.search(
                r"(?:地下|地上)?[B0-9一二三四五六七八九十]+层.{0,12}平面",
                context,
            ))
            detail_title = any(
                token in context for token in _PDF_DETAIL_TITLE_TOKENS
            )
            score = (
                1.0
                + (5.0 if plan_title else 0.0)
                + (6.0 if primary_plan else 0.0)
                - (3.0 if detail_title else 0.0)
            )
            candidates.append({
                "denominator": denominator,
                "score": score,
                "entity_id": entity.entity_id,
                "locator": entity.locator,
                "text": text,
            })
    if not candidates:
        return None

    grouped: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        denominator = int(candidate["denominator"])
        group = grouped.setdefault(denominator, {
            "denominator": denominator,
            "best_score": float("-inf"),
            "count": 0,
            "entity_ids": [],
            "texts": [],
        })
        group["best_score"] = max(group["best_score"], candidate["score"])
        group["count"] += 1
        group["entity_ids"].append(candidate["entity_id"])
        if candidate["text"] not in group["texts"]:
            group["texts"].append(candidate["text"])
    ranked = sorted(
        grouped.values(),
        key=lambda item: (item["best_score"], min(item["count"], 5)),
        reverse=True,
    )
    winner = ranked[0]
    if len(ranked) > 1:
        runner_up = ranked[1]
        if (
            winner["best_score"] == runner_up["best_score"]
            and min(winner["count"], 5) == min(runner_up["count"], 5)
        ):
            values = ", ".join(
                f"1:{item['denominator']}" for item in ranked[:5]
            )
            raise ValueError(
                "PDF primary plan scale is ambiguous; detected " + values
                + ". Select the main-plan scale in the request."
            )
    denominator = int(winner["denominator"])
    return {
        "denominator": denominator,
        "scale_to_m": denominator * 25.4 / 72.0 / 1000.0,
        "entity_ids": list(dict.fromkeys(winner["entity_ids"])),
        "texts": winner["texts"][:10],
        "candidate_denominators": [
            int(item["denominator"]) for item in ranked
        ],
    }


def _dominant_rotation_deg(
    segments: list[Segment], scales: dict[str, float]
) -> float:
    samples = []
    for segment in segments:
        scale = scales[segment.entity.source_file_id]
        dx = (segment.end[0] - segment.start[0]) * scale
        dy = (segment.end[1] - segment.start[1]) * scale
        length = math.hypot(dx, dy)
        if length <= 0.5:
            continue
        angle = math.degrees(math.atan2(dy, dx)) % 90.0
        if angle > 45.0:
            angle -= 90.0
        samples.extend([angle] * max(1, min(100, round(length))))
    return -float(statistics.median(samples)) if samples else 0.0


def _normalized_rotation_deg(value: float) -> float:
    """Return a stable signed rotation in the Cartesian source frame."""

    result = (float(value) + 180.0) % 360.0 - 180.0
    return 180.0 if math.isclose(result, -180.0, abs_tol=1.0e-9) else result


def _pdf_display_rotation_deg(
    config: WallPipelineConfig,
    source_entities: SourceEntities,
) -> tuple[float, dict[str, int]]:
    """Resolve the PDF /Rotate transform represented by emitted page frames.

    Source adapters deliberately retain unrotated media-box coordinates.  A
    clockwise PDF /Rotate value therefore becomes its inverse in the
    Cartesian y-up model frame.  The current coordinate contract has one
    global rotation, so mixed page rotations must be registered explicitly in
    a future per-frame transform instead of being silently combined.
    """

    if config.source.type != "pdf" or not config.coordinate.apply_pdf_page_rotation:
        return 0.0, {}
    rotations_by_frame: dict[str, int] = {}
    for entity in source_entities.entities:
        frame_id = _entity_frame_id(entity, config.source.type)
        raw = entity.style.get("page_rotation_deg")
        if raw is None:
            continue
        try:
            rotation = int(raw) % 360
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"PDF page rotation is invalid for frame {frame_id}"
            ) from exc
        if rotation not in {0, 90, 180, 270}:
            raise ValueError(
                f"PDF page rotation is unsupported for frame {frame_id}: {rotation}"
            )
        existing = rotations_by_frame.get(frame_id)
        if existing is not None and existing != rotation:
            raise ValueError(
                f"PDF frame {frame_id} contains inconsistent page rotations"
            )
        rotations_by_frame[frame_id] = rotation
    unique = set(rotations_by_frame.values())
    if len(unique) > 1:
        raise ValueError(
            "PDF source frames use mixed page rotations; process them separately "
            "or provide a future per-frame rotation registration"
        )
    page_rotation = next(iter(unique), 0)
    return _normalized_rotation_deg(-page_rotation), rotations_by_frame


def _grid_axis_candidates(
    segments: list[Segment],
    scales: dict[str, float],
    coordinate: CoordinateConfig,
    rotation_deg: float,
    *,
    min_length_m: float,
    axis_tolerance_deg: float,
    source_type: str,
) -> tuple[
    list[tuple[float, tuple[float, float], tuple[float, float], Segment]],
    list[tuple[float, tuple[float, float], tuple[float, float], Segment]],
]:
    """Return vertical (X) and horizontal (Y) authoritative grid lines."""

    vertical = []
    horizontal = []
    for segment in segments:
        scale = scales.get(segment.entity.source_file_id)
        if scale is None:
            continue
        start = _transform_point(
            segment.start,
            scale,
            coordinate,
            rotation_deg,
            frame_id=_entity_frame_id(segment.entity, source_type),
            source_file_id=segment.entity.source_file_id,
        )
        end = _transform_point(
            segment.end,
            scale,
            coordinate,
            rotation_deg,
            frame_id=_entity_frame_id(segment.entity, source_type),
            source_file_id=segment.entity.source_file_id,
        )
        length = math.dist(start, end)
        if length < min_length_m:
            continue
        dx, dy = abs(end[0] - start[0]), abs(end[1] - start[1])
        angle = math.degrees(math.atan2(dy, dx)) if dx or dy else 0.0
        if abs(90.0 - angle) <= axis_tolerance_deg:
            vertical.append(((start[0] + end[0]) / 2.0, start, end, segment))
        elif angle <= axis_tolerance_deg:
            horizontal.append(((start[1] + end[1]) / 2.0, start, end, segment))
    return vertical, horizontal


def _cluster_grid_candidates(
    candidates: list[tuple[float, tuple[float, float], tuple[float, float], Segment]],
    tolerance_m: float,
) -> list[list[tuple[float, tuple[float, float], tuple[float, float], Segment]]]:
    groups: list[list[tuple[float, tuple[float, float], tuple[float, float], Segment]]] = []
    for candidate in sorted(candidates, key=lambda item: item[0]):
        if not groups or abs(candidate[0] - statistics.fmean(
            item[0] for item in groups[-1]
        )) > tolerance_m:
            groups.append([candidate])
        else:
            groups[-1].append(candidate)
    return groups


def _grid_first_coordinate(
    config: WallPipelineConfig,
    source_entities: SourceEntities,
    wall_segments: list[Segment],
    scales: dict[str, float],
) -> tuple[float, CoordinateConfig, dict[str, object]]:
    """Resolve rotation and local origin from source grid lines first.

    When vector axes are configured they are the engineering reference frame;
    walls and columns consume that already-resolved frame.  Wall direction is
    retained only as the backwards-compatible fallback for drawings without
    an axis scope.
    """

    grid_segments = _scoped_segments(
        source_entities.entities,
        config.grid.include_layers,
        config.grid.exclude_layers,
    ) if config.grid.include_layers else []
    page_rotation, page_rotations_by_frame = _pdf_display_rotation_deg(
        config, source_entities
    )
    if config.coordinate.rotation_deg == "auto":
        engineering_rotation = _dominant_rotation_deg(
            grid_segments or wall_segments, scales
        )
    else:
        engineering_rotation = float(config.coordinate.rotation_deg)
    rotation = _normalized_rotation_deg(page_rotation + engineering_rotation)
    effective = config.coordinate
    metadata: dict[str, object] = {
        "grid_segment_count": len(grid_segments),
        "grid_first": bool(grid_segments),
        "origin_shift_m": [0.0, 0.0],
        "pdf_page_rotation_applied_deg": page_rotation,
        "pdf_page_rotations_by_frame": page_rotations_by_frame,
        "engineering_rotation_deg": engineering_rotation,
    }
    if not grid_segments or not config.grid.normalize_origin:
        return rotation, effective, metadata

    vertical, horizontal = _grid_axis_candidates(
        grid_segments,
        scales,
        effective,
        rotation,
        min_length_m=config.grid.min_length_m,
        axis_tolerance_deg=config.grid.axis_tolerance_deg,
        source_type=config.source.type,
    )
    x_groups = _cluster_grid_candidates(vertical, config.grid.cluster_tolerance_m)
    y_groups = _cluster_grid_candidates(horizontal, config.grid.cluster_tolerance_m)
    metadata["detected_x_axis_count"] = len(x_groups)
    metadata["detected_y_axis_count"] = len(y_groups)
    if not x_groups or not y_groups:
        return rotation, effective, metadata
    x_origin = min(statistics.fmean(item[0] for item in group) for group in x_groups)
    y_origin = min(statistics.fmean(item[0] for item in group) for group in y_groups)
    shift = (-x_origin, -y_origin)
    effective = effective.model_copy(update={
        "translation_m": (
            effective.translation_m[0] + shift[0],
            effective.translation_m[1] + shift[1],
        ),
    })
    metadata["origin_shift_m"] = [round(shift[0], 9), round(shift[1], 9)]
    return rotation, effective, metadata


def _transform_point(
    point: tuple[float, float], scale: float, coordinate: CoordinateConfig,
    rotation_deg: float,
    *,
    frame_id: str | None = None,
    source_file_id: str | None = None,
) -> tuple[float, float]:
    x = (point[0] - coordinate.source_origin[0]) * scale
    y = (point[1] - coordinate.source_origin[1]) * scale
    radians = math.radians(rotation_deg)
    cosine, sine = math.cos(radians), math.sin(radians)
    # A source-level offset is a useful default for every page in one PDF;
    # an exact page/frame entry wins when supplementary sheets need separate
    # registration.  This keeps the common one-file configuration concise
    # without ever combining geometry across frames.
    offset = coordinate.frame_offsets_m.get(frame_id or "")
    if offset is None and source_file_id:
        offset = coordinate.frame_offsets_m.get(source_file_id)
    if offset is None:
        offset = (0.0, 0.0)
    return (
        round(x * cosine - y * sine + coordinate.translation_m[0] + offset[0], 9),
        round(x * sine + y * cosine + coordinate.translation_m[1] + offset[1], 9),
    )


def _frame_has_offset(
    frame_id: str,
    source_entities: SourceEntities,
    coordinate: CoordinateConfig,
    source_type: str = "dxf",
) -> bool:
    """Check exact-frame or owning-source registration for diagnostics."""

    if frame_id in coordinate.frame_offsets_m:
        return True
    owners = {
        entity.source_file_id
        for entity in source_entities.entities
        if _entity_frame_id(entity, source_type) == frame_id
    }
    return any(owner in coordinate.frame_offsets_m for owner in owners)


def _entity_frame_id(entity: SourceEntity, source_type: str) -> str:
    """Resolve a stable local frame even for hand-built source fixtures."""

    if entity.frame_id:
        return entity.frame_id
    if source_type == "pdf" and entity.page_no is not None:
        return f"{entity.source_file_id}:page:{entity.page_no:04d}"
    return entity.source_file_id


def _actual_pdf_page_frames(manifest: SourceManifest) -> dict[str, str]:
    """Read bounded PDF page counts and return the frames that really exist.

    A page with no vector/text entities is still a valid source frame (for
    example, a supplementary sheet carrying only a grid).  Entity-derived
    frames alone cannot represent that case, so inspect the PDF page table at
    the geometry boundary.  The adapter's hash and page-budget checks are
    reused before opening each file.
    """

    from backend.engines.wall_pipeline.adapters import (
        max_pdf_pages,
        verify_source_file_against_manifest,
    )

    try:
        import pymupdf as fitz
    except Exception as exc:  # pragma: no cover - dependency packaging failure
        raise ValueError("PDF frame validation requires PyMuPDF") from exc

    result: dict[str, str] = {}
    for source in manifest.sources:
        try:
            verify_source_file_against_manifest(source)
            document = fitz.open(source.path)
        except Exception as exc:
            raise ValueError(
                "PDF page frame validation failed for " + source.source_file_id
            ) from exc
        try:
            try:
                page_count = int(document.page_count)
            except Exception as exc:
                raise ValueError(
                    "PDF page frame count is invalid for " + source.source_file_id
                ) from exc
            if page_count < 0:
                raise ValueError(
                    "PDF page frame count is invalid for "
                    + source.source_file_id
                )
            # Match the adapter policy: selecting one explicit page does not
            # require materializing the whole (possibly very large) document.
            if source.page_no is None and page_count > max_pdf_pages():
                raise ValueError(
                    "PDF page frame count exceeds the configured limit for "
                    + source.source_file_id
                )
            page_indexes = (
                [source.page_no - 1]
                if source.page_no is not None
                else range(page_count)
            )
            for page_index in page_indexes:
                if not 0 <= page_index < page_count:
                    raise ValueError(
                        "PDF source page does not exist for " + source.source_file_id
                    )
                frame_id = f"{source.source_file_id}:page:{page_index + 1:04d}"
                result[frame_id] = source.source_file_id
        finally:
            try:
                document.close()
            except Exception:
                pass
    return result


def _validate_frame_registry(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    source_entities: SourceEntities,
) -> tuple[set[str], dict[str, str]]:
    """Validate source/page frames before applying any coordinate offsets.

    ``frame_offsets_m`` is configuration, not evidence.  Treating every key
    in it as a valid frame would let a typo (or a substituted artifact) move
    geometry from an unparsed PDF page, and would give CAD files synthetic
    page frames they cannot produce.  The adapters are the authority for
    frame identity: PDF frames are derived from an emitted page number, while
    CAD frames are exactly their source file ID.

    The returned owner map is reused by grid transformation and by callers
    that need to check ``frame_id``/``source_file_id`` consistency.
    """

    if manifest.source_type != config.source.type:
        raise ValueError(
            "source manifest type does not match the pipeline configuration"
        )
    source_ids = {source.source_file_id for source in manifest.sources}
    if not source_ids:
        raise ValueError("source manifest contains no source frames")
    known_frames: set[str] = set(source_ids)
    owners: dict[str, str] = {source_id: source_id for source_id in source_ids}
    actual_pdf_frames = (
        _actual_pdf_page_frames(manifest)
        if config.source.type == "pdf" else {}
    )
    known_frames.update(actual_pdf_frames)
    owners.update(actual_pdf_frames)

    for entity in source_entities.entities:
        source_id = entity.source_file_id
        if source_id not in source_ids:
            raise ValueError(
                "source entity references an unknown source_file_id: " + source_id
            )
        if config.source.type == "pdf":
            if entity.page_no is None:
                raise ValueError(
                    "PDF source entity must identify a page: " + entity.entity_id
                )
            expected = f"{source_id}:page:{entity.page_no:04d}"
        else:
            if entity.page_no is not None:
                raise ValueError(
                    "CAD source entity cannot identify a PDF page: "
                    + entity.entity_id
                )
            expected = source_id
        actual = entity.frame_id or expected
        if actual != expected:
            raise ValueError(
                "source entity frame does not match its source/page: "
                + entity.entity_id
            )
        if config.source.type == "pdf" and actual not in actual_pdf_frames:
            raise ValueError(
                "PDF source entity references a page that does not exist: "
                + actual
            )
        known_frames.add(actual)
        owners[actual] = source_id

    for frame_id in config.coordinate.frame_offsets_m:
        # A source-level offset is valid for both CAD and PDF (for PDF it is
        # the default transform inherited by each page).
        if frame_id in source_ids:
            continue
        if config.source.type != "pdf":
            raise ValueError(
                "CAD frame_offsets_m may only reference source_file_id: "
                + frame_id
            )
        # A PDF page frame is valid only when the adapter actually emitted
        # that page frame.  Prefix-only matching would accept e.g. page 9999.
        if frame_id not in known_frames or frame_id not in owners:
            raise ValueError(
                "PDF frame_offsets_m references an unparsed page frame: "
                + frame_id
            )

    return known_frames, owners


def _angle_delta(first: np.ndarray, second: np.ndarray) -> float:
    cosine = float(np.clip(abs(np.dot(first, second)), -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def _source_ref(segment: Segment) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_file_id=segment.entity.source_file_id,
        entity_id=segment.entity.entity_id,
        page_no=segment.entity.page_no,
        locator=f"{segment.entity.locator}/segment:{segment.index}",
        layer=segment.entity.layer,
        frame_id=_entity_frame_id(segment.entity, "pdf")
        if segment.entity.page_no is not None
        else (segment.entity.frame_id or segment.entity.source_file_id),
    )


def _column_source_ref(entity: SourceEntity) -> EvidenceSourceRef:
    """Build a trace reference for a closed column outline (not a segment)."""

    return EvidenceSourceRef(
        source_file_id=entity.source_file_id,
        entity_id=entity.entity_id,
        page_no=entity.page_no,
        locator=entity.locator,
        layer=entity.layer,
        frame_id=_entity_frame_id(entity, "pdf")
        if entity.page_no is not None
        else (entity.frame_id or entity.source_file_id),
    )


def _source_entity_key(entity: SourceEntity) -> tuple[str, str, str]:
    """Return a collision-resistant identity for one source geometry object."""

    return (entity.source_file_id, entity.entity_id, entity.locator)


def _segment_key(segment: Segment) -> tuple[str, str, str, int]:
    """Identify a segment without conflating equal handles across source frames."""

    return (*_source_entity_key(segment.entity), segment.index)


def _source_ref_key(ref: EvidenceSourceRef) -> tuple[str, str, str, str]:
    return (
        ref.source_file_id,
        ref.entity_id,
        ref.locator,
        ref.frame_id or "",
    )


def _ref_frame_id(ref: EvidenceSourceRef) -> str:
    if ref.frame_id:
        return ref.frame_id
    if ref.page_no is not None:
        return f"{ref.source_file_id}:page:{ref.page_no:04d}"
    return ref.source_file_id


def _candidate_frame_ids(refs: Iterable[EvidenceSourceRef]) -> set[str]:
    return {_ref_frame_id(ref) for ref in refs}


def _pair_segments(
    segments: list[tuple[Segment, tuple[float, float], tuple[float, float]]],
    config: WallPipelineConfig,
    declared_thicknesses_by_frame: dict[str, set[float]] | None = None,
) -> tuple[list[WallEvidenceItem], set[tuple[str, str, str, int]]]:
    # Pairing used to compare every segment with every other segment.  A real
    # floor plan can contain several thousand wall-face segments, making that
    # O(n²) loop the dominant (and sometimes unbounded-looking) part of the
    # pipeline.  A spatial index generates only finite segments within the
    # maximum possible wall thickness; the original deterministic checks below
    # remain the authority, so this is an acceleration rather than a heuristic
    # change to recognition semantics.
    eligible: list[tuple[int, Segment, tuple[float, float], tuple[float, float], float, np.ndarray, LineString]] = []
    for original_index, (segment, start, end) in enumerate(segments):
        vector = np.asarray(end, dtype=float) - np.asarray(start, dtype=float)
        length = float(np.linalg.norm(vector))
        if length < config.wall.min_length_m:
            continue
        unit = vector / length
        eligible.append((
            original_index, segment, start, end, length, unit,
            LineString((start, end)),
        ))
    if len(eligible) < 2:
        return [], set()

    tree = STRtree([item[6] for item in eligible])
    # Keep the spatial prefilter consistent with the bounded engineering
    # tolerance below.  Without the margin, a nominal 800 mm pair converted
    # from a PDF can measure 800.09 mm and be excluded before the deterministic
    # thickness check ever sees it.
    declared_thicknesses_by_frame = declared_thicknesses_by_frame or {}
    declared_thicknesses = {
        value for values in declared_thicknesses_by_frame.values() for value in values
    }
    pair_search_distance = (
        max(float(config.wall.max_thickness_m), *declared_thicknesses)
        if declared_thicknesses else float(config.wall.max_thickness_m)
    ) + float(config.wall.thickness_tolerance_m)
    pair_indexes: set[tuple[int, int]] = set()
    for first_position, first in enumerate(eligible):
        try:
            nearby = tree.query(
                first[6], predicate="dwithin",
                distance=pair_search_distance,
            )
        except (TypeError, ValueError):
            # Shapely >=2.1 (the supported runtime) accepts ``dwithin``.  The
            # fallback keeps the adapter usable with a compatible 2.x build
            # that lacks the predicate while retaining a bounded spatial query.
            nearby = tree.query(first[6].buffer(pair_search_distance))
        for second_position in nearby:
            second_position = int(second_position)
            if second_position <= first_position:
                continue
            pair_indexes.add((first_position, second_position))

    candidates = []
    thickness_midpoint = statistics.fmean((
        config.wall.min_thickness_m, config.wall.max_thickness_m
    ))
    for first_position, second_position in sorted(pair_indexes):
        _, first, a0, a1, length_a, au, _ = eligible[first_position]
        _, second, b0, b1, length_b, bu, _ = eligible[second_position]
        if _angle_delta(au, bu) > config.wall.parallel_tolerance_deg:
            continue
        normal = np.asarray([-au[1], au[0]])
        a0_array = np.asarray(a0)
        b0_array = np.asarray(b0)
        b1_array = np.asarray(b1)
        b_values = [float(np.dot(np.asarray(point) - a0_array, au))
                    for point in (b0, b1)]
        overlap_start = max(0.0, min(b_values))
        overlap_end = min(length_a, max(b_values))
        overlap = overlap_end - overlap_start
        if overlap < config.wall.min_length_m or (
            overlap / min(length_a, length_b) < config.wall.min_overlap_ratio
        ):
            continue
        distances = [abs(float(np.dot(np.asarray(point) - a0_array, normal)))
                     for point in (b0, b1)]
        thickness = statistics.fmean(distances)
        if max(distances) - min(distances) > max(0.01, thickness * 0.1):
            continue
        within_default_range = (
            config.wall.min_thickness_m
            <= thickness
            <= config.wall.max_thickness_m + config.wall.thickness_tolerance_m
        )
        # A drawing table can declare walls thicker than the default search
        # range.  Admit only those discrete widths, not every spacing up to
        # the largest table value, and never borrow another sheet's table.
        pair_frames = {
            _entity_frame_id(segment.entity, config.source.type)
            for segment in (first, second)
        }
        declared_match = any(
            abs(thickness - nominal) <= config.wall.thickness_tolerance_m
            for frame_id in pair_frames
            for nominal in declared_thicknesses_by_frame.get(frame_id, set())
        )
        if not within_default_range and not declared_match:
            continue
        a_start = a0_array + overlap_start * au
        a_end = a0_array + overlap_end * au
        signed = statistics.fmean([
            float(np.dot(np.asarray(point) - a0_array, normal))
            for point in (b0, b1)
        ])
        center_start = a_start + normal * signed / 2.0
        center_end = a_end + normal * signed / 2.0
        score = overlap / max(length_a, length_b)
        first_index = eligible[first_position][0]
        second_index = eligible[second_position][0]
        # Keep the contribution interval on each source face.  A long CAD
        # face commonly borders several adjacent wall strips; consuming the
        # whole segment after the first match silently drops the remaining
        # walls.  Non-overlapping intervals may be paired independently,
        # while genuinely duplicate intervals are still rejected below.
        second_origin = np.asarray(b0)
        second_axis = bu
        interval_b = [
            float(np.dot(point - second_origin, second_axis))
            for point in (a_start, a_end)
        ]
        candidates.append((
            -score, abs(thickness - thickness_midpoint), first_index,
            second_index, center_start, center_end, thickness,
            min(overlap_start, overlap_end), max(overlap_start, overlap_end),
            min(interval_b), max(interval_b),
        ))
    candidates.sort(key=lambda item: item[:4])
    used: set[tuple[str, str, str, int]] = set()
    consumed: dict[tuple[str, str, str, int], list[tuple[float, float]]] = {}

    def interval_available(key, start_value, end_value) -> bool:
        for old_start, old_end in consumed.get(key, []):
            overlap = min(end_value, old_end) - max(start_value, old_start)
            if overlap > config.wall.dedupe_tolerance_m:
                return False
        return True

    result: list[WallEvidenceItem] = []
    for (
        _, __, first_index, second_index, start, end, thickness,
        first_interval_start, first_interval_end,
        second_interval_start, second_interval_end,
    ) in candidates:
        first_key = _segment_key(segments[first_index][0])
        second_key = _segment_key(segments[second_index][0])
        if not interval_available(first_key, first_interval_start, first_interval_end):
            continue
        if not interval_available(second_key, second_interval_start, second_interval_end):
            continue
        consumed.setdefault(first_key, []).append((first_interval_start, first_interval_end))
        consumed.setdefault(second_key, []).append((second_interval_start, second_interval_end))
        used.update((first_key, second_key))
        first, second = segments[first_index][0], segments[second_index][0]
        refs = []
        for ref in (_source_ref(first), _source_ref(second)):
            if _source_ref_key(ref) not in {_source_ref_key(item) for item in refs}:
                refs.append(ref)
        method = (
            "closed_vector_strip"
            if _source_entity_key(first.entity) == _source_entity_key(second.entity)
            else "parallel_vector_faces"
        )
        identity = "|".join(sorted(ref.locator for ref in refs))
        evidence_id = "wall_ev_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        result.append(WallEvidenceItem(
            evidence_id=evidence_id,
            method=method,
            centerline_m=(tuple(map(float, start)), tuple(map(float, end))),
            thickness_m=round(float(thickness), 6),
            source_refs=refs,
            confidence=round(min(0.99, 0.82 + 0.17 * (-_)), 4),
        ))
    return result, used


def build_wall_evidence(
    config: WallPipelineConfig, manifest: SourceManifest,
    source_entities: SourceEntities,
) -> WallEvidence:
    _validate_frame_registry(config, manifest, source_entities)
    raw_segments = _segments(source_entities.entities, config)
    inferred_pdf_scales: dict[str, dict[str, Any]] = {}
    if (
        config.source.type == "pdf"
        and config.coordinate.scale_to_m is None
        and not config.coordinate.calibration
    ):
        for source in manifest.sources:
            inference = _infer_pdf_scale(
                source_entities, source.source_file_id
            )
            if inference is not None:
                inferred_pdf_scales[source.source_file_id] = inference
    scales = {
        source.source_file_id: _scale_to_m(
            config.coordinate, source_entities.source_units, source.source_file_id,
            source_type=config.source.type,
            inferred_pdf_scale_to_m=(
                inferred_pdf_scales.get(source.source_file_id, {}).get("scale_to_m")
            ),
        )
        for source in manifest.sources
    }
    rotation, effective_coordinate, grid_alignment = _grid_first_coordinate(
        config,
        source_entities,
        raw_segments,
        scales,
    )
    transformed = [(
        segment,
        _transform_point(
            segment.start, scales[segment.entity.source_file_id],
            effective_coordinate, rotation,
            frame_id=_entity_frame_id(segment.entity, config.source.type),
            source_file_id=segment.entity.source_file_id,
        ),
        _transform_point(
            segment.end, scales[segment.entity.source_file_id],
            effective_coordinate, rotation,
            frame_id=_entity_frame_id(segment.entity, config.source.type),
            source_file_id=segment.entity.source_file_id,
        ),
    ) for segment in raw_segments]
    # Never pair geometry from two local drawing frames.  A PDF page or a
    # supplementary DWG can reuse the same coordinates; pairing them would
    # manufacture a wall that has no source evidence.  The caller may opt into
    # a shared project frame with ``coordinate.frame_offsets_m``.
    # Keep the original index alongside each frame-local tuple.  The pairing
    # helper identifies consumed segments by ``(entity_id, segment_index)``;
    # mapping those keys back to the global list explicitly avoids marking an
    # equal-looking segment from another frame as consumed.
    grouped: dict[
        str,
        list[tuple[int, tuple[Segment, tuple[float, float], tuple[float, float]]]],
    ] = {}
    for global_index, item in enumerate(transformed):
        frame_id = _entity_frame_id(item[0].entity, config.source.type)
        grouped.setdefault(frame_id, []).append((global_index, item))
    items: list[WallEvidenceItem] = []
    used_global: set[int] = set()
    frame_ids = sorted(grouped)

    # A frame registration is an explicit engineering assertion that two
    # sheets share one project coordinate system.  Without that assertion we
    # must keep pairing frame-local (otherwise two unrelated PDF pages with
    # coincident coordinates can manufacture a wall).  Once every involved
    # frame has an exact offset, pairing across the registered frames is safe
    # and is needed for the common case where one sheet supplies one wall face
    # and a supplementary sheet supplies the opposing face.
    cross_frame_pairing = (
        len(frame_ids) <= 1
        or all(frame_id in config.coordinate.frame_offsets_m for frame_id in frame_ids)
    )
    # Read dimension declarations before face pairing; resolving types only
    # after pairing is too late for walls rejected by the default 600 mm cap.
    # Text only widens eligibility for a proven pair of vector faces.  It
    # never creates a coordinate, wall, or nominal size in the evidence.
    specification_rows = _text_rows_in_frame(
        config, manifest, source_entities, scales, rotation, effective_coordinate,
    )
    declared_specs = extract_specifications(
        specification_rows,
        schedule_alignment_tolerance_m=config.wall.schedule_alignment_tolerance_m,
        schedule_alignment_gap_m=config.wall.schedule_alignment_gap_m,
        column_association_radius_m=config.column.specification_association_distance_m,
    )
    declarations: dict[tuple[str, str], set[float]] = {}
    for spec in declared_specs:
        thickness_mm = spec.dimensions_mm.get("thickness_mm")
        if spec.component_kind == "wall" and spec.mark and thickness_mm is not None:
            declarations.setdefault((spec.frame_id, spec.mark), set()).add(
                thickness_mm / 1000.0
            )
    declared_thicknesses_by_frame: dict[str, set[float]] = {}
    for (frame_id, _), values in declarations.items():
        # Conflicting rows are a review issue, not permission to guess.
        if len(values) == 1 and next(iter(values)) > config.wall.max_thickness_m:
            declared_thicknesses_by_frame.setdefault(frame_id, set()).update(values)
    if cross_frame_pairing:
        frame_items, frame_used = _pair_segments(transformed, config, declared_thicknesses_by_frame)
        items.extend(frame_items)
        for global_index, (segment, _, __) in enumerate(transformed):
            if _segment_key(segment) in frame_used:
                used_global.add(global_index)
    else:
        for frame_id in frame_ids:
            frame_entries = grouped[frame_id]
            frame_items, frame_used = _pair_segments(
                [item for _, item in frame_entries], config, declared_thicknesses_by_frame
            )
            items.extend(frame_items)
            for global_index, (segment, _, __) in frame_entries:
                key = _segment_key(segment)
                if key in frame_used:
                    used_global.add(global_index)
    used = used_global
    if config.wall.allow_single_line_evidence:
        for index, (segment, start, end) in enumerate(transformed):
            if index in used or math.dist(start, end) < config.wall.min_length_m:
                continue
            ref = _source_ref(segment)
            identity = ref.locator + ":single"
            items.append(WallEvidenceItem(
                evidence_id="wall_ev_" + hashlib.sha256(
                    identity.encode("utf-8")
                ).hexdigest()[:20],
                method="single_semantic_line",
                centerline_m=(start, end),
                thickness_m=round(statistics.fmean((
                    config.wall.min_thickness_m, config.wall.max_thickness_m
                )), 6),
                source_refs=[ref], confidence=0.60,
                limitations=["single line has no opposing face thickness evidence"],
            ))
    diagnostics = list(source_entities.diagnostics)
    if declared_thicknesses_by_frame:
        diagnostics.append({
            "severity": "info",
            "code": "WALL_DECLARED_THICKNESS_SEARCH",
            "default_max_thickness_m": config.wall.max_thickness_m,
            "declarations": [
                {"frame_id": spec.frame_id, "mark": spec.mark,
                 "thickness_mm": spec.dimensions_mm["thickness_mm"],
                 "evidence_entity_ids": list(spec.entity_ids)}
                for spec in declared_specs
                if spec.component_kind == "wall" and spec.mark
                and len(declarations.get((spec.frame_id, spec.mark), set())) == 1
                and spec.dimensions_mm.get("thickness_mm", 0.0) / 1000.0
                in declared_thicknesses_by_frame.get(spec.frame_id, set())
            ],
        })
    for source_file_id, inference in inferred_pdf_scales.items():
        diagnostics.append({
            "severity": "info",
            "code": "PDF_SCALE_INFERRED",
            "source_file_id": source_file_id,
            "scale": f"1:{inference['denominator']}",
            "scale_to_m": inference["scale_to_m"],
            "evidence_entity_ids": inference["entity_ids"],
            "source_texts": inference["texts"],
            "candidate_denominators": inference["candidate_denominators"],
        })
    if not items:
        diagnostics.append({
            "severity": "error", "code": "NO_GEOMETRIC_WALL_EVIDENCE",
            "segment_count": len(raw_segments),
        })
    unmapped_frames = [
        frame_id for frame_id in frame_ids
        if len(frame_ids) > 1 and not _frame_has_offset(
            frame_id, source_entities, config.coordinate, config.source.type
        )
    ]
    if unmapped_frames:
        diagnostics.append({
            "severity": "error",
            "code": "SOURCE_FRAMES_NOT_REGISTERED",
            "frames": unmapped_frames,
            "guidance": "set coordinate.frame_offsets_m for every supplementary/page frame",
        })
    transform_chain = [
        {
            "operation": "unit_convert",
            "scale_to_m_by_source": scales,
            "pdf_scale_inference": inferred_pdf_scales,
        },
        {"operation": "translate_origin", "source_origin": config.coordinate.source_origin},
        {"operation": "rotate", "rotation_deg": rotation,
         "mode": (
             "grid_axis"
             if grid_alignment.get("grid_first")
             and config.coordinate.rotation_deg == "auto"
             else "dominant_axis"
             if config.coordinate.rotation_deg == "auto"
             else "configured"
         ),
         "pdf_page_rotation_applied_deg": grid_alignment.get(
             "pdf_page_rotation_applied_deg", 0.0
         ),
         "engineering_rotation_deg": grid_alignment.get(
             "engineering_rotation_deg", rotation
         ),
         "source_frame": config.coordinate.source_frame,
         "apply_base_point_rotation": config.coordinate.apply_base_point_rotation},
        {"operation": "grid_first_alignment", **grid_alignment},
        {"operation": "translate", "translation_m": effective_coordinate.translation_m,
         "target_origin": config.coordinate.origin,
         "frame_offsets_m": config.coordinate.frame_offsets_m,
         "frame_ids": frame_ids,
         "cross_frame_pairing": cross_frame_pairing},
    ]
    return WallEvidence(
        tenant_id=config.tenant_id,
        project_id=config.project_id,
        source_entities_sha256=canonical_sha256(source_entities),
        transform_chain=transform_chain,
        items=sorted(items, key=lambda item: item.evidence_id),
        diagnostics=diagnostics,
    )


def _dedupe_and_merge(
    evidence: WallEvidence, config: WallPipelineConfig,
) -> list[CandidateWall]:
    translate_operation = next(
        (
            operation for operation in evidence.transform_chain
            if operation.get("operation") == "translate"
        ),
        {},
    )
    allow_cross_frame_merge = bool(translate_operation.get("cross_frame_pairing"))
    candidates: list[CandidateWall] = []
    for item in evidence.items:
        line = LineString(item.centerline_m)
        duplicate = None
        for existing in candidates:
            if not allow_cross_frame_merge and _candidate_frame_ids(
                existing.source_refs
            ).isdisjoint(_candidate_frame_ids(item.source_refs)):
                continue
            other = LineString((existing.start, existing.end))
            if (
                line.hausdorff_distance(other) <= config.wall.dedupe_tolerance_m
                and abs(item.thickness_m - existing.thickness_m) <= 0.02
            ):
                duplicate = existing
                break
        if duplicate:
            duplicate.evidence_ids.append(item.evidence_id)
            for ref in item.source_refs:
                if _source_ref_key(ref) not in {
                    _source_ref_key(value) for value in duplicate.source_refs
                }:
                    duplicate.source_refs.append(ref)
            duplicate.confidence = max(duplicate.confidence, item.confidence)
        else:
            candidates.append(CandidateWall(
                start=item.centerline_m[0], end=item.centerline_m[1],
                thickness_m=item.thickness_m,
                evidence_ids=[item.evidence_id], source_refs=list(item.source_refs),
                confidence=item.confidence,
            ))

    changed = True
    while changed:
        changed = False
        for first_index, first in enumerate(candidates):
            first_line = LineString((first.start, first.end))
            first_vector = np.asarray(first.end) - np.asarray(first.start)
            first_unit = first_vector / np.linalg.norm(first_vector)
            for second_index in range(first_index + 1, len(candidates)):
                second = candidates[second_index]
                if not allow_cross_frame_merge and _candidate_frame_ids(
                    first.source_refs
                ).isdisjoint(_candidate_frame_ids(second.source_refs)):
                    continue
                if abs(first.thickness_m - second.thickness_m) > 0.02:
                    continue
                second_vector = np.asarray(second.end) - np.asarray(second.start)
                second_unit = second_vector / np.linalg.norm(second_vector)
                if _angle_delta(first_unit, second_unit) > config.wall.parallel_tolerance_deg:
                    continue
                if first_line.distance(LineString((second.start, second.end))) > config.wall.break_gap_m:
                    continue
                origin = np.asarray(first.start)
                values = [float(np.dot(np.asarray(point) - origin, first_unit))
                          for point in (first.start, first.end, second.start, second.end)]
                start = origin + min(values) * first_unit
                end = origin + max(values) * first_unit
                refs = list(first.source_refs)
                refs.extend(
                    ref for ref in second.source_refs
                    if _source_ref_key(ref) not in {_source_ref_key(item) for item in refs}
                )
                first.start, first.end = tuple(start), tuple(end)
                first.evidence_ids = list(dict.fromkeys(
                    first.evidence_ids + second.evidence_ids
                ))
                first.source_refs = refs
                first.confidence = min(first.confidence, second.confidence)
                candidates.pop(second_index)
                changed = True
                break
            if changed:
                break
    return candidates


def _infinite_intersection(
    first: CandidateWall, second: CandidateWall
) -> tuple[float, float] | None:
    p = np.asarray(first.start, dtype=float)
    r = np.asarray(first.end, dtype=float) - p
    q = np.asarray(second.start, dtype=float)
    s = np.asarray(second.end, dtype=float) - q
    cross = float(np.cross(r, s))
    if abs(cross) <= 1e-9:
        return None
    t = float(np.cross(q - p, s) / cross)
    point = p + t * r
    return float(point[0]), float(point[1])


def _frames_can_interact(
    first: CandidateWall | set[str],
    second: CandidateWall | set[str],
    *,
    allow_cross_frame: bool,
) -> bool:
    """Return whether two geometry items may contribute to one junction.

    ``build_wall_evidence`` keeps unregistered PDF pages and supplementary
    drawings in separate local frames.  It is not enough to stop the initial
    parallel-face pairing: healing and topology discovery also compare
    coordinates, and coincident local coordinates from two sheets would
    otherwise create a false connection.  A registered project frame is the
    explicit opt-in for those cross-frame comparisons.
    """

    if allow_cross_frame:
        return True
    first_frames = (
        first
        if isinstance(first, set)
        else _candidate_frame_ids(first.source_refs)
    )
    second_frames = (
        second
        if isinstance(second, set)
        else _candidate_frame_ids(second.source_refs)
    )
    return bool(first_frames.intersection(second_frames))


def _heal_junctions(
    walls: list[CandidateWall],
    tolerance: float,
    *,
    allow_cross_frame: bool = False,
) -> None:
    for index, wall in enumerate(walls):
        # Keep the source candidate's direction immutable while healing both
        # ends.  Snapping each end to an independently detected crossing can
        # otherwise turn one straight wall into a subtly skewed segment when
        # PDF/CAD coordinates contain sub-pixel noise (or when the two ends
        # meet walls drawn a fraction of a millimetre apart).
        base_start = np.asarray(wall.start, dtype=float)
        base_end = np.asarray(wall.end, dtype=float)
        base_vector = base_end - base_start
        base_length = float(np.linalg.norm(base_vector))
        if base_length <= 1.0e-9:
            continue
        base_unit = base_vector / base_length

        def project_to_wall_line(point: tuple[float, float]) -> tuple[float, float]:
            value = base_start + base_unit * float(
                np.dot(np.asarray(point, dtype=float) - base_start, base_unit)
            )
            return float(value[0]), float(value[1])

        for endpoint_name in ("start", "end"):
            endpoint = getattr(wall, endpoint_name)
            best: tuple[float, tuple[float, float]] | None = None
            for other_index, other in enumerate(walls):
                if index == other_index:
                    continue
                if not _frames_can_interact(
                    wall, other, allow_cross_frame=allow_cross_frame
                ):
                    continue
                intersection = _infinite_intersection(wall, other)
                # Source face pairs normally stop at the outer face of the
                # perpendicular wall.  The centreline intersection can thus
                # be up to half of both wall widths away without representing
                # a drawing gap.  Use that physical allowance instead of a
                # single global snap radius.
                pair_tolerance = max(
                    tolerance,
                    (wall.thickness_m + other.thickness_m) / 2.0 + 0.02,
                )
                if intersection is None:
                    # Collinear fragments are the common failure mode when a
                    # door/opening mask interrupts one wall face.  The
                    # parallel-face merger intentionally preserves larger
                    # gaps, so heal only an endpoint-to-endpoint gap inside
                    # the physical wall-width allowance.  Never use a text
                    # label or a colour to extend the wall.
                    first_vector = np.asarray(wall.end) - np.asarray(wall.start)
                    second_vector = np.asarray(other.end) - np.asarray(other.start)
                    first_length = float(np.linalg.norm(first_vector))
                    second_length = float(np.linalg.norm(second_vector))
                    if first_length <= 1.0e-9 or second_length <= 1.0e-9:
                        continue
                    angle = _angle_delta(
                        first_vector / first_length,
                        second_vector / second_length,
                    )
                    if angle > 2.0:
                        continue
                    # A parallel wall offset by its physical half-width is
                    # not a broken collinear fragment.  Using the full
                    # ``pair_tolerance`` here would let a nearby parallel
                    # wall steal the endpoint and move the real junction to
                    # the wrong line.  Accept only drafting-scale
                    # cross-track noise; the along-line gap is still bounded
                    # by the physical tolerance below.
                    other_unit = second_vector / second_length
                    cross_track = abs(float(np.cross(
                        np.asarray(endpoint, dtype=float)
                        - np.asarray(other.start, dtype=float),
                        other_unit,
                    )))
                    cross_track_tolerance = min(
                        tolerance,
                        0.05 * min(wall.thickness_m, other.thickness_m),
                    )
                    if cross_track > cross_track_tolerance:
                        continue
                    endpoint_options = [
                        (math.dist(endpoint, other.start), other.start),
                        (math.dist(endpoint, other.end), other.end),
                    ]
                    distance, intersection = min(endpoint_options, key=lambda item: item[0])
                    if distance > pair_tolerance:
                        continue
                    other_line = LineString((other.start, other.end))
                    if other_line.distance(Point(endpoint)) > pair_tolerance:
                        continue
                else:
                    distance = math.dist(endpoint, intersection)
                if distance > pair_tolerance:
                    continue
                # The crossing point is authoritative for the connection,
                # but its transverse component is drafting noise relative to
                # this wall.  Project it back onto the candidate's original
                # centreline so healing never rotates/skews the wall.
                intersection = project_to_wall_line(intersection)
                other_line = LineString((other.start, other.end))
                if other_line.distance(Point(intersection)) > pair_tolerance:
                    continue
                if best is None or distance < best[0]:
                    best = distance, intersection
            if best is not None:
                setattr(wall, endpoint_name, best[1])


def _trim_wall_junctions(
    walls: list[CandidateWall],
    junctions: list[TopologyJunction],
    tolerance: float,
) -> int:
    """Move wall endpoints to the outside face of intersecting walls.

    Wall centerlines commonly meet at the same node.  Creating two finite
    wall solids from those centerlines makes their half-thicknesses overlap,
    which is harmless for a drawing but becomes a real clash for downstream
    quantity and interference checks.  For L/T/X geometric junctions, trim
    only endpoints; Z junctions are semantic annotations and have no physical
    intersection point.  Each terminating wall is moved by *the other wall's*
    half-thickness (projected along its direction).  The previous
    ``(own + other) / 2`` rule trimmed both members by the full combined
    half-width; at a T-junction this left a visible gap equal to the sum of
    both wall widths, so a core-tube perimeter could be reported as open even
    though the source faces met.  Using the other wall's half-width keeps the
    two finite solids touching at the junction without double-trimming it.
    """
    trim_by_endpoint: dict[tuple[int, str], float] = {}
    for junction in junctions:
        if junction.kind not in {"L", "T", "X"}:
            continue
        point = np.asarray(junction.point_m, dtype=float)
        member_indices = [
            int(wall_id.rsplit("_", 1)[-1]) - 1
            for wall_id in junction.wall_ids
            if wall_id.startswith("wall_")
        ]
        member_indices = [
            index for index in member_indices if 0 <= index < len(walls)
        ]
        for index in member_indices:
            wall = walls[index]
            start = np.asarray(wall.start, dtype=float)
            end = np.asarray(wall.end, dtype=float)
            length = float(np.linalg.norm(end - start))
            if length <= 1.0e-9:
                continue
            endpoint_options = [
                ("start", start, end),
                ("end", end, start),
            ]
            for endpoint_name, endpoint, inward_point in endpoint_options:
                if float(np.linalg.norm(endpoint - point)) > tolerance:
                    continue
                inward = inward_point - endpoint
                inward_length = float(np.linalg.norm(inward))
                if inward_length <= 1.0e-9:
                    continue
                inward /= inward_length
                required = 0.0
                for other_index in member_indices:
                    if other_index == index:
                        continue
                    other = walls[other_index]
                    other_start = np.asarray(other.start, dtype=float)
                    other_end = np.asarray(other.end, dtype=float)
                    other_vector = other_end - other_start
                    other_length = float(np.linalg.norm(other_vector))
                    if other_length <= 1.0e-9:
                        continue
                    other_unit = other_vector / other_length
                    sine = abs(float(np.cross(inward, other_unit)))
                    if sine <= 0.2:
                        continue
                    required = max(
                        required,
                        other.thickness_m / (2.0 * sine) + _JUNCTION_CLEARANCE_M,
                    )
                if required > 0.0:
                    key = (index, endpoint_name)
                    trim_by_endpoint[key] = max(trim_by_endpoint.get(key, 0.0), required)

    trimmed = 0
    for (index, endpoint_name), distance in trim_by_endpoint.items():
        wall = walls[index]
        start = np.asarray(wall.start, dtype=float)
        end = np.asarray(wall.end, dtype=float)
        endpoint = start if endpoint_name == "start" else end
        inward_point = end if endpoint_name == "start" else start
        inward = inward_point - endpoint
        length = float(np.linalg.norm(inward))
        if length <= 1.0e-9:
            continue
        # Keep a valid Revit curve.  A fragment this short is already below
        # the configured wall evidence minimum and must be rejected by the
        # normal geometry gate rather than becoming a zero-length curve.
        distance = min(distance, max(0.0, length - 1.0e-6))
        if distance <= 1.0e-9:
            continue
        replacement = endpoint + inward / length * distance
        if endpoint_name == "start":
            wall.start = (float(replacement[0]), float(replacement[1]))
        else:
            wall.end = (float(replacement[0]), float(replacement[1]))
        trimmed += 1
    return trimmed


def _reconcile_post_trim_topology(
    walls: list[CandidateWall],
    junctions: list[TopologyJunction],
    tolerance_m: float,
) -> tuple[list[TopologyJunction], dict[int, list[str]]]:
    """Drop stale/duplicate topology after finite-solid endpoint trimming.

    Topology is classified from centreline intersections before endpoints are
    net-cut to remove wall-solid collisions.  A nearby opening or clustered
    drafting point can make that first pass associate three walls even though
    none of the final finite walls can physically reach another.  Revit must
    never be asked to bridge that gap.  Retain a junction only when at least
    one approved wall pair remains within the same thickness-aware tolerance
    used by the Revit compiler, then rebuild per-wall labels from the retained
    facts.
    """

    lines = [LineString((wall.start, wall.end)) for wall in walls]
    retained: list[TopologyJunction] = []
    labels: dict[int, list[str]] = {index: [] for index in range(len(walls))}
    signatures: set[tuple[str, tuple[str, ...]]] = set()
    for junction in junctions:
        indexes: list[int] = []
        for wall_id in junction.wall_ids:
            try:
                index = int(str(wall_id).split("_")[-1]) - 1
            except (TypeError, ValueError):
                indexes = []
                break
            if not 0 <= index < len(walls):
                indexes = []
                break
            indexes.append(index)
        if len(indexes) < 2:
            continue
        connectable = False
        for position, first_index in enumerate(indexes):
            first = walls[first_index]
            for second_index in indexes[position + 1:]:
                second = walls[second_index]
                pair_tolerance = max(
                    tolerance_m,
                    math.sqrt(2.0) * (
                        first.thickness_m + second.thickness_m
                    ) / 2.0 + 0.02,
                )
                if lines[first_index].distance(lines[second_index]) <= pair_tolerance:
                    connectable = True
                    break
            if connectable:
                break
        signature = (junction.kind, tuple(sorted(junction.wall_ids)))
        if not connectable or signature in signatures:
            continue
        signatures.add(signature)
        retained.append(TopologyJunction(
            junction_id=f"junction_{len(retained) + 1:04d}",
            kind=junction.kind,
            point_m=junction.point_m,
            wall_ids=list(junction.wall_ids),
        ))
        for index in indexes:
            labels[index].append(junction.kind)
    return retained, labels


def _wall_collision_metrics(
    walls: list[WallRecord],
) -> tuple[int, float]:
    """Measure true plan-solid overlaps, excluding boundary-only contact."""
    centerlines = [LineString((wall.start_m, wall.end_m)) for wall in walls]
    footprints = [
        centerline.buffer(
            wall.thickness_m / 2.0, cap_style=2, join_style=2,
        )
        for centerline, wall in zip(centerlines, walls)
    ]
    count = 0
    maximum = 0.0
    for index, first in enumerate(footprints):
        for second_index in range(index + 1, len(footprints)):
            second = footprints[second_index]
            area = float(first.intersection(second).area)
            if area <= _WALL_COLLISION_AREA_EPS_M2:
                continue
            # Solid overlap at a genuine non-parallel centerline intersection
            # is the physical L/T/Z junction that Revit must join.  Collision
            # auditing is reserved for parallel duplicate runs; those are
            # removed before this metric is evaluated.
            if _line_angle_delta(
                centerlines[index], centerlines[second_index]
            ) > math.radians(2.0) and not centerlines[index].intersection(
                centerlines[second_index]
            ).is_empty:
                continue
            count += 1
            maximum = max(maximum, area)
    return count, maximum


def _line_angle_delta(left: LineString, right: LineString) -> float:
    """Return the acute angle between two centerlines in radians."""

    left_start, left_end = left.coords[0], left.coords[-1]
    right_start, right_end = right.coords[0], right.coords[-1]
    left_angle = math.atan2(left_end[1] - left_start[1], left_end[0] - left_start[0])
    right_angle = math.atan2(right_end[1] - right_start[1], right_end[0] - right_start[0])
    delta = abs((left_angle - right_angle) % math.pi)
    return min(delta, math.pi - delta)


def _parallel_wall_angle_delta(left: CandidateWall, right: CandidateWall) -> float:
    """Return the acute angle between two wall centerlines in radians."""

    return _line_angle_delta(
        LineString((left.start, left.end)), LineString((right.start, right.end))
    )


def _remove_parallel_wall_overlaps(
    walls: list[CandidateWall],
    *,
    allow_cross_frame: bool = False,
) -> tuple[list[CandidateWall], int]:
    """Remove duplicate parallel wall solids before assigning model IDs.

    Two non-parallel walls are expected to overlap at an L/T/Z junction and
    are left intact for native Revit joining.  Parallel solids with a real
    area overlap cannot represent two physical wall runs; they are usually a
    paired-face fragment plus a low-confidence single-line recovery.  Keep
    the candidate with stronger confidence, more source evidence and then a
    deterministic identity tie-breaker.
    """

    active = list(walls)
    removed = 0
    index = 0
    while index < len(active):
        current = active[index]
        current_line = LineString((current.start, current.end))
        current_footprint = current_line.buffer(
            current.thickness_m / 2.0, cap_style=2, join_style=2,
        )
        duplicate_index = None
        for other_index in range(index + 1, len(active)):
            other = active[other_index]
            # Identical local coordinates on two unregistered sheets are not
            # duplicate physical walls.  Preserve both so the frame registry
            # can report the missing registration instead of silently deleting
            # one source's evidence.
            if not allow_cross_frame and _candidate_frame_ids(
                current.source_refs
            ).isdisjoint(_candidate_frame_ids(other.source_refs)):
                continue
            if _parallel_wall_angle_delta(current, other) > math.radians(2.0):
                continue
            other_footprint = LineString((other.start, other.end)).buffer(
                other.thickness_m / 2.0, cap_style=2, join_style=2,
            )
            if current_footprint.intersection(other_footprint).area <= 1.0e-7:
                continue
            current_identity = "|".join(sorted(ref.locator for ref in current.source_refs))
            other_identity = "|".join(sorted(ref.locator for ref in other.source_refs))
            current_score = (
                float(current.confidence), len(current.source_refs),
                current_line.length, current_identity,
            )
            other_score = (
                float(other.confidence), len(other.source_refs),
                LineString((other.start, other.end)).length, other_identity,
            )
            duplicate_index = other_index if current_score >= other_score else index
            break
        if duplicate_index is None:
            index += 1
            continue
        active.pop(duplicate_index)
        removed += 1
        if duplicate_index == index:
            # Re-evaluate the replacement now occupying this position.
            continue
    return active, removed


def _column_footprint(column: ColumnRecord) -> Polygon:
    """Return a column's authoritative plan footprint in project metres."""

    center_x, center_y = column.center_m
    polygon = Polygon([
        (center_x + float(point[0]), center_y + float(point[1]))
        for point in column.profile_m
    ])
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty or polygon.geom_type != "Polygon":
        raise ValueError("column footprint is not a valid polygon: " + column.column_id)
    return polygon


def _geometry_coordinates(geometry) -> list[tuple[float, float]]:
    """Collect coordinates from a Shapely intersection without assumptions about type."""

    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        coordinates = list(geometry.exterior.coords)
        coordinates.extend(
            coordinate
            for ring in geometry.interiors
            for coordinate in ring.coords
        )
        return [(float(x), float(y)) for x, y in coordinates]
    if hasattr(geometry, "geoms"):
        result: list[tuple[float, float]] = []
        for item in geometry.geoms:
            result.extend(_geometry_coordinates(item))
        return result
    if hasattr(geometry, "coords"):
        return [
            (float(point[0]), float(point[1]))
            for point in geometry.coords
        ]
    return []


def _resolve_wall_column_collisions(
    walls: list[CandidateWall],
    columns: list[ColumnRecord],
    *,
    min_length_m: float,
) -> tuple[list[CandidateWall], dict[str, int]]:
    """Give overlapping plan volume to columns and split the wall around it.

    A structural column may be drawn on top of a wall face or directly inside
    a wall run.  Keeping both source solids in Revit creates a real
    interference even when the drawing intentionally shows one continuous
    reinforced-concrete shape.  The deterministic ownership rule is that the
    column keeps its complete source polygon and the wall is shortened/split
    only across the one-dimensional intervals where its finite footprint
    intersects that polygon.  No coordinate is inferred from labels or OCR.
    """

    if not columns:
        return walls, {
            "wall_column_conflict_wall_count": 0,
            "wall_column_split_wall_count": 0,
            "wall_column_removed_wall_count": 0,
        }
    column_polygons = [_column_footprint(column) for column in columns]
    resolved: list[CandidateWall] = []
    conflict_wall_count = 0
    split_wall_count = 0
    removed_wall_count = 0
    # A micrometre pad only absorbs floating-point noise at the ownership
    # boundary; it is far below the source/revit audit tolerance.
    boundary_pad_m = 1.0e-6
    area_tolerance_m2 = 1.0e-7

    for wall in walls:
        start = np.asarray(wall.start, dtype=float)
        end = np.asarray(wall.end, dtype=float)
        vector = end - start
        length = float(np.linalg.norm(vector))
        if length <= 1.0e-9:
            continue
        unit = vector / length
        line = LineString((wall.start, wall.end))
        footprint = line.buffer(
            wall.thickness_m / 2.0, cap_style=2, join_style=2,
        )
        intervals: list[tuple[float, float]] = []
        for polygon in column_polygons:
            intersection = footprint.intersection(polygon)
            if float(intersection.area) <= area_tolerance_m2:
                continue
            points = _geometry_coordinates(intersection)
            if not points:
                representative = intersection.representative_point()
                points = [(float(representative.x), float(representative.y))]
            projections = [
                float(line.project(Point(point)))
                for point in points
            ]
            lower = max(0.0, min(projections) - boundary_pad_m)
            upper = min(length, max(projections) + boundary_pad_m)
            if upper - lower > boundary_pad_m:
                intervals.append((lower, upper))
        if not intervals:
            resolved.append(wall)
            continue

        conflict_wall_count += 1
        intervals.sort()
        merged: list[list[float]] = []
        for lower, upper in intervals:
            if not merged or lower > merged[-1][1] + boundary_pad_m:
                merged.append([lower, upper])
            else:
                merged[-1][1] = max(merged[-1][1], upper)
        pieces: list[tuple[float, float]] = []
        cursor = 0.0
        for lower, upper in merged:
            if lower - cursor >= min_length_m:
                pieces.append((cursor, lower))
            cursor = max(cursor, upper)
        if length - cursor >= min_length_m:
            pieces.append((cursor, length))

        if not pieces:
            removed_wall_count += 1
            continue
        if len(pieces) > 1:
            split_wall_count += 1
        for lower, upper in pieces:
            piece_start = start + unit * lower
            piece_end = start + unit * upper
            resolved.append(CandidateWall(
                start=(float(piece_start[0]), float(piece_start[1])),
                end=(float(piece_end[0]), float(piece_end[1])),
                thickness_m=wall.thickness_m,
                evidence_ids=list(wall.evidence_ids),
                source_refs=list(wall.source_refs),
                confidence=wall.confidence,
            ))
    return resolved, {
        "wall_column_conflict_wall_count": conflict_wall_count,
        "wall_column_split_wall_count": split_wall_count,
        "wall_column_removed_wall_count": removed_wall_count,
    }


def _wall_column_collision_metrics(
    walls: list[WallRecord], columns: list[ColumnRecord],
) -> tuple[int, float]:
    """Measure residual wall/column plan-solid overlap after ownership resolution."""

    column_polygons = [_column_footprint(column) for column in columns]
    count = 0
    maximum = 0.0
    for wall in walls:
        wall_shape = LineString((wall.start_m, wall.end_m)).buffer(
            wall.thickness_m / 2.0, cap_style=2, join_style=2,
        )
        for column_shape in column_polygons:
            area = float(wall_shape.intersection(column_shape).area)
            if area <= 1.0e-7:
                continue
            count += 1
            maximum = max(maximum, area)
    return count, maximum


def _unique_directions(vectors: list[np.ndarray], tolerance_deg: float = 8.0) -> list[np.ndarray]:
    result: list[np.ndarray] = []
    for vector in vectors:
        unit = vector / np.linalg.norm(vector)
        if not any(math.degrees(math.acos(float(np.clip(np.dot(unit, item), -1, 1))))
                   <= tolerance_deg for item in result):
            result.append(unit)
    return result


def _classify_topology(
    walls: list[CandidateWall],
    tolerance: float,
    *,
    allow_cross_frame: bool = False,
) -> tuple[list[TopologyJunction], dict[int, list[str]]]:
    lines = [LineString((wall.start, wall.end)) for wall in walls]
    # Keep the frame ownership of every candidate point.  Clustering only by
    # distance is unsafe for multi-page drawings because each page commonly
    # starts at (0, 0); the same local coordinate is not evidence of a join.
    point_records: list[tuple[Point, set[str]]] = [
        (Point(point), _candidate_frame_ids(wall.source_refs))
        for wall in walls
        for point in (wall.start, wall.end)
    ]
    for first_index, first in enumerate(lines):
        for second_index, second in enumerate(lines[first_index + 1:], start=first_index + 1):
            if not _frames_can_interact(
                walls[first_index], walls[second_index],
                allow_cross_frame=allow_cross_frame,
            ):
                continue
            intersection = first.intersection(second)
            if intersection.geom_type == "Point":
                point_records.append((
                    intersection,
                    _candidate_frame_ids(walls[first_index].source_refs).union(
                        _candidate_frame_ids(walls[second_index].source_refs)
                    ),
                ))
    clusters: list[tuple[Point, set[str]]] = []
    for point, frames in point_records:
        match = None
        for index, (existing, existing_frames) in enumerate(clusters):
            if point.distance(existing) > tolerance / 2:
                continue
            if not allow_cross_frame and not frames.intersection(existing_frames):
                continue
            match = index
            break
        if match is None:
            clusters.append((point, set(frames)))
        else:
            existing, existing_frames = clusters[match]
            # Preserve the first point for deterministic output while
            # retaining all frame owners represented by the cluster.
            existing_frames.update(frames)
    junctions: list[TopologyJunction] = []
    wall_labels: dict[int, list[str]] = {index: [] for index in range(len(walls))}
    junction_frame_sets: list[set[str]] = []
    for point, point_frames in clusters:
        member_ids = [index for index, line in enumerate(lines)
                      if line.distance(point) <= tolerance and _frames_can_interact(
                          walls[index], point_frames,
                          allow_cross_frame=allow_cross_frame,
                      )]
        if len(member_ids) < 2:
            continue
        vectors = []
        for index in member_ids:
            wall = walls[index]
            start, end = np.asarray(wall.start), np.asarray(wall.end)
            current = np.asarray((point.x, point.y))
            if np.linalg.norm(start - current) <= tolerance:
                vectors.append(end - current)
            elif np.linalg.norm(end - current) <= tolerance:
                vectors.append(start - current)
            else:
                vectors.extend((start - current, end - current))
        directions = _unique_directions([
            vector for vector in vectors if np.linalg.norm(vector) > 1e-9
        ])
        count = len(directions)
        if count >= 4:
            kind = "X"
        elif count == 3:
            kind = "T"
        elif count == 2:
            dot = float(np.dot(directions[0], directions[1]))
            kind = "straight" if dot < -0.95 else "L"
        else:
            continue
        wall_ids = [f"wall_{index + 1:04d}" for index in member_ids]
        junction = TopologyJunction(
            junction_id=f"junction_{len(junctions) + 1:04d}", kind=kind,
            point_m=(round(point.x, 6), round(point.y, 6)), wall_ids=wall_ids,
        )
        junctions.append(junction)
        junction_frame_sets.append(
            set().union(*(
                _candidate_frame_ids(walls[index].source_refs)
                for index in member_ids
            ))
        )
        for index in member_ids:
            wall_labels[index].append(kind)

    # A Z is a central wall whose two ends are L junctions and whose adjacent
    # arms are parallel.  It is recorded in addition to the endpoint L facts.
    for index, wall in enumerate(walls):
        endpoint_junctions: list[tuple[int, TopologyJunction] | None] = []
        for endpoint in (wall.start, wall.end):
            match = next(
                (
                    (junction_index, junction)
                    for junction_index, junction in enumerate(junctions)
                    if junction.kind == "L"
                    and math.dist(endpoint, junction.point_m) <= tolerance
                    and _frames_can_interact(
                        wall,
                        junction_frame_sets[junction_index],
                        allow_cross_frame=allow_cross_frame,
                    )
                ),
                None,
            )
            endpoint_junctions.append(match)
        if all(endpoint_junctions):
            neighbors = []
            own_id = f"wall_{index + 1:04d}"
            for _, junction in endpoint_junctions:
                neighbors.extend(value for value in junction.wall_ids if value != own_id)
            if len(neighbors) == 2:
                first = walls[int(neighbors[0].split("_")[-1]) - 1]
                second = walls[int(neighbors[1].split("_")[-1]) - 1]
                first_u = np.asarray(first.end) - np.asarray(first.start)
                second_u = np.asarray(second.end) - np.asarray(second.start)
                first_u, second_u = first_u / np.linalg.norm(first_u), second_u / np.linalg.norm(second_u)
                if _angle_delta(first_u, second_u) <= 8.0:
                    midpoint = ((wall.start[0] + wall.end[0]) / 2,
                                (wall.start[1] + wall.end[1]) / 2)
                    junctions.append(TopologyJunction(
                        junction_id=f"junction_{len(junctions) + 1:04d}", kind="Z",
                        point_m=(round(midpoint[0], 6), round(midpoint[1], 6)),
                        wall_ids=[own_id, *neighbors],
                    ))
                    wall_labels[index].append("Z")
    return junctions, wall_labels


def _transform_grid(
    config: WallPipelineConfig, evidence: WallEvidence,
    manifest: SourceManifest, source_entities: SourceEntities,
) -> list[GridAxis]:
    scales, rotation, effective_coordinate = _column_transform_parameters(
        config, manifest, source_entities, evidence
    )
    if not config.grid.axes:
        if not config.grid.include_layers:
            return []
        segments = _scoped_segments(
            source_entities.entities,
            config.grid.include_layers,
            config.grid.exclude_layers,
        )
        vertical, horizontal = _grid_axis_candidates(
            segments,
            scales,
            effective_coordinate,
            rotation,
            min_length_m=config.grid.min_length_m,
            axis_tolerance_deg=config.grid.axis_tolerance_deg,
            source_type=config.source.type,
        )

        def axes_from_groups(
            candidates: list[
                tuple[float, tuple[float, float], tuple[float, float], Segment]
            ],
            *,
            axis: str,
        ) -> list[GridAxis]:
            result: list[GridAxis] = []
            groups = _cluster_grid_candidates(
                candidates, config.grid.cluster_tolerance_m
            )
            for index, group in enumerate(groups):
                coordinate = statistics.fmean(item[0] for item in group)
                representative = max(
                    group, key=lambda item: math.dist(item[1], item[2])
                )
                segment = representative[3]
                if axis == "X":
                    start = (coordinate, min(min(item[1][1], item[2][1]) for item in group))
                    end = (coordinate, max(max(item[1][1], item[2][1]) for item in group))
                else:
                    start = (min(min(item[1][0], item[2][0]) for item in group), coordinate)
                    end = (max(max(item[1][0], item[2][0]) for item in group), coordinate)
                result.append(GridAxis(
                    axis_id=f"grid_{axis.lower()}_{index + 1:04d}",
                    label=f"{axis}-{index + 1:02d}",
                    start_m=(round(start[0], 6), round(start[1], 6)),
                    end_m=(round(end[0], 6), round(end[1], 6)),
                    source_file_id=segment.entity.source_file_id,
                    frame_id=_entity_frame_id(segment.entity, config.source.type),
                ))
            return result

        return axes_from_groups(vertical, axis="X") + axes_from_groups(
            horizontal, axis="Y"
        )

    known_frames, frame_owners = _validate_frame_registry(
        config, manifest, source_entities
    )
    default_source_id = manifest.sources[0].source_file_id
    source_ids = {source.source_file_id for source in manifest.sources}
    # Adapters expose the exact local frame on each entity.  Keeping this
    # reverse map lets a page frame (``source_0001:page:0002``) reuse the
    # source file's unit while still receiving its own registered offset.
    frame_sources = dict(frame_owners)
    result = []
    for index, axis in enumerate(config.grid.axes):
        frame_id = axis.frame_id or axis.source_file_id or default_source_id
        if frame_id not in known_frames:
            field = "frame_id" if axis.frame_id else "source_file_id"
            raise ValueError("grid axis references unknown " + field + ": " + frame_id)
        mapped_source = frame_sources.get(frame_id)
        if axis.source_file_id and mapped_source and mapped_source != axis.source_file_id:
            raise ValueError(
                "grid axis frame and source_file_id disagree: " + frame_id
            )
        source_id = axis.source_file_id or mapped_source
        if source_id is None:
            # A frame may be registered by an explicit offset before any
            # entity is emitted (for example an empty/supplementary page).
            # Infer its owning source only from an unambiguous source prefix.
            source_id = frame_sources.get(frame_id)
        if source_id not in source_ids:
            raise ValueError(
                "grid axis frame has no known source_file_id: " + frame_id
            )
        scale = scales[source_id]
        result.append(GridAxis(
            axis_id=f"grid_{index + 1:04d}", label=axis.label,
            start_m=_transform_point(
            axis.start, scale, effective_coordinate, rotation, frame_id=frame_id,
            source_file_id=source_id,
            ),
            end_m=_transform_point(
            axis.end, scale, effective_coordinate, rotation, frame_id=frame_id,
            source_file_id=source_id,
            ),
            source_file_id=axis.source_file_id,
            frame_id=axis.frame_id,
        ))
    return result


def _column_transform_parameters(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    source_entities: SourceEntities,
    evidence: WallEvidence,
) -> tuple[dict[str, float], float, CoordinateConfig]:
    """Resolve the exact transform already recorded by wall evidence."""

    unit_operation = next(
        (item for item in evidence.transform_chain
         if item.get("operation") == "unit_convert"),
        {},
    )
    scales = {
        str(source_id): float(scale)
        for source_id, scale in (unit_operation.get("scale_to_m_by_source") or {}).items()
    }
    rotation_operation = next(
        (item for item in evidence.transform_chain
         if item.get("operation") == "rotate"),
        {},
    )
    rotation = float(rotation_operation.get("rotation_deg") or 0.0)
    # Hand-authored evidence fixtures may not carry the transform operation;
    # resolve the same deterministic policy used by wall segments instead of
    # silently treating their coordinates as metres.
    if not scales:
        for source in manifest.sources:
            inferred_scale = (
                _infer_pdf_scale(source_entities, source.source_file_id)
                if config.source.type == "pdf"
                and config.coordinate.scale_to_m is None
                and not config.coordinate.calibration
                else None
            )
            scales[source.source_file_id] = _scale_to_m(
                config.coordinate,
                source_entities.source_units,
                source.source_file_id,
                source_type=config.source.type,
                inferred_pdf_scale_to_m=(
                    inferred_scale.get("scale_to_m")
                    if inferred_scale is not None else None
                ),
            )
    translation_operation = next(
        (item for item in evidence.transform_chain
         if item.get("operation") == "translate"),
        {},
    )
    translation = translation_operation.get("translation_m")
    coordinate = config.coordinate
    if isinstance(translation, (list, tuple)) and len(translation) == 2:
        coordinate = coordinate.model_copy(update={
            "translation_m": (float(translation[0]), float(translation[1])),
        })
    return scales, rotation, coordinate


def _opening_source_ref(entity: SourceEntity) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_file_id=entity.source_file_id,
        entity_id=entity.entity_id,
        page_no=entity.page_no,
        locator=entity.locator,
        layer=entity.layer,
        frame_id=entity.frame_id or entity.source_file_id,
    )


def _opening_entity_points(entity: SourceEntity) -> list[tuple[float, float]]:
    geometry = entity.geometry
    if "start" in geometry and "end" in geometry:
        values = [geometry["start"], geometry["end"]]
    else:
        values = geometry.get("points") or []
    points: list[tuple[float, float]] = []
    for value in values:
        try:
            point = (float(value[0]), float(value[1]))
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(item) for item in point):
            continue
        if not points or math.dist(points[-1], point) > 1.0e-9:
            points.append(point)
    if len(points) > 1 and math.dist(points[0], points[-1]) <= 1.0e-9:
        points.pop()
    return points


def _opening_entity_polygon(
    entity: SourceEntity,
    *,
    scales: dict[str, float],
    coordinate: CoordinateConfig,
    rotation: float,
    source_type: str,
) -> Polygon | None:
    raw_points = entity.geometry.get("points") or []
    # A Shapely Polygon closes an open polyline implicitly.  That behaviour
    # is unsafe here: a nearby leader or annotation could be turned into a
    # fabricated opening rectangle.  Accept an explicit closed flag, or the
    # CAD-export pattern where the first vertex is repeated at the end.
    explicitly_closed = bool(entity.geometry.get("closed"))
    repeated_endpoint = len(raw_points) >= 3
    if repeated_endpoint:
        try:
            repeated_endpoint = math.dist(
                (float(raw_points[0][0]), float(raw_points[0][1])),
                (float(raw_points[-1][0]), float(raw_points[-1][1])),
            ) <= 1.0e-6
        except (IndexError, KeyError, TypeError, ValueError):
            repeated_endpoint = False
    if not explicitly_closed and not repeated_endpoint:
        return None
    points = _opening_entity_points(entity)
    if len(points) < 3:
        return None
    transformed = [
        _transform_point(
            point,
            scales[entity.source_file_id],
            coordinate,
            rotation,
            frame_id=_entity_frame_id(entity, source_type),
            source_file_id=entity.source_file_id,
        )
        for point in points
    ]
    polygon = Polygon(transformed)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty or polygon.geom_type != "Polygon" or polygon.area <= 1.0e-9:
        return None
    return polygon


def _transformed_opening_line_polygons(
    entities: list[SourceEntity],
    *,
    boundary_layers: list[str],
    scales: dict[str, float],
    coordinate: CoordinateConfig,
    rotation: float,
    source_type: str,
) -> list[tuple[SourceEntity, Polygon]]:
    """Recover closed opening markers flattened into PDF LINE records.

    Only lines from one source drawing are polygonized together.  This is
    important for ``S-WALL-HO-TEXT`` where a whole sheet contains thousands
    of independent annotation strokes; polygonizing the complete layer would
    join unrelated details and fabricate openings.
    """

    grouped: dict[tuple[str, str, str, object], list[tuple[SourceEntity, LineString]]] = {}
    for entity in entities:
        if entity.kind not in {"line", "mline"} or not _matches_layer(
            entity.layer, boundary_layers, []
        ):
            continue
        geometry = entity.geometry
        if "start" not in geometry or "end" not in geometry:
            continue
        scale = scales.get(entity.source_file_id)
        if scale is None:
            continue
        try:
            start = _transform_point(
                (float(geometry["start"][0]), float(geometry["start"][1])),
                scale, coordinate, rotation,
                frame_id=_entity_frame_id(entity, source_type),
                source_file_id=entity.source_file_id,
            )
            end = _transform_point(
                (float(geometry["end"][0]), float(geometry["end"][1])),
                scale, coordinate, rotation,
                frame_id=_entity_frame_id(entity, source_type),
                source_file_id=entity.source_file_id,
            )
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        line = LineString((start, end))
        if line.length <= _MIN_COLUMN_PROFILE_EDGE_M:
            continue
        key = (
            entity.source_file_id,
            _entity_frame_id(entity, source_type),
            entity.layer,
            entity.style.get("drawing_index", ""),
        )
        grouped.setdefault(key, []).append((entity, line))

    result: list[tuple[SourceEntity, Polygon]] = []
    for entries in grouped.values():
        if len(entries) < 4:
            continue
        try:
            graph = unary_union([line for _, line in entries])
            polygons = list(polygonize(graph))
        except Exception:
            continue
        for polygon in polygons:
            if polygon.is_empty or polygon.area <= 1.0e-9:
                continue
            # A drawing group can contain several text-outline loops.  Keep
            # only the bounded face represented by the line group; dimension
            # limits are checked by the caller against the configured range.
            result.append((entries[0][0], polygon))
    return result


def _opening_record_refs(
    boundary: SourceEntity | None,
    label: SourceEntity,
) -> list[EvidenceSourceRef]:
    refs: list[EvidenceSourceRef] = []
    for entity in (boundary, label):
        if entity is None:
            continue
        ref = _opening_source_ref(entity)
        if (ref.source_file_id, ref.entity_id, ref.locator) not in {
            (item.source_file_id, item.entity_id, item.locator) for item in refs
        }:
            refs.append(ref)
    return refs


def _detect_openings(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    source_entities: SourceEntities,
    evidence: WallEvidence,
    walls: list[CandidateWall],
) -> list[OpeningRecord]:
    """Pair configured opening labels with nearby vector geometry and walls.

    This is intentionally conservative: a label is only promoted when it has
    a same-frame vector marker.  The label can classify a shear-wall opening,
    but it never supplies the marker coordinates or extends a wall by itself.
    """

    opening_config = config.opening
    if not opening_config.label_layers:
        return []
    scales, rotation, coordinate = _column_transform_parameters(
        config, manifest, source_entities, evidence
    )
    try:
        label_re = re.compile(opening_config.label_pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid opening label pattern: {exc}") from exc
    labels: list[tuple[SourceEntity, str, Point]] = []
    for entity in source_entities.entities:
        if entity.kind != "text" or not _semantic_text_matches(
            entity,
            opening_config.label_layers,
            opening_config.exclude_layers,
            source_type=config.source.type,
        ):
            continue
        match = label_re.search(str(entity.geometry.get("text") or ""))
        point = entity.geometry.get("point")
        scale = scales.get(entity.source_file_id)
        if match is None or not point or scale is None:
            continue
        transformed = _transform_point(
            (float(point[0]), float(point[1])),
            scale,
            coordinate,
            rotation,
            frame_id=_entity_frame_id(entity, config.source.type),
            source_file_id=entity.source_file_id,
        )
        labels.append((entity, re.sub(r"\s+", "", match.group(0)).upper(), Point(transformed)))
    if not labels:
        return []

    boundaries: list[tuple[SourceEntity, Polygon]] = []
    for entity in source_entities.entities:
        if not opening_config.boundary_layers or entity.kind not in {
            "line", "polyline", "hatch_boundary", "mline"
        } or not _matches_layer(
            entity.layer, opening_config.boundary_layers, opening_config.exclude_layers
        ):
            continue
        polygon = _opening_entity_polygon(
            entity,
            scales=scales,
            coordinate=coordinate,
            rotation=rotation,
            source_type=config.source.type,
        )
        if polygon is None:
            continue
        rectangle = polygon.minimum_rotated_rectangle
        rectangle_points = list(rectangle.exterior.coords)[:-1]
        lengths = [
            math.dist(
                rectangle_points[index],
                rectangle_points[(index + 1) % len(rectangle_points)],
            )
            for index in range(len(rectangle_points))
        ]
        width, depth = max(lengths), min(lengths)
        if not (
            opening_config.min_width_m <= width <= opening_config.max_width_m
            and opening_config.min_depth_m <= depth <= opening_config.max_depth_m
        ):
            continue
        boundaries.append((entity, polygon))

    # PDF exports commonly flatten a closed opening marker into LINE records
    # on the configured boundary layer.  Recover those local drawing groups
    # without treating an open leader or a text glyph as a void boundary.
    for representative, polygon in _transformed_opening_line_polygons(
        source_entities.entities,
        boundary_layers=opening_config.boundary_layers,
        scales=scales,
        coordinate=coordinate,
        rotation=rotation,
        source_type=config.source.type,
    ):
        rectangle = polygon.minimum_rotated_rectangle
        rectangle_points = list(rectangle.exterior.coords)[:-1]
        if len(rectangle_points) < 4:
            continue
        lengths = [
            math.dist(
                rectangle_points[index],
                rectangle_points[(index + 1) % len(rectangle_points)],
            )
            for index in range(len(rectangle_points))
        ]
        width, depth = max(lengths), min(lengths)
        if not (
            opening_config.min_width_m <= width <= opening_config.max_width_m
            and opening_config.min_depth_m <= depth <= opening_config.max_depth_m
        ):
            continue
        boundaries.append((representative, polygon))

    records: list[OpeningRecord] = []
    used_boundaries: set[int] = set()
    for label_entity, mark, label_point in labels:
        label_frame = _entity_frame_id(label_entity, config.source.type)
        choices: list[tuple[float, int, SourceEntity, Polygon]] = []
        for boundary_index, (boundary_entity, polygon) in enumerate(boundaries):
            if (
                boundary_entity.source_file_id != label_entity.source_file_id
                or _entity_frame_id(boundary_entity, config.source.type) != label_frame
            ):
                continue
            distance = float(label_point.distance(polygon))
            if distance <= opening_config.association_distance_m:
                choices.append((distance, boundary_index, boundary_entity, polygon))
        choices.sort(key=lambda item: (item[0], item[1]))
        selected: tuple[float, int, SourceEntity, Polygon] | None = None
        for choice in choices:
            if choice[1] not in used_boundaries:
                selected = choice
                used_boundaries.add(choice[1])
                break
        boundary_entity = selected[2] if selected else None
        polygon = selected[3] if selected else None
        if polygon is not None:
            rectangle_points = list(polygon.minimum_rotated_rectangle.exterior.coords)[:-1]
            boundary_points = [
                (round(float(point[0]), 6), round(float(point[1]), 6))
                for point in rectangle_points
            ]
            center = polygon.centroid
            lengths = [
                math.dist(
                    rectangle_points[index],
                    rectangle_points[(index + 1) % len(rectangle_points)],
                )
                for index in range(len(rectangle_points))
            ]
            width, depth = max(lengths), min(lengths)
            marker_point = Point(center)
        else:
            marker_point = label_point
            boundary_points = []
            width = depth = 0.0

        host_choices: list[tuple[float, int]] = []
        for wall_index, wall in enumerate(walls):
            if not _frames_can_interact(
                wall,
                {_entity_frame_id(ref, config.source.type) for ref in (
                    [boundary_entity, label_entity] if boundary_entity else [label_entity]
                ) if ref is not None},
                allow_cross_frame=False,
            ):
                continue
            wall_line = LineString((wall.start, wall.end))
            distance = float(
                wall_line.distance(marker_point if polygon is None else polygon)
            )
            if distance <= opening_config.association_distance_m:
                host_choices.append((distance, wall_index))
        host_choices.sort(key=lambda item: (item[0], item[1]))
        host_wall_ids = [
            f"wall_{host_choices[0][1] + 1:04d}"
        ] if host_choices else []
        status = "matched" if boundary_entity is not None and host_wall_ids else "review_required"
        limitations: list[str] = []
        if boundary_entity is None:
            limitations.append("opening label has no same-frame closed vector boundary")
        if not host_wall_ids:
            limitations.append("opening marker is not associated with a modeled wall host")
        identity = "|".join(
            sorted(ref.locator for ref in _opening_record_refs(boundary_entity, label_entity))
        )
        opening_id = "opening_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        confidence = 0.95 if status == "matched" else 0.55
        records.append(OpeningRecord(
            opening_id=opening_id,
            mark=mark,
            type_name=(
                f"{mark}-{_format_spec_mm(width * 1000)}x"
                f"{_format_spec_mm(depth * 1000)}mm"
            ),
            center_m=(round(float(marker_point.x), 6), round(float(marker_point.y), 6)),
            boundary_m=boundary_points,
            width_m=round(float(width), 6),
            depth_m=round(float(depth), 6),
            host_wall_ids=host_wall_ids,
            source_refs=_opening_record_refs(boundary_entity, label_entity),
            confidence=confidence,
            status=status,
            limitations=limitations,
        ))
    return sorted(records, key=lambda item: item.opening_id)


def _column_points(entity: SourceEntity) -> list[tuple[float, float]]:
    points = entity.geometry.get("points") or []
    result: list[tuple[float, float]] = []
    for point in points:
        if len(point) < 2:
            continue
        value = (float(point[0]), float(point[1]))
        if not result or math.dist(result[-1], value) > 1.0e-9:
            result.append(value)
    if len(result) > 1 and math.dist(result[0], result[-1]) <= 1.0e-9:
        result.pop()
    return result


def _clean_column_polygon(
    points: list[tuple[float, float]],
) -> Polygon | None:
    """Return a Revit-safe closed profile without inventing geometry."""

    cleaned: list[tuple[float, float]] = []
    for point in points:
        value = (float(point[0]), float(point[1]))
        if cleaned and math.dist(cleaned[-1], value) < _MIN_COLUMN_PROFILE_EDGE_M:
            continue
        # Some HATCH EdgePaths repeat the first loop and then append one or
        # two source edges again.  The first valid return to the origin is the
        # authoritative closure; the trailing repeat is not a second shape.
        if (
            len(cleaned) >= 3
            and math.dist(cleaned[0], value) < _MIN_COLUMN_PROFILE_EDGE_M
        ):
            break
        cleaned.append(value)
    while (
        len(cleaned) >= 4
        and math.dist(cleaned[0], cleaned[-1]) < _MIN_COLUMN_PROFILE_EDGE_M
    ):
        cleaned.pop()
    if len(cleaned) < 3:
        return None
    polygon = Polygon(cleaned)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty or polygon.geom_type != "Polygon":
        return None
    ring = [(float(x), float(y)) for x, y in list(polygon.exterior.coords)[:-1]]
    compact: list[tuple[float, float]] = []
    for point in ring:
        if not compact or math.dist(compact[-1], point) >= _MIN_COLUMN_PROFILE_EDGE_M:
            compact.append(point)
    if (
        len(compact) >= 4
        and math.dist(compact[0], compact[-1]) < _MIN_COLUMN_PROFILE_EDGE_M
    ):
        compact.pop()
    if len(compact) < 3 or any(
        math.dist(compact[index], compact[(index + 1) % len(compact)])
        < _MIN_COLUMN_PROFILE_EDGE_M
        for index in range(len(compact))
    ):
        return None
    result = Polygon(compact)
    return result if result.is_valid and result.area > 1.0e-8 else None


def _column_record(
    *,
    config: WallPipelineConfig,
    polygon: Polygon,
    source_refs: list[EvidenceSourceRef],
    identity: str,
    confidence: float,
    profile_kind: str = "rectangular",
    type_mark: str | None = None,
) -> ColumnRecord:
    rectangle = polygon.minimum_rotated_rectangle
    rectangle_points = list(rectangle.exterior.coords)[:-1]
    edge_lengths = [
        math.dist(
            rectangle_points[index],
            rectangle_points[(index + 1) % len(rectangle_points)],
        )
        for index in range(len(rectangle_points))
    ]
    width_m, depth_m = max(edge_lengths), min(edge_lengths)
    center = polygon.centroid
    profile_points = list(polygon.exterior.coords)[:-1]
    profile_m = [
        (round(float(point[0] - center.x), 6), round(float(point[1] - center.y), 6))
        for point in profile_points
    ]
    area = float(polygon.area)
    height = float(config.level.wall_height_m)
    mark = re.sub(r"\s+", "", type_mark or "").upper() or None
    standard = config.modeling_standard
    schedule_mark = mark or (
        f"{standard.naming_prefix}-C-{_format_spec_mm(width_m * 1000)}x{_format_spec_mm(depth_m * 1000)}"
        if profile_kind == "rectangular"
        else f"{standard.naming_prefix}-C-IR-{len(profile_m)}V"
    )
    type_name = (
        f"{standard.column_type_prefix}-{mark}"
        if mark
        else (
            f"{standard.column_type_prefix}-{_format_spec_mm(width_m * 1000)}x{_format_spec_mm(depth_m * 1000)}mm"
            if profile_kind == "rectangular"
            else f"{standard.column_type_prefix}-IR-{len(profile_m)}V"
        )
    )
    material = config.column.material_name
    return ColumnRecord(
        column_id="column_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        center_m=(round(float(center.x), 6), round(float(center.y), 6)),
        profile_m=profile_m,
        width_m=round(float(width_m), 6),
        depth_m=round(float(depth_m), 6),
        height_m=height,
        level_id=config.level.id,
        source_refs=source_refs,
        confidence=round(float(confidence), 4),
        profile_kind=profile_kind,
        type_mark=schedule_mark,
        construction=ConstructionInfo(
            category="Column",
            type_name=type_name,
            type_mark=schedule_mark,
            family_name=(
                "混凝土 - 矩形 - 柱"
                if profile_kind == "rectangular"
                else f"BuildMate - 异形结构柱 - {schedule_mark}"
            ),
            family_type=type_name,
            representation=(
                "loadable_family"
                if profile_kind == "rectangular"
                else "profile_directshape"
            ),
            classification_code=config.column.classification_code,
            classification_system=standard.classification_system,
            material_name=material,
            material_status="provided" if material else "unspecified",
        ),
        quantities=QuantityTakeoff(
            footprint_area_m2=round(area, 6),
            cross_section_area_m2=round(area, 6),
            side_area_m2=round(float(polygon.length) * height, 6),
            gross_volume_m3=round(area * height, 6),
        ),
    )


def _transformed_column_polygon(
    entity: SourceEntity,
    *,
    scales: dict[str, float],
    coordinate: CoordinateConfig,
    rotation: float,
    source_type: str,
) -> Polygon | None:
    points = _column_points(entity)
    if len(points) < 4:
        return None
    scale = scales.get(entity.source_file_id)
    if scale is None:
        raise ValueError("column source entity has no unit scale: " + entity.entity_id)
    transformed = [
        _transform_point(
            point,
            scale,
            coordinate,
            rotation,
            frame_id=_entity_frame_id(entity, source_type),
            source_file_id=entity.source_file_id,
        )
        for point in points
    ]
    return _clean_column_polygon(transformed)


def _transformed_column_profile_lines(
    entities: list[SourceEntity],
    *,
    profile_layers: list[str],
    scales: dict[str, float],
    coordinate: CoordinateConfig,
    rotation: float,
    source_type: str,
) -> list[tuple[list[SourceEntity], Polygon]]:
    """Polygonize line-only column profile layers into traceable profiles.

    PDF exports frequently flatten a closed irregular-column outline into
    independent ``LINE`` records, while DXF exports provide one closed
    polyline or HATCH boundary.  Treating the PDF lines as wall evidence
    loses the profile before Revit ever sees it.  Polygonization is performed
    per source frame/layer and the resulting polygon retains every boundary
    line as provenance; no text or visual classification supplies coordinates.
    """

    grouped: dict[tuple[str, str, str, object], list[tuple[SourceEntity, LineString]]] = {}
    # A PDF producer may emit every edge as its own drawing path, assigning a
    # different drawing_index to each line.  Keep the index-isolated groups
    # for normal exports, but collect singleton paths by source frame/layer so
    # we can recover a closed profile from endpoint-connected edges below.
    singleton_groups: dict[tuple[str, str, str], list[tuple[SourceEntity, LineString]]] = {}
    for entity in entities:
        if entity.kind not in {"line", "mline"}:
            continue
        if not _matches_layer(entity.layer, profile_layers, []):
            continue
        points = entity.geometry
        if "start" not in points or "end" not in points:
            continue
        scale = scales.get(entity.source_file_id)
        if scale is None:
            continue
        start = _transform_point(
            (float(points["start"][0]), float(points["start"][1])),
            scale, coordinate, rotation,
            frame_id=_entity_frame_id(entity, source_type),
            source_file_id=entity.source_file_id,
        )
        end = _transform_point(
            (float(points["end"][0]), float(points["end"][1])),
            scale, coordinate, rotation,
            frame_id=_entity_frame_id(entity, source_type),
            source_file_id=entity.source_file_id,
        )
        line = LineString((start, end))
        if line.length >= _MIN_COLUMN_PROFILE_EDGE_M:
            key = (
                entity.source_file_id,
                _entity_frame_id(entity, source_type),
                entity.layer,
                # PDF drawing_index is the only safe boundary for flattened
                # LINE records.  DXF entities do not carry it, so the empty
                # value retains the historical layer-level behaviour there.
                entity.style.get("drawing_index", ""),
            )
            grouped.setdefault(key, []).append((entity, line))

    result: list[tuple[list[SourceEntity], Polygon]] = []
    groups: list[list[tuple[SourceEntity, LineString]]] = []
    for key, entries in grouped.items():
        if len(entries) > 1 or key[3] == "":
            groups.append(entries)
            continue
        singleton_groups.setdefault(key[:3], []).extend(entries)

    # Reconnect only endpoint-touching singleton paths.  This avoids treating
    # unrelated annotation lines on the same sheet as one profile while still
    # handling PDF exports that split a triangle/L/T outline into individual
    # drawing records.  The small tolerance is a source-quantisation margin,
    # not a geometry inference radius.
    for entries in singleton_groups.values():
        parent = list(range(len(entries)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        endpoints = [
            (tuple(line.coords[0]), tuple(line.coords[-1]))
            for _, line in entries
        ]
        for first_index, (first_start, first_end) in enumerate(endpoints):
            for second_index in range(first_index + 1, len(endpoints)):
                second_start, second_end = endpoints[second_index]
                if min(
                    math.dist(first_start, second_start),
                    math.dist(first_start, second_end),
                    math.dist(first_end, second_start),
                    math.dist(first_end, second_end),
                ) <= _MIN_COLUMN_PROFILE_EDGE_M:
                    union(first_index, second_index)
        components: dict[int, list[tuple[SourceEntity, LineString]]] = {}
        for index, entry in enumerate(entries):
            components.setdefault(find(index), []).append(entry)
        groups.extend(
            component for component in components.values() if len(component) >= 3
        )

    for entries in groups:
        # unary_union nodes touching/crossing linework before polygonize.  A
        # direct polygonize(entries) misses loops whose PDF segments meet at
        # numerically equal but separately emitted endpoints.
        try:
            graph = unary_union([line for _, line in entries])
            raw_polygons = list(polygonize(graph))
            # Hatch exports add diagonals across a profile.  The individual
            # polygonize faces are triangles; their union is the closed
            # authoritative profile.
            merged = unary_union(raw_polygons) if raw_polygons else None
            if merged is None or merged.is_empty:
                polygons = []
            elif merged.geom_type == "Polygon":
                polygons = [merged]
            else:
                polygons = [item for item in getattr(merged, "geoms", ())
                            if item.geom_type == "Polygon"]
        except Exception:
            continue
        for polygon in polygons:
            cleaned = _clean_column_polygon([
                (float(x), float(y))
                for x, y in list(polygon.exterior.coords)[:-1]
            ])
            # A triangular or otherwise three-sided profile is a valid
            # irregular structural column.  Requiring four vertices here
            # silently discarded exactly the kind of non-rectangular GBZ/YBZ
            # profile the dedicated scope is meant to preserve.
            if cleaned is None or len(cleaned.exterior.coords) - 1 < 3:
                continue
            boundary = cleaned.boundary
            refs = [
                entity for entity, line in entries
                if line.distance(boundary) <= 1.0e-6
                and line.intersection(boundary).length >= _MIN_COLUMN_PROFILE_EDGE_M
            ]
            if refs:
                result.append((refs, cleaned))
    return result


def _is_rectangular_column_profile(polygon: Polygon) -> bool:
    """Return true for a rectangle, including a rotated rectangle."""

    rectangle = polygon.minimum_rotated_rectangle
    area = float(polygon.area)
    if area <= 1.0e-9:
        return False
    # The symmetric difference is robust to vertex order and tiny PDF
    # quantization while distinguishing L/T/Z profiles from true rectangles.
    return float(polygon.symmetric_difference(rectangle).area) <= max(
        1.0e-6, area * 1.0e-3
    )


def _model_frame_bounds(
    walls: list[CandidateWall], grid: list[GridAxis],
) -> tuple[float, float, float, float] | None:
    """Derive a conservative engineering frame for column acceptance.

    Some CAD exports contain proxy/XREF column blocks with coordinates many
    kilometres away from the drawing frame.  Accepting those blocks makes a
    Revit view effectively empty because ``Zoom To Fit`` has to include the
    outliers.  The authoritative wall/grid frame is deterministic and already
    resolved before columns are built; a fixed relative margin keeps legitimate
    edge columns while rejecting detached export artefacts.
    """

    points = [point for wall in walls for point in (wall.start, wall.end)]
    points.extend(point for axis in grid for point in (axis.start_m, axis.end_m))
    if not points:
        return None
    min_x = min(float(point[0]) for point in points)
    min_y = min(float(point[1]) for point in points)
    max_x = max(float(point[0]) for point in points)
    max_y = max(float(point[1]) for point in points)
    span = max(max_x - min_x, max_y - min_y, 1.0)
    margin = max(1.0, span * 0.20)
    return min_x - margin, min_y - margin, max_x + margin, max_y + margin


def _grid_plan_frame_bounds(
    grid: list[GridAxis],
) -> tuple[float, float, float, float] | None:
    """Return the main plan frame established by an authoritative grid.

    Structural sheets often contain small wall/column details outside the
    floor-plan grid while reusing the same CAD layers as the real plan.  When
    a grid is mandatory, those detached details must not become building
    elements.  Per-axis margins retain exterior walls beyond the outer grid
    without allowing the much wider X span to pull in a remote Y detail.
    """

    vertical = [
        axis for axis in grid
        if abs(float(axis.start_m[0]) - float(axis.end_m[0])) <= 1.0e-6
    ]
    horizontal = [
        axis for axis in grid
        if abs(float(axis.start_m[1]) - float(axis.end_m[1])) <= 1.0e-6
    ]
    if len(vertical) < 2 or len(horizontal) < 2:
        return None
    points = [point for axis in grid for point in (axis.start_m, axis.end_m)]
    min_x = min(float(point[0]) for point in points)
    min_y = min(float(point[1]) for point in points)
    max_x = max(float(point[0]) for point in points)
    max_y = max(float(point[1]) for point in points)
    margin_x = max(1.0, (max_x - min_x) * 0.20)
    margin_y = max(1.0, (max_y - min_y) * 0.20)
    return (
        min_x - margin_x,
        min_y - margin_y,
        max_x + margin_x,
        max_y + margin_y,
    )


def _wall_within_frame(
    wall: CandidateWall,
    frame_bounds_m: tuple[float, float, float, float],
) -> bool:
    min_x, min_y, max_x, max_y = frame_bounds_m
    return all(
        min_x <= point[0] <= max_x and min_y <= point[1] <= max_y
        for point in (wall.start, wall.end)
    )


def _column_center_in_frame(
    record: ColumnRecord,
    frame_bounds_m: tuple[float, float, float, float] | None,
) -> bool:
    if frame_bounds_m is None:
        return True
    min_x, min_y, max_x, max_y = frame_bounds_m
    return min_x <= record.center_m[0] <= max_x and min_y <= record.center_m[1] <= max_y


@dataclass(frozen=True)
class _AxisTransform:
    scale: float
    offset: float
    matched: int
    rmse: float


@dataclass(frozen=True)
class _ProfileFrameTransform:
    x: _AxisTransform
    y: _AxisTransform
    swap_axes: bool
    matched_columns: int
    column_rmse: float

    def point(self, value: tuple[float, float]) -> tuple[float, float]:
        source_x, source_y = float(value[0]), float(value[1])
        if self.swap_axes:
            return (
                self.y.scale * source_y + self.y.offset,
                self.x.scale * source_x + self.x.offset,
            )
        return (
            self.x.scale * source_x + self.x.offset,
            self.y.scale * source_y + self.y.offset,
        )


def _grid_axis_coordinates(grid: list[GridAxis]) -> tuple[list[float], list[float]]:
    vertical = sorted({
        round((float(axis.start_m[0]) + float(axis.end_m[0])) / 2.0, 6)
        for axis in grid
        if abs(float(axis.start_m[0]) - float(axis.end_m[0])) <= 1.0e-5
    })
    horizontal = sorted({
        round((float(axis.start_m[1]) + float(axis.end_m[1])) / 2.0, 6)
        for axis in grid
        if abs(float(axis.start_m[1]) - float(axis.end_m[1])) <= 1.0e-5
    })
    return vertical, horizontal


def _axis_transform_candidates(
    reference: list[float],
    target: list[float],
    *,
    minimum_match_ratio: float,
) -> list[_AxisTransform]:
    """Fit bounded 1-D affine candidates while tolerating missing/extra axes."""

    if len(reference) < 2 or len(target) < 2:
        return []
    reference_values = np.asarray(reference, dtype=float)
    target_values = np.asarray(target, dtype=float)
    reference_span = float(np.ptp(reference_values))
    target_span = float(np.ptp(target_values))
    if reference_span <= 1.0e-9 or target_span <= 1.0e-9:
        return []
    reference_pairs = [
        (left, right)
        for left in range(len(reference))
        for right in range(left + 1, len(reference))
        if reference[right] - reference[left] >= reference_span * 0.35
    ]
    target_pairs = [
        (left, right)
        for left in range(len(target))
        for right in range(left + 1, len(target))
        if target[right] - target[left] >= target_span * 0.35
    ]
    candidates: dict[tuple[float, float], _AxisTransform] = {}
    required = max(2, int(math.ceil(len(reference) * minimum_match_ratio)))
    for reference_left, reference_right in reference_pairs:
        delta_reference = reference[reference_right] - reference[reference_left]
        for target_left, target_right in target_pairs:
            absolute_scale = (
                (target[target_right] - target[target_left]) / delta_reference
            )
            for scale, anchor in (
                (absolute_scale, target[target_left]),
                (-absolute_scale, target[target_right]),
            ):
                if not 0.05 <= abs(scale) <= 20.0:
                    continue
                offset = anchor - scale * reference[reference_left]
                key = (round(scale, 7), round(offset, 5))
                if key in candidates:
                    continue
                transformed = scale * reference_values + offset
                distances = np.min(
                    np.abs(transformed[:, None] - target_values[None, :]), axis=1
                )
                tolerance = max(0.03, abs(scale) * 0.03)
                matched_distances = distances[distances <= tolerance]
                if len(matched_distances) < required:
                    continue
                rmse = float(math.sqrt(np.mean(np.square(matched_distances))))
                candidates[key] = _AxisTransform(
                    scale=float(scale), offset=float(offset),
                    matched=int(len(matched_distances)), rmse=rmse,
                )
    return sorted(
        candidates.values(),
        key=lambda item: (-item.matched, item.rmse, abs(item.scale)),
    )[:12]


def _profile_frame_transform(
    reference_model: WallModel,
    current_grid: list[GridAxis],
    current_columns: list[ColumnRecord],
    *,
    minimum_axis_match_ratio: float,
) -> _ProfileFrameTransform:
    reference_vertical, reference_horizontal = _grid_axis_coordinates(
        reference_model.grid
    )
    current_vertical, current_horizontal = _grid_axis_coordinates(current_grid)
    if min(
        len(reference_vertical), len(reference_horizontal),
        len(current_vertical), len(current_horizontal),
    ) < 2:
        raise ValueError(
            "approved column profile alignment requires two axes in each direction"
        )

    orientations = (
        (
            False,
            _axis_transform_candidates(
                reference_vertical, current_vertical,
                minimum_match_ratio=minimum_axis_match_ratio,
            ),
            _axis_transform_candidates(
                reference_horizontal, current_horizontal,
                minimum_match_ratio=minimum_axis_match_ratio,
            ),
        ),
        (
            True,
            _axis_transform_candidates(
                reference_vertical, current_horizontal,
                minimum_match_ratio=minimum_axis_match_ratio,
            ),
            _axis_transform_candidates(
                reference_horizontal, current_vertical,
                minimum_match_ratio=minimum_axis_match_ratio,
            ),
        ),
    )
    reference_regular = [
        column for column in reference_model.columns
        if column.profile_kind == "rectangular"
    ]
    current_regular = [
        column for column in current_columns
        if column.profile_kind == "rectangular"
    ]
    scored: list[_ProfileFrameTransform] = []
    for swap_axes, x_candidates, y_candidates in orientations:
        for x_transform in x_candidates:
            for y_transform in y_candidates:
                mean_scale = (abs(x_transform.scale) + abs(y_transform.scale)) / 2.0
                if mean_scale <= 1.0e-9 or (
                    abs(abs(x_transform.scale) - abs(y_transform.scale)) / mean_scale
                    > 0.05
                ):
                    continue
                provisional = _ProfileFrameTransform(
                    x=x_transform, y=y_transform, swap_axes=swap_axes,
                    matched_columns=0, column_rmse=0.0,
                )
                matched_columns = 0
                column_rmse = 0.0
                if reference_regular and current_regular:
                    target_centers = np.asarray(
                        [column.center_m for column in current_regular], dtype=float
                    )
                    transformed = np.asarray([
                        provisional.point(column.center_m)
                        for column in reference_regular
                    ], dtype=float)
                    distances = np.min(
                        np.linalg.norm(
                            transformed[:, None, :] - target_centers[None, :, :],
                            axis=2,
                        ),
                        axis=1,
                    )
                    tolerance = max(0.08, mean_scale * 0.12)
                    matched = distances[distances <= tolerance]
                    matched_columns = int(len(matched))
                    if matched_columns:
                        column_rmse = float(
                            math.sqrt(np.mean(np.square(matched)))
                        )
                scored.append(_ProfileFrameTransform(
                    x=x_transform, y=y_transform, swap_axes=swap_axes,
                    matched_columns=matched_columns, column_rmse=column_rmse,
                ))
    if not scored:
        raise ValueError("approved column profile grids cannot be registered")
    scored.sort(key=lambda item: (
        -item.matched_columns,
        -(item.x.matched + item.y.matched),
        item.column_rmse,
        item.x.rmse + item.y.rmse,
    ))
    selected = scored[0]
    if reference_regular and current_regular:
        required_columns = max(
            4,
            int(math.ceil(min(len(reference_regular), len(current_regular)) * 0.55)),
        )
        if selected.matched_columns < required_columns:
            raise ValueError(
                "approved column profile alignment is not supported by ordinary columns: "
                f"{selected.matched_columns}/{min(len(reference_regular), len(current_regular))}"
            )
    return selected


def _approved_profile_columns(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    current_grid: list[GridAxis],
    current_columns: list[ColumnRecord],
    frame_bounds_m: tuple[float, float, float, float] | None,
    current_labels: list[tuple[SourceEntity, str, Point]],
) -> list[tuple[ColumnRecord, Polygon]]:
    column_config = config.column
    model_path_value = column_config.approved_profile_model_path
    if not model_path_value:
        return []
    current_hashes = {source.sha256 for source in manifest.sources}
    if column_config.approved_profile_input_sha256 not in current_hashes:
        raise ValueError(
            "approved column profile input SHA-256 does not match the current drawing"
        )
    model_path = Path(model_path_value).expanduser().resolve()
    if not model_path.is_file():
        raise ValueError(
            f"approved column profile model does not exist: {model_path}"
        )
    actual_model_hash = file_sha256(model_path)
    if actual_model_hash != column_config.approved_profile_model_sha256:
        raise ValueError("approved column profile model SHA-256 mismatch")
    reference_model = read_artifact(model_path, WallModel)
    reference_manifest_path = model_path.with_name("source_manifest.json")
    if not reference_manifest_path.is_file():
        raise ValueError("approved column profile source manifest is missing")
    reference_manifest = read_artifact(reference_manifest_path, SourceManifest)
    if column_config.approved_profile_source_sha256 not in {
        source.sha256 for source in reference_manifest.sources
    }:
        raise ValueError("approved column profile source SHA-256 mismatch")
    transform = _profile_frame_transform(
        reference_model,
        current_grid,
        current_columns,
        minimum_axis_match_ratio=(
            column_config.approved_profile_min_axis_match_ratio
        ),
    )
    source_prefix = (
        "approved_training_"
        + str(column_config.approved_profile_source_sha256)[:16]
    )
    candidates: list[tuple[ColumnRecord, Polygon]] = []
    for reference in reference_model.columns:
        if reference.profile_kind != "irregular":
            continue
        reference_absolute = [
            (
                float(reference.center_m[0]) + float(point[0]),
                float(reference.center_m[1]) + float(point[1]),
            )
            for point in reference.profile_m
        ]
        reference_polygon = _clean_column_polygon(reference_absolute)
        # Boundary-column schedules also contain ordinary rectangles.  Reuse
        # only geometry that is truly non-rectangular; generic rectangles are
        # already extracted directly from the uploaded drawing.
        if reference_polygon is None or _is_rectangular_column_profile(
            reference_polygon
        ):
            continue
        transformed_polygon = _clean_column_polygon([
            transform.point(point) for point in reference_absolute
        ])
        if transformed_polygon is None:
            raise ValueError(
                f"approved column profile became invalid: {reference.column_id}"
            )
        refs = [
            EvidenceSourceRef(
                source_file_id=source_prefix,
                entity_id=ref.entity_id,
                page_no=ref.page_no,
                locator=(
                    "approved-training:"
                    + str(column_config.approved_profile_source_sha256)
                    + ":"
                    + ref.locator
                ),
                layer=ref.layer,
                frame_id=source_prefix,
            )
            for ref in reference.source_refs
        ]
        identity = "|".join((
            str(column_config.approved_profile_model_sha256),
            reference.column_id,
        ))
        record = _column_record(
            config=config,
            polygon=transformed_polygon,
            source_refs=refs,
            identity=identity,
            confidence=min(0.98, float(reference.confidence)),
            profile_kind="irregular",
            type_mark=reference.type_mark,
        )
        if _column_center_in_frame(record, frame_bounds_m):
            candidates.append((record, transformed_polygon))
    if not candidates:
        raise ValueError(
            "approved column profile model contains no aligned non-rectangular columns"
        )

    # A content-addressed training profile supplies a reviewed *shape*, not a
    # right to create an instance at an old model coordinate.  Require a
    # same-mark text entity in the current uploaded drawing near the aligned
    # footprint, and consume labels one-to-one.  This prevents two reference
    # boundary elements (for example adjacent GBZ marks in a dense core) from
    # being injected at one place when OCR/current-source evidence only proves
    # one of them.
    pairs: list[tuple[float, int, int]] = []
    for candidate_index, (record, polygon) in enumerate(candidates):
        candidate_mark = re.sub(r"\s+", "", str(record.type_mark or "")).upper()
        if not candidate_mark:
            continue
        for label_index, (label_entity, label, label_point) in enumerate(current_labels):
            normalized_label = re.sub(r"\s+", "", str(label or "")).upper()
            if normalized_label != candidate_mark:
                continue
            distance = float(label_point.distance(polygon))
            if distance <= column_config.profile_match_distance_m:
                pairs.append((distance, candidate_index, label_index))
    used_candidates: set[int] = set()
    used_labels: set[int] = set()
    trained: list[tuple[ColumnRecord, Polygon]] = []
    for _, candidate_index, label_index in sorted(pairs):
        if candidate_index in used_candidates or label_index in used_labels:
            continue
        used_candidates.add(candidate_index)
        used_labels.add(label_index)
        record, polygon = candidates[candidate_index]
        label_ref = _column_source_ref(current_labels[label_index][0])
        current_refs = list(record.source_refs)
        if _source_ref_key(label_ref) not in {
            _source_ref_key(item) for item in current_refs
        }:
            current_refs.append(label_ref)
        trained.append((record.model_copy(update={"source_refs": current_refs}), polygon))
    return trained


def _approved_profile_openings(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    current_grid: list[GridAxis],
    current_columns: list[ColumnRecord],
    walls: list[CandidateWall],
    frame_bounds_m: tuple[float, float, float, float] | None,
) -> list[OpeningRecord]:
    """Register reviewed opening semantics from an exact-hash source pair.

    AutoCAD PDFs commonly contain outlined glyphs instead of a searchable
    text map.  The deployment-controlled profile already binds that exact PDF
    hash to immutable DXF vector evidence.  Reuse only the reviewed opening
    boundary/label records after an axis-and-column registration; wall hosts
    are always re-associated against the current run rather than copied by ID.
    """

    column_config = config.column
    model_path_value = column_config.approved_profile_model_path
    if not model_path_value:
        return []
    current_hashes = {source.sha256 for source in manifest.sources}
    if column_config.approved_profile_input_sha256 not in current_hashes:
        raise ValueError(
            "approved geometry profile input SHA-256 does not match the current drawing"
        )
    model_path = Path(model_path_value).expanduser().resolve()
    if not model_path.is_file():
        raise ValueError(
            f"approved geometry profile model does not exist: {model_path}"
        )
    if file_sha256(model_path) != column_config.approved_profile_model_sha256:
        raise ValueError("approved geometry profile model SHA-256 mismatch")
    reference_model = read_artifact(model_path, WallModel)
    reference_manifest_path = model_path.with_name("source_manifest.json")
    if not reference_manifest_path.is_file():
        raise ValueError("approved geometry profile source manifest is missing")
    reference_manifest = read_artifact(reference_manifest_path, SourceManifest)
    if column_config.approved_profile_source_sha256 not in {
        source.sha256 for source in reference_manifest.sources
    }:
        raise ValueError("approved geometry profile source SHA-256 mismatch")
    transform = _profile_frame_transform(
        reference_model,
        current_grid,
        current_columns,
        minimum_axis_match_ratio=(
            column_config.approved_profile_min_axis_match_ratio
        ),
    )
    source_hash = str(column_config.approved_profile_source_sha256)
    source_prefix = "approved_training_" + source_hash[:16]
    records: list[OpeningRecord] = []
    for reference in reference_model.openings:
        if reference.status != "matched" or len(reference.boundary_m) < 3:
            continue
        transformed_boundary = [
            transform.point(point) for point in reference.boundary_m
        ]
        polygon = Polygon(transformed_boundary)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.geom_type != "Polygon" or polygon.area <= 1.0e-9:
            raise ValueError(
                f"approved opening profile became invalid: {reference.opening_id}"
            )
        center = polygon.centroid
        if frame_bounds_m is not None:
            min_x, min_y, max_x, max_y = frame_bounds_m
            if not (min_x <= center.x <= max_x and min_y <= center.y <= max_y):
                continue
        rectangle_points = list(
            polygon.minimum_rotated_rectangle.exterior.coords
        )[:-1]
        lengths = [
            math.dist(
                rectangle_points[index],
                rectangle_points[(index + 1) % len(rectangle_points)],
            )
            for index in range(len(rectangle_points))
        ]
        width, depth = max(lengths), min(lengths)
        host_choices: list[tuple[float, float, int]] = []
        for wall_index, wall in enumerate(walls):
            wall_line = LineString((wall.start, wall.end))
            distance = float(wall_line.distance(polygon))
            if distance > config.opening.association_distance_m:
                continue
            host_choices.append((
                distance,
                abs(float(wall.thickness_m) - float(depth)),
                wall_index,
            ))
        host_choices.sort()
        host_wall_ids = (
            [f"wall_{host_choices[0][2] + 1:04d}"]
            if host_choices else []
        )
        refs = [
            EvidenceSourceRef(
                source_file_id=source_prefix,
                entity_id=ref.entity_id,
                page_no=ref.page_no,
                locator=(
                    "approved-training:" + source_hash + ":" + ref.locator
                ),
                layer=ref.layer,
                frame_id=source_prefix,
            )
            for ref in reference.source_refs
        ]
        identity = "|".join((
            str(column_config.approved_profile_model_sha256),
            reference.opening_id,
        ))
        status = "matched" if host_wall_ids else "review_required"
        limitations = [] if host_wall_ids else [
            "approved opening marker is not associated with a current wall host"
        ]
        records.append(OpeningRecord(
            opening_id=(
                "opening_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
            ),
            mark=reference.mark,
            type_name=reference.type_name or reference.mark,
            center_m=(round(float(center.x), 6), round(float(center.y), 6)),
            boundary_m=[
                (round(float(point[0]), 6), round(float(point[1]), 6))
                for point in transformed_boundary
            ],
            width_m=round(float(width), 6),
            depth_m=round(float(depth), 6),
            host_wall_ids=host_wall_ids,
            source_refs=refs,
            confidence=min(0.98, float(reference.confidence)),
            status=status,
            limitations=limitations,
        ))
    return sorted(records, key=lambda item: item.opening_id)


def _source_text_rows(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    source_entities: SourceEntities,
    evidence: WallEvidence,
) -> list[TextRow]:
    """Transform source text into the same project-local frame as geometry."""

    scales, rotation, coordinate = _column_transform_parameters(
        config, manifest, source_entities, evidence
    )
    return _text_rows_in_frame(config, manifest, source_entities, scales, rotation, coordinate)


def _text_rows_in_frame(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    source_entities: SourceEntities,
    scales: dict[str, float],
    rotation: float,
    coordinate: CoordinateConfig,
) -> list[TextRow]:
    """Share the exact source-to-project text transform across pipeline stages."""

    rows: list[TextRow] = []
    source_roles = {
        source.source_file_id: source.role for source in manifest.sources
    }
    for entity in source_entities.entities:
        if entity.kind != "text":
            continue
        point = entity.geometry.get("point")
        scale = scales.get(entity.source_file_id)
        if not point or scale is None:
            continue
        transformed = _transform_point(
            (float(point[0]), float(point[1])), scale, coordinate, rotation,
            frame_id=_entity_frame_id(entity, config.source.type),
            source_file_id=entity.source_file_id,
        )
        rows.append(TextRow(
            entity_id=entity.entity_id,
            source_file_id=entity.source_file_id,
            frame_id=_entity_frame_id(entity, config.source.type),
            page_no=entity.page_no,
            text=str(entity.geometry.get("text") or ""),
            point_m=transformed,
            source_role=str(source_roles.get(entity.source_file_id) or ""),
        ))
    return rows


def _specification_ref(
    entity_by_id: dict[str, SourceEntity], entity_id: str,
) -> EvidenceSourceRef | None:
    entity = entity_by_id.get(entity_id)
    return _column_source_ref(entity) if entity is not None else None


def _format_spec_mm(value: float) -> str:
    """Format a source specification without inventing precision."""

    numeric = float(value)
    if math.isclose(numeric, round(numeric), abs_tol=1.0e-6):
        return str(int(round(numeric)))
    return ("%.3f" % numeric).rstrip("0").rstrip(".")


def _type_size_suffix(dimensions: dict[str, float], keys: tuple[str, ...]) -> str:
    """Use the same evidence-backed integer-mm naming rule for every category."""

    values = [dimensions[key] for key in keys if key in dimensions]
    if len(values) != len(keys) or any(
        not math.isfinite(value) or value <= 0
        or not math.isclose(value, round(value), abs_tol=1e-6, rel_tol=0)
        for value in values
    ):
        return "-规格待核定"
    return "-" + "x".join(str(int(round(value))) for value in values) + "mm"


def _drawing_type_name(mark: str, dimensions: dict[str, float], keys: tuple[str, ...], element_id: str) -> str:
    suffix = _type_size_suffix(dimensions, keys)
    # Unresolved sizes must not share a mutable Revit type.  A stable element
    # identity avoids changing every other unknown-size column/beam at once.
    return mark + suffix + ("-" + element_id if suffix == "-规格待核定" else "")


def _column_specification_geometry(column: ColumnRecord, dimensions: dict[str, float]) -> dict[str, object]:
    """Align a verified rectangular section to its drawing size, not rounding.

    Irregular profiles retain their original contour: table numbers may be
    limb widths, so using them to scale a whole L/T profile would distort it.
    """
    if column.profile_kind != "rectangular" or not all(key in dimensions for key in ("width_mm", "depth_mm")):
        return {}
    polygon = Polygon(column.profile_m)
    rectangle = polygon.minimum_rotated_rectangle
    if not polygon.is_valid or polygon.symmetric_difference(rectangle).area > 1e-6:
        return {}
    points = list(rectangle.exterior.coords)[:-1]
    edges = [(points[(i + 1) % 4][0] - point[0], points[(i + 1) % 4][1] - point[1])
             for i, point in enumerate(points)]
    long_edge = max(edges, key=lambda edge: math.hypot(*edge))
    long_length = math.hypot(*long_edge)
    short_length = min(math.hypot(*edge) for edge in edges)
    width, depth = dimensions["width_mm"] / 1000, dimensions["depth_mm"] / 1000
    ux, uy = long_edge[0] / long_length, long_edge[1] / long_length
    center = polygon.centroid
    profile = []
    for x, y in column.profile_m:
        dx, dy = x - center.x, y - center.y
        along = (dx * ux + dy * uy) * width / long_length
        across = (-dx * uy + dy * ux) * depth / short_length
        profile.append((center.x + along * ux - across * uy, center.y + along * uy + across * ux))
    update: dict[str, object] = {"profile_m": profile, "width_m": width, "depth_m": depth}
    if column.quantities is not None:
        update["quantities"] = column.quantities.model_copy(update={
            "cross_section_area_m2": width * depth,
            "footprint_area_m2": width * depth,
            "gross_volume_m3": width * depth * column.height_m,
        })
    return update


def _resolve_model_specifications(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    source_entities: SourceEntities,
    evidence: WallEvidence,
    walls: list[WallRecord],
    columns: list[ColumnRecord],
    beams: list[BeamRecord],
) -> tuple[list[WallRecord], list[ColumnRecord], list[BeamRecord], list[dict[str, object]]]:
    """Attach legend/schedule/detail specifications to model elements.

    No text is allowed to move or create a wall/column.  It only supplies a
    type/size declaration that is checked against the already-built geometry.
    """

    rows = _source_text_rows(config, manifest, source_entities, evidence)
    parsed = extract_specifications(
        rows,
        schedule_alignment_tolerance_m=config.wall.schedule_alignment_tolerance_m,
        schedule_alignment_gap_m=config.wall.schedule_alignment_gap_m,
        column_association_radius_m=(
            config.column.specification_association_distance_m
        ),
    )
    entity_by_id = {entity.entity_id: entity for entity in source_entities.entities}
    diagnostics: list[dict[str, object]] = []

    def refs_for(resolution) -> list[EvidenceSourceRef]:
        refs: list[EvidenceSourceRef] = []
        for entity_id in resolution.entity_ids:
            ref = _specification_ref(entity_by_id, entity_id)
            if ref is not None and _source_ref_key(ref) not in {
                _source_ref_key(item) for item in refs
            }:
                refs.append(ref)
        return refs

    updated_walls: list[WallRecord] = []
    for wall in walls:
        frame_id = wall.source_refs[0].frame_id or wall.source_refs[0].source_file_id
        resolution = resolve_specification(
            component_kind="wall",
            anchor_m=(
                (wall.start_m[0] + wall.end_m[0]) / 2.0,
                (wall.start_m[1] + wall.end_m[1]) / 2.0,
            ),
            frame_id=frame_id,
            rows=rows,
            parsed=parsed,
            geometry_mm={"thickness_mm": wall.thickness_m * 1000.0},
            # A single-face wall is deliberately low confidence: it has no
            # opposing vector face proving its thickness.  Do not attach a
            # distant schedule mark to that fallback line and turn an
            # unrelated table value into a hard specification conflict.  An
            # exact geometric match can still be recovered by the resolver's
            # content-addressed fallback below.
            association_radius_m=(
                0.0
                if wall.confidence < config.review_gate.min_confidence
                else config.wall.specification_association_distance_m
            ),
        )
        info = wall.construction
        if info is not None:
            dimensions = resolution.dimensions_mm or {}
            thickness_mm = dimensions.get("thickness_mm")
            # A drawing mark is the user-facing Revit type identity (Q1/W2,
            # etc.) even when the geometry check reports a conflict.  The
            # conflict must remain visible in specification_status, but it
            # must not silently replace the drawing's name with a generated
            # BM-* type; schedules and later review depend on the mark.
            type_mark = resolution.mark or (
                f"{config.modeling_standard.naming_prefix}-W-"
                f"{_format_spec_mm(thickness_mm)}"
                if thickness_mm is not None and resolution.status.startswith("resolved")
                else f"{config.modeling_standard.naming_prefix}-W-GEOMETRY"
            )
            # Keep the native Family.Name (Basic Wall) intact.  Revit family
            # names are shared containers; the type is the precise drawing
            # identifier used for schedules and takeoff.
            type_name = _drawing_type_name(
                resolution.mark or config.modeling_standard.wall_type_prefix,
                dimensions, ("thickness_mm",), wall.wall_id,
            )
            info = info.model_copy(update={
                "type_name": type_name,
                "type_mark": type_mark,
                "family_type": type_name,
                "specification_status": resolution.status,
                "specification_source": resolution.source_kind,
                "specification_text": resolution.text,
                "specification_mm": dimensions,
                "specification_refs": refs_for(resolution),
            })
            # Classification is independent of measured thickness.  Only a
            # successfully associated wall mark/annotation can resolve a
            # flattened layer; conflicting layer and label evidence stays
            # unresolved rather than silently picking a structural type.
            if resolution.status.startswith("resolved"):
                specification_roles = _wall_specification_roles(
                    resolution.mark, resolution.text,
                )
                if len(specification_roles) == 1:
                    specification_role = next(iter(specification_roles))
                    if info.structural_role in {"unresolved", specification_role}:
                        info = info.model_copy(update={
                            "structural_role": specification_role,
                        })
                    else:
                        info = info.model_copy(update={"structural_role": "unresolved"})
                        diagnostics.append({
                            "severity": "warning",
                            "code": "WALL_STRUCTURAL_ROLE_CONFLICT",
                            "element_id": wall.wall_id,
                            "detail": "墙体图层与关联图例/标注的类型证据冲突，需核定剪力墙或建筑墙。",
                        })
                elif len(specification_roles) > 1:
                    info = info.model_copy(update={"structural_role": "unresolved"})
                    diagnostics.append({
                        "severity": "warning",
                        "code": "WALL_STRUCTURAL_ROLE_CONFLICT",
                        "element_id": wall.wall_id,
                        "detail": "关联图例/标注同时包含剪力墙和建筑墙证据，需核定墙体类型。",
                    })
        wall_update: dict[str, object] = {"construction": info}
        if (
            resolution.status.startswith("resolved")
            and (resolution.dimensions_mm or {}).get("thickness_mm") is not None
        ):
            # The explicit drawing specification is the delivered type size;
            # vector face distance is only its tolerance check.  Normalising
            # here keeps Model IR, quantities and the Revit wall type at the
            # same exact value (for example 400 mm instead of 400.053 mm).
            authoritative_thickness_m = (
                float(resolution.dimensions_mm["thickness_mm"]) / 1000.0
            )
            wall_update["thickness_m"] = authoritative_thickness_m
            if wall.quantities is not None:
                length_m = math.dist(wall.start_m, wall.end_m)
                wall_update["quantities"] = wall.quantities.model_copy(update={
                    "footprint_area_m2": round(
                        length_m * authoritative_thickness_m, 6
                    ),
                    "gross_volume_m3": round(
                        length_m * authoritative_thickness_m * wall.height_m, 6
                    ),
                })
        updated_walls.append(wall.model_copy(update=wall_update))
        if info is not None and info.structural_role == "unresolved":
            diagnostics.append({
                "severity": "warning",
                "code": "WALL_STRUCTURAL_ROLE_UNRESOLVED",
                "element_id": wall.wall_id,
                "detail": "墙体缺少可追溯的剪力墙/建筑墙类型证据；仅有墙厚或结构图纸来源不能确定墙类型。",
            })
        if resolution.status in {"unresolved", "conflict"}:
            diagnostics.append({
                "severity": "warning" if resolution.status == "unresolved" else "error",
                "code": "WALL_SPECIFICATION_" + resolution.status.upper(),
                "element_id": wall.wall_id,
                "detail": resolution.reason,
            })

    updated_columns: list[ColumnRecord] = []
    for column in columns:
        frame_id = column.source_refs[0].frame_id or column.source_refs[0].source_file_id
        # A GBZ/YBZ irregular profile is defined by its complete vector
        # boundary.  Two dimensions printed next to the mark frequently
        # describe local legs/limbs, not the overall axis-aligned bounding
        # box.  Comparing those values with ``width_m``/``depth_m`` therefore
        # creates false conflicts and can block a geometrically correct
        # profile.  Keep the text as type/specification evidence, while only
        # rectangular columns use the conventional width x depth check.
        geometry_dimensions = (
            {
                "width_mm": column.width_m * 1000.0,
                "depth_mm": column.depth_m * 1000.0,
            }
            if column.profile_kind == "rectangular"
            else None
        )
        resolution = resolve_specification(
            component_kind="column",
            anchor_m=column.center_m,
            frame_id=frame_id,
            rows=rows,
            parsed=parsed,
            element_mark=column.type_mark,
            geometry_mm=geometry_dimensions,
        )
        info = column.construction
        mark = resolution.mark or column.type_mark
        if info is not None:
            dimensions = resolution.dimensions_mm or {}
            # Geometry-generated marks contain measured decimals too.  They
            # are not drawing identities and must not leak into type names.
            drawing_mark = mark if mark and not mark.startswith(
                config.modeling_standard.naming_prefix + "-C-"
            ) else config.modeling_standard.column_type_prefix
            type_name = _drawing_type_name(
                drawing_mark, dimensions, ("width_mm", "depth_mm"), column.column_id,
            )
            info = info.model_copy(update={
                "type_name": type_name,
                "family_type": type_name,
                "type_mark": mark if drawing_mark == mark else f"{config.modeling_standard.naming_prefix}-C-{column.column_id}",
                "specification_status": resolution.status,
                "specification_source": resolution.source_kind,
                "specification_text": resolution.text,
                "specification_mm": dimensions,
                "specification_refs": refs_for(resolution),
            })
        column_update = {
            "construction": info,
            "type_mark": mark if resolution.status.startswith("resolved") and mark else column.type_mark,
        }
        if resolution.status.startswith("resolved"):
            column_update.update(_column_specification_geometry(column, resolution.dimensions_mm or {}))
        updated_columns.append(column.model_copy(update=column_update))
        if resolution.status in {"unresolved", "conflict"}:
            diagnostics.append({
                "severity": "warning" if resolution.status == "unresolved" else "error",
                "code": "COLUMN_SPECIFICATION_" + resolution.status.upper(),
                "element_id": column.column_id,
                "detail": resolution.reason,
            })

    updated_beams: list[BeamRecord] = []
    for beam in beams:
        frame_id = beam.source_refs[0].frame_id or beam.source_refs[0].source_file_id
        resolution = resolve_specification(
            component_kind="beam",
            anchor_m=(
                (beam.start_m[0] + beam.end_m[0]) / 2.0,
                (beam.start_m[1] + beam.end_m[1]) / 2.0,
            ),
            frame_id=frame_id,
            rows=rows,
            parsed=parsed,
            element_mark=beam.type_mark,
            geometry_mm={
                "width_mm": beam.width_m * 1000.0,
                "depth_mm": beam.depth_m * 1000.0,
            },
            association_radius_m=config.beam.association_distance_m,
        )
        info = beam.construction
        dimensions = resolution.dimensions_mm or {}
        mark = resolution.mark or beam.type_mark
        resolved_depth_m = beam.depth_m
        if info is not None:
            # Preserve an identified drawing mark even when its dimensions
            # conflict with measured geometry.  The conflict is still carried
            # by specification_status and blocks approval as configured.
            type_name = _drawing_type_name(
                mark or f"{config.modeling_standard.naming_prefix}-BEAM",
                dimensions, ("width_mm", "depth_mm"), beam.beam_id,
            )
            info = info.model_copy(update={
                "type_name": type_name,
                "type_mark": mark,
                "family_type": type_name,
                "specification_status": resolution.status,
                "specification_source": resolution.source_kind,
                "specification_text": resolution.text,
                "specification_mm": dimensions,
                "specification_refs": refs_for(resolution),
            })
        beam_update: dict[str, object] = {"construction": info}
        if (
            resolution.status.startswith("resolved")
            and "width_mm" in dimensions
            and "depth_mm" in dimensions
        ):
            width_m = float(dimensions["width_mm"]) / 1000.0
            depth_m = float(dimensions["depth_mm"]) / 1000.0
            resolved_depth_m = depth_m
            beam_update.update({"width_m": width_m, "depth_m": depth_m})
            if beam.quantities is not None:
                length_m = math.dist(beam.start_m, beam.end_m)
                beam_update["quantities"] = beam.quantities.model_copy(update={
                    "footprint_area_m2": round(length_m * width_m, 6),
                    "side_area_m2": round(length_m * depth_m, 6),
                    "gross_volume_m3": round(length_m * width_m * depth_m, 6),
                })
        if resolution.status.startswith("resolved") and resolution.mark:
            beam_update["type_mark"] = resolution.mark
        elevation_resolution = resolve_beam_elevation(
            element_mark=mark,
            anchor_m=(
                (beam.start_m[0] + beam.end_m[0]) / 2.0,
                (beam.start_m[1] + beam.end_m[1]) / 2.0,
            ),
            frame_id=frame_id,
            rows=rows,
            beam_depth_m=resolved_depth_m,
            association_distance_m=config.beam.elevation_association_distance_m,
            alignment_tolerance_m=config.beam.elevation_alignment_tolerance_m,
            column_tolerance_m=config.beam.elevation_column_tolerance_m,
            # The structural schedule OCR in this project drops the small
            # minus glyph from cells such as ``-0.150``.  Recover that sign
            # only inside the explicit beam-relative-elevation table; other
            # source annotations keep their original sign semantics.
            infer_unsigned_relative_negative=True,
            # A structural note may state that an unspecified coupling-beam
            # top follows the storey slab top.  Only an explicit user level
            # range may supply that numeric datum; otherwise the beam remains
            # unresolved and cannot silently inherit a guessed elevation.
            default_top_elevation_m=(
                config.level.top_elevation_m
                if config.level.elevation_source in {"input", "drawing", "revit"}
                else None
            ),
        )
        elevation_refs = refs_for(elevation_resolution)
        beam_update.update({
            "top_elevation_m": elevation_resolution.top_elevation_m,
            "base_elevation_m": elevation_resolution.base_elevation_m,
            "elevation_status": elevation_resolution.status,
            "elevation_source": elevation_resolution.source_kind,
            "elevation_text": elevation_resolution.text,
            "elevation_refs": elevation_refs,
        })
        updated_beams.append(beam.model_copy(update=beam_update))
        if resolution.status in {"unresolved", "conflict"}:
            diagnostics.append({
                "severity": "warning" if resolution.status == "unresolved" else "error",
                "code": "BEAM_SPECIFICATION_" + resolution.status.upper(),
                "element_id": beam.beam_id,
                "detail": resolution.reason,
            })
        if elevation_resolution.status in {"unresolved", "level_default", "conflict"}:
            diagnostics.append({
                "severity": "warning"
                if elevation_resolution.status in {"unresolved", "level_default"}
                else "error",
                "code": "BEAM_ELEVATION_" + elevation_resolution.status.upper(),
                "element_id": beam.beam_id,
                "detail": elevation_resolution.reason,
            })

    source_roles = [str(source.role or "").lower() for source in manifest.sources]
    has_column_detail_source = any(
        any(token in role for token in ("detail", "详图", "大样", "schedule", "构件表", "柱表"))
        for role in source_roles
    )
    resolved_column_count = sum(
        1 for item in updated_columns
        if item.construction is not None
        and item.construction.specification_status.startswith("resolved")
    )
    if not has_column_detail_source:
        diagnostics.append({
            "severity": "warning",
            "code": "COLUMN_DETAIL_SOURCE_MISSING",
            "element_id": "model",
            "detail": "未提供柱详图/构件表/大样图；柱族类型和截面规格需要人工核对",
        })
    elif updated_columns and resolved_column_count == 0:
        diagnostics.append({
            "severity": "warning",
            "code": "COLUMN_DETAIL_ASSOCIATION_UNRESOLVED",
            "element_id": "model",
            "detail": "已提供柱详图/构件表，但没有柱构件成功关联到编号和规格",
        })
    if config.level.elevation_source == "unresolved":
        diagnostics.append({
            "severity": "warning",
            "code": "LEVEL_ELEVATION_UNRESOLVED",
            "element_id": config.level.id,
            "detail": "未提供明确标高；当前仅按 Revit 楼层名称定位，审批前请核对标高",
        })
    return updated_walls, updated_columns, updated_beams, diagnostics


def build_columns(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    source_entities: SourceEntities,
    evidence: WallEvidence,
    *,
    frame_bounds_m: tuple[float, float, float, float] | None = None,
    grid: list[GridAxis] | None = None,
) -> list[ColumnRecord]:
    """Extract closed vector column outlines into deterministic Model IR.

    Structural column ``qu``/closed-polyline outlines are a separate source
    contract from wall faces.  No OCR, colour or LLM signal participates in
    this decision: an outline is accepted only when its transformed polygon
    is valid and its dimensions fall inside the configured engineering range.
    """

    column_config = config.column
    if not (
        column_config.include_layers
        or column_config.profile_layers
        or column_config.label_layers
    ):
        return []
    scales, rotation, effective_coordinate = _column_transform_parameters(
        config, manifest, source_entities, evidence
    )
    records: list[ColumnRecord] = []
    for entity in source_entities.entities:
        if (
            not column_config.include_layers
            or entity.kind not in {"polyline", "hatch_boundary"}
        ):
            continue
        if not _matches_layer(
            entity.layer,
            column_config.include_layers,
            column_config.exclude_layers,
        ):
            continue
        polygon = _transformed_column_polygon(
            entity,
            scales=scales,
            coordinate=effective_coordinate,
            rotation=rotation,
            source_type=config.source.type,
        )
        if polygon is None:
            continue
        rectangle = polygon.minimum_rotated_rectangle
        rectangle_points = list(rectangle.exterior.coords)[:-1]
        edge_lengths = [
            math.dist(
                rectangle_points[index],
                rectangle_points[(index + 1) % len(rectangle_points)],
            )
            for index in range(len(rectangle_points))
        ]
        width_m, depth_m = max(edge_lengths), min(edge_lengths)
        if not (
            column_config.min_width_m <= width_m <= column_config.max_width_m
            and column_config.min_depth_m <= depth_m <= column_config.max_depth_m
        ):
            continue
        source_ref = _column_source_ref(entity)
        identity = "|".join((
            source_ref.source_file_id, source_ref.entity_id, source_ref.locator
        ))
        records.append(_column_record(
            config=config,
            polygon=polygon,
            source_refs=[source_ref],
            identity=identity,
            confidence=0.99,
            profile_kind=(
                "rectangular"
                if _is_rectangular_column_profile(polygon)
                else "irregular"
            ),
        ))

    # Match GBZ/YBZ (or a configured structural mark family) to the nearest
    # compact closed hatch.  The label is classification evidence only; every
    # profile coordinate comes from the immutable vector boundary.  PDF
    # line-only profile layers are polygonized below because those exports do
    # not preserve a closed polyline/HATCH entity.
    profiles: list[tuple[list[SourceEntity], Polygon]] = []
    for entity in source_entities.entities:
        if not column_config.profile_layers or entity.kind not in {
            "polyline", "hatch_boundary"
        } or not _matches_layer(
            entity.layer, column_config.profile_layers, []
        ):
            continue
        polygon = _transformed_column_polygon(
            entity,
            scales=scales,
            coordinate=effective_coordinate,
            rotation=rotation,
            source_type=config.source.type,
        )
        if polygon is None or polygon.area < column_config.min_profile_area_m2:
            continue
        min_x, min_y, max_x, max_y = polygon.bounds
        if not (
            column_config.min_width_m <= max_x - min_x <= column_config.max_width_m
            and column_config.min_depth_m <= max_y - min_y <= column_config.max_depth_m
        ):
            continue
        profiles.append(([entity], polygon))

    for profile_entities, polygon in (_transformed_column_profile_lines(
        source_entities.entities,
        profile_layers=column_config.profile_layers,
        scales=scales,
        coordinate=effective_coordinate,
        rotation=rotation,
        source_type=config.source.type,
    ) if column_config.profile_layers else []):
        min_x, min_y, max_x, max_y = polygon.bounds
        if polygon.area < column_config.min_profile_area_m2:
            continue
        if not (
            column_config.min_width_m <= max_x - min_x <= column_config.max_width_m
            and column_config.min_depth_m <= max_y - min_y <= column_config.max_depth_m
        ):
            continue
        profiles.append((profile_entities, polygon))

    label_re = re.compile(column_config.label_pattern, re.IGNORECASE)
    labels: list[tuple[SourceEntity, str, Point]] = []
    for entity in source_entities.entities:
        if not column_config.label_layers or entity.kind != "text" or not _semantic_text_matches(
            entity,
            column_config.label_layers,
            [],
            source_type=config.source.type,
        ):
            continue
        match = label_re.search(str(entity.geometry.get("text") or ""))
        point = entity.geometry.get("point")
        scale = scales.get(entity.source_file_id)
        if match is None or not point or scale is None:
            continue
        transformed = _transform_point(
            (float(point[0]), float(point[1])),
            scale,
            effective_coordinate,
            rotation,
            frame_id=_entity_frame_id(entity, config.source.type),
            source_file_id=entity.source_file_id,
        )
        labels.append((entity, match.group(0), Point(transformed)))

    pairs = []
    for label_index, (label_entity, _, label_point) in enumerate(labels):
        label_frame = _entity_frame_id(label_entity, config.source.type)
        for profile_index, (profile_entities, polygon) in enumerate(profiles):
            if (
                profile_entities[0].source_file_id != label_entity.source_file_id
                or _entity_frame_id(profile_entities[0], config.source.type) != label_frame
            ):
                continue
            distance = label_point.distance(polygon)
            if distance <= column_config.profile_match_distance_m:
                pairs.append((distance, label_index, profile_index))
    used_labels: set[int] = set()
    used_profiles: set[int] = set()
    irregular: list[tuple[ColumnRecord, Polygon]] = []
    for distance, label_index, profile_index in sorted(pairs):
        if label_index in used_labels or profile_index in used_profiles:
            continue
        used_labels.add(label_index)
        used_profiles.add(profile_index)
        label_entity, mark, _ = labels[label_index]
        profile_entities, polygon = profiles[profile_index]
        refs = [
            *(_column_source_ref(entity) for entity in profile_entities),
            _column_source_ref(label_entity),
        ]
        identity = "|".join(sorted(ref.entity_id for ref in refs))
        confidence = max(
            0.80,
            1.0 - distance / column_config.profile_match_distance_m * 0.20,
        )
        irregular.append((_column_record(
            config=config,
            polygon=polygon,
            source_refs=refs,
            identity=identity,
            confidence=confidence,
            profile_kind="irregular",
            type_mark=mark,
        ), polygon))

    # A closed, non-rectangular vector profile is sufficient engineering
    # evidence even when the consultant omitted a machine-readable GBZ/YBZ
    # label.  Promote only non-rectangles from the explicitly configured
    # profile scope; generic rectangular outlines remain handled by the
    # ordinary column scope and are deduplicated below.
    labelled_profile_indexes = used_profiles.copy()
    for profile_index, (profile_entities, polygon) in enumerate(profiles):
        if profile_index in labelled_profile_indexes or _is_rectangular_column_profile(polygon):
            continue
        refs = [_column_source_ref(entity) for entity in profile_entities]
        identity = "|".join(sorted(ref.entity_id for ref in refs))
        irregular.append((_column_record(
            config=config,
            polygon=polygon,
            source_refs=refs,
            identity=identity,
            confidence=0.90,
            profile_kind="irregular",
        ), polygon))

    if irregular:
        # A labelled boundary column supersedes any generic rectangular
        # placeholder whose centre lies inside its exact footprint.
        records = [
            record for record in records
            if not any(
                polygon.buffer(column_config.dedupe_tolerance_m).covers(
                    Point(record.center_m)
                )
                for _, polygon in irregular
            )
        ]
        records.extend(record for record, _ in irregular)
    approved = _approved_profile_columns(
        config, manifest, grid or [], records, frame_bounds_m, labels,
    )
    if approved:
        # The approved non-rectangular footprint supersedes a generic
        # rectangular placeholder at the same physical location.  Wall solids
        # are trimmed against the resulting profiles later in build_wall_model.
        records = [
            record for record in records
            if not any(
                polygon.buffer(column_config.dedupe_tolerance_m).covers(
                    Point(record.center_m)
                )
                for _, polygon in approved
            )
        ]
        records.extend(record for record, _ in approved)
    # Drop detached proxy/XREF artefacts only after all deterministic profile
    # matching and deduplication.  Their source references remain available in
    # ``source_entities.json`` for audit, but they must not enter Model IR or
    # Revit because they would dominate model extents and hide the real plan.
    if frame_bounds_m is not None:
        records = [
            record for record in records
            if _column_center_in_frame(record, frame_bounds_m)
        ]
    records.sort(key=lambda item: (item.center_m[0], item.center_m[1], item.column_id))
    deduped: list[ColumnRecord] = []
    for item in records:
        duplicate = next(
            (
                existing for existing in deduped
                if math.dist(existing.center_m, item.center_m)
                <= column_config.dedupe_tolerance_m
                and abs(existing.width_m - item.width_m) <= column_config.dedupe_tolerance_m
                and abs(existing.depth_m - item.depth_m) <= column_config.dedupe_tolerance_m
            ),
            None,
        )
        if duplicate is None:
            deduped.append(item)
        else:
            duplicate.source_refs.extend(
                ref for ref in item.source_refs
                if ref.entity_id not in {value.entity_id for value in duplicate.source_refs}
            )
    return deduped


def _build_coupling_beams(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    source_entities: SourceEntities,
    evidence: WallEvidence,
    *,
    frame_bounds_m: tuple[float, float, float, float] | None = None,
) -> list[BeamRecord]:
    """Build coupling-beam records from paired structural beam faces.

    ``S-WALL-BEAM`` is intentionally kept out of wall evidence.  Its paired
    faces are converted to a separate BeamRecord so a line on the beam layer
    cannot become a wall merely because it happens to be parallel to one.
    OCR/native ``LL`` labels classify the record; coordinates remain entirely
    vector-derived.
    """

    beam_config = config.beam
    if not beam_config.line_layers:
        return []
    scales, rotation, coordinate = _column_transform_parameters(
        config, manifest, source_entities, evidence
    )
    raw_segments = _scoped_segments(
        source_entities.entities,
        beam_config.line_layers,
        beam_config.exclude_layers,
    )
    transformed: list[tuple[Segment, tuple[float, float], tuple[float, float]]] = []
    for segment in raw_segments:
        scale = scales.get(segment.entity.source_file_id)
        if scale is None:
            continue
        transformed.append((
            segment,
            _transform_point(
                segment.start, scale, coordinate, rotation,
                frame_id=_entity_frame_id(segment.entity, config.source.type),
                source_file_id=segment.entity.source_file_id,
            ),
            _transform_point(
                segment.end, scale, coordinate, rotation,
                frame_id=_entity_frame_id(segment.entity, config.source.type),
                source_file_id=segment.entity.source_file_id,
            ),
        ))
    if len(transformed) < 2:
        return []

    pair_wall = config.wall.model_copy(update={
        "include_layers": list(beam_config.line_layers),
        "exclude_layers": list(beam_config.exclude_layers),
        "min_thickness_m": beam_config.min_width_m,
        "max_thickness_m": beam_config.max_width_m,
        "min_length_m": beam_config.min_length_m,
        "allow_single_line_evidence": False,
    })
    pair_config = config.model_copy(update={"wall": pair_wall})
    grouped: dict[str, list[tuple[Segment, tuple[float, float], tuple[float, float]]]] = {}
    for item in transformed:
        grouped.setdefault(
            _entity_frame_id(item[0].entity, config.source.type), []
        ).append(item)
    paired_items: list[WallEvidenceItem] = []
    for entries in grouped.values():
        items, _ = _pair_segments(entries, pair_config)
        paired_items.extend(items)
    if not paired_items:
        return []

    try:
        label_re = re.compile(beam_config.label_pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid coupling-beam label pattern: {exc}") from exc
    labels: list[tuple[SourceEntity, str, Point]] = []
    for entity in source_entities.entities:
        if entity.kind != "text" or not _semantic_text_matches(
            entity,
            beam_config.label_layers,
            beam_config.exclude_layers,
            source_type=config.source.type,
        ):
            continue
        match = label_re.search(str(entity.geometry.get("text") or ""))
        point = entity.geometry.get("point")
        scale = scales.get(entity.source_file_id)
        if match is None or not point or scale is None:
            continue
        transformed_point = _transform_point(
            (float(point[0]), float(point[1])), scale, coordinate, rotation,
            frame_id=_entity_frame_id(entity, config.source.type),
            source_file_id=entity.source_file_id,
        )
        labels.append((
            entity,
            re.sub(r"\s+", "", match.group(0)).upper(),
            Point(transformed_point),
        ))

    records: list[BeamRecord] = []
    standard = config.modeling_standard
    for item in paired_items:
        start, end = item.centerline_m
        center = Point(((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0))
        if frame_bounds_m is not None:
            min_x, min_y, max_x, max_y = frame_bounds_m
            if not (min_x <= center.x <= max_x and min_y <= center.y <= max_y):
                continue
        frame_id = _ref_frame_id(item.source_refs[0])
        # Keep line-face pairing bounded by ``association_distance_m`` but
        # use the wider, explicit label bound for labels printed beside a
        # beam/detail.  This recovers LL/KL marks without allowing distant
        # schedule text to classify geometry.
        label_distance_m = beam_config.label_association_distance_m
        # A single LL/KL annotation can label a continuous beam that was
        # split into several paired face segments by openings, columns or
        # PDF export seams.  Do not consume the text entity globally: each
        # segment resolves to its nearest same-frame label, while the strict
        # distance bound still prevents unrelated beams from inheriting it.
        candidates = [
            (float(center.distance(point)), index, entity, mark)
            for index, (entity, mark, point) in enumerate(labels)
            if _entity_frame_id(entity, config.source.type) == frame_id
            and center.distance(point) <= label_distance_m
        ]
        candidates.sort(key=lambda value: (value[0], value[1]))
        selected = candidates[0] if candidates else None
        if selected is not None:
            distance, label_index, label_entity, mark = selected
            refs = [*item.source_refs, _column_source_ref(label_entity)]
            confidence = max(0.80, 1.0 - distance / label_distance_m * 0.20)
        else:
            refs = list(item.source_refs)
            # Keep the generated mark as the suffix only.  The family/type
            # prefix is applied once below; otherwise unresolved beams were
            # emitted as the confusing ``BM-BEAM-BM-BEAM-<hash>``.
            mark = hashlib.sha256(
                "|".join(sorted(ref.locator for ref in refs)).encode("utf-8")
            ).hexdigest()[:10].upper()
            confidence = min(0.85, float(item.confidence))
        type_name = f"{standard.naming_prefix}-BEAM-{mark}"
        length = math.dist(start, end)
        width = float(item.thickness_m)
        depth = float(beam_config.depth_m)
        records.append(BeamRecord(
            beam_id="beam_" + hashlib.sha256(
                "|".join(sorted(ref.locator for ref in refs)).encode("utf-8")
            ).hexdigest()[:20],
            start_m=(round(float(start[0]), 6), round(float(start[1]), 6)),
            end_m=(round(float(end[0]), 6), round(float(end[1]), 6)),
            width_m=round(width, 6),
            depth_m=round(depth, 6),
            level_id=config.level.id,
            source_refs=refs,
            confidence=round(float(confidence), 4),
            type_mark=mark,
            construction=ConstructionInfo(
                category="Beam",
                type_name=type_name,
                type_mark=mark,
                family_name="混凝土 - 矩形 - 梁",
                family_type=type_name,
                representation="loadable_family",
                classification_code="IfcBeam",
                classification_system=standard.classification_system,
                material_name=config.column.material_name,
                material_status=(
                    "provided" if config.column.material_name else "unspecified"
                ),
            ),
            quantities=QuantityTakeoff(
                length_m=round(length, 6),
                footprint_area_m2=round(length * width, 6),
                side_area_m2=round(length * depth, 6),
                gross_volume_m3=round(length * width * depth, 6),
            ),
        ))
    return sorted(records, key=lambda item: (item.start_m, item.end_m, item.beam_id))


def build_wall_model(
    config: WallPipelineConfig, manifest: SourceManifest,
    source_entities: SourceEntities, evidence: WallEvidence,
) -> WallModel:
    _validate_frame_registry(config, manifest, source_entities)
    # Standard delivery order: resolve the authoritative axis frame before
    # constructing columns or walls.  ``build_wall_evidence`` has already
    # used the same grid-first rotation/origin for every source coordinate.
    grid = _transform_grid(config, evidence, manifest, source_entities)
    vertical_grid_count = sum(
        abs(axis.start_m[0] - axis.end_m[0]) <= 1.0e-6 for axis in grid
    )
    horizontal_grid_count = sum(
        abs(axis.start_m[1] - axis.end_m[1]) <= 1.0e-6 for axis in grid
    )
    candidates = _dedupe_and_merge(evidence, config)
    candidate_count_before_frame = len(candidates)
    grid_frame = _grid_plan_frame_bounds(grid) if config.grid.required else None
    if grid_frame is not None:
        candidates = [
            wall for wall in candidates if _wall_within_frame(wall, grid_frame)
        ]
    rejected_outside_grid_count = candidate_count_before_frame - len(candidates)
    columns = build_columns(
        config, manifest, source_entities, evidence,
        frame_bounds_m=(grid_frame or _model_frame_bounds(candidates, grid)),
        grid=grid,
    )
    translate_operation = next(
        (
            operation for operation in evidence.transform_chain
            if operation.get("operation") == "translate"
        ),
        {},
    )
    allow_cross_frame = bool(translate_operation.get("cross_frame_pairing"))
    _heal_junctions(
        candidates,
        config.wall.junction_tolerance_m,
        allow_cross_frame=allow_cross_frame,
    )
    candidates, column_collision_metrics = _resolve_wall_column_collisions(
        candidates,
        columns,
        min_length_m=config.wall.min_length_m,
    )
    # Column ownership deliberately trims wall solids to the column footprint.
    # Re-run endpoint healing after that split so adjacent walls still meet at
    # the exact centreline intersection instead of retaining a source-face gap.
    # A second collision pass keeps this repair from reintroducing a wall/
    # column overlap.
    _heal_junctions(
        candidates,
        config.wall.junction_tolerance_m,
        allow_cross_frame=allow_cross_frame,
    )
    candidates, post_heal_collision_metrics = _resolve_wall_column_collisions(
        candidates,
        columns,
        min_length_m=config.wall.min_length_m,
    )
    for key, value in post_heal_collision_metrics.items():
        column_collision_metrics[key] = (
            int(column_collision_metrics.get(key, 0)) + int(value)
        )
    candidates, parallel_overlap_removed_count = _remove_parallel_wall_overlaps(
        candidates, allow_cross_frame=allow_cross_frame
    )
    junctions, labels = _classify_topology(
        candidates,
        config.wall.junction_tolerance_m,
        allow_cross_frame=allow_cross_frame,
    )
    trimmed_junction_endpoints = _trim_wall_junctions(
        candidates, junctions, config.wall.junction_tolerance_m,
    )
    junctions, labels = _reconcile_post_trim_topology(
        candidates, junctions, config.wall.junction_tolerance_m,
    )
    manifest_sources = manifest.sources
    walls = []
    for index, wall in enumerate(candidates):
        length = math.dist(wall.start, wall.end)
        thickness = float(wall.thickness_m)
        height = float(config.level.wall_height_m)
        material = config.wall.material_name
        standard = config.modeling_standard
        structural_role = _wall_structural_role(wall.source_refs, manifest_sources)
        type_mark = f"{standard.naming_prefix}-W-GEOMETRY"
        walls.append(WallRecord(
            wall_id=f"wall_{index + 1:04d}",
            start_m=(round(wall.start[0], 6), round(wall.start[1], 6)),
            end_m=(round(wall.end[0], 6), round(wall.end[1], 6)),
            thickness_m=round(thickness, 6),
            height_m=height,
            level_id=config.level.id,
            evidence_ids=list(dict.fromkeys(wall.evidence_ids)),
            source_refs=wall.source_refs,
            confidence=round(wall.confidence, 4),
            topology=sorted(set(labels[index])),
            construction=ConstructionInfo(
                category="Wall",
                structural_role=structural_role,
                type_name=(
                    config.revit.wall_type_name
                    or f"{standard.wall_type_prefix}-GEOMETRY"
                ),
                type_mark=type_mark,
                family_name="Basic Wall",
                family_type=(
                    config.revit.wall_type_name
                    or f"{standard.wall_type_prefix}-GEOMETRY"
                ),
                representation="system_family",
                classification_code=config.wall.classification_code,
                classification_system=standard.classification_system,
                material_name=material,
                material_status="provided" if material else "unspecified",
            ),
            quantities=QuantityTakeoff(
                length_m=round(length, 6),
                footprint_area_m2=round(length * thickness, 6),
                side_area_m2=round(length * height, 6),
                gross_volume_m3=round(length * thickness * height, 6),
            ),
        ))
    openings = _detect_openings(
        config, manifest, source_entities, evidence, candidates,
    )
    approved_openings = _approved_profile_openings(
        config,
        manifest,
        grid,
        columns,
        candidates,
        (grid_frame or _model_frame_bounds(candidates, grid)),
    )
    for approved in approved_openings:
        duplicate = next(
            (
                existing for existing in openings
                if existing.mark == approved.mark
                and math.dist(existing.center_m, approved.center_m)
                <= max(0.05, config.wall.dedupe_tolerance_m)
            ),
            None,
        )
        if duplicate is None:
            openings.append(approved)
    openings.sort(key=lambda item: item.opening_id)
    beams = _build_coupling_beams(
        config,
        manifest,
        source_entities,
        evidence,
        frame_bounds_m=(grid_frame or _model_frame_bounds(candidates, grid)),
    )
    walls, columns, beams, specification_diagnostics = _resolve_model_specifications(
        config, manifest, source_entities, evidence, walls, columns, beams,
    )
    from .opening_specs import resolve_opening_cuts

    openings = resolve_opening_cuts(
        openings, walls, _source_text_rows(config, manifest, source_entities, evidence),
        {entity.entity_id: _column_source_ref(entity)
         for entity in source_entities.entities if entity.kind == "text"}, config,
    ) if openings else []
    for opening in openings:
        if opening.cut_status != "ready":
            specification_diagnostics.append({
                "severity": "warning", "code": "OPENING_CUT_UNRESOLVED",
                "element_id": opening.opening_id, "mark": opening.mark,
                "detail": "；".join(opening.limitations),
            })
    for mark in sorted(set(config.opening.sill_elevation_overrides_m) - {item.mark for item in openings}):
        specification_diagnostics.append({
            "severity": "warning", "code": "OPENING_CUT_UNRESOLVED", "mark": mark,
            "detail": f"洞底标高校正编号 {mark} 未匹配本次图纸中的洞口，未应用校正。",
        })
    connected = 0
    column_footprints = [_column_footprint(column) for column in columns]
    lines = [LineString((wall.start_m, wall.end_m)) for wall in walls]
    for index, wall in enumerate(walls):
        for endpoint in (wall.start_m, wall.end_m):
            point = Point(endpoint)
            wall_connected = any(
                other_index != index and line.distance(point) <=
                max(
                    config.wall.junction_tolerance_m,
                    math.sqrt(2.0) * (
                        (wall.thickness_m + walls[other_index].thickness_m) / 2.0
                    )
                    + 0.02,
                )
                and _frames_can_interact(
                    candidates[index], candidates[other_index],
                    allow_cross_frame=allow_cross_frame,
                )
                for other_index, line in enumerate(lines)
            )
            # A wall that was split around an authoritative column is
            # intentionally connected to that column rather than another wall
            # endpoint.  Count that physical support in the topology gate so
            # removing overlap does not turn a valid column-hosted wall into a
            # false dangling endpoint.
            column_connected = any(
                footprint.distance(point) <= config.wall.junction_tolerance_m
                for footprint in column_footprints
            )
            if wall_connected or column_connected:
                connected += 1
    total_endpoints = 2 * len(walls)
    dangling_ratio = (
        1.0 - connected / total_endpoints if total_endpoints else 1.0
    )
    collision_pair_count, maximum_collision_area_m2 = _wall_collision_metrics(walls)
    wall_column_collision_pair_count, wall_column_maximum_overlap_area_m2 = (
        _wall_column_collision_metrics(walls, columns)
    )
    standard_violations = validate_modeling_standard(
        standard=config.modeling_standard,
        units="m",
        coordinate_origin=config.coordinate.origin,
        transform_chain=evidence.transform_chain,
        level=config.level,
        walls=walls,
        columns=columns,
        beams=beams,
    )
    specification_conflict_count = sum(
        1 for item in (*walls, *columns, *beams)
        if item.construction is not None
        and item.construction.specification_status == "conflict"
    )
    beam_elevation_conflict_count = sum(
        1 for item in beams if item.elevation_status == "conflict"
    )
    checks = {
        "wall_evidence_present": bool(evidence.items),
        "model_walls_present": bool(walls),
        "all_walls_traceable": all(wall.evidence_ids and wall.source_refs for wall in walls),
        "all_walls_confident": all(
            wall.confidence >= config.review_gate.min_confidence for wall in walls
        ),
        "topology_within_limit": (
            dangling_ratio <= config.review_gate.max_dangling_endpoint_ratio
        ),
        "source_diagnostics_have_no_errors": not any(
            item.get("severity") == "error" for item in evidence.diagnostics
        ),
        "column_geometry_valid": all(
            column.width_m > 0 and column.depth_m > 0 and column.source_refs
            for column in columns
        ),
            "grid_present_when_required": (
            not config.grid.required
            or (vertical_grid_count >= 2 and horizontal_grid_count >= 2)
        ),
        "wall_collision_free": collision_pair_count == 0,
        "wall_column_collision_free": wall_column_collision_pair_count == 0,
        "specification_conflicts_free": specification_conflict_count == 0,
        "beam_elevation_conflicts_free": beam_elevation_conflict_count == 0,
    }
    errors = [name for name, passed in checks.items() if not passed]
    gate = ReviewGate(
        status="pass" if not errors else "fail", checks=checks, errors=errors,
        standard_violations=[item.as_dict() for item in standard_violations],
        diagnostics=[
            *[
                item for item in evidence.diagnostics
                if item.get("code") in {
                    "PDF_SCALE_INFERRED",
                    "PDF_OCR_TEXT_EXTRACTED",
                    "PDF_OCR_NO_TEXT",
                    "PDF_OCR_UNAVAILABLE",
                    "PDF_OCR_FAILED",
                }
            ],
            *specification_diagnostics,
        ],
        metrics={
            "source_entity_count": len(source_entities.entities),
            "wall_evidence_count": len(evidence.items),
            "wall_count": len(walls),
            "junction_count": len(junctions),
            "column_count": len(columns),
            "grid_axis_count": len(grid),
            "grid_x_axis_count": vertical_grid_count,
            "grid_y_axis_count": horizontal_grid_count,
            "wall_candidate_count_before_grid_frame": candidate_count_before_frame,
            "wall_rejected_outside_grid_frame_count": rejected_outside_grid_count,
            "trimmed_junction_endpoint_count": trimmed_junction_endpoints,
            "parallel_wall_overlap_removed_count": parallel_overlap_removed_count,
            "wall_collision_pair_count": collision_pair_count,
            "wall_collision_max_overlap_area_m2": round(maximum_collision_area_m2, 9),
            "wall_column_collision_pair_count": wall_column_collision_pair_count,
            "wall_column_collision_max_overlap_area_m2": round(
                wall_column_maximum_overlap_area_m2, 9
            ),
            **column_collision_metrics,
            "column_supported_endpoint_count": sum(
                1
                for wall in walls
                for endpoint in (wall.start_m, wall.end_m)
                if any(
                    footprint.distance(Point(endpoint))
                    <= config.wall.junction_tolerance_m
                    for footprint in column_footprints
                )
            ),
            "connected_endpoint_ratio": round(1.0 - dangling_ratio, 6),
            "opening_label_count": len(openings),
            "opening_cut_ready_count": sum(item.cut_status == "ready" for item in openings),
            "opening_cut_pending_count": sum(item.cut_status != "ready" for item in openings),
            "opening_geometry_match_count": sum(
                1 for opening in openings if opening.status == "matched"
            ),
            "opening_review_required_count": sum(
                1 for opening in openings if opening.status == "review_required"
            ),
            "modeling_standard_violation_count": len(standard_violations),
            "specification_resolved_count": sum(
                1 for item in (*walls, *columns, *beams)
                if item.construction is not None
                and item.construction.specification_status.startswith("resolved")
            ),
            "specification_unresolved_count": sum(
                1 for item in (*walls, *columns, *beams)
                if item.construction is not None
                and item.construction.specification_status == "unresolved"
            ),
            "specification_conflict_count": specification_conflict_count,
            "beam_elevation_resolved_count": sum(
                1 for item in beams if item.elevation_status == "resolved"
            ),
            "beam_elevation_level_default_count": sum(
                1 for item in beams if item.elevation_status == "level_default"
            ),
            "beam_elevation_unresolved_count": sum(
                1 for item in beams if item.elevation_status == "unresolved"
            ),
            "beam_elevation_conflict_count": beam_elevation_conflict_count,
        },
    )
    return WallModel(
        tenant_id=config.tenant_id,
        project_id=config.project_id,
        wall_evidence_sha256=canonical_sha256(evidence),
        coordinate_origin=config.coordinate.origin,
        modeling_standard=config.modeling_standard,
        transform_chain=evidence.transform_chain,
        level=config.level,
        grid=grid,
        walls=walls,
        columns=columns,
        beams=beams,
        openings=openings,
        junctions=junctions,
        gate=gate,
        revit=config.revit,
    )
