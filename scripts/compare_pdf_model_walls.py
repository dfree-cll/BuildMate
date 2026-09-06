"""Build a wall-only PDF vector reference and compare it with a model preview.

The two hotel B1 detail sheets retain useful PDF optional-content layer names.
This tool deliberately derives its reference from heavy, black, double-line wall
geometry on wall layers; model walls are only loaded after reference extraction.
The result is a reproducible high-confidence benchmark, not a manually labelled
ground truth.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.compare_revit_dxf_walls import compare


Point = tuple[float, float]
Bounds = tuple[float, float, float, float]
EXPECTED_WALL_THICKNESSES_M = (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60)
WALL_LAYER_SUFFIXES = ("A-WALL-S", "A-WALL-CONC", "S-WALL")


@dataclass(frozen=True)
class GridTransform:
    """Axis-aligned map from grid-local metres to rotated PDF page points."""

    x_scale: float
    x_offset: float
    y_scale: float
    y_offset: float
    x_samples: tuple[tuple[str, float, float], ...]
    y_samples: tuple[tuple[str, float, float], ...]

    def local_to_page(self, point: Point) -> Point:
        return (
            self.x_scale * point[0] + self.x_offset,
            self.y_scale * point[1] + self.y_offset,
        )

    def page_to_local(self, point: Point) -> Point:
        return (
            (point[0] - self.x_offset) / self.x_scale,
            (point[1] - self.y_offset) / self.y_scale,
        )

    def report(self) -> dict[str, Any]:
        def residuals(samples: Sequence[tuple[str, float, float]], axis: str) -> list[float]:
            scale = self.x_scale if axis == "x" else self.y_scale
            offset = self.x_offset if axis == "x" else self.y_offset
            return [abs(page - (scale * local + offset)) for _, local, page in samples]

        x_residuals = residuals(self.x_samples, "x")
        y_residuals = residuals(self.y_samples, "y")
        return {
            "method": "least_squares_named_grid_centres",
            "page_points_per_metre": [round(self.x_scale, 9), round(self.y_scale, 9)],
            "page_offset_points": [round(self.x_offset, 6), round(self.y_offset, 6)],
            "x_grid_sample_count": len(self.x_samples),
            "y_grid_sample_count": len(self.y_samples),
            "x_max_residual_points": round(max(x_residuals, default=0.0), 4),
            "y_max_residual_points": round(max(y_residuals, default=0.0), 4),
            "x_samples": [
                {"label": label, "local_m": local, "page_point": round(page, 4)}
                for label, local, page in self.x_samples
            ],
            "y_samples": [
                {"label": label, "local_m": local, "page_point": round(page, 4)}
                for label, local, page in self.y_samples
            ],
        }


@dataclass(frozen=True)
class Face:
    orientation: str
    fixed: float
    lo: float
    hi: float
    layer: str

    @property
    def length(self) -> float:
        return self.hi - self.lo


def _linear_fit(samples: Sequence[tuple[float, float]]) -> tuple[float, float]:
    if len(samples) < 2:
        raise ValueError("at least two samples are required for a linear fit")
    mean_x = statistics.fmean(value[0] for value in samples)
    mean_y = statistics.fmean(value[1] for value in samples)
    denominator = sum((x - mean_x) ** 2 for x, _ in samples)
    if denominator <= 1e-12:
        raise ValueError("grid samples do not span more than one coordinate")
    scale = sum((x - mean_x) * (y - mean_y) for x, y in samples) / denominator
    return scale, mean_y - scale * mean_x


def _rotated_word_center(page: Any, word: Sequence[Any]) -> Point:
    import pymupdf as fitz

    x0, y0, x1, y1 = map(float, word[:4])
    matrix = page.rotation_matrix
    corners = (
        fitz.Point(x0, y0) * matrix,
        fitz.Point(x1, y0) * matrix,
        fitz.Point(x0, y1) * matrix,
        fitz.Point(x1, y1) * matrix,
    )
    xs = [point.x for point in corners]
    ys = [point.y for point in corners]
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


def _dedupe_label_samples(
    samples: Iterable[tuple[str, float, float]],
) -> list[tuple[str, float, float]]:
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for label, local, page in samples:
        grouped[label].append((local, page))
    return [
        (label, values[0][0], statistics.median(value[1] for value in values))
        for label, values in sorted(grouped.items())
    ]


def derive_grid_transform(page: Any) -> GridTransform:
    """Find the main plan's G-grid from text bounding-box centres."""

    words = page.get_text("words")
    numbered = []
    lettered = []
    for word in words:
        text = str(word[4]).strip().upper()
        centre_x, centre_y = _rotated_word_center(page, word)
        number_match = re.fullmatch(r"G-(\d+)", text)
        if number_match and centre_y <= 120.0:
            number = int(number_match.group(1))
            numbered.append((text, (number - 1) * 9.0, centre_x))
        letter_match = re.fullmatch(r"G-([A-G])", text)
        if letter_match:
            lettered.append((text, (ord(letter_match.group(1)) - ord("A")) * 9.0,
                             centre_x, centre_y))

    x_samples = _dedupe_label_samples(numbered)
    if len(x_samples) < 4:
        raise ValueError("could not find the main plan's numbered G-grid row")

    clusters: list[list[tuple[str, float, float, float]]] = []
    for sample in sorted(lettered, key=lambda item: item[2]):
        if not clusters or abs(sample[2] - statistics.fmean(item[2] for item in clusters[-1])) > 20.0:
            clusters.append([sample])
        else:
            clusters[-1].append(sample)
    if not clusters:
        raise ValueError("could not find the main plan's lettered G-grid column")
    main_column = max(clusters, key=lambda values: len({item[0] for item in values}))
    y_samples = _dedupe_label_samples(
        (label, local, page_y) for label, local, _, page_y in main_column
    )
    if len(y_samples) < 4:
        raise ValueError("could not find enough lettered G-grid labels")

    x_scale, x_offset = _linear_fit([(local, page) for _, local, page in x_samples])
    y_scale, y_offset = _linear_fit([(local, page) for _, local, page in y_samples])
    if x_scale <= 0.0 or y_scale >= 0.0:
        raise ValueError("unexpected PDF grid direction")
    return GridTransform(
        x_scale=x_scale,
        x_offset=x_offset,
        y_scale=y_scale,
        y_offset=y_offset,
        x_samples=tuple(x_samples),
        y_samples=tuple(y_samples),
    )


