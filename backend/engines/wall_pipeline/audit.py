"""Independent source/Revit rendering and overlay audit."""

from __future__ import annotations

import math
import re
from pathlib import Path

import cv2
import numpy as np

from backend.engines.wall_pipeline.adapters import (
    max_pdf_pages,
    verify_source_file_against_manifest,
)

from backend.engines.wall_pipeline.contracts import (
    AuditReport,
    RevitResult,
    SourceEntities,
    SourceManifest,
    WallModel,
    WallEvidence,
)
from backend.engines.wall_pipeline.io import canonical_sha256, file_sha256


# Independent CAD and Revit renderers use different minimum stroke widths.
# At the 2400px/150-DPI delivery raster a Revit 2020 projection line can be
# four pixels wide while a DXF/PDF hairline is one or two pixels.  A four-pixel
# neighbourhood is the smallest fixed tolerance that treats that renderer
# difference as the same geometric edge without absorbing normal drawing
# displacement (the equal-canvas registration remains identity-only).
_EDGE_TOLERANCE_PX = 4
_REGISTRATION_MAX_DIMENSION = 800
_PDF_RENDER_SCALE = 2.0
_MAX_PDF_RENDER_PIXELS = 120_000_000
# The delivery contract is intentionally strict.  A caller may request a
# stricter threshold, but no code path may lower the independent comparison
# floor for a model that is marked deliverable.
REQUIRED_SIMILARITY = 0.95


def _read_image(path: Path, flags: int) -> np.ndarray | None:
    """Decode an image through bytes so Windows Unicode paths are supported.

    OpenCV's Windows ``imread``/``imwrite`` wrappers still use the active
    code page on some builds.  Revit 2020 commonly exports a localized view
    name (for example ``... - 楼层平面 - ...png``), so passing that path
    directly can return ``None`` even though the file is valid.  NumPy/Python
    perform the filesystem I/O with the native Unicode API; OpenCV only sees
    the in-memory bytes.
    """

    try:
        encoded = np.fromfile(str(path), dtype=np.uint8)
    except (OSError, ValueError):
        return None
    if encoded.size == 0:
        return None
    return cv2.imdecode(encoded, flags)


def _write_image(path: Path, image: np.ndarray) -> bool:
    """Write an image through bytes so localized Windows paths remain valid."""

    suffix = path.suffix.lower() or ".png"
    try:
        ok, encoded = cv2.imencode(suffix, image)
        if not ok:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded.tobytes())
        return True
    except (OSError, ValueError, cv2.error):
        return False


def _edge_mask(image: np.ndarray) -> np.ndarray:
    """Return a binary edge mask used for raster comparison.

    The source and Revit images are intentionally read independently.  This
    helper only derives observations from each raster; it never consults the
    WallModel geometry.
    """

    return cv2.Canny(image, 80, 180)


