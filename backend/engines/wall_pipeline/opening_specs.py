"""Rectangular opening schedules: text supplies size/Z, vectors supply XY."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re

from shapely.geometry import LineString, Polygon

from .contracts import EvidenceSourceRef, OpeningRecord, WallRecord, WallPipelineConfig
from .specifications import TextRow


_MARK = re.compile(r"^(?:JD|HOLE|OPENING)\s*-?\s*\d+[A-Z]?$", re.I)
_SIZE = re.compile(r"^(\d{2,5})\s*[xX×*]\s*(\d{2,5})\s*(?:mm|毫米)?$", re.I)
_ELEVATION = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*(mm|毫米|m|米)?$", re.I)


def _text(value: str) -> str:
    value = re.sub(r"\s+", "", value).replace("−", "-").replace("±", "+")
    return value.replace("。", ".")


@dataclass(frozen=True)
class OpeningSpec:
    mark: str
    width_mm: float
    height_mm: float
    sill_m: float
    sill_explicit_sign: bool
    rows: tuple[TextRow, ...]


def infer_opening_elevation_reference(rows: list[TextRow]) -> tuple[str | None, tuple[TextRow, ...]]:
    """Read the opening-datum convention from a drawing note.

    Structural sheets commonly state that the opening centre/elevation is
    changed to the opening *bottom* elevation relative to ``±0.000``.  That
    note is binding evidence for the datum, but it does not invent a missing
    sign on an individual table cell.
    """
    matched: list[TextRow] = []
    for row in rows:
        value = _text(row.text).lower()
        if (("相对标高" in value and ("底相对标高" in value or "洞底" in value)
             and "改为" in value and "标注" in value
             and re.search(r"(?:相对于|相对)\+?0\.000\d*", value))):
            matched.append(row)
    if matched:
        return "project", tuple(matched)
    return None, ()


def extract_opening_specs(
    rows: list[TextRow], *, alignment_m: float, search_m: float,
    mark_pattern: str = _MARK.pattern,
) -> list[OpeningSpec]:
    """Match explicit headers and cells in both upright and rotated tables.

    No nearest arbitrary number is accepted: mark, size and sill cells must
    share a table row AND each must align with its own explicit header.
    """
    result = []
    mark_expression = re.compile(mark_pattern, re.I)
    for mark_header in rows:
        if _text(mark_header.text) not in {"洞口编号", "墙洞编号"}:
            continue
        page_rows = [
            row for row in rows
            if (row.source_file_id, row.frame_id, row.page_no)
            == (mark_header.source_file_id, mark_header.frame_id, mark_header.page_no)
            and math.dist(row.point_m, mark_header.point_m) <= search_m
        ]
        size_headers = [row for row in page_rows if _text(row.text) in {"洞口尺寸", "宽x高", "宽×高", "洞口宽x高"}]
        sill_headers = [row for row in page_rows if _text(row.text) in {"洞底标高", "洞口底标高"}]
        marks = [row for row in page_rows if mark_expression.fullmatch(_text(row.text))]
        sizes = [row for row in page_rows if _SIZE.fullmatch(_text(row.text))]
        elevations = [row for row in page_rows if _ELEVATION.fullmatch(_text(row.text))]
        for size_header in size_headers:
            for sill_header in sill_headers:
                for column_axis in (0, 1):
                    row_axis = 1 - column_axis
                    # Headers lie on the header row, at distinct columns.
                    if any(abs(row.point_m[row_axis] - mark_header.point_m[row_axis]) > alignment_m * 2
                           for row in (size_header, sill_header)):
                        continue
                    if min(abs(size_header.point_m[column_axis] - mark_header.point_m[column_axis]),
                           abs(sill_header.point_m[column_axis] - size_header.point_m[column_axis]),
                           abs(sill_header.point_m[column_axis] - mark_header.point_m[column_axis])) <= alignment_m * 2:
                        continue
                    table_marks = [
                        row for row in marks
                        if abs(row.point_m[column_axis] - mark_header.point_m[column_axis]) <= alignment_m
                    ]
                    for mark_row in table_marks:
                        # Never let a wide OCR tolerance swallow a neighbouring row.
                        separations = [abs(row.point_m[row_axis] - mark_row.point_m[row_axis])
                                       for row in table_marks if row.entity_id != mark_row.entity_id]
                        row_tolerance = min(alignment_m, min(separations) * 0.4) if separations else alignment_m
                        def cells(values, header):
                            return [row for row in values
                                    # OCR bounding-box centres can drift by a
                                    # few tenths of a metre after a rotated
                                    # page is transformed.  Keep this margin
                                    # local to the header column; row matching
                                    # remains separately bounded below.
                                    if abs(row.point_m[column_axis] - header.point_m[column_axis]) <= alignment_m * 1.5
                                    and abs(row.point_m[row_axis] - mark_row.point_m[row_axis]) <= row_tolerance]
                        size_cells = cells(sizes, size_header)
                        sill_cells = cells(elevations, sill_header)
                        if len(size_cells) != 1 or len(sill_cells) != 1:
                            continue
                        size_row, sill_row = size_cells[0], sill_cells[0]
                        width, height = map(float, _SIZE.fullmatch(_text(size_row.text)).groups())
                        value, unit = _ELEVATION.fullmatch(_text(sill_row.text)).groups()
                        sill = float(value) / (1000 if unit and unit.lower() in {"mm", "毫米"} else 1)
                        if not (50 <= width <= 20000 and 50 <= height <= 20000 and abs(sill) <= 1000):
                            continue
                        result.append(OpeningSpec(
                            _text(mark_row.text).upper(), width, height, sill,
                            _text(sill_row.text).startswith(("+", "-")),
                            (mark_header, size_header, sill_header, mark_row, size_row, sill_row),
                        ))
    unique: dict[tuple, OpeningSpec] = {}
    for item in result:
        key = (item.mark, item.width_mm, item.height_mm, item.sill_m,
               item.rows[0].source_file_id, item.rows[0].frame_id, item.rows[0].page_no)
        unique[key] = item
    return list(unique.values())


def resolve_opening_cuts(
    openings: list[OpeningRecord], walls: list[WallRecord], rows: list[TextRow],
    refs_by_id: dict[str, EvidenceSourceRef], config: WallPipelineConfig,
) -> list[OpeningRecord]:
    specs = extract_opening_specs(
        rows, alignment_m=config.opening.schedule_alignment_tolerance_m,
        search_m=config.opening.schedule_search_distance_m,
        mark_pattern=config.opening.label_pattern,
    )
    hosts = {wall.wall_id: wall for wall in walls}
    inferred_reference, datum_rows = infer_opening_elevation_reference(rows)
    effective_reference = config.opening.elevation_reference
    if effective_reference == "unresolved" and inferred_reference is not None:
        effective_reference = inferred_reference
    datum_refs = [refs_by_id[row.entity_id] for row in datum_rows if row.entity_id in refs_by_id]
    result = []
    for opening in openings:
        candidates = [spec for spec in specs if spec.mark == _text(opening.mark).upper()]
        signatures = {(spec.width_mm, spec.height_mm, spec.sill_m) for spec in candidates}
        update = {
            "type_name": opening.mark + "-规格待核定", "cut_status": "review_required",
            "specification_mm": {}, "specification_refs": [],
            "cut_start_m": None, "cut_end_m": None,
            "base_elevation_m": None, "top_elevation_m": None,
            "elevation_source": "unresolved",
        }
        reasons = list(opening.limitations)
        if len(signatures) != 1:
            reasons.append("洞口表尺寸/洞底标高缺失或同编号存在冲突，尚未实际开洞")
        else:
            spec = candidates[0]
            refs = list(dict.fromkeys(row.entity_id for row in spec.rows))
            spec_refs = [refs_by_id[key] for key in refs if key in refs_by_id]
            update["specification_mm"] = {"width_mm": spec.width_mm, "height_mm": spec.height_mm}
            update["specification_refs"] = spec_refs + [ref for ref in datum_refs if ref not in spec_refs]
            update["source_refs"] = list(opening.source_refs) + [
                ref for ref in (*spec_refs, *datum_refs) if ref not in opening.source_refs
            ]
            update["type_name"] = f"{opening.mark}-{int(spec.width_mm)}x{int(spec.height_mm)}mm"
            corrected_sill = config.opening.sill_elevation_overrides_m.get(spec.mark)
            sill = spec.sill_m if corrected_sill is None else corrected_sill
            elevation_source = "drawing" if corrected_sill is None else "input"
            # OCR often drops the minus glyph in a rotated basement table.
            # When the sheet explicitly binds opening elevations to project
            # ±0.000 and the supplied level is wholly below zero, a positive,
            # unsigned table value is a depth below datum.  Explicit signs
            # and operator corrections always win.
            if (corrected_sill is None and not spec.sill_explicit_sign
                    and effective_reference == "project"
                    and config.level.elevation_m < 0
                    and (config.level.top_elevation_m or 0) <= 0
                    and datum_rows
                    and sill > 0):
                sill = -sill
                elevation_source = "drawing_inferred_basement"
            update["elevation_source"] = elevation_source
            reason = None
            if len(spec_refs) != len(refs):
                reason = "洞口表证据引用不完整"
            elif effective_reference == "unresolved":
                reason = "需确认洞底标高基准：项目±0.000或本层底标高"
            elif (opening.status != "matched" or len(opening.host_wall_ids) != 1
                  or len(opening.boundary_m) < 3 or opening.host_wall_ids[0] not in hosts):
                reason = "洞口缺少完整边界或唯一宿主墙"
            else:
                host = hosts[opening.host_wall_ids[0]]
                polygon = Polygon(opening.boundary_m)
                line = LineString((host.start_m, host.end_m))
                base = sill + (config.level.elevation_m if effective_reference == "level" else 0)
                top = base + spec.height_mm / 1000
                length = line.length
                if length <= 0:
                    raise ValueError("opening host wall has zero length: " + host.wall_id)
                along = ((host.end_m[0] - host.start_m[0]) / length,
                         (host.end_m[1] - host.start_m[1]) / length)
                positions = [sum((point[i] - host.start_m[i]) * along[i] for i in (0, 1))
                             for point in opening.boundary_m]
                observed_width = max(positions) - min(positions)
                center = (max(positions) + min(positions)) / 2
                half_width = spec.width_mm / 2000
                if not polygon.is_valid or line.distance(polygon) > host.thickness_m / 2 + 0.005:
                    reason = "洞口边界不与宿主墙相交"
                elif abs(observed_width * 1000 - spec.width_mm) > 5:
                    reason = "洞口平面矢量宽度与图纸表格不一致，需复核比例/宿主"
                elif center - half_width < 0.005 or center + half_width > length - 0.005:
                    reason = "宿主墙未连续覆盖整个洞口，需先复核断墙"
                elif base < config.level.elevation_m or top > config.level.elevation_m + host.height_m:
                    reason = "洞口竖向范围超出宿主墙，请核定标高基准"
                else:
                    update.update({
                        "cut_status": "ready",
                        "cut_start_m": tuple(host.start_m[i] + (center - half_width) * along[i] for i in (0, 1)),
                        "cut_end_m": tuple(host.start_m[i] + (center + half_width) * along[i] for i in (0, 1)),
                        "base_elevation_m": base, "top_elevation_m": top,
                    })
            if reason:
                reasons.append(reason + "；尚未实际开洞")
        update["limitations"] = list(dict.fromkeys(reasons))
        result.append(OpeningRecord.model_validate({**opening.model_dump(), **update}))
    # Reject intersecting cuts on the same host rather than double-cut a wall
    # or let two labels claim the same physical opening.
    conflicted = set()
    for index, first in enumerate(result):
        if first.cut_status != "ready":
            continue
        for other_index in range(index + 1, len(result)):
            other = result[other_index]
            if other.cut_status != "ready" or other.host_wall_ids != first.host_wall_ids:
                continue
            overlap = LineString((first.cut_start_m, first.cut_end_m)).intersection(
                LineString((other.cut_start_m, other.cut_end_m)))
            if overlap.length > 0.001 and min(first.top_elevation_m, other.top_elevation_m) > max(first.base_elevation_m, other.base_elevation_m):
                conflicted.update((index, other_index))
    for index in conflicted:
        result[index] = result[index].model_copy(update={
            "cut_status": "review_required",
            "limitations": [*result[index].limitations, "洞口切割范围重复或重叠；尚未实际开洞"],
        })
    return result