def _drawing_segments(item: Sequence[Any]) -> list[tuple[Any, Any]]:
    import pymupdf as fitz

    kind = item[0]
    if kind == "l":
        return [(item[1], item[2])]
    if kind == "re":
        rect = item[1]
        corners = (
            fitz.Point(rect.x0, rect.y0), fitz.Point(rect.x1, rect.y0),
            fitz.Point(rect.x1, rect.y1), fitz.Point(rect.x0, rect.y1),
        )
        return list(zip(corners, (*corners[1:], corners[0])))
    return []


def _is_wall_layer(layer: str) -> bool:
    normalized = layer.strip().upper()
    return normalized.endswith(WALL_LAYER_SUFFIXES)


def extract_heavy_wall_faces(
    page: Any,
    transform: GridTransform,
    bounds: Bounds,
    *,
    stroke_width_points: float = 1.44,
    stroke_width_tolerance_points: float = 0.06,
    axis_tolerance_points: float = 0.35,
    minimum_face_length_m: float = 0.25,
) -> tuple[list[Face], dict[str, Any]]:
    """Extract axis-aligned heavy wall-face strokes without consulting the model."""

    x_min, x_max, y_min, y_max = bounds
    raw_faces: list[Face] = []
    drawing_count = 0
    layer_segment_counts: Counter[str] = Counter()
    for drawing in page.get_drawings():
        layer = str(drawing.get("layer") or "")
        color = drawing.get("color")
        width = float(drawing.get("width") or 0.0)
        if not _is_wall_layer(layer):
            continue
        if color is None or max(map(float, color)) > 0.05:
            continue
        if abs(width - stroke_width_points) > stroke_width_tolerance_points:
            continue
        if not str(drawing.get("dashes") or "[]").strip().startswith("[]"):
            continue
        drawing_count += 1
        for item in drawing.get("items") or []:
            for raw_start, raw_end in _drawing_segments(item):
                page_start = raw_start * page.rotation_matrix
                page_end = raw_end * page.rotation_matrix
                dx = abs(page_end.x - page_start.x)
                dy = abs(page_end.y - page_start.y)
                if dx <= axis_tolerance_points and dy > axis_tolerance_points:
                    first = transform.page_to_local((page_start.x, page_start.y))
                    second = transform.page_to_local((page_end.x, page_end.y))
                    fixed = (first[0] + second[0]) / 2.0
                    lo, hi = sorted((first[1], second[1]))
                    if not x_min <= fixed <= x_max:
                        continue
                    lo, hi = max(lo, y_min), min(hi, y_max)
                    orientation = "vertical"
                elif dy <= axis_tolerance_points and dx > axis_tolerance_points:
                    first = transform.page_to_local((page_start.x, page_start.y))
                    second = transform.page_to_local((page_end.x, page_end.y))
                    fixed = (first[1] + second[1]) / 2.0
                    lo, hi = sorted((first[0], second[0]))
                    if not y_min <= fixed <= y_max:
                        continue
                    lo, hi = max(lo, x_min), min(hi, x_max)
                    orientation = "horizontal"
                else:
                    continue
                if hi - lo < minimum_face_length_m:
                    continue
                raw_faces.append(Face(orientation, fixed, lo, hi, layer))
                layer_segment_counts[layer] += 1

    deduped: dict[tuple[str, int, int, int], Face] = {}
    for face in raw_faces:
        key = (
            face.orientation,
            round(face.fixed / 0.002),
            round(face.lo / 0.002),
            round(face.hi / 0.002),
        )
        deduped.setdefault(key, face)
    faces = sorted(
        deduped.values(), key=lambda value: (value.orientation, value.fixed, value.lo, value.hi)
    )
    return faces, {
        "wall_layer_suffixes": list(WALL_LAYER_SUFFIXES),
        "required_colour": "black",
        "required_solid_stroke": True,
        "stroke_width_points": stroke_width_points,
        "stroke_width_tolerance_points": stroke_width_tolerance_points,
        "axis_tolerance_points": axis_tolerance_points,
        "minimum_face_length_m": minimum_face_length_m,
        "matched_drawing_path_count": drawing_count,
        "raw_face_count": len(raw_faces),
        "deduplicated_face_count": len(faces),
        "layer_segment_counts": dict(layer_segment_counts.most_common()),
    }