def _content_bounds(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    points = np.argwhere(mask > 0)
    if points.size == 0:
        return None
    y_min, x_min = points.min(axis=0)
    y_max, x_max = points.max(axis=0)
    return (
        int(x_min),
        int(y_min),
        int(x_max - x_min + 1),
        int(y_max - y_min + 1),
    )


def _tolerant_edge_iou(
    source_mask: np.ndarray,
    revit_mask: np.ndarray,
    *,
    tolerance_px: int = _EDGE_TOLERANCE_PX,
) -> float:
    """Compute a conservative stroke-width-normalized edge similarity.

    A literal pixel-area IoU penalizes a correct Revit wall simply because its
    renderer uses a wider minimum lineweight than a CAD hairline.  Match each
    edge against the other raster's bounded neighbourhood, then take the
    smaller directional coverage (source recall versus Revit precision).  The
    result is still symmetric and rejects missing/extra geometry, while being
    invariant to that renderer-only stroke width difference.
    """

    source = source_mask > 0
    revit = revit_mask > 0
    source_count = int(source.sum())
    revit_count = int(revit.sum())
    if not source_count or not revit_count:
        return 0.0
    radius = max(0, int(tolerance_px))
    kernel = np.ones((radius * 2 + 1, radius * 2 + 1), dtype=np.uint8)
    source_near = cv2.dilate(source.astype(np.uint8), kernel) > 0
    revit_near = cv2.dilate(revit.astype(np.uint8), kernel) > 0
    matched_source = int(np.logical_and(source, revit_near).sum())
    matched_revit = int(np.logical_and(revit, source_near).sum())
    source_coverage = matched_source / float(source_count)
    revit_precision = matched_revit / float(revit_count)
    return float(max(0.0, min(1.0, source_coverage, revit_precision)))


def _raw_edge_iou(
    source_mask: np.ndarray,
    revit_mask: np.ndarray,
    *,
    tolerance_px: int = _EDGE_TOLERANCE_PX,
) -> float:
    """Return the conventional area IoU as a diagnostic metric.

    It is intentionally not the acceptance score because lineweight is a
    renderer detail, but retaining it in the report makes the normalization
    auditable instead of hiding the raw raster observation.
    """

    source = source_mask > 0
    revit = revit_mask > 0
    source_count = int(source.sum())
    revit_count = int(revit.sum())
    if not source_count or not revit_count:
        return 0.0
    radius = max(0, int(tolerance_px))
    kernel = np.ones((radius * 2 + 1, radius * 2 + 1), dtype=np.uint8)
    source_near = cv2.dilate(source.astype(np.uint8), kernel) > 0
    revit_near = cv2.dilate(revit.astype(np.uint8), kernel) > 0
    matched_source = int(np.logical_and(source, revit_near).sum())
    matched_revit = int(np.logical_and(revit, source_near).sum())
    intersection = (matched_source + matched_revit) / 2.0
    union = source_count + revit_count - intersection
    if union <= 0.0:
        return 0.0
    return float(max(0.0, min(1.0, intersection / union)))


def _warp_edges(
    revit_edges: np.ndarray,
    source_shape: tuple[int, int],
    scale_x: float,
    scale_y: float,
    translate_x: float,
    translate_y: float,
) -> np.ndarray:
    height, width = source_shape
    matrix = np.array(
        [[scale_x, 0.0, translate_x], [0.0, scale_y, translate_y]],
        dtype=np.float32,
    )
    return cv2.warpAffine(
        revit_edges,
        matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def _register_edges_core(
    source_edges: np.ndarray,
    revit_edges: np.ndarray,
) -> tuple[np.ndarray, dict[str, float | int | str]]:
    """Register two independently rendered edge masks.

    Equal-sized canvases are deliberately compared in their original frame:
    an unexplained model translation must remain visible and fail the audit.
    When canvas dimensions differ (the normal crop/padding case), a bounded
    content-box registration estimates scale and translation, then performs a
    small deterministic local refinement.  This avoids an unrestricted image
    registration step that could align a genuinely misplaced model and hide
    the defect.
    """

    source_height, source_width = source_edges.shape[:2]
    revit_height, revit_width = revit_edges.shape[:2]
    same_canvas = (source_height, source_width) == (revit_height, revit_width)
    if same_canvas:
        warped = revit_edges.copy()
        return warped, {
            "registration_mode": "identity",
            "registration_scale_x": 1.0,
            "registration_scale_y": 1.0,
            "registration_translation_x_px": 0.0,
            "registration_translation_y_px": 0.0,
            "registration_search_iou": round(
                _tolerant_edge_iou(source_edges, warped), 6
            ),
        }

    source_bounds = _content_bounds(source_edges)
    revit_bounds = _content_bounds(revit_edges)
    if source_bounds is None or revit_bounds is None:
        # Keep a deterministic fallback for text-only/blank renders.  The
        # final IoU remains zero, but the audit can still emit an overlay and
        # useful registration metadata instead of inventing a match.
        scale_x = source_width / max(float(revit_width), 1.0)
        scale_y = source_height / max(float(revit_height), 1.0)
        translate_x = 0.0
        translate_y = 0.0
        warped = _warp_edges(
            revit_edges,
            (source_height, source_width),
            scale_x,
            scale_y,
            translate_x,
            translate_y,
        )
        return warped, {
            "registration_mode": "canvas_scale_no_content",
            "registration_scale_x": round(float(scale_x), 6),
            "registration_scale_y": round(float(scale_y), 6),
            "registration_translation_x_px": 0.0,
            "registration_translation_y_px": 0.0,
            "registration_search_iou": 0.0,
        }

    source_x, source_y, source_w, source_h = source_bounds
    revit_x, revit_y, revit_w, revit_h = revit_bounds
    # Content dimensions are more reliable than full canvas dimensions when
    # one renderer adds a crop or a different amount of white page margin.
    scale_x = source_w / max(float(revit_w), 1.0)
    scale_y = source_h / max(float(revit_h), 1.0)
    translate_x = (source_x + source_w / 2.0) - scale_x * (
        revit_x + revit_w / 2.0
    )
    translate_y = (source_y + source_h / 2.0) - scale_y * (
        revit_y + revit_h / 2.0
    )

    # Downsample only for registration search.  The final comparison and
    # overlay are always rendered at the source raster's original resolution.
    source_factor = min(
        1.0, _REGISTRATION_MAX_DIMENSION / max(source_width, source_height)
    )
    revit_factor = min(
        1.0, _REGISTRATION_MAX_DIMENSION / max(revit_width, revit_height)
    )
    source_small_size = (
        max(1, round(source_width * source_factor)),
        max(1, round(source_height * source_factor)),
    )
    revit_small_size = (
        max(1, round(revit_width * revit_factor)),
        max(1, round(revit_height * revit_factor)),
    )
    source_small = cv2.resize(
        source_edges, source_small_size, interpolation=cv2.INTER_NEAREST
    )
    revit_small = cv2.resize(
        revit_edges, revit_small_size, interpolation=cv2.INTER_NEAREST
    )
    # ``_EDGE_TOLERANCE_PX`` is defined in final-canvas pixels.  Reusing that
    # radius after downsampling made the registration search 3x more lenient
    # on a 2400 -> 800 image, so several nearby translations looked equally
    # good and the search could choose a transform that failed at full
    # resolution.  Scale the radius into the search canvas and keep one pixel
    # as the renderer-noise floor; final acceptance still uses the unchanged
    # four-pixel engineering tolerance on the original independent rasters.
    search_tolerance_px = max(
        1, int(round(_EDGE_TOLERANCE_PX * source_factor))
    )

    def score(
        candidate_scale_x: float,
        candidate_scale_y: float,
        candidate_translate_x: float,
        candidate_translate_y: float,
    ) -> float:
        small_scale_x = candidate_scale_x * source_factor / revit_factor
        small_scale_y = candidate_scale_y * source_factor / revit_factor
        warped_small = _warp_edges(
            revit_small,
            source_small.shape[:2],
            small_scale_x,
            small_scale_y,
            candidate_translate_x * source_factor,
            candidate_translate_y * source_factor,
        )
        return _tolerant_edge_iou(
            source_small,
            warped_small,
            tolerance_px=search_tolerance_px,
        )

    best = (
        score(scale_x, scale_y, translate_x, translate_y),
        scale_x,
        scale_y,
        translate_x,
        translate_y,
    )
    # The content-box estimate is normally exact.  Refine only a small
    # translation window and tiny scale window so arbitrary model movement is
    # not silently normalized away.
    search_px = max(
        4.0,
        min(16.0, 0.02 * min(source_width, source_height)),
    )
    translation_steps = range(-int(search_px), int(search_px) + 1, 2)
    for delta_x in translation_steps:
        for delta_y in translation_steps:
            for scale_delta in (-0.005, 0.0, 0.005):
                candidate = (
                    score(
                        scale_x * (1.0 + scale_delta),
                        scale_y * (1.0 + scale_delta),
                        translate_x + delta_x,
                        translate_y + delta_y,
                    ),
                    scale_x * (1.0 + scale_delta),
                    scale_y * (1.0 + scale_delta),
                    translate_x + delta_x,
                    translate_y + delta_y,
                )
                if candidate[0] > best[0]:
                    best = candidate

    _, best_scale_x, best_scale_y, best_translate_x, best_translate_y = best
    warped = _warp_edges(
        revit_edges,
        (source_height, source_width),
        best_scale_x,
        best_scale_y,
        best_translate_x,
        best_translate_y,
    )
    return warped, {
        "registration_mode": "content_bbox",
        "registration_scale_x": round(float(best_scale_x), 6),
        "registration_scale_y": round(float(best_scale_y), 6),
        "registration_translation_x_px": round(float(best_translate_x), 3),
        "registration_translation_y_px": round(float(best_translate_y), 3),
        "registration_search_iou": round(float(best[0]), 6),
        "registration_search_tolerance_px": search_tolerance_px,
    }


def _register_edges(
    source_edges: np.ndarray,
    revit_edges: np.ndarray,
) -> tuple[np.ndarray, dict[str, float | int | str]]:
    """Try the four PDF/Revit page orientations before bounded registration.

    A Revit export can use a portrait view while the source sheet is
    landscape (or vice versa).  Only right-angle rotations are considered;
    arbitrary affine fitting would be capable of hiding a real coordinate
    defect.  Equal-sized canvases intentionally retain the identity-only
    policy from ``_register_edges_core`` so an unexplained translation cannot
    be normalized away.
    """

    same_canvas = source_edges.shape[:2] == revit_edges.shape[:2]
    if same_canvas:
        return _register_edges_core(source_edges, revit_edges)

    source_landscape = source_edges.shape[1] >= source_edges.shape[0]
    revit_landscape = revit_edges.shape[1] >= revit_edges.shape[0]
    if source_landscape == revit_landscape:
        # A one-pixel crop/rounding difference must not make a landscape plan
        # eligible for a 90° rotation.  Such a rotation can score well for an
        # L-shaped or otherwise symmetric drawing while hiding a real axis
        # swap.  Portrait-vs-landscape canvases still use the bounded
        # right-angle candidates below (the documented PDF/Revit page case).
        orientations = (
            (0, revit_edges),
            (180, cv2.rotate(revit_edges, cv2.ROTATE_180)),
        )
    else:
        orientations = (
            (0, revit_edges),
            (90, cv2.rotate(revit_edges, cv2.ROTATE_90_CLOCKWISE)),
            (180, cv2.rotate(revit_edges, cv2.ROTATE_180)),
            (270, cv2.rotate(revit_edges, cv2.ROTATE_90_COUNTERCLOCKWISE)),
        )
    best_warped: np.ndarray | None = None
    best_metrics: dict[str, float | int | str] | None = None
    best_score = -1.0
    for angle, oriented in orientations:
        warped, metrics = _register_edges_core(source_edges, oriented)
        score = float(metrics.get("registration_search_iou") or 0.0)
        # Keep the first orientation on an exact tie.  This makes the result
        # reproducible and avoids arbitrary 180° flips for symmetric plans.
        if score > best_score:
            best_score = score
            best_warped = warped
            best_metrics = dict(metrics)
            best_metrics["registration_rotation_deg"] = angle
    assert best_warped is not None and best_metrics is not None
    return best_warped, best_metrics


def _render_pdf_source(manifest: SourceManifest, output: Path) -> None:
    import pymupdf as fitz
    from PIL import Image

    images = []
    page_specs: list[tuple[str, int]] = []
    total_area_pt = 0.0
    sources_by_id = {source.source_file_id: source for source in manifest.sources}
    if len(sources_by_id) != len(manifest.sources):
        raise ValueError("PDF source manifest contains duplicate source_file_id values")
    for source in manifest.sources:
        # Open the original PDF independently of the vector adapter.  Re-check
        # the manifest identity before each source so a replacement between
        # extraction and audit cannot be accepted as the source-side half of
        # the comparison.
        verify_source_file_against_manifest(source)
        try:
            document = fitz.open(source.path)
        except Exception as exc:
            raise RuntimeError(
                f"PDF source render open failed: {source.source_file_id}"
            ) from exc
        try:
            try:
                page_count = int(document.page_count)
            except Exception as exc:
                raise RuntimeError(
                    f"PDF source render page count failed: {source.source_file_id}"
                ) from exc
            if page_count < 0:
                raise RuntimeError(
                    f"PDF source render page count invalid: {source.source_file_id}"
                )
            page_indexes = (
                [source.page_no - 1]
                if source.page_no is not None
                else range(page_count)
            )
            if len(page_indexes) > max_pdf_pages():
                raise ValueError(
                    f"PDF_PAGE_LIMIT_EXCEEDED: {source.source_file_id} "
                    f"({len(page_indexes)} pages; limit {max_pdf_pages()})"
                )
            for page_index in page_indexes:
                if not 0 <= page_index < page_count:
                    raise ValueError(
                        f"PDF source render page {page_index + 1} does not exist: "
                        f"{source.source_file_id}"
                    )
                page = document[page_index]
                media_box = page.mediabox
                page_width = float(media_box.width)
                page_height = float(media_box.height)
                if not math.isfinite(page_width) or not math.isfinite(page_height):
                    raise ValueError(
                        f"PDF source render page dimensions are invalid: {source.source_file_id}"
                    )
                if page_width <= 0.0 or page_height <= 0.0:
                    raise ValueError(
                        f"PDF source render page dimensions are non-positive: {source.source_file_id}"
                    )
                total_area_pt += page_width * page_height
                page_specs.append((source.source_file_id, page_index))
        finally:
            document.close()
    if not page_specs:
        raise ValueError("PDF source has no pages to render")

    # Keep the fallback bounded as well as the vector adapter.  Derive one
    # common scale so concatenated pages retain a deterministic relative size.
    scale = _PDF_RENDER_SCALE
    if total_area_pt > 0.0:
        bounded_scale = min(
            scale,
            (_MAX_PDF_RENDER_PIXELS / total_area_pt) ** 0.5,
        )
        # A very large page cannot be rendered safely even at the minimum
        # useful scale.  Failing here is preferable to allocating an image
        # that exceeds the process memory budget.
        if bounded_scale < 0.1:
            raise ValueError(
                "PDF source render pixel limit exceeded; reduce page size or select fewer pages"
            )
        scale = bounded_scale
    scale = max(0.1, scale)
    estimated_pixels = 0
    for source_id, page_index in page_specs:
        source = sources_by_id[source_id]
        document = None
        try:
            document = fitz.open(source.path)
            page = document[page_index]
            width = max(1, int(math.ceil(float(page.mediabox.width) * scale)))
            height = max(1, int(math.ceil(float(page.mediabox.height) * scale)))
            estimated_pixels += width * height
        except Exception as exc:
            raise RuntimeError(
                f"PDF source render size check failed: {source.source_file_id}:{page_index + 1}"
            ) from exc
        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass
    if estimated_pixels > _MAX_PDF_RENDER_PIXELS:
        raise ValueError(
            "PDF source render pixel limit exceeded; reduce page size or select fewer pages"
        )
    verified_render_sources: set[str] = set()
    for source_id, page_index in page_specs:
        source = sources_by_id[source_id]
        if source_id not in verified_render_sources:
            verify_source_file_against_manifest(source)
            verified_render_sources.add(source_id)
        try:
            document = fitz.open(source.path)
        except Exception as exc:
            raise RuntimeError(
                f"PDF source render open failed: {source.source_file_id}"
            ) from exc
        try:
            page = document[page_index]
            # The source adapter deliberately uses the unrotated media-box
            # frame.  Apply the inverse page rotation here too; otherwise a
            # text-only rotated PDF would be audited in a different frame
            # from its vector counterpart.
            matrix = page.derotation_matrix * fitz.Matrix(scale, scale)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            images.append(Image.frombytes(
                "RGB", (pixmap.width, pixmap.height), pixmap.samples
            ))
        except Exception as exc:
            raise RuntimeError(
                f"PDF source render page failed: {source.source_file_id}:"
                f"{page_index + 1}"
            ) from exc
        finally:
            document.close()
    width = sum(image.width for image in images)
    height = max(image.height for image in images)
    canvas = Image.new("RGB", (width, height), "white")
    x = 0
    for image in images:
        canvas.paste(image, (x, 0))
        x += image.width
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


def _render_pdf_structural_source(
    manifest: SourceManifest,
    output: Path,
    evidence: WallEvidence,
) -> None:
    """Render only authoritative wall/column layers from the original PDF.

    This is still an independent source render: it reopens the immutable PDF
    and reads its own vector drawings.  The layer scope merely removes title
    blocks, dimensions and reinforcement annotation that are outside the
    wall/column delivery contract; it never consults WallModel geometry.
    """

    import pymupdf as fitz

    operations = {item.get("operation"): item for item in evidence.transform_chain}
    scales = operations.get("unit_convert", {}).get("scale_to_m_by_source") or {}
    source_origin = operations.get("translate_origin", {}).get("source_origin") or (0.0, 0.0)
    rotation = float(operations.get("rotate", {}).get("rotation_deg") or 0.0)
    translation = operations.get("translate", {}).get("translation_m") or (0.0, 0.0)
    frame_offsets = operations.get("translate", {}).get("frame_offsets_m") or {}
    cosine, sine = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    wall = manifest.wall
    column = manifest.column
    grid = manifest.grid

    def selected(layer: str) -> bool:
        wall_selected = (
            not wall.include_layers
            or (
                any(re.search(pattern, layer, re.IGNORECASE) for pattern in wall.include_layers)
                and not any(re.search(pattern, layer, re.IGNORECASE) for pattern in wall.exclude_layers)
            )
        )
        column_selected = bool(column.include_layers) and (
            any(re.search(pattern, layer, re.IGNORECASE) for pattern in column.include_layers)
            and not any(re.search(pattern, layer, re.IGNORECASE) for pattern in column.exclude_layers)
        )
        profile_selected = bool(column.profile_layers) and any(
            re.search(pattern, layer, re.IGNORECASE)
            for pattern in column.profile_layers
        )
        grid_selected = bool(grid.include_layers) and (
            any(re.search(pattern, layer, re.IGNORECASE) for pattern in grid.include_layers)
            and not any(re.search(pattern, layer, re.IGNORECASE) for pattern in grid.exclude_layers)
        )
        return wall_selected or column_selected or profile_selected or grid_selected

    def transform(point, source_id: str, frame_id: str):
        scale = float(scales.get(source_id) or 1.0)
        x = (float(point[0]) - float(source_origin[0])) * scale
        y = (float(point[1]) - float(source_origin[1])) * scale
        offset = frame_offsets.get(frame_id) or frame_offsets.get(source_id) or (0.0, 0.0)
        return (
            cosine * x - sine * y + float(translation[0]) + float(offset[0]),
            sine * x + cosine * y + float(translation[1]) + float(offset[1]),
        )

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for source in manifest.sources:
        verify_source_file_against_manifest(source)
        document = fitz.open(source.path)
        try:
            page_indexes = (
                [source.page_no - 1]
                if source.page_no is not None else range(int(document.page_count))
            )
            for page_index in page_indexes:
                if not 0 <= page_index < int(document.page_count):
                    raise ValueError("PDF source render page does not exist")
                page = document[page_index]
                frame_id = f"{source.source_file_id}:page:{page_index + 1:04d}"
                page_height = float(page.mediabox.height)
                for drawing in page.get_drawings():
                    layer = str(drawing.get("layer") or "")
                    if not selected(layer):
                        continue
                    for item in drawing.get("items") or []:
                        kind = item[0]
                        points = []
                        if kind == "l":
                            points = [item[1], item[2]]
                        elif kind == "re":
                            rect = item[1]
                            points = [
                                (rect.x0, rect.y0), (rect.x1, rect.y0),
                                (rect.x1, rect.y1), (rect.x0, rect.y1),
                            ]
                        elif kind == "qu":
                            quad = item[1]
                            points = [quad.ul, quad.ur, quad.lr, quad.ll]
                        if len(points) < 2:
                            continue
                        local = [
                            (float(point[0]), page_height - float(point[1]))
                            for point in points
                        ]
                        transformed = [transform(point, source.source_file_id, frame_id) for point in local]
                        segments.extend(zip(transformed, (*transformed[1:], transformed[0])))
        finally:
            document.close()
    if not segments:
        raise ValueError("PDF source has no structural wall/column vector geometry")
    points = [point for segment in segments for point in segment]
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    width, height, margin = 2400, 1600, 40
    scale = min(
        (width - 2 * margin) / max(max_x - min_x, 1e-9),
        (height - 2 * margin) / max(max_y - min_y, 1e-9),
    )
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)

    def pixel(point):
        return (
            round(margin + (point[0] - min_x) * scale),
            round(height - margin - (point[1] - min_y) * scale),
        )

    for start, end in segments:
        cv2.line(canvas, pixel(start), pixel(end), (0, 0, 0), 1, cv2.LINE_AA)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not _write_image(output, canvas):
        raise RuntimeError(f"failed to write structural PDF source render: {output}")


def _render_vector_source(source_entities: SourceEntities, output: Path) -> None:
    segments = []
    for entity in source_entities.entities:
        geometry = entity.geometry
        if "start" in geometry and "end" in geometry:
            segments.append((geometry["start"], geometry["end"]))
        else:
            points = geometry.get("points") or []
            segments.extend(zip(points, points[1:]))
    if not segments:
        raise ValueError("source drawing has no renderable vector geometry")
    points = [point for segment in segments for point in segment]
    min_x = min(float(point[0]) for point in points)
    max_x = max(float(point[0]) for point in points)
    min_y = min(float(point[1]) for point in points)
    max_y = max(float(point[1]) for point in points)
    width, height, margin = 2400, 1600, 40
    scale = min(
        (width - 2 * margin) / max(max_x - min_x, 1e-9),
        (height - 2 * margin) / max(max_y - min_y, 1e-9),
    )
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)

    def pixel(point):
        return (
            round(margin + (float(point[0]) - min_x) * scale),
            round(height - margin - (float(point[1]) - min_y) * scale),
        )

    for start, end in segments:
        cv2.line(canvas, pixel(start), pixel(end), (0, 0, 0), 1, cv2.LINE_AA)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not _write_image(output, canvas):
        raise RuntimeError(f"failed to write source render: {output}")


