"""PDF/DWG/DXF source adapters producing one source-entity contract."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from backend.engines.wall_pipeline.contracts import (
    ManifestSource,
    SourceEntities,
    SourceEntity,
    SourceManifest,
    WallPipelineConfig,
)
from backend.engines.wall_pipeline.io import canonical_sha256, file_sha256


# Source adapters run on user-provided files and can otherwise allocate an
# unbounded number of page/entity objects before the deterministic geometry
# gate gets a chance to reject them.  Keep these limits deliberately generous
# for normal architectural sheets; callers/tests may lower them for a
# deployment-specific budget.
_MAX_SOURCE_BYTES = 512 * 1024 * 1024
_MAX_PDF_PAGES = 500
_MAX_PDF_ENTITIES = 500_000

# RapidOCR internally bounds an input image to roughly 2,000 pixels.  Passing
# one complete A0 sheet therefore destroys the small legend/detail text even
# when the PDF was initially rendered at a useful DPI.  Keep the full render
# bounded, then OCR overlapping tiles that remain within the recogniser's
# native input budget.  These constants control text evidence only; they do
# not participate in wall or column geometry.
_PDF_OCR_TARGET_DPI = 150
_PDF_OCR_MAX_RENDER_PIXELS = 60_000_000
# A larger tile keeps the recogniser's effective input below its native
# resize limit while reducing overlapping inference calls on A0/A1 sheets.
# 3,000 px retains the 150 DPI text detail but needs roughly one third as
# many calls as the old 1,900 px tiles on a typical rotated A0 plan.
_PDF_OCR_TILE_SIZE_PX = 3_000
_PDF_OCR_TILE_OVERLAP_PX = 160
_PDF_OCR_MAX_TEXT_ENTITIES = 20_000
_PDF_OCR_CACHE_VERSION = 2


def _default_ocr_cache_dir(output_dir: Path) -> Path:
    """Return a stable cache location shared by task output directories.

    Workflow runs use one directory per task.  Looking for the conventional
    ``wall_pipeline`` segment prevents an identical upload from paying the
    OCR cost again in a new task, while the environment override supports a
    deployment-specific cache volume.
    """

    configured = os.environ.get("WALL_PIPELINE_OCR_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    resolved = Path(output_dir).resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name.lower() == "wall_pipeline":
            return candidate / ".ocr-cache"
    return resolved.parent / ".ocr-cache"


def _ocr_cache_path(
    source: ManifestSource,
    page_index: int,
    *,
    cache_dir: Path | None,
) -> Path | None:
    if cache_dir is None:
        return None
    identity = "|".join((
        str(_PDF_OCR_CACHE_VERSION),
        source.sha256,
        str(page_index + 1),
        str(_PDF_OCR_TARGET_DPI),
        str(_PDF_OCR_TILE_SIZE_PX),
        str(_PDF_OCR_TILE_OVERLAP_PX),
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"{digest}.json"


def _valid_ocr_candidate(value: Any) -> bool:
    """Validate a cached OCR candidate before using it as source evidence."""

    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        return False
    if not value["text"].strip() or not isinstance(value.get("normalized_text"), str):
        return False
    center = value.get("center")
    points = value.get("points")
    if (
        not isinstance(center, list) or len(center) != 2
        or not isinstance(points, list) or not points
    ):
        return False
    try:
        if not all(math.isfinite(float(item)) for item in center):
            return False
        if any(
            not isinstance(point, list) or len(point) != 2
            or not all(math.isfinite(float(item)) for item in point)
            for point in points
        ):
            return False
        confidence = value.get("confidence")
        if confidence is not None and not math.isfinite(float(confidence)):
            return False
    except (TypeError, ValueError):
        return False
    return True


def _read_ocr_cache(
    path: Path | None,
    *,
    source_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], int, int] | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("cache_schema_version") != _PDF_OCR_CACHE_VERSION:
            return None
        if source_sha256 is not None and payload.get("source_sha256") != source_sha256:
            return None
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not all(
            _valid_ocr_candidate(item) for item in candidates
        ):
            return None
        render_dpi = int(payload["render_dpi"])
        tile_count = int(payload["tile_count"])
        if render_dpi < 1 or tile_count < 1:
            return None
        return candidates, render_dpi, tile_count
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        # A cache is an optimisation, never an authority.  Corrupt or
        # partially-written entries are ignored and recomputed from source.
        return None


def _write_ocr_cache(
    path: Path | None,
    *,
    source_sha256: str,
    candidates: list[dict[str, Any]],
    render_dpi: int,
    tile_count: int,
) -> None:
    if path is None:
        return
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({
                "cache_schema_version": _PDF_OCR_CACHE_VERSION,
                "source_sha256": source_sha256,
                "render_dpi": render_dpi,
                "tile_count": tile_count,
                "candidates": candidates,
            }, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
        temporary = None
    except OSError:
        # OCR output remains valid even when a read-only or full cache volume
        # prevents persistence.  The next run simply recomputes it.
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def max_pdf_pages() -> int:
    """Return the active PDF page budget for adapters and audit renderers."""

    return _MAX_PDF_PAGES


def validate_source_file(path: Path, *, source_kind: str) -> int:
    """Validate a local source before handing it to a third-party parser.

    The returned size is the pre-read snapshot used by the manifest builder.
    Keeping this check at the source boundary prevents a large or empty file
    from being handed to a parser (or fully hashed) first.
    """

    if not path.exists():
        raise FileNotFoundError(f"{source_kind}_SOURCE_NOT_FOUND: {path}")
    if not path.is_file():
        raise ValueError(f"{source_kind}_SOURCE_NOT_A_FILE: {path}")
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"{source_kind}_SOURCE_STAT_FAILED: {path}") from exc
    if size_bytes <= 0:
        raise ValueError(f"{source_kind}_SOURCE_EMPTY: {path}")
    if size_bytes > _MAX_SOURCE_BYTES:
        raise ValueError(
            f"{source_kind}_SOURCE_TOO_LARGE: {path} ({size_bytes} bytes; "
            f"limit {_MAX_SOURCE_BYTES})"
        )
    return size_bytes


# Kept for callers that imported the old private helper while the adapter was
# still experimental.  New code should use the public name above.
_validate_source_file = validate_source_file


def verify_source_file_against_manifest(source: ManifestSource) -> int:
    """Ensure the bytes about to be parsed still match the source manifest.

    Manifest creation and adapter execution are separate stages (and may be
    separated by a queue or a human pause).  Re-checking the content hash at
    this boundary prevents a replaced source from producing artifacts that are
    falsely attributed to the original manifest.  The returned size is the
    post-manifest snapshot for callers that need an audit metric.
    """

    path = Path(source.path)
    source_kind = path.suffix.lstrip(".").upper() or "SOURCE"
    try:
        size_bytes = validate_source_file(path, source_kind=source_kind)
        actual_sha256 = file_sha256(path)
    except Exception as exc:
        raise RuntimeError(
            f"{source_kind}_SOURCE_CHANGED_AFTER_MANIFEST: {path}: "
            f"{type(exc).__name__}"
        ) from exc
    if size_bytes != source.size_bytes:
        raise RuntimeError(
            # Keep the broad changed-source code in the message as well as the
            # size-specific detail.  Callers that only understand the original
            # ``*_SOURCE_CHANGED_AFTER_MANIFEST`` contract must still classify
            # this as a source mutation, while newer diagnostics can surface
            # the exact reason.
            f"{source_kind}_SOURCE_SIZE_CHANGED_AFTER_MANIFEST: {path}; "
            f"{source_kind}_SOURCE_CHANGED_AFTER_MANIFEST"
        )
    if actual_sha256 != source.sha256:
        raise RuntimeError(
            f"{source_kind}_SOURCE_CHANGED_AFTER_MANIFEST: {path}"
        )
    return size_bytes


def _entity_id(source_file_id: str, locator: str) -> str:
    digest = hashlib.sha256(f"{source_file_id}:{locator}".encode("utf-8")).hexdigest()
    return "entity_" + digest[:20]


def _point(value: Any) -> tuple[float, float]:
    return float(value[0]), float(value[1])


def _pdf_point(value: Any, page_height_pt: float) -> tuple[float, float]:
    """Convert PyMuPDF's top-left/y-down point to a page-local y-up point.

    ``Page.get_drawings`` and ``Page.get_text`` expose coordinates in the
    unrotated PDF media box with the origin at the top-left.  The downstream
    geometry and Revit contracts use a Cartesian (bottom-left, y-up) frame.
    Keeping this conversion at the source boundary prevents every later stage
    from having to guess which convention a point uses.
    """

    x, y = _point(value)
    return x, float(page_height_pt) - y


def _pdf_frame_metadata(page: Any, frame_id: str) -> dict[str, Any]:
    """Return deterministic metadata needed to replay a PDF page transform."""

    # ``mediabox`` remains in the unrotated page frame even when the page has
    # a /Rotate entry; that is exactly the frame used by ``get_drawings``.
    media_box = page.mediabox
    return {
        "coordinate_frame": "pdf_media_box_y_up",
        "frame_id": frame_id,
        "page_width_pt": float(media_box.width),
        "page_height_pt": float(media_box.height),
        "page_rotation_deg": int(page.rotation or 0) % 360,
        "page_media_box_pt": [
            float(media_box.x0), float(media_box.y0),
            float(media_box.x1), float(media_box.y1),
        ],
    }


def _ocr_pdf_text_entities(
    page: Any,
    *,
    source: ManifestSource,
    page_index: int,
    page_height_pt: float,
    frame_id: str,
    entity_offset: int,
    cache_dir: Path | None = None,
) -> tuple[list[SourceEntity], dict[str, Any] | None]:
    """Return OCR text evidence for vectorised/scanned sheets with no text map.

    OCR is deliberately emitted as ``kind=text`` only.  The geometry engine
    may use it to resolve a legend or detail specification, but it can never
    create or move a wall/column coordinate.
    """

    try:
        import io
        import numpy as np
        import pymupdf as fitz
        from PIL import Image

        cache_path = _ocr_cache_path(
            source, page_index, cache_dir=cache_dir
        )
        cached = _read_ocr_cache(
            cache_path, source_sha256=source.sha256
        )
        cache_hit = cached is not None
        if cached is not None:
            candidates, render_dpi, tile_count = cached
        else:
            from backend.engines.pdf_parser import _get_ocr
            engine = _get_ocr()
            if engine is None:
                return [], {
                    "severity": "warning", "code": "PDF_OCR_UNAVAILABLE",
                    "source_file_id": source.source_file_id,
                    "page_no": page_index + 1,
                }

            # Render the rotated display page at a useful text DPI.  For
            # unusually large media boxes reduce the DPI before allocating the
            # pixmap rather than resizing the finished image and losing small
            # text.
            page_rect = page.rect
            target_scale = _PDF_OCR_TARGET_DPI / 72.0
            target_pixels = (
                float(page_rect.width) * target_scale
                * float(page_rect.height) * target_scale
            )
            if target_pixels > _PDF_OCR_MAX_RENDER_PIXELS:
                target_scale *= math.sqrt(
                    _PDF_OCR_MAX_RENDER_PIXELS / target_pixels
                )
            render_dpi = max(72, int(math.floor(target_scale * 72.0)))
            pixmap = page.get_pixmap(dpi=render_dpi, alpha=False)
            image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
            scale_x = float(image.width) / max(float(page_rect.width), 1.0)
            scale_y = float(image.height) / max(float(page_rect.height), 1.0)
            tile_size = max(256, int(_PDF_OCR_TILE_SIZE_PX))
            overlap = min(
                max(0, int(_PDF_OCR_TILE_OVERLAP_PX)), tile_size // 3
            )
            stride = max(1, tile_size - overlap)

            def starts(length: int) -> list[int]:
                if length <= tile_size:
                    return [0]
                values = list(range(0, max(1, length - tile_size + 1), stride))
                last = length - tile_size
                if values[-1] != last:
                    values.append(last)
                return values

            # Candidate coordinates are first mapped from the rotated pixmap
            # back into PyMuPDF's unrotated media-box frame.  Only then are
            # they changed to the pipeline's bottom-left/y-up convention.  The
            # old width/height ratio mapping was incorrect for /Rotate 90/270.
            candidates = []
            tile_count = 0
            for tile_y in starts(image.height):
                for tile_x in starts(image.width):
                    tile_count += 1
                    crop = image.crop((
                        tile_x,
                        tile_y,
                        min(tile_x + tile_size, image.width),
                        min(tile_y + tile_size, image.height),
                    ))
                    result, _ = engine(np.asarray(crop))
                    if not result:
                        continue
                    for line in result:
                        if len(line) < 2 or not str(line[1]).strip():
                            continue
                        box = line[0]
                        if not box:
                            continue
                        media_points: list[tuple[float, float]] = []
                        for point in box:
                            rotated_point = fitz.Point(
                                (tile_x + float(point[0])) / scale_x,
                                (tile_y + float(point[1])) / scale_y,
                            )
                            media_point = rotated_point * page.derotation_matrix
                            media_points.append((
                                min(max(float(media_point.x), 0.0), float(page.mediabox.width)),
                                min(max(float(media_point.y), 0.0), page_height_pt),
                            ))
                        center = (
                            sum(point[0] for point in media_points) / len(media_points),
                            sum(point[1] for point in media_points) / len(media_points),
                        )
                        text = str(line[1]).strip()
                        confidence = (
                            float(line[2]) if len(line) > 2 and line[2] is not None
                            else None
                        )
                        candidates.append({
                            "text": text,
                            "normalized_text": re.sub(r"\s+", "", text).casefold(),
                            "center": center,
                            "points": media_points,
                            "confidence": confidence,
                        })

        if not candidates:
            return [], {
                "severity": "warning", "code": "PDF_OCR_NO_TEXT",
                "source_file_id": source.source_file_id,
                "page_no": page_index + 1,
            }

        # Overlapping tiles deliberately see the same words.  Prefer the
        # highest-confidence copy within a small media-box distance while
        # retaining legitimately repeated labels elsewhere on the sheet.
        candidates.sort(
            key=lambda item: (
                item["normalized_text"],
                item["center"][0], item["center"][1],
                -(item["confidence"] or 0.0),
            )
        )
        unique: list[dict[str, Any]] = []
        for candidate in candidates:
            duplicate_index = next((
                index for index, existing in enumerate(unique)
                if existing["normalized_text"] == candidate["normalized_text"]
                and math.hypot(
                    existing["center"][0] - candidate["center"][0],
                    existing["center"][1] - candidate["center"][1],
                ) <= 5.0
            ), None)
            if duplicate_index is None:
                unique.append(candidate)
            elif ((candidate["confidence"] or 0.0)
                  > (unique[duplicate_index]["confidence"] or 0.0)):
                unique[duplicate_index] = candidate
        if len(unique) > _PDF_OCR_MAX_TEXT_ENTITIES:
            raise ValueError(
                "PDF_OCR_ENTITY_LIMIT_EXCEEDED: "
                f"{source.source_file_id}:page:{page_index + 1} "
                f"({len(unique)}; limit {_PDF_OCR_MAX_TEXT_ENTITIES})"
            )
        if not cache_hit:
            _write_ocr_cache(
                cache_path,
                source_sha256=source.sha256,
                candidates=unique,
                render_dpi=render_dpi,
                tile_count=tile_count,
            )

        entities: list[SourceEntity] = []
        for index, candidate in enumerate(unique):
            points = candidate["points"]
            center = candidate["center"]
            locator = f"page:{page_index + 1}/ocr:{entity_offset + index}"
            y_up_points = [
                _pdf_point(point, page_height_pt) for point in points
            ]
            entities.append(SourceEntity(
                entity_id=_entity_id(source.source_file_id, locator),
                source_file_id=source.source_file_id,
                page_no=page_index + 1,
                kind="text",
                layer="OCR",
                geometry={
                    "point": _pdf_point(center, page_height_pt),
                    "bbox": [
                        min(point[0] for point in y_up_points),
                        min(point[1] for point in y_up_points),
                        max(point[0] for point in y_up_points),
                        max(point[1] for point in y_up_points),
                    ],
                    "text": candidate["text"],
                },
                style={
                    "source": "rapidocr",
                    "confidence": candidate["confidence"],
                    "render_dpi": render_dpi,
                    **_pdf_frame_metadata(page, frame_id),
                },
                locator=locator,
                frame_id=frame_id,
            ))
        return entities, {
            "severity": "info", "code": "PDF_OCR_TEXT_EXTRACTED",
            "source_file_id": source.source_file_id,
            "page_no": page_index + 1,
            "entity_count": len(entities),
            "tile_count": tile_count,
            "render_dpi": render_dpi,
            "page_rotation_deg": int(page.rotation or 0) % 360,
            "cache_hit": cache_hit,
        }
    except Exception as exc:
        return [], {
            "severity": "warning", "code": "PDF_OCR_FAILED",
            "source_file_id": source.source_file_id,
            "page_no": page_index + 1,
            "detail": f"{type(exc).__name__}: {exc}"[:500],
        }


class PDFSourceAdapter:
    def __init__(self, config: WallPipelineConfig | None = None):
        # Keep the no-argument form used by older callers/tests.  The manifest
        # carries the same immutable wall scope, so a caller that does not
        # provide the config still gets identical filtering semantics.
        self.config = config

    def extract(self, manifest: SourceManifest, output_dir: Path) -> SourceEntities:
        import pymupdf as fitz

        entities: list[SourceEntity] = []
        diagnostics: list[dict[str, Any]] = []
        pdf_entity_count = 0
        wall_config = self.config.wall if self.config is not None else manifest.wall
        column_config = self.config.column if self.config is not None else manifest.column
        beam_config = self.config.beam if self.config is not None else manifest.beam
        grid_config = self.config.grid if self.config is not None else manifest.grid
        try:
            wall_include_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in wall_config.include_layers
            )
            wall_exclude_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in wall_config.exclude_layers
            )
            column_include_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in column_config.include_layers
            )
            column_exclude_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in column_config.exclude_layers
            )
            column_profile_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in column_config.profile_layers
            )
            column_label_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in column_config.label_layers
            )
            opening_label_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in (
                    self.config.opening.label_layers
                    if self.config is not None else manifest.opening.label_layers
                )
            )
            opening_boundary_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in (
                    self.config.opening.boundary_layers
                    if self.config is not None else manifest.opening.boundary_layers
                )
            )
            beam_line_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in beam_config.line_layers
            )
            beam_label_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in beam_config.label_layers
            )
            beam_exclude_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in beam_config.exclude_layers
            )
            opening_exclude_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in (
                    self.config.opening.exclude_layers
                    if self.config is not None else manifest.opening.exclude_layers
                )
            )
            grid_include_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in grid_config.include_layers
            )
            grid_exclude_patterns = tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in grid_config.exclude_layers
            )
        except re.error as exc:
            raise ValueError(f"invalid layer regular expression: {exc}") from exc

        def layer_selected(layer: str) -> bool:
            # An empty include list preserves the historical PDF adapter
            # behaviour (emit every source entity).  Once a wall scope is
            # configured, reject a drawing before iterating its items so very
            # large consultant sheets cannot exhaust the entity budget on
            # annotations, hatches or title-block paths.
            # Preserve the historical empty wall-scope behaviour, while
            # allowing a configured column scope to be emitted alongside it.
            wall_selected = (
                not wall_include_patterns
                or (
                    any(pattern.search(layer) for pattern in wall_include_patterns)
                    and not any(pattern.search(layer) for pattern in wall_exclude_patterns)
                )
            )
            column_selected = bool(column_include_patterns) and (
                any(pattern.search(layer) for pattern in column_include_patterns)
                and not any(pattern.search(layer) for pattern in column_exclude_patterns)
            )
            column_profile_selected = bool(column_profile_patterns) and any(
                pattern.search(layer) for pattern in column_profile_patterns
            )
            column_label_selected = bool(column_label_patterns) and any(
                pattern.search(layer) for pattern in column_label_patterns
            )
            opening_label_selected = bool(opening_label_patterns) and (
                any(pattern.search(layer) for pattern in opening_label_patterns)
                and not any(pattern.search(layer) for pattern in opening_exclude_patterns)
            )
            opening_boundary_selected = bool(opening_boundary_patterns) and (
                any(pattern.search(layer) for pattern in opening_boundary_patterns)
                and not any(pattern.search(layer) for pattern in opening_exclude_patterns)
            )
            beam_line_selected = bool(beam_line_patterns) and (
                any(pattern.search(layer) for pattern in beam_line_patterns)
                and not any(pattern.search(layer) for pattern in beam_exclude_patterns)
            )
            beam_label_selected = bool(beam_label_patterns) and (
                any(pattern.search(layer) for pattern in beam_label_patterns)
                and not any(pattern.search(layer) for pattern in beam_exclude_patterns)
            )
            grid_selected = bool(grid_include_patterns) and (
                any(pattern.search(layer) for pattern in grid_include_patterns)
                and not any(pattern.search(layer) for pattern in grid_exclude_patterns)
            )
            return (
                wall_selected
                or column_selected
                or column_profile_selected
                or column_label_selected
                or opening_label_selected
                or opening_boundary_selected
                or beam_line_selected
                or beam_label_selected
                or grid_selected
            )

        skipped_drawings = 0
        skipped_items = 0
        for source in manifest.sources:
            path = Path(source.path)
            if path.suffix.lower() != ".pdf":
                raise ValueError(f"PDF source must have .pdf extension: {path}")
            verify_source_file_against_manifest(source)
            try:
                document = fitz.open(path)
            except Exception as exc:
                raise ValueError(
                    f"PDF_OPEN_FAILED: {path}: {type(exc).__name__}"
                ) from exc
            try:
                # Read the parser-reported count once.  Besides avoiding a
                # mutable-property race, this lets us fail closed for corrupt
                # or negative counts before constructing page indexes.
                try:
                    page_count = int(document.page_count)
                except Exception as exc:
                    raise RuntimeError(
                        f"PDF_PAGE_COUNT_FAILED: {path}"
                    ) from exc
                if page_count < 0:
                    raise RuntimeError(
                        f"PDF_PAGE_COUNT_INVALID: {path} ({page_count})"
                    )
                if source.page_no is not None:
                    page_indexes = [source.page_no - 1]
                else:
                    # Keep this as a range until the limit is checked; a
                    # corrupt page count must not first allocate a massive
                    # Python list.
                    if page_count > _MAX_PDF_PAGES:
                        raise ValueError(
                            f"PDF_PAGE_LIMIT_EXCEEDED: {path} ({page_count} pages; "
                            f"limit {_MAX_PDF_PAGES})"
                        )
                    page_indexes = range(page_count)
                if len(page_indexes) > _MAX_PDF_PAGES:
                    raise ValueError(
                        f"PDF_PAGE_LIMIT_EXCEEDED: {path} ({len(page_indexes)} pages; "
                        f"limit {_MAX_PDF_PAGES})"
                    )
                for page_index in page_indexes:
                    if not 0 <= page_index < page_count:
                        raise ValueError(
                            f"page {page_index + 1} does not exist in {path}"
                        )
                    page = document[page_index]
                    frame_id = f"{source.source_file_id}:page:{page_index + 1:04d}"
                    page_height_pt = float(page.mediabox.height)
                    frame_metadata = _pdf_frame_metadata(page, frame_id)
                    drawing_index = 0
                    try:
                        drawings = page.get_drawings()
                    except Exception as exc:
                        raise RuntimeError(
                            "PDF_DRAWING_EXTRACTION_FAILED: "
                            f"{source.source_file_id}:page:{page_index + 1}"
                        ) from exc
                    for drawing in drawings:
                        layer = str(drawing.get("layer") or "")
                        if not layer_selected(layer):
                            skipped_drawings += 1
                            skipped_items += len(drawing.get("items") or [])
                            continue
                        style = {
                            "stroke_width": float(drawing.get("width") or 0.0),
                            "stroke_color": drawing.get("color"),
                            "fill_color": drawing.get("fill"),
                            "dashes": str(drawing.get("dashes") or ""),
                            # Preserve the flattened PDF drawing group so
                            # line-only profiles can be rebuilt locally.
                            "drawing_index": drawing_index,
                            **frame_metadata,
                        }
                        for item_index, item in enumerate(drawing.get("items") or []):
                            pdf_entity_count += 1
                            if pdf_entity_count > _MAX_PDF_ENTITIES:
                                raise ValueError(
                                    "PDF_ENTITY_LIMIT_EXCEEDED: "
                                    f"{path} (limit {_MAX_PDF_ENTITIES})"
                                )
                            locator = f"page:{page_index + 1}/drawing:{drawing_index}/item:{item_index}"
                            kind = item[0]
                            if kind == "l":
                                geometry = {
                                    "start": _pdf_point(item[1], page_height_pt),
                                    "end": _pdf_point(item[2], page_height_pt),
                                }
                                entity_kind = "line"
                            elif kind == "re":
                                rect = item[1]
                                geometry = {"points": [
                                    _pdf_point((rect.x0, rect.y0), page_height_pt),
                                    _pdf_point((rect.x1, rect.y0), page_height_pt),
                                    _pdf_point((rect.x1, rect.y1), page_height_pt),
                                    _pdf_point((rect.x0, rect.y1), page_height_pt),
                                    _pdf_point((rect.x0, rect.y0), page_height_pt),
                                ]}
                                entity_kind = "polyline"
                            elif kind == "qu":
                                quad = item[1]
                                geometry = {"points": [
                                    _pdf_point(quad.ul, page_height_pt),
                                    _pdf_point(quad.ur, page_height_pt),
                                    _pdf_point(quad.lr, page_height_pt),
                                    _pdf_point(quad.ll, page_height_pt),
                                    _pdf_point(quad.ul, page_height_pt),
                                ]}
                                entity_kind = "polyline"
                            else:
                                diagnostics.append({
                                    "severity": "info",
                                    "code": "PDF_CURVE_NOT_WALL_DECISIVE",
                                    "source_file_id": source.source_file_id,
                                    "locator": locator,
                                    "item_kind": str(kind),
                                })
                                continue
                            entities.append(SourceEntity(
                                entity_id=_entity_id(source.source_file_id, locator),
                                source_file_id=source.source_file_id,
                                page_no=page_index + 1,
                                kind=entity_kind,
                                layer=layer,
                                geometry=geometry,
                                style=style,
                                locator=locator,
                                frame_id=frame_id,
                            ))
                        drawing_index += 1
                    try:
                        words = page.get_text("words")
                    except Exception as exc:
                        raise RuntimeError(
                            "PDF_TEXT_EXTRACTION_FAILED: "
                            f"{source.source_file_id}:page:{page_index + 1}"
                        ) from exc
                    for word_index, word in enumerate(words):
                        pdf_entity_count += 1
                        if pdf_entity_count > _MAX_PDF_ENTITIES:
                            raise ValueError(
                                "PDF_ENTITY_LIMIT_EXCEEDED: "
                                f"{path} (limit {_MAX_PDF_ENTITIES})"
                            )
                        locator = f"page:{page_index + 1}/word:{word_index}"
                        x0, y0, x1, y1 = map(float, word[:4])
                        entities.append(SourceEntity(
                            entity_id=_entity_id(source.source_file_id, locator),
                            source_file_id=source.source_file_id,
                            page_no=page_index + 1,
                            kind="text",
                            geometry={
                                "point": _pdf_point(
                                    ((x0 + x1) / 2.0, (y0 + y1) / 2.0),
                                    page_height_pt,
                                ),
                                # Preserve the transformed text box for OCR/
                                # review consumers without changing the legacy
                                # point/text fields.
                                "bbox": [x0, page_height_pt - y1,
                                          x1, page_height_pt - y0],
                                "text": str(word[4]),
                            },
                            style=frame_metadata,
                            locator=locator,
                            frame_id=frame_id,
                        ))
                    if not words:
                        ocr_entities, ocr_diagnostic = _ocr_pdf_text_entities(
                            page,
                            source=source,
                            page_index=page_index,
                            page_height_pt=page_height_pt,
                            frame_id=frame_id,
                            entity_offset=pdf_entity_count,
                            cache_dir=_default_ocr_cache_dir(output_dir),
                        )
                        for entity in ocr_entities:
                            pdf_entity_count += 1
                            if pdf_entity_count > _MAX_PDF_ENTITIES:
                                raise ValueError(
                                    "PDF_ENTITY_LIMIT_EXCEEDED: "
                                    f"{path} (limit {_MAX_PDF_ENTITIES})"
                                )
                            entities.append(entity)
                        if ocr_diagnostic is not None:
                            diagnostics.append(ocr_diagnostic)
            finally:
                document.close()
            # A source may be replaced while the parser is reading it.  The
            # second content check prevents a partially mixed parse from being
            # published under the original manifest identity.
            verify_source_file_against_manifest(source)
        if (
            wall_include_patterns
            or column_include_patterns
            or column_profile_patterns
            or column_label_patterns
            or opening_label_patterns
            or opening_boundary_patterns
            or beam_line_patterns
            or beam_label_patterns
            or grid_include_patterns
        ) and (skipped_drawings or skipped_items):
            diagnostics.append({
                "severity": "info",
                "code": "PDF_ENTITIES_SKIPPED_BY_LAYER_SCOPE",
                "drawing_count": skipped_drawings,
                "item_count": skipped_items,
                "include_layers": [
                    *wall_config.include_layers,
                    *column_config.include_layers,
                    *column_config.profile_layers,
                    *column_config.label_layers,
                    *(self.config.opening.label_layers if self.config is not None else manifest.opening.label_layers),
                    *(self.config.opening.boundary_layers if self.config is not None else manifest.opening.boundary_layers),
                    *grid_config.include_layers,
                    *beam_config.line_layers,
                    *beam_config.label_layers,
                ],
                "exclude_layers": [
                    *wall_config.exclude_layers,
                    *column_config.exclude_layers,
                    *(self.config.opening.exclude_layers if self.config is not None else manifest.opening.exclude_layers),
                    *grid_config.exclude_layers,
                    *beam_config.exclude_layers,
                ],
            })
        return SourceEntities(
            tenant_id=manifest.tenant_id,
            project_id=manifest.project_id,
            manifest_sha256=canonical_sha256(manifest),
            entities=entities,
            source_units={item.source_file_id: "pt" for item in manifest.sources},
            diagnostics=diagnostics,
        )


_DXF_UNIT_NAMES = {
    1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m",
}


_MAX_INSERT_DEPTH = 32
_MAX_EXPANDED_ENTITIES = 100_000


@dataclass(frozen=True)
class _BlockLayerScope:
    """Small, cached summary used before expanding an INSERT.

    ``Insert.virtual_entities`` expands the immediate block cheaply, but it
    recursively materialises every nested INSERT.  Real consultant drawings
    often contain tens of thousands of furniture/detail entities in blocks
    that are irrelevant to wall extraction.  We inspect the block graph first
    and only expand a placement when a configured layer can be reached.  The
    summary keeps layer-0 geometry (BYBLOCK/BYLAYER) separate so it is only
    admitted when the placement itself is on an included layer.
    """

    explicit_match: bool
    zero_geometry_path: bool


def _build_block_layer_scope(
    document: Any,
    include_layers: list[str],
    exclude_layers: list[str],
    *,
    column_include_layers: list[str] | None = None,
    column_exclude_layers: list[str] | None = None,
    additional_layer_scopes: list[tuple[list[str], list[str]]] | None = None,
) -> tuple[
    Callable[[Any, str], bool],
    Callable[[Any, str], bool],
    dict[str, int],
] | None:
    """Build a bounded INSERT expansion predicate for a DXF document.

    The predicate is deliberately opt-in: an empty include list preserves the
    adapter's historical "emit every source entity" behaviour.  Configured
    include/exclude regexes are validated here (at the source boundary), then
    applied to the raw block graph before any expensive virtual expansion.
    """

    column_include_layers = column_include_layers or []
    column_exclude_layers = column_exclude_layers or []
    additional_layer_scopes = additional_layer_scopes or []
    # Keep the historical no-scope behaviour when walls are unconfigured.  A
    # column scope by itself is still opt-in and must not make an otherwise
    # unrestricted CAD import recurse through every block.
    if not include_layers:
        return None
    try:
        scopes = [
            (
                tuple(re.compile(pattern, re.IGNORECASE) for pattern in include_layers),
                tuple(re.compile(pattern, re.IGNORECASE) for pattern in exclude_layers),
            )
        ]
        if column_include_layers:
            scopes.append((
                tuple(re.compile(pattern, re.IGNORECASE) for pattern in column_include_layers),
                tuple(re.compile(pattern, re.IGNORECASE) for pattern in column_exclude_layers),
            ))
        for extra_includes, extra_excludes in additional_layer_scopes:
            if not extra_includes:
                continue
            scopes.append((
                tuple(re.compile(pattern, re.IGNORECASE) for pattern in extra_includes),
                tuple(re.compile(pattern, re.IGNORECASE) for pattern in extra_excludes),
            ))
    except re.error as exc:
        raise ValueError(f"invalid layer regular expression: {exc}") from exc

    def layer_matches(layer: str) -> bool:
        return any(
            any(pattern.search(layer) for pattern in includes)
            and not any(pattern.search(layer) for pattern in excludes)
            for includes, excludes in scopes
        )

    # Materialise each block's direct children once.  Iterating the block
    # collection is bounded by the DXF block table, not by INSERT fan-out.
    records: dict[str, tuple[bool, bool, tuple[tuple[str, str], ...]]] = {}
    try:
        block_iterable = document.blocks
        for block in block_iterable:
            name = str(getattr(block, "name", "") or "")
            if not name:
                continue
            direct_match = False
            zero_geometry = False
            children: list[tuple[str, str]] = []
            for child in block:
                try:
                    kind = child.dxftype()
                    layer = str(getattr(child.dxf, "layer", "") or "")
                except Exception:
                    # A malformed child is still left to the normal adapter
                    # diagnostics if its block is selected; it must not make
                    # the pre-scan recurse without a bound.
                    continue
                if kind == "INSERT":
                    child_name = str(getattr(child.dxf, "name", "") or "")
                    if child_name:
                        children.append((child_name, layer))
                elif layer == "0":
                    zero_geometry = True
                elif layer_matches(layer):
                    direct_match = True
            records[name] = (direct_match, zero_geometry, tuple(children))
    except Exception as exc:
        # A parser-specific block-table failure should be explicit rather than
        # silently disabling the scope filter and reintroducing fan-out.
        raise RuntimeError(f"DXF block scope scan failed: {exc}") from exc

    summary_cache: dict[str, _BlockLayerScope] = {}
    visiting: set[str] = set()

    def summarize(name: str) -> _BlockLayerScope:
        cached = summary_cache.get(name)
        if cached is not None:
            return cached
        if name in visiting:
            # Cycles are handled by the expansion walker as well.  Returning
            # an empty summary here prevents the pre-scan from looping.
            return _BlockLayerScope(False, False)
        record = records.get(name)
        if record is None:
            return _BlockLayerScope(False, False)
        direct_match, zero_geometry, children = record
        visiting.add(name)
        explicit_match = direct_match
        zero_path = zero_geometry
        for child_name, child_layer in children:
            child_summary = summarize(child_name)
            # A nested block's explicit wall layer is independent of the
            # INSERT layer.  A layer-0 path, however, inherits the placement
            # layer and is only relevant when that layer is included.
            explicit_match = explicit_match or child_summary.explicit_match
            child_effective_match = layer_matches(child_layer)
            explicit_match = explicit_match or (
                child_summary.zero_geometry_path and child_effective_match
            )
            zero_path = zero_path or child_summary.zero_geometry_path
        visiting.remove(name)
        result = _BlockLayerScope(explicit_match, zero_path)
        summary_cache[name] = result
        return result

    for block_name in records:
        summarize(block_name)

    skipped: dict[str, int] = {}

    def should_expand(entity: Any, inherited_layer: str = "") -> bool:
        try:
            name = str(getattr(entity.dxf, "name", "") or "")
            layer = str(getattr(entity.dxf, "layer", "") or "")
        except Exception:
            return False
        effective_layer = layer if layer and layer != "0" else inherited_layer
        summary = summary_cache.get(name) or summarize(name)
        allowed = summary.explicit_match or (
            summary.zero_geometry_path and layer_matches(effective_layer)
        )
        if not allowed:
            skipped[name or "<unnamed>"] = skipped.get(name or "<unnamed>", 0) + 1
        return allowed

    def should_emit_entity(entity: Any, inherited_layer: str = "") -> bool:
        # Text is a semantic source for legends, schedules and details.  Keep
        # it even when the geometry scope is narrowed to a wall layer; the
        # downstream specification resolver still cannot use text to create
        # or move coordinates.  Entity and source-size budgets remain active.
        try:
            if str(entity.dxftype()).upper() in {"TEXT", "MTEXT"}:
                return True
            layer = str(getattr(entity.dxf, "layer", "") or "")
        except Exception:
            return False
        effective_layer = layer if layer and layer != "0" else inherited_layer
        return layer_matches(effective_layer)

    return should_expand, should_emit_entity, skipped


def _walk_dxf_entities(
    entities: Iterable[Any], *, from_mline: bool = False,
    _depth: int = 0, _stack: tuple[str, ...] = (),
    _budget: list[int] | None = None,
    insert_filter: Callable[[Any, str], bool] | None = None,
    insert_skip_callback: Callable[[Any], None] | None = None,
    entity_filter: Callable[[Any, str], bool] | None = None,
    entity_skip_callback: Callable[[Any], None] | None = None,
    _inherited_layer: str = "",
):
    """Flatten INSERT/MLINE placements with bounded, diagnosable expansion.

    A malformed/cyclic block reference must not recurse forever in an API
    worker.  ``_stack`` tracks the active INSERT block names (not a global set,
    so two legitimate placements of the same block remain independent), while
    ``_budget`` bounds pathological expansion fan-out.
    """

    budget = _budget if _budget is not None else [0]
    for entity in entities:
        budget[0] += 1
        if budget[0] > _MAX_EXPANDED_ENTITIES:
            yield (
                None, from_mline,
                "DXF placement expansion exceeded the entity limit "
                f"({_MAX_EXPANDED_ENTITIES})",
            )
            return
        try:
            kind = entity.dxftype()
        except (AttributeError, TypeError, ValueError) as exc:
            yield (None, from_mline, f"entity type lookup failed: {exc}")
            continue
        except Exception as exc:
            # Treat parser-provided entity objects as an untrusted boundary;
            # one malformed proxy must become a diagnostic, not abort the
            # whole source extraction worker.
            yield (None, from_mline, f"entity type lookup failed: {exc}")
            continue
        if kind in {"INSERT", "MLINE"}:
            block_name = ""
            if kind == "INSERT":
                try:
                    block_name = str(getattr(entity.dxf, "name", "") or "")
                except (AttributeError, TypeError, ValueError):
                    block_name = ""
                if _depth >= _MAX_INSERT_DEPTH:
                    yield (
                        None, from_mline,
                        f"INSERT expansion depth exceeded ({_MAX_INSERT_DEPTH})"
                        + (f": {block_name}" if block_name else ""),
                    )
                    continue
                if block_name and block_name in _stack:
                    yield (
                        None, from_mline,
                        f"INSERT expansion cycle detected: "
                        f"{' -> '.join((*_stack, block_name))}",
                    )
                    continue
                if insert_filter is not None:
                    try:
                        if not insert_filter(entity, _inherited_layer):
                            if insert_skip_callback is not None:
                                insert_skip_callback(entity)
                            continue
                    except Exception as exc:
                        # Scope predicates are built from the parser's block
                        # table.  If one unexpectedly fails, fail closed and
                        # retain an actionable diagnostic instead of expanding
                        # an unbounded placement.
                        yield (
                            None, from_mline,
                            f"INSERT layer-scope evaluation failed: {exc}",
                        )
                        continue
            elif _depth >= _MAX_INSERT_DEPTH:
                # MLINE virtual entities are normally finite, but malformed
                # proxy objects can return themselves recursively.  Apply the
                # same depth guard used for INSERT expansion to keep this
                # boundary bounded and diagnosable.
                yield (
                    None, from_mline,
                    f"{kind} expansion depth exceeded ({_MAX_INSERT_DEPTH})",
                )
                continue
            try:
                child_entities = entity.virtual_entities()
                child_stack = (*_stack, block_name) if block_name else _stack
                try:
                    entity_layer = str(getattr(entity.dxf, "layer", "") or "")
                except Exception:
                    entity_layer = ""
                child_inherited_layer = (
                    entity_layer if entity_layer and entity_layer != "0"
                    else _inherited_layer
                )
                yield from _walk_dxf_entities(
                    child_entities,
                    from_mline=from_mline or kind == "MLINE",
                    _depth=_depth + 1,
                    _stack=child_stack,
                    _budget=budget,
                    insert_filter=insert_filter,
                    insert_skip_callback=insert_skip_callback,
                    entity_filter=entity_filter,
                    entity_skip_callback=entity_skip_callback,
                    _inherited_layer=child_inherited_layer,
                )
            except (AttributeError, TypeError, ValueError, RecursionError) as exc:
                yield (None, from_mline, f"{kind} expansion failed: {exc}")
            except Exception as exc:
                # ``virtual_entities`` is an external parser boundary.  Make
                # unexpected parser failures explicit in SourceEntities rather
                # than silently discarding the placement.
                yield (None, from_mline, f"{kind} expansion failed: {exc}")
            continue
        if entity_filter is not None:
            try:
                entity_layer = str(getattr(entity.dxf, "layer", "") or "")
            except Exception:
                entity_layer = ""
            try:
                if not entity_filter(entity, _inherited_layer):
                    if entity_skip_callback is not None:
                        entity_skip_callback(entity)
                    continue
            except Exception as exc:
                yield (
                    None, from_mline,
                    f"DXF layer-scope evaluation failed: {exc}",
                )
                continue
        yield entity, from_mline, None


class DXFSourceAdapter:
    def __init__(self, config: WallPipelineConfig):
        self.config = config

    def _convert_dwg(self, source: ManifestSource, output_dir: Path) -> Path:
        converter = self.config.source.converter
        executable = (
            converter.executable if converter and converter.executable
            else os.environ.get("ODA_FILE_CONVERTER", "")
        )
        if not executable or not Path(executable).is_file():
            raise RuntimeError(
                "ODA File Converter is required for DWG input; configure "
                "source.converter.executable or ODA_FILE_CONVERTER"
            )
        stage = output_dir / "oda" / source.source_file_id
        input_dir, converted_dir = stage / "input", stage / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        converted_dir.mkdir(parents=True, exist_ok=True)
        staged_source = input_dir / Path(source.path).name
        try:
            shutil.copy2(source.path, staged_source)
        except OSError as exc:
            raise RuntimeError(
                f"DWG_SOURCE_STAGING_FAILED: {source.source_file_id}"
            ) from exc
        # The source can change between the manifest check and copy2.  Verify
        # the bytes handed to ODA, not just the original path we inspected.
        try:
            staged_sha256 = file_sha256(staged_source)
        except Exception as exc:
            raise RuntimeError(
                f"DWG_SOURCE_STAGING_HASH_FAILED: {source.source_file_id}"
            ) from exc
        if staged_sha256 != source.sha256:
            raise RuntimeError(
                f"DWG_SOURCE_CHANGED_AFTER_MANIFEST: {source.path}"
            )
        values = {
            "input_dir": str(input_dir),
            "output_dir": str(converted_dir),
            "output_version": converter.output_version if converter else "ACAD2018",
            "output_type": "DXF",
            "recursive": "0",
            "audit": "1",
        }
        template = converter.command if converter and converter.command else [
            "{input_dir}", "{output_dir}", "{output_version}",
            "{output_type}", "{recursive}", "{audit}",
        ]
        command = [str(executable), *[token.format(**values) for token in template]]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=converter.timeout_seconds if converter else 300,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "ODA DWG conversion timed out after %ss"
                % (converter.timeout_seconds if converter else 300)
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"ODA DWG conversion could not start: {exc}") from exc
        expected = converted_dir / (staged_source.stem + ".dxf")
        if result.returncode != 0 or not expected.is_file():
            detail = (result.stderr or result.stdout or "no converter output")[-1000:]
            raise RuntimeError(f"ODA DWG conversion failed: {detail}")
        return expected

    def extract(self, manifest: SourceManifest, output_dir: Path) -> SourceEntities:
        import ezdxf

        entities: list[SourceEntity] = []
        diagnostics: list[dict[str, Any]] = []
        source_units: dict[str, str] = {}
        for source in manifest.sources:
            path = Path(source.path)
            if path.suffix.lower() == ".dwg":
                verify_source_file_against_manifest(source)
                dxf_path = self._convert_dwg(source, output_dir)
                validate_source_file(dxf_path, source_kind="DXF")
                diagnostics.append({
                    "severity": "info", "code": "ODA_CONVERSION_SUCCEEDED",
                    "source_file_id": source.source_file_id,
                    "converted_path": str(dxf_path.resolve()),
                })
            elif path.suffix.lower() == ".dxf":
                verify_source_file_against_manifest(source)
                dxf_path = path
            else:
                raise ValueError(f"DWG/DXF adapter cannot read {path.suffix}: {path}")
            try:
                document = ezdxf.readfile(dxf_path)
            except Exception as exc:
                raise RuntimeError(
                    f"DXF parsing failed for {path.name}: {str(exc)[:500]}"
                ) from exc
            try:
                unit_code = int(document.header.get("$INSUNITS", 0) or 0)
            except (TypeError, ValueError) as exc:
                diagnostics.append({
                    "severity": "error", "code": "DXF_UNITS_INVALID",
                    "source_file_id": source.source_file_id,
                    "detail": str(exc),
                })
                unit_code = 0
            source_units[source.source_file_id] = _DXF_UNIT_NAMES.get(unit_code, "unitless")
            if source_units[source.source_file_id] == "unitless":
                diagnostics.append({
                    "severity": "warning",
                    "code": "DXF_UNITS_UNRESOLVED",
                    "source_file_id": source.source_file_id,
                    "insunits_code": unit_code,
                    "guidance": "set coordinate.source_unit or coordinate.scale_to_m explicitly",
                })
            try:
                auditor = document.audit()
            except Exception as exc:
                raise RuntimeError(
                    f"DXF audit failed for {path.name}: {str(exc)[:500]}"
                ) from exc
            if auditor.errors:
                diagnostics.append({
                    "severity": "warning", "code": "DXF_AUDIT_ERRORS",
                    "source_file_id": source.source_file_id,
                    "count": len(auditor.errors),
                })
            try:
                modelspace = document.modelspace()
            except Exception as exc:
                raise RuntimeError(
                    f"DXF modelspace extraction failed for {path.name}: "
                    f"{str(exc)[:500]}"
                ) from exc
            # Layer scope is applied before recursive INSERT expansion.  This
            # is the critical guard for real consultant drawings: a plan block
            # may reference furniture/detail blocks containing hundreds of
            # thousands of entities that can never become wall evidence.
            scope_result = _build_block_layer_scope(
                document,
                self.config.wall.include_layers,
                self.config.wall.exclude_layers,
                column_include_layers=self.config.column.include_layers,
                column_exclude_layers=self.config.column.exclude_layers,
                additional_layer_scopes=[
                    (self.config.column.profile_layers, []),
                    (self.config.column.label_layers, []),
                    (self.config.grid.include_layers, self.config.grid.exclude_layers),
                    (self.config.opening.label_layers, self.config.opening.exclude_layers),
                    (self.config.opening.boundary_layers, self.config.opening.exclude_layers),
                    (self.config.beam.line_layers, self.config.beam.exclude_layers),
                    (self.config.beam.label_layers, self.config.beam.exclude_layers),
                ],
            )
            insert_filter = scope_result[0] if scope_result else None
            entity_filter = scope_result[1] if scope_result else None
            skipped_inserts = scope_result[2] if scope_result else None
            skipped_entities: dict[str, int] = {}

            def record_skipped_entity(entity: Any) -> None:
                try:
                    kind = str(entity.dxftype())
                except Exception:
                    kind = "<invalid>"
                skipped_entities[kind] = skipped_entities.get(kind, 0) + 1

            item_index = 0
            for entity, from_mline, expansion_error in _walk_dxf_entities(
                modelspace,
                insert_filter=insert_filter,
                insert_skip_callback=None,
                entity_filter=entity_filter,
                entity_skip_callback=record_skipped_entity,
            ):
                if expansion_error:
                    diagnostics.append({
                        "severity": "error", "code": "DXF_PLACEMENT_EXPANSION_FAILED",
                        "source_file_id": source.source_file_id,
                        "detail": expansion_error,
                    })
                    continue
                try:
                    kind = entity.dxftype()
                except (AttributeError, TypeError, ValueError) as exc:
                    diagnostics.append({
                        "severity": "error", "code": "DXF_ENTITY_TYPE_INVALID",
                        "source_file_id": source.source_file_id,
                        "detail": str(exc),
                    })
                    item_index += 1
                    continue
                handle = str(getattr(entity.dxf, "handle", "") or "virtual")
                locator = f"entity:{handle}/placed:{item_index}"
                layer = str(getattr(entity.dxf, "layer", "") or "")
                style = {
                    "color": int(getattr(entity.dxf, "color", 256) or 256),
                    "linetype": str(getattr(entity.dxf, "linetype", "BYLAYER") or "BYLAYER"),
                }
                payload: SourceEntity | None = None
                if kind == "LINE":
                    payload = SourceEntity(
                        entity_id=_entity_id(source.source_file_id, locator),
                        source_file_id=source.source_file_id,
                        kind="mline" if from_mline else "line", layer=layer,
                        geometry={"start": _point(entity.dxf.start),
                                  "end": _point(entity.dxf.end)},
                        style=style, locator=locator,
                        frame_id=source.source_file_id,
                    )
                elif kind in {"LWPOLYLINE", "POLYLINE"}:
                    if kind == "LWPOLYLINE":
                        points = [_point(point) for point in entity.get_points("xy")]
                        closed = bool(entity.closed)
                    else:
                        points = [_point(vertex.dxf.location) for vertex in entity.vertices]
                        closed = bool(entity.is_closed)
                    if closed and points and points[0] != points[-1]:
                        points.append(points[0])
                    if len(points) >= 2:
                        payload = SourceEntity(
                            entity_id=_entity_id(source.source_file_id, locator),
                            source_file_id=source.source_file_id,
                            kind="polyline", layer=layer,
                            geometry={"points": points, "closed": closed},
                            style=style, locator=locator,
                            frame_id=source.source_file_id,
                        )
                elif kind == "HATCH":
                    # ``from_hatch`` applies the placement transform of a
                    # virtual HATCH and expands both polyline and edge paths.
                    # Reading ``entity.paths`` directly drops curved/edge
                    # boundaries and, on inserted detail blocks, can leave
                    # points in block-local coordinates.  Those two cases are
                    # exactly where labelled L/T boundary-column profiles live
                    # in consultant structural drawings.
                    from ezdxf.path import from_hatch

                    for path_index, hatch_path in enumerate(from_hatch(entity)):
                        try:
                            points = [
                                (float(point.x), float(point.y))
                                for point in hatch_path.flattening(1.0)
                            ]
                        except (AttributeError, TypeError, ValueError) as exc:
                            diagnostics.append({
                                "severity": "warning",
                                "code": "HATCH_BOUNDARY_FLATTEN_FAILED",
                                "source_file_id": source.source_file_id,
                                "locator": locator,
                                "detail": str(exc)[:500],
                            })
                            continue
                        if len(points) < 3:
                            continue
                        if points[0] != points[-1]:
                            points.append(points[0])
                        boundary_locator = f"{locator}/boundary:{path_index}"
                        entities.append(SourceEntity(
                            entity_id=_entity_id(source.source_file_id, boundary_locator),
                            source_file_id=source.source_file_id,
                            kind="hatch_boundary", layer=layer,
                            geometry={"points": points, "closed": True},
                            style=style, locator=boundary_locator,
                            frame_id=source.source_file_id,
                        ))
                elif kind in {"TEXT", "MTEXT"}:
                    text = str(entity.dxf.text if kind == "TEXT" else entity.text)
                    payload = SourceEntity(
                        entity_id=_entity_id(source.source_file_id, locator),
                        source_file_id=source.source_file_id,
                        kind="text", layer=layer,
                        geometry={"point": _point(entity.dxf.insert), "text": text},
                        style=style, locator=locator,
                        frame_id=source.source_file_id,
                    )
                else:
                    # Keep unsupported entities visible to review/diagnostic
                    # consumers.  They are intentionally not promoted to wall
                    # geometry, but silently dropping them makes coverage
                    # impossible to assess on real drawings.
                    diagnostics.append({
                        "severity": "warning",
                        "code": "UNSUPPORTED_DXF_ENTITY",
                        "source_file_id": source.source_file_id,
                        "entity_type": kind,
                        "locator": locator,
                        "layer": layer,
                    })
                if payload is not None:
                    entities.append(payload)
                item_index += 1
            if skipped_inserts:
                # Keep diagnostics bounded and useful.  One aggregate record
                # replaces potentially thousands of per-placement messages.
                skipped_total = sum(skipped_inserts.values())
                diagnostics.append({
                    "severity": "info",
                    "code": "DXF_INSERTS_SKIPPED_BY_LAYER_SCOPE",
                    "source_file_id": source.source_file_id,
                    "count": skipped_total,
                    "block_names": sorted(skipped_inserts)[:100],
                    "block_name_count": len(skipped_inserts),
                    "include_layers": [
                        *self.config.wall.include_layers,
                        *self.config.column.include_layers,
                        *self.config.column.profile_layers,
                        *self.config.column.label_layers,
                        *self.config.grid.include_layers,
                        *self.config.opening.label_layers,
                        *self.config.opening.boundary_layers,
                    ],
                    "exclude_layers": [
                        *self.config.wall.exclude_layers,
                        *self.config.column.exclude_layers,
                        *self.config.grid.exclude_layers,
                        *self.config.opening.exclude_layers,
                    ],
                })
            if skipped_entities:
                diagnostics.append({
                    "severity": "info",
                    "code": "DXF_ENTITIES_SKIPPED_BY_LAYER_SCOPE",
                    "source_file_id": source.source_file_id,
                    "count": sum(skipped_entities.values()),
                    "entity_types": dict(sorted(skipped_entities.items())),
                    "include_layers": [
                        *self.config.wall.include_layers,
                        *self.config.column.include_layers,
                        *self.config.column.profile_layers,
                        *self.config.column.label_layers,
                        *self.config.grid.include_layers,
                        *self.config.opening.label_layers,
                        *self.config.opening.boundary_layers,
                    ],
                    "exclude_layers": [
                        *self.config.wall.exclude_layers,
                        *self.config.column.exclude_layers,
                        *self.config.grid.exclude_layers,
                        *self.config.opening.exclude_layers,
                    ],
                })
            # The DXF parser can run for a long time on large drawings; verify
            # that the source identity did not change while entities were read.
            verify_source_file_against_manifest(source)
        return SourceEntities(
            tenant_id=manifest.tenant_id,
            project_id=manifest.project_id,
            manifest_sha256=canonical_sha256(manifest),
            entities=entities,
            source_units=source_units,
            diagnostics=diagnostics,
        )


def extract_source_entities(
    config: WallPipelineConfig, manifest: SourceManifest, output_dir: Path
) -> SourceEntities:
    if config.source.type == "pdf":
        return PDFSourceAdapter(config).extract(manifest, output_dir)
    return DXFSourceAdapter(config).extract(manifest, output_dir)