def _closest_expected_thickness(value: float) -> tuple[float, float]:
    expected = min(EXPECTED_WALL_THICKNESSES_M, key=lambda item: abs(item - value))
    return expected, abs(expected - value)


def pair_wall_faces(
    faces: Sequence[Face],
    *,
    thickness_tolerance_m: float = 0.025,
    minimum_overlap_m: float = 0.30,
    minimum_shorter_face_overlap: float = 0.45,
) -> list[dict[str, Any]]:
    """Pair parallel wall faces and return their overlap centre-lines."""

    candidates: list[dict[str, Any]] = []
    for orientation in ("horizontal", "vertical"):
        oriented = [face for face in faces if face.orientation == orientation]
        for index, first in enumerate(oriented):
            for second in oriented[index + 1:]:
                separation = second.fixed - first.fixed
                if separation > max(EXPECTED_WALL_THICKNESSES_M) + thickness_tolerance_m:
                    break
                if separation < min(EXPECTED_WALL_THICKNESSES_M) - thickness_tolerance_m:
                    continue
                expected, thickness_error = _closest_expected_thickness(separation)
                if thickness_error > thickness_tolerance_m:
                    continue
                lo, hi = max(first.lo, second.lo), min(first.hi, second.hi)
                overlap = hi - lo
                if overlap < minimum_overlap_m:
                    continue
                if overlap / min(first.length, second.length) < minimum_shorter_face_overlap:
                    continue
                fixed = (first.fixed + second.fixed) / 2.0
                start = [lo, fixed] if orientation == "horizontal" else [fixed, lo]
                end = [hi, fixed] if orientation == "horizontal" else [fixed, hi]
                candidates.append({
                    "orientation": orientation,
                    "start": start,
                    "end": end,
                    "thickness_m": separation,
                    "expected_thickness_m": expected,
                    "thickness_error_m": thickness_error,
                    "source_layers": sorted({first.layer, second.layer}),
                })
    return candidates