def _transformed_source_segments(
    source_entities: SourceEntities,
    evidence: WallEvidence,
    include_layers: list[str] | None = None,
    anchor_layers: list[str] | None = None,
    anchor_min_length_m: float | None = None,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Return exactly the vector segments used by the CAD source renderer.

    Keeping extraction, frame selection and the source transform in one helper
    is important: the audit frame must describe the pixels that the renderer
    actually draws, rather than a second, subtly different interpretation of
    ``SourceEntities``.  The helper deliberately has no ``WallModel`` input so
    omitted/generated walls cannot influence the source frame.
    """

    operations = {item.get("operation"): item for item in evidence.transform_chain}
    unit_operation = operations.get("unit_convert") or {}
    scales = unit_operation.get("scale_to_m_by_source") or {}
    origin_operation = operations.get("translate_origin") or {}
    source_origin = origin_operation.get("source_origin") or (0.0, 0.0)
    rotate_operation = operations.get("rotate") or {}
    rotation_deg = float(rotate_operation.get("rotation_deg") or 0.0)
    translate_operation = operations.get("translate") or {}
    translation = translate_operation.get("translation_m") or (0.0, 0.0)
    frame_offsets = translate_operation.get("frame_offsets_m") or {}
    radians = np.deg2rad(rotation_deg)
    cosine, sine = float(np.cos(radians)), float(np.sin(radians))

    def transform(point, entity):
        scale = float(scales.get(entity.source_file_id, 1.0))
        x = (float(point[0]) - float(source_origin[0])) * scale
        y = (float(point[1]) - float(source_origin[1])) * scale
        frame_id = entity.frame_id or (
            f"{entity.source_file_id}:page:{entity.page_no:04d}"
            if entity.page_no is not None else entity.source_file_id
        )
        offset = frame_offsets.get(frame_id)
        if offset is None:
            offset = frame_offsets.get(entity.source_file_id, (0.0, 0.0))
        return (
            cosine * x - sine * y + float(translation[0]) + float(offset[0]),
            sine * x + cosine * y + float(translation[1]) + float(offset[1]),
        )

    candidates: list[tuple[tuple[float, float], tuple[float, float], str]] = []
    for entity in source_entities.entities:
        if include_layers:
            try:
                if not any(re.search(pattern, entity.layer, re.IGNORECASE) for pattern in include_layers):
                    continue
            except re.error as exc:
                raise ValueError(f"invalid source layer regular expression: {exc}") from exc
        geometry = entity.geometry
        if "start" in geometry and "end" in geometry:
            candidates.append((transform(geometry["start"], entity),
                               transform(geometry["end"], entity),
                               entity.layer))
        else:
            points = geometry.get("points") or []
            transformed = [transform(point, entity) for point in points]
            candidates.extend(
                (start, end, entity.layer)
                for start, end in zip(transformed, transformed[1:])
            )

    # Some CAD exports keep proxy/XREF entities on a structural layer while
    # retaining coordinates from the author's global survey frame.  They can
    # be millions of metres away from the actual plan and would make both
    # independent renderers appear blank.  When anchor layers are supplied,
    # derive a generous engineering frame from those immutable source layers
    # and exclude only candidates outside that frame.  The filter is source
    # side only; it never consults WallModel/evidence IDs or generated output.
    if anchor_layers:
        try:
            anchors = [
                segment for segment in candidates
                if any(re.search(pattern, segment[2], re.IGNORECASE) for pattern in anchor_layers)
                and (
                    anchor_min_length_m is None
                    or math.dist(segment[0], segment[1]) >= anchor_min_length_m
                )
            ]
        except re.error as exc:
            raise ValueError(f"invalid source anchor layer regular expression: {exc}") from exc
        if anchors:
            points = [point for start, end, _ in anchors for point in (start, end)]
            min_x = min(point[0] for point in points)
            max_x = max(point[0] for point in points)
            min_y = min(point[1] for point in points)
            max_y = max(point[1] for point in points)
            margin_x = max((max_x - min_x) * 0.20, 1.0)
            margin_y = max((max_y - min_y) * 0.20, 1.0)
            candidates = [
                segment for segment in candidates
                if all(
                    min_x - margin_x <= point[0] <= max_x + margin_x
                    and min_y - margin_y <= point[1] <= max_y + margin_y
                    for point in (segment[0], segment[1])
                )
            ]
    return [(start, end) for start, end, _ in candidates]


def _render_transformed_source(
    source_entities: SourceEntities,
    evidence: WallEvidence,
    output: Path,
    include_layers: list[str] | None = None,
    anchor_layers: list[str] | None = None,
    anchor_min_length_m: float | None = None,
) -> None:
    """Render parsed CAD source entities after the recorded source transform.

    This remains independent of ``WallModel``: only the immutable source
    entities and the transform chain produced by the geometry stage are used.
    PDF sources intentionally bypass this helper and are rendered from the
    original PDF pages by :func:`_render_pdf_source`.
    """

    segments = _transformed_source_segments(
        source_entities, evidence, include_layers, anchor_layers,
        anchor_min_length_m,
    )
    if not segments:
        raise ValueError("source drawing has no renderable vector geometry")
    points = [point for segment in segments for point in segment]
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    width, height, margin = 2400, 1600, 40
    scale = min(
        (width - 2 * margin) / max(max_x - min_x, 1e-9),
        (height - 2 * margin) / max(max_y - min_y, 1e-9),
    )
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)

    def pixel(point):
        return (
            round(margin + (point[0] - min_x) * scale),
            round(height - margin - (point[1] - min_y) * scale),
        )

    for start, end in segments:
        cv2.line(canvas, pixel(start), pixel(end), (0, 0, 0), 1, cv2.LINE_AA)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not _write_image(output, canvas):
        raise RuntimeError(f"failed to write transformed source render: {output}")


def source_geometry_bounds(
    source_entities: SourceEntities,
    evidence: WallEvidence,
    include_layers: list[str] | None = None,
    anchor_layers: list[str] | None = None,
    anchor_min_length_m: float | None = None,
) -> tuple[float, float, float, float]:
    """Return transformed source bounds for the independent Revit view frame.

    This deliberately reads only source entities and the recorded transform
    chain.  It must not use WallModel/wall evidence, because those are the
    downstream products whose omissions the independent audit is intended to
    detect.
    """

    segments = _transformed_source_segments(
        source_entities, evidence, include_layers, anchor_layers,
        anchor_min_length_m,
    )
    bounds: list[float] | None = None
    for start, end in segments:
        for point in (start, end):
            x, y = point
            if bounds is None:
                bounds = [x, y, x, y]
            else:
                bounds[0] = min(bounds[0], x)
                bounds[1] = min(bounds[1], y)
                bounds[2] = max(bounds[2], x)
                bounds[3] = max(bounds[3], y)
    if bounds is None or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise ValueError("source drawing has no valid transformed geometry bounds")
    return tuple(round(value, 9) for value in bounds)  # type: ignore[return-value]


def _has_renderable_vector_entities(source_entities: SourceEntities) -> bool:
    """Return whether the source contract contains drawable vector segments."""

    for entity in source_entities.entities:
        geometry = entity.geometry
        if "start" in geometry and "end" in geometry:
            return True
        points = geometry.get("points") or []
        if len(points) >= 2:
            return True
    return False


def render_original_source(
    manifest: SourceManifest, source_entities: SourceEntities, output: Path,
    evidence: WallEvidence | None = None,
    include_layers: list[str] | None = None,
    anchor_layers: list[str] | None = None,
    anchor_min_length_m: float | None = None,
) -> Path:
    """Render from the original source path/entities, never from WallModel."""

    # A PDF already has an authoritative raster renderer.  Always use the
    # original PDF pages for the audit snapshot—even when vector entities were
    # extracted—so the source side cannot be reconstructed from a downstream
    # evidence/model payload.  The parser/evidence artifacts remain available
    # for geometry diagnostics, but they are not the source image used for
    # acceptance.
    if manifest.source_type == "pdf":
        if evidence is not None and (
            manifest.wall.include_layers
            or manifest.column.include_layers
            or manifest.column.profile_layers
            or manifest.grid.include_layers
        ):
            _render_pdf_structural_source(manifest, output, evidence)
        else:
            _render_pdf_source(manifest, output)
    elif evidence is not None and _has_renderable_vector_entities(source_entities):
        _render_transformed_source(
            source_entities, evidence, output, include_layers, anchor_layers,
            anchor_min_length_m,
        )
    else:
        _render_vector_source(source_entities, output)
    return output


def create_independent_audit(
    wall_model: WallModel,
    revit_result: RevitResult,
    source_render: Path,
    overlay_path: Path,
    *,
    minimum_edge_iou: float = REQUIRED_SIMILARITY,
    minimum_source_coverage: float = REQUIRED_SIMILARITY,
    minimum_revit_precision: float = REQUIRED_SIMILARITY,
) -> AuditReport:
    def effective_threshold(name: str, value: float) -> float:
        # Keep the low-level audit API backwards compatible for diagnostic
        # callers while making the production acceptance floor impossible to
        # bypass by passing a smaller value.
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")
        return max(REQUIRED_SIMILARITY, value)

    minimum_edge_iou = effective_threshold("minimum_edge_iou", minimum_edge_iou)
    minimum_source_coverage = effective_threshold(
        "minimum_source_coverage", minimum_source_coverage
    )
    minimum_revit_precision = effective_threshold(
        "minimum_revit_precision", minimum_revit_precision
    )
    errors = []
    expected_model_hash = canonical_sha256(wall_model)
    if revit_result.wall_model_sha256 != expected_model_hash:
        errors.append("Revit result does not reference this wall model")
    if (
        revit_result.tenant_id != wall_model.tenant_id
        or revit_result.project_id != wall_model.project_id
    ):
        errors.append("Revit result tenant/project does not match the wall model")
    if wall_model.review_status != "approved":
        errors.append("wall model has no human approval")
    if revit_result.status != "succeeded":
        errors.append("Revit result is not succeeded")
    if revit_result.status == "succeeded":
        readback = revit_result.readback or {}
        if not revit_result.transaction_id:
            errors.append("Revit result has no transaction id")
        wall_count = readback.get("wall_count")
        if isinstance(wall_count, bool) or not isinstance(wall_count, int):
            errors.append("Revit result wall read-back count is missing")
        elif wall_count != len(wall_model.walls):
            errors.append("Revit result wall read-back count does not match wall model")
        if len(revit_result.created_element_ids) != len(wall_model.walls):
            errors.append("Revit result element-id count does not match wall model")
        column_count = readback.get("column_count", 0)
        if isinstance(column_count, bool) or not isinstance(column_count, int):
            errors.append("Revit result column read-back count is missing")
        elif column_count != len(wall_model.columns):
            errors.append("Revit result column read-back count does not match column model")
        if len(revit_result.created_column_ids) != len(wall_model.columns):
            errors.append("Revit result column element-id count does not match column model")
        beam_count = readback.get("beam_count", 0)
        if isinstance(beam_count, bool) or not isinstance(beam_count, int):
            errors.append("Revit result coupling-beam read-back count is missing")
        elif beam_count != len(wall_model.beams):
            errors.append("Revit result coupling-beam read-back count does not match beam model")
        if len(revit_result.created_beam_ids) != len(wall_model.beams):
            errors.append("Revit result coupling-beam element-id count does not match beam model")
    revit_render = Path(revit_result.actual_view_path or "")
    if not source_render.is_file():
        errors.append("source render is missing")
    if not revit_render.is_file():
        errors.append("actual Revit view render is missing")
    if errors:
        return AuditReport(
            tenant_id=wall_model.tenant_id,
            project_id=wall_model.project_id,
            wall_model_sha256=expected_model_hash,
            revit_result_sha256=canonical_sha256(revit_result),
            status="blocked", independent_sources=False, errors=errors,
        )

    source_hash = file_sha256(source_render)
    revit_hash = file_sha256(revit_render)
    if revit_result.actual_view_sha256 != revit_hash:
        errors.append("actual Revit view hash does not match Revit result")
    independent = (
        source_render.resolve() != revit_render.resolve() and source_hash != revit_hash
    )
    if not independent:
        errors.append("source and Revit renders are not independent artifacts")
    if errors:
        return AuditReport(
            tenant_id=wall_model.tenant_id, project_id=wall_model.project_id,
            wall_model_sha256=expected_model_hash,
            revit_result_sha256=canonical_sha256(revit_result),
            status="blocked", independent_sources=False,
            source_render_path=str(source_render.resolve()),
            source_render_sha256=source_hash,
            revit_render_path=str(revit_render.resolve()),
            revit_render_sha256=revit_hash,
            errors=errors,
        )

    source_image = _read_image(source_render, cv2.IMREAD_GRAYSCALE)
    revit_image = _read_image(revit_render, cv2.IMREAD_GRAYSCALE)
    if source_image is None or revit_image is None:
        raise ValueError("source or Revit render cannot be decoded")
    source_edges = _edge_mask(source_image)
    revit_edges = _edge_mask(revit_image)
    registered_revit_edges, registration_metrics = _register_edges(
        source_edges, revit_edges
    )
    source_mask = source_edges > 0
    revit_mask = registered_revit_edges > 0
    edge_iou = _tolerant_edge_iou(
        source_edges, registered_revit_edges, tolerance_px=_EDGE_TOLERANCE_PX
    )
    raw_edge_iou = _raw_edge_iou(
        source_edges, registered_revit_edges, tolerance_px=_EDGE_TOLERANCE_PX
    )
    source_near = cv2.dilate(
        source_mask.astype(np.uint8),
        np.ones((_EDGE_TOLERANCE_PX * 2 + 1,) * 2, dtype=np.uint8),
    ) > 0
    revit_near = cv2.dilate(
        revit_mask.astype(np.uint8),
        np.ones((_EDGE_TOLERANCE_PX * 2 + 1,) * 2, dtype=np.uint8),
    ) > 0
    source_coverage = (
        int(np.logical_and(source_mask, revit_near).sum())
        / int(source_mask.sum())
        if source_mask.any()
        else 0.0
    )
    revit_precision = (
        int(np.logical_and(revit_mask, source_near).sum())
        / int(revit_mask.sum())
        if revit_mask.any()
        else 0.0
    )
    # ``edge_iou`` is already the conservative minimum of the two directional
    # matches; retaining the explicit coverage/precision terms below keeps the
    # report self-auditing and prevents one side from masking missing or extra
    # model geometry.
    similarity_score = min(edge_iou, source_coverage, revit_precision)
    overlay = np.full((*source_image.shape, 3), 255, dtype=np.uint8)
    overlay[source_mask] = (0, 180, 0)
    overlay[revit_mask] = (180, 0, 180)
    overlay[np.logical_and(source_mask, revit_mask)] = (0, 0, 0)
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    if not _write_image(overlay_path, overlay):
        raise RuntimeError(f"failed to write overlay: {overlay_path}")
    audit_errors: list[str] = []
    if edge_iou < minimum_edge_iou:
        audit_errors.append(
            "registered edge IoU %.6f is below the required %.6f"
            % (edge_iou, minimum_edge_iou)
        )
    if source_coverage < minimum_source_coverage:
        audit_errors.append(
            "source edge coverage %.6f is below the required %.6f"
            % (source_coverage, minimum_source_coverage)
        )
    if revit_precision < minimum_revit_precision:
        audit_errors.append(
            "Revit edge precision %.6f is below the required %.6f"
            % (revit_precision, minimum_revit_precision)
        )
    if similarity_score < REQUIRED_SIMILARITY:
        audit_errors.append(
            "independent drawing-to-Revit similarity %.6f is below the required %.6f"
            % (similarity_score, REQUIRED_SIMILARITY)
        )
    passed = not audit_errors
    return AuditReport(
        tenant_id=wall_model.tenant_id, project_id=wall_model.project_id,
        wall_model_sha256=expected_model_hash,
        revit_result_sha256=canonical_sha256(revit_result),
        status="pass" if passed else "fail",
        independent_sources=True,
        source_render_path=str(source_render.resolve()),
        source_render_sha256=source_hash,
        revit_render_path=str(revit_render.resolve()),
        revit_render_sha256=revit_hash,
        overlay_path=str(overlay_path.resolve()),
        metrics={
            "edge_iou": round(edge_iou, 6),
            "raw_edge_iou": round(raw_edge_iou, 6),
            "source_edge_coverage": round(source_coverage, 6),
            "revit_edge_precision": round(revit_precision, 6),
            "similarity_score": round(similarity_score, 6),
            "required_similarity": REQUIRED_SIMILARITY,
            **registration_metrics,
            "edge_tolerance_px": _EDGE_TOLERANCE_PX,
            "minimum_edge_iou": minimum_edge_iou,
            "minimum_source_coverage": minimum_source_coverage,
            "minimum_revit_precision": minimum_revit_precision,
            "comparison_canvas_width": int(source_image.shape[1]),
            "comparison_canvas_height": int(source_image.shape[0]),
            "comparison_basis": "independently rendered raster edges",
        },
        errors=audit_errors,
    )