def _wall_axis_values(wall: dict[str, Any]) -> tuple[str, float, float, float]:
    start, end = wall["start"], wall["end"]
    if abs(float(end[0]) - float(start[0])) >= abs(float(end[1]) - float(start[1])):
        return "horizontal", (float(start[1]) + float(end[1])) / 2.0, *sorted(
            (float(start[0]), float(end[0])))
    return "vertical", (float(start[0]) + float(end[0])) / 2.0, *sorted(
        (float(start[1]), float(end[1])))


def merge_wall_centerlines(
    walls: Sequence[dict[str, Any]],
    *,
    fixed_tolerance_m: float = 0.02,
    gap_tolerance_m: float = 0.06,
    id_prefix: str = "pdf-wall",
) -> list[dict[str, Any]]:
    """Merge duplicate/adjacent centre-lines, including sheet-overlap duplicates."""

    by_orientation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for wall in walls:
        orientation, fixed, lo, hi = _wall_axis_values(wall)
        copy = dict(wall)
        copy.update({"orientation": orientation, "fixed": fixed, "lo": lo, "hi": hi})
        by_orientation[orientation].append(copy)

    merged: list[dict[str, Any]] = []
    for orientation, oriented in by_orientation.items():
        fixed_groups: list[list[dict[str, Any]]] = []
        for wall in sorted(oriented, key=lambda item: (item["fixed"], item["lo"])):
            if not fixed_groups:
                fixed_groups.append([wall])
                continue
            group_fixed = statistics.median(item["fixed"] for item in fixed_groups[-1])
            if abs(wall["fixed"] - group_fixed) <= fixed_tolerance_m:
                fixed_groups[-1].append(wall)
            else:
                fixed_groups.append([wall])

        for fixed_group in fixed_groups:
            runs: list[list[dict[str, Any]]] = []
            for wall in sorted(fixed_group, key=lambda item: (item["lo"], item["hi"])):
                if not runs or wall["lo"] > max(item["hi"] for item in runs[-1]) + gap_tolerance_m:
                    runs.append([wall])
                else:
                    runs[-1].append(wall)
            for run in runs:
                fixed = statistics.median(item["fixed"] for item in run)
                lo = min(item["lo"] for item in run)
                hi = max(item["hi"] for item in run)
                thicknesses = [float(item.get("thickness_m") or 0.0) for item in run]
                start = [lo, fixed] if orientation == "horizontal" else [fixed, lo]
                end = [hi, fixed] if orientation == "horizontal" else [fixed, hi]
                merged.append({
                    "start": start,
                    "end": end,
                    "thickness_m": statistics.median(thicknesses),
                    "paired": True,
                    "wall_group": "PDF_VECTOR_REFERENCE",
                    "geometry_source": "PDF_HEAVY_DOUBLE_LINE_WALL_LAYER",
                    "source_layers": sorted({
                        layer for item in run for layer in item.get("source_layers", [])
                    }),
                    "source_sheet_ids": sorted({
                        sheet for item in run for sheet in item.get("source_sheet_ids", [])
                    }),
                })

    for index, wall in enumerate(sorted(
        merged, key=lambda item: (_wall_axis_values(item)[0], _wall_axis_values(item)[1],
                                  _wall_axis_values(item)[2], _wall_axis_values(item)[3])
    ), start=1):
        wall["id"] = f"{id_prefix}-{index:04d}"
    return sorted(merged, key=lambda item: item["id"])


def load_model_preview(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    walls = []
    for item in payload.get("model_elements", []):
        if item.get("type") != "Wall" or not item.get("start") or not item.get("end"):
            continue
        walls.append({
            "id": str(item.get("id") or f"model-wall-{len(walls) + 1}"),
            "start": [float(item["start"][0]) / 1000.0, float(item["start"][1]) / 1000.0],
            "end": [float(item["end"][0]) / 1000.0, float(item["end"][1]) / 1000.0],
            "thickness": float(item.get("thickness") or item.get("thickness_mm") or 0.0),
            "paired": bool(item.get("paired")),
            "wall_group": item.get("wall_group"),
            "geometry_source": item.get("geometry_source"),
            "source_layers": list(item.get("source_layers") or []),
        })
    return walls


def _wall_intersects_bounds(wall: dict[str, Any], bounds: Bounds) -> bool:
    x_min, x_max, y_min, y_max = bounds
    start, end = wall["start"], wall["end"]
    return not (
        max(float(start[0]), float(end[0])) < x_min
        or min(float(start[0]), float(end[0])) > x_max
        or max(float(start[1]), float(end[1])) < y_min
        or min(float(start[1]), float(end[1])) > y_max
    )


def _clip_wall_to_bounds(wall: dict[str, Any], bounds: Bounds) -> dict[str, Any] | None:
    if not _wall_intersects_bounds(wall, bounds):
        return None
    x_min, x_max, y_min, y_max = bounds
    orientation, fixed, lo, hi = _wall_axis_values(wall)
    copy = dict(wall)
    if orientation == "horizontal":
        lo, hi = max(lo, x_min), min(hi, x_max)
        if not y_min <= fixed <= y_max or hi - lo <= 1e-9:
            return None
        copy["start"], copy["end"] = [lo, fixed], [hi, fixed]
    else:
        lo, hi = max(lo, y_min), min(hi, y_max)
        if not x_min <= fixed <= x_max or hi - lo <= 1e-9:
            return None
        copy["start"], copy["end"] = [fixed, lo], [fixed, hi]
    return copy


def _compact_comparison(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items()
            if key not in {"actual_rows", "predicted_rows"}}


def _reference_layer_family(wall: dict[str, Any]) -> str:
    layers = [str(value).upper() for value in wall.get("source_layers") or []]
    if any(layer.endswith("S-WALL") for layer in layers):
        return "structural_wall"
    if any(layer.endswith("A-WALL-CONC") for layer in layers):
        return "architectural_concrete_wall"
    return "architectural_partition_wall"


def _length(wall: dict[str, Any]) -> float:
    return math.dist(tuple(map(float, wall["start"][:2])), tuple(map(float, wall["end"][:2])))


def render_local_diff(
    path: Path,
    reference: list[dict[str, Any]],
    model: list[dict[str, Any]],
    comparison: dict[str, Any],
) -> None:
    from PIL import Image, ImageDraw, ImageFont

    reference_coverage = {row["id"]: row["coverage"] for row in comparison["actual_rows"]}
    model_coverage = {row["id"]: row["coverage"] for row in comparison["predicted_rows"]}
    points = [
        tuple(map(float, point[:2]))
        for wall in [*reference, *model]
        for point in (wall["start"], wall["end"])
    ]
    if not points:
        raise ValueError("cannot render an empty wall comparison")
    x_min, x_max = min(point[0] for point in points), max(point[0] for point in points)
    y_min, y_max = min(point[1] for point in points), max(point[1] for point in points)
    width, height = 3600, 1180
    left, top, right, bottom = 90, 95, 40, 120
    drawing_width = width - left - right
    drawing_height = height - top - bottom
    scale = min(
        drawing_width / max(x_max - x_min, 1e-9),
        drawing_height / max(y_max - y_min, 1e-9),
    )
    x_padding = (drawing_width - (x_max - x_min) * scale) / 2.0
    y_padding = (drawing_height - (y_max - y_min) * scale) / 2.0
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    def pixel(point: Sequence[float]) -> tuple[float, float]:
        return (
            left + x_padding + (float(point[0]) - x_min) * scale,
            top + y_padding + (y_max - float(point[1])) * scale,
        )

    for grid_x in range(math.floor(x_min / 10.0) * 10, math.ceil(x_max / 10.0) * 10 + 1, 10):
        x = pixel((grid_x, y_min))[0]
        draw.line((x, top, x, height - bottom), fill=(225, 225, 225), width=1)
    for grid_y in range(math.floor(y_min / 10.0) * 10, math.ceil(y_max / 10.0) * 10 + 1, 10):
        y = pixel((x_min, grid_y))[1]
        draw.line((left, y, width - right, y), fill=(225, 225, 225), width=1)

    for wall in reference:
        covered = reference_coverage[wall["id"]] >= 0.5
        draw.line([pixel(wall["start"]), pixel(wall["end"])],
                  fill=(44, 160, 44) if covered else (214, 39, 40), width=5)
    for wall in model:
        covered = model_coverage[wall["id"]] >= 0.5
        draw.line([pixel(wall["start"]), pixel(wall["end"])],
                  fill=(31, 119, 180) if covered else (255, 127, 14), width=2)
    recall = comparison["length_weighted_recall"] * 100.0
    precision = comparison["length_weighted_precision"] * 100.0
    title_font = ImageFont.load_default(size=28)
    legend_font = ImageFont.load_default(size=20)
    draw.text((left, 22),
              f"PDF wall reference vs current {len(model)}-wall model | "
              f"length recall {recall:.2f}% | precision {precision:.2f}%",
              fill=(0, 0, 0), font=title_font)
    legend = (
        ((44, 160, 44), "PDF covered"), ((214, 39, 40), "PDF missed"),
        ((31, 119, 180), "Model supported"), ((255, 127, 14), "Model unsupported"),
    )
    legend_x = left
    legend_y = height - 62
    for colour, label in legend:
        draw.line((legend_x, legend_y + 10, legend_x + 55, legend_y + 10), fill=colour, width=6)
        draw.text((legend_x + 65, legend_y), label, fill=(0, 0, 0), font=legend_font)
        legend_x += 420
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def render_pdf_diff(
    path: Path,
    page: Any,
    transform: GridTransform,
    reference: list[dict[str, Any]],
    model: list[dict[str, Any]],
    comparison: dict[str, Any],
    *,
    dpi: int = 60,
) -> None:
    from PIL import Image, ImageDraw, ImageFont
    import pymupdf as fitz

    scale = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    reference_coverage = {row["id"]: row["coverage"] for row in comparison["actual_rows"]}
    model_coverage = {row["id"]: row["coverage"] for row in comparison["predicted_rows"]}

    def pixel(point: Sequence[float]) -> tuple[float, float]:
        page_point = transform.local_to_page((float(point[0]), float(point[1])))
        return page_point[0] * scale, page_point[1] * scale

    for wall in reference:
        covered = reference_coverage[wall["id"]] >= 0.5
        draw.line([pixel(wall["start"]), pixel(wall["end"])],
                  fill=(30, 170, 45, 210) if covered else (220, 25, 25, 235), width=3)
    for wall in model:
        covered = model_coverage[wall["id"]] >= 0.5
        draw.line([pixel(wall["start"]), pixel(wall["end"])],
                  fill=(25, 100, 230, 205) if covered else (255, 125, 0, 230), width=2)

    font = ImageFont.load_default(size=18)
    recall = comparison["length_weighted_recall"] * 100.0
    precision = comparison["length_weighted_precision"] * 100.0
    label = f"Green=covered PDF  Red=missed PDF  Blue=supported model  Orange=unsupported model   R={recall:.2f}%  P={precision:.2f}%"
    draw.rectangle((8, 8, min(image.width - 8, 1150), 42), fill=(255, 255, 255, 225))
    draw.text((16, 14), label, fill=(0, 0, 0, 255), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(image, overlay).convert("RGB").save(path)


def _process_sheet(
    pdf_path: Path,
    sheet_id: str,
    bounds: Bounds,
) -> tuple[Any, Any, GridTransform, list[dict[str, Any]], dict[str, Any]]:
    import pymupdf as fitz

    document = fitz.open(pdf_path)
    if document.page_count != 1:
        document.close()
        raise ValueError(f"expected one page in {pdf_path}, found {document.page_count}")
    page = document[0]
    transform = derive_grid_transform(page)
    faces, extraction = extract_heavy_wall_faces(page, transform, bounds)
    paired = pair_wall_faces(faces)
    for wall in paired:
        wall["source_sheet_ids"] = [sheet_id]
    reference = merge_wall_centerlines(paired, id_prefix=f"{sheet_id.lower()}-pdf-wall")
    extraction.update({
        "bounds_grid_local_m": list(bounds),
        "raw_pair_candidate_count": len(paired),
        "merged_reference_wall_count": len(reference),
        "merged_reference_total_length_m": round(sum(_length(wall) for wall in reference), 3),
    })
    return document, page, transform, reference, extraction


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = json.loads(args.sheets_config.read_text(encoding="utf-8"))
    raw_sheets = config.get("sheets") if isinstance(config, dict) else None
    if not isinstance(raw_sheets, list) or not raw_sheets:
        raise ValueError("sheets config must contain a non-empty sheets list")
    sheet_specs = []
    for item in raw_sheets:
        sheet_id = str(item.get("id") or "").strip()
        pdf_path = Path(str(item.get("path") or ""))
        if not pdf_path.is_absolute():
            pdf_path = args.sheets_config.parent / pdf_path
        bounds = item.get("bounds_grid_local_m")
        if not sheet_id or not isinstance(bounds, list) or len(bounds) != 4:
            raise ValueError("each sheet needs id, path and four bounds_grid_local_m values")
        sheet_specs.append((pdf_path.resolve(), sheet_id, tuple(map(float, bounds))))
    model = load_model_preview(args.model_preview)
    sheet_results = []
    combined_candidates = []
    open_documents = []
    try:
        for pdf_path, sheet_id, bounds in sheet_specs:
            document, page, transform, reference, extraction = _process_sheet(
                pdf_path, sheet_id, bounds)
            open_documents.append(document)
            sheet_model = [
                clipped for wall in model
                if (clipped := _clip_wall_to_bounds(wall, bounds)) is not None
            ]
            sheet_comparison = compare(
                reference, sheet_model,
                angle_tolerance_deg=args.angle_tolerance_deg,
                lateral_tolerance_m=args.lateral_tolerance_m)
            sheet_overlay = args.output.parent / f"{sheet_id.lower()}_pdf_wall_diff.png"
            render_pdf_diff(
                sheet_overlay, page, transform, reference, sheet_model, sheet_comparison)
            sheet_results.append({
                "sheet_id": sheet_id,
                "pdf": str(pdf_path.resolve()),
                "bounds_grid_local_m": list(bounds),
                "alignment": transform.report(),
                "extraction": extraction,
                "comparison": _compact_comparison(sheet_comparison),
                "overlay": str(sheet_overlay.resolve()),
            })
            combined_candidates.extend(reference)

        combined_reference = merge_wall_centerlines(
            combined_candidates, id_prefix="combined-pdf-wall")
        combined_comparison = compare(
            combined_reference, model,
            angle_tolerance_deg=args.angle_tolerance_deg,
            lateral_tolerance_m=args.lateral_tolerance_m)
        paired_model = [wall for wall in model if wall.get("paired")]
        unpaired_model = [wall for wall in model if not wall.get("paired")]
        paired_comparison = compare(
            combined_reference, paired_model,
            angle_tolerance_deg=args.angle_tolerance_deg,
            lateral_tolerance_m=args.lateral_tolerance_m)
        unpaired_comparison = compare(
            combined_reference, unpaired_model,
            angle_tolerance_deg=args.angle_tolerance_deg,
            lateral_tolerance_m=args.lateral_tolerance_m)
        reference_families = {}
        for family in (
            "architectural_partition_wall",
            "architectural_concrete_wall",
            "structural_wall",
        ):
            family_reference = [
                wall for wall in combined_reference
                if _reference_layer_family(wall) == family
            ]
            family_comparison = compare(
                family_reference, model,
                angle_tolerance_deg=args.angle_tolerance_deg,
                lateral_tolerance_m=args.lateral_tolerance_m)
            reference_families[family] = {
                "reference_wall_count": len(family_reference),
                "reference_total_length_m": round(
                    sum(_length(wall) for wall in family_reference), 3),
                "comparison": _compact_comparison(family_comparison),
            }
        render_local_diff(args.overlay, combined_reference, model, combined_comparison)
        report = {
            "schema_version": "buildmate.pdf-model-wall-comparison/1.0",
            "status": "PDF_VECTOR_HEURISTIC_REFERENCE",
            "interpretation": (
                "Reference walls are independently extracted from heavy black double-line vectors "
                "on PDF wall layers. This is higher-confidence than count-ratio or source-gate "
                "coverage, but it is not a manually labelled ground truth."
            ),
            "sources": {
                "model_preview": str(args.model_preview.resolve()),
                "pdf_sheets": [str(spec[0].resolve()) for spec in sheet_specs],
            },
            "reference_extraction": {
                "wall_layer_suffixes": list(WALL_LAYER_SUFFIXES),
                "expected_wall_thicknesses_m": list(EXPECTED_WALL_THICKNESSES_M),
                "independent_of_model_geometry": True,
                "sheets": sheet_results,
                "overlap_merge": {
                    "input_sheet_wall_count": len(combined_candidates),
                    "combined_wall_count": len(combined_reference),
                    "combined_total_length_m": round(
                        sum(_length(wall) for wall in combined_reference), 3),
                },
            },
            "model": {
                "wall_count": len(model),
                "paired_count": sum(bool(wall.get("paired")) for wall in model),
                "unpaired_count": sum(not bool(wall.get("paired")) for wall in model),
                "total_length_m": round(sum(_length(wall) for wall in model), 3),
            },
            "tolerance": {
                "angle_deg": args.angle_tolerance_deg,
                "lateral_m": args.lateral_tolerance_m,
                "coverage_threshold": 0.5,
            },
            "combined_comparison": _compact_comparison(combined_comparison),
            "candidate_subset_comparisons": {
                "paired_only": _compact_comparison(paired_comparison),
                "unpaired_review_only": _compact_comparison(unpaired_comparison),
                "all": _compact_comparison(combined_comparison),
            },
            "reference_layer_family_comparisons": reference_families,
            "limitations": [
                "Only solid black 1.44-point axis-aligned faces on explicit wall layers are used.",
                "Curved, diagonal, hairline-only, filled-only, or incorrectly layered walls are outside this reference.",
                "Door openings remain gaps unless the PDF contains continuous wall faces behind them.",
            ],
            "outputs": {
                "report": str(args.output.resolve()),
                "combined_overlay": str(args.overlay.resolve()),
                "sheet_overlays": [item["overlay"] for item in sheet_results],
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        for document in open_documents:
            document.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheets-config", type=Path, required=True)
    parser.add_argument("--model-preview", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--angle-tolerance-deg", type=float, default=2.0)
    parser.add_argument("--lateral-tolerance-m", type=float, default=0.15)
    args = parser.parse_args()
    report = run(args)
    comparison = report["combined_comparison"]
    print(json.dumps({
        "reference_wall_count": comparison["actual_count"],
        "model_wall_count": comparison["predicted_count"],
        "length_weighted_recall": comparison["length_weighted_recall"],
        "length_weighted_precision": comparison["length_weighted_precision"],
        "report": report["outputs"]["report"],
        "overlay": report["outputs"]["combined_overlay"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
