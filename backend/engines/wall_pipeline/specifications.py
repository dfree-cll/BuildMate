"""Resolve drawing specifications from legends, schedules and details.

This module deliberately does not infer a specification by rounding measured
geometry.  Text is treated as classification/evidence only; coordinates still
come from vector geometry.  A caller may therefore distinguish a real
``267``-millimetre legend value from a measured ``264.7``-millimetre wall.
The same evidence-first rule applies to coupling-beam top/base elevations:
relative level text is resolved only when a beam heading, mark or constrained
schedule row identifies the value.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Iterable, Literal


ComponentKind = Literal["wall", "column", "beam", "unknown"]
SourceKind = Literal["legend", "schedule", "detail", "annotation"]


@dataclass(frozen=True)
class TextRow:
    entity_id: str
    source_file_id: str
    frame_id: str
    page_no: int | None
    text: str
    point_m: tuple[float, float]
    # The source role is supplied by the authenticated pipeline manifest.
    # It lets a vector-only detail sheet be treated as detail evidence even
    # when its CAD text has no machine-readable "详图" title.
    source_role: str = ""


@dataclass(frozen=True)
class ParsedSpecification:
    component_kind: ComponentKind
    mark: str | None
    dimensions_mm: dict[str, float]
    source_kind: SourceKind
    text: str
    entity_ids: tuple[str, ...]
    source_file_id: str
    frame_id: str
    page_no: int | None
    point_m: tuple[float, float]


@dataclass(frozen=True)
class SpecificationResolution:
    status: Literal[
        "resolved_from_legend",
        "resolved_from_schedule",
        "resolved_from_detail",
        "resolved_from_annotation",
        "unresolved",
        "conflict",
    ]
    source_kind: Literal["legend", "schedule", "detail", "annotation", "none"]
    mark: str | None = None
    text: str | None = None
    dimensions_mm: dict[str, float] | None = None
    entity_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class BeamElevationEvidence:
    """A source-traceable top or bottom elevation for one coupling beam."""

    reference: Literal["top", "base"]
    elevation_m: float
    mark: str | None
    source_kind: Literal["drawing", "schedule", "detail", "annotation"]
    text: str
    entity_ids: tuple[str, ...]
    source_file_id: str
    frame_id: str
    page_no: int | None
    point_m: tuple[float, float]


@dataclass(frozen=True)
class BeamElevationResolution:
    """Resolved beam elevation, or an explicit unresolved/conflict result."""

    status: Literal["resolved", "level_default", "unresolved", "conflict"]
    top_elevation_m: float | None = None
    base_elevation_m: float | None = None
    source_kind: Literal[
        "drawing", "schedule", "detail", "annotation", "level_default", "none"
    ] = "none"
    text: str | None = None
    entity_ids: tuple[str, ...] = ()
    reason: str | None = None


# Chinese CAD exports often use either a multiplication sign or a plain x.
_DIMENSIONS_RE = re.compile(
    r"(?<!\d)(\d{2,5}(?:\.\d+)?)\s*(?:x|×|X|\*|\\)\s*"
    r"(\d{2,5}(?:\.\d+)?)(?:\s*(?:x|×|X|\*)\s*"
    r"(\d{2,5}(?:\.\d+)?))?(?!\d)"
)
# OCR on rotated structural schedules occasionally drops the separator in a
# dimension cell (``9001000`` for ``900x1000``), or replaces it with a comma.
# These forms are accepted only for an explicitly classified column mark and
# only when both resulting dimensions are engineering-sized values.
_COMPACT_DIMENSIONS_RE = re.compile(r"^\d{6,8}$")
_SEPARATED_DIMENSIONS_RE = re.compile(
    r"^(\d{2,4})\s*[,;/、]\s*(\d{2,4})$"
)
_DIMENSION_TOKEN_RE = re.compile(
    r"^(?:\d{2,4}\s*(?:x|×|X|\*|\\)\s*\d{2,4}"
    r"|\d{2,4}\s*[,;/、]\s*\d{2,4}|\d{6,8})$",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(\d{2,5}(?:\.\d+)?)(?![A-Za-z0-9])")
_MARK_RE = re.compile(
    r"(?<![A-Z0-9])((?:GBZ|YBZ|KZ|XZ|GZ|GWQ|LL|KL|Q|W)\s*-?\s*\d+[A-Z]?)(?![A-Z0-9])",
    re.IGNORECASE,
)
_WALL_THICKNESS_RE = re.compile(
    r"(?:墙\s*(?:体\s*)?厚(?:度)?|WALL\s*(?:THK|THICKNESS)|THK)"
    r"\s*[:：=]?\s*[^\d@]{0,12}(\d{2,4}(?:\.\d+)?)\s*(?:MM|毫米)?",
    re.IGNORECASE,
)
_COLUMN_SECTION_LABEL_RE = re.compile(
    r"(?:柱\s*(?:截面|尺寸)|COLUMN\s*(?:SECTION|SIZE))",
    re.IGNORECASE,
)
_NON_COLUMN_SECTION_TOKENS = (
    "洞口", "墙洞", "侧墙洞", "梁洞", "过水洞", "孔洞", "宽x高", "宽×高",
    "梁截面", "墙厚",
)

# A PDF table is commonly extracted as two independent rows: a marked wall
# cell (for example ``Q1（3排）``) and the thickness value below/above it.
# These limits are deliberately narrow on the aligned axis and bounded on
# the other axis so a distant plan annotation cannot become a specification.
_SCHEDULE_ALIGNMENT_TOLERANCE_M = 0.10
_SCHEDULE_ALIGNMENT_GAP_M = 8.0
_ABBREVIATED_CONTEXT_DISTANCE_M = 4.0
_PARENTHETICAL_RE = re.compile(r"(?:（[^（）]*）|\([^()]*\))")

# Elevations are deliberately parsed separately from dimensions.  A value
# such as ``0.150`` is meaningful only when a nearby heading or beam mark
# identifies it as an elevation; otherwise it could be a spacing, ratio or
# ordinary drawing annotation.
_ELEVATION_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])([+\-]?\d+(?:\.\d+)?|[+\-]?\.\d+)"
    r"\s*(mm|毫米|m|米)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_STANDALONE_ELEVATION_RE = re.compile(
    r"^[+\-]?\d+(?:\.\d+)?\s*(?:mm|毫米|m|米)?$",
    re.IGNORECASE,
)
_TOP_ELEVATION_RE = re.compile(
    r"(?:梁|连梁|BEAM|COUPLING\s*BEAM)?\s*"
    r"(?:顶|上)(?:部)?\s*(?:相对)?\s*标高|"
    r"(?:BEAM|COUPLING\s*BEAM)\s*TOP\s*ELEVATION",
    re.IGNORECASE,
)
_BASE_ELEVATION_RE = re.compile(
    r"(?:梁|连梁|BEAM|COUPLING\s*BEAM)?\s*"
    r"(?:底|下)(?:部)?\s*(?:相对)?\s*标高|"
    r"(?:BEAM|COUPLING\s*BEAM)\s*(?:BASE|BOTTOM)\s*ELEVATION",
    re.IGNORECASE,
)
# Structural drawing exports often abbreviate a relative elevation as
# ``h+4.200`` / ``h-0.200``.  Require a signed value with a decimal
# decimal digits and a word boundary so pipe notes such as ``DN150_h+5_100``
# are not interpreted as beam elevations.
_H_ELEVATION_RE = re.compile(
    r"(?<![A-Za-z0-9_])h\s*([+\-])\s*"
    r"(\d{1,3})\s*[.,_]\s*(\d{1,3})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
# Only an explicit beam elevation heading may open a schedule association.
# Generic ``h+...`` annotations and explanatory notes also contain the words
# ``梁``/``标高`` in real sheets, but they are not table columns and must not
# be allowed to bind every nearby numeric token to one LL/KL mark.
_BEAM_ELEVATION_HEADER_RE = re.compile(
    r"(?:连梁|梁|BEAM|COUPLING\s*BEAM)\s*"
    r"(?:顶|上)(?:部)?\s*(?:相对)?\s*标高|"
    r"(?:连梁|梁|BEAM|COUPLING\s*BEAM)\s*"
    r"(?:底|下)(?:部)?\s*(?:相对)?\s*标高|"
    r"(?:BEAM|COUPLING\s*BEAM)\s*(?:TOP|BASE|BOTTOM)\s*ELEVATION",
    re.IGNORECASE,
)
_DEFAULT_BEAM_TOP_NOTE_RE = re.compile(
    r"(?:未注明|未标注|未给出|未表示)\s*"
    r"(?:连梁|梁)\s*(?:梁)?\s*(?:顶|上)(?:部)?\s*"
    r"(?:相对)?\s*标高"
    r"[^。；;]{0,80}?"
    r"(?:同|等于|取|为)\s*(?:该|本|同)层\s*"
    r"(?:顶板|楼板|板)\s*(?:标高)?",
    re.IGNORECASE,
)


def _clean(value: str) -> str:
    # Remove common AutoCAD formatting controls, but preserve the original
    # human-readable text in the output separately at the call site.  Do not
    # remove token boundaries: ``GBZ1 600x300`` must not become
    # ``GBZ1600x300`` and inflate the first section dimension to 1600 mm.
    cleaned = re.sub(r"\\[A-Za-z][^;]*;?", "", str(value or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalise_ocr_elevation_text(value: str) -> str:
    """Normalise OCR spacing/punctuation without inventing a value.

    RapidOCR frequently emits a table cell such as ``0. 150`` or uses a
    Chinese full stop/underscore as the decimal separator.  Only characters
    immediately between numeric digits are changed; surrounding text and the
    original evidence string remain untouched.
    """
    text = _clean(value).replace("±", "+")
    text = re.sub(r"(?<=[+\-])\s+(?=\d)", "", text)
    text = re.sub(r"(?<=\d)\s*[，。]\s*(?=\d)", ".", text)
    text = re.sub(r"(?<=\d)\s*[,._]\s*(?=\d)", ".", text)
    return text


def _source_kind(text: str, source_role: str = "") -> SourceKind:
    role = _clean(source_role).lower()
    if any(token in role for token in ("detail", "详图", "大样")):
        return "detail"
    if any(token in role for token in ("schedule", "构件表", "柱表", "墙表")):
        return "schedule"
    if any(token in role for token in ("legend", "图例")):
        return "legend"
    value = _clean(text).lower()
    if any(token in value for token in ("大样", "详图", "detail", "section")):
        return "detail"
    if any(token in value for token in ("构件表", "柱表", "墙表", "schedule", "明细表")):
        return "schedule"
    if any(token in value for token in ("图例", "legend", "说明", "标准")):
        return "legend"
    return "annotation"


def _component_kind(text: str, mark: str | None) -> ComponentKind:
    value = _clean(text).upper()
    if mark and mark.upper().startswith(("LL", "KL")):
        return "beam"
    if any(token in value for token in ("梁", "连梁", "BEAM", "COUPLING BEAM")):
        return "beam"
    if mark and mark.upper().startswith(("KZ", "XZ", "GZ", "YBZ", "GBZ")):
        return "column"
    if any(token in value for token in ("柱", "截面", "COLUMN", "KZ", "XZ", "GZ")):
        return "column"
    if any(token in value for token in ("墙", "墙厚", "剪力", "WALL", "厚度")):
        return "wall"
    if mark and mark.upper().startswith(("Q", "W")):
        return "wall"
    return "unknown"


def _dimension_map(
    text: str, component_kind: ComponentKind, mark: str | None,
) -> dict[str, float]:
    value = _clean(text)
    if component_kind == "wall":
        thickness_match = _WALL_THICKNESS_RE.search(value)
        if thickness_match:
            thickness = float(thickness_match.group(1))
            return {"thickness_mm": thickness} if 50.0 <= thickness <= 2000.0 else {}
        # Some compact legend cells use ``Q1 267`` without repeating the
        # word "thickness".  Accept that form only when the complete
        # remainder is one plausible size; never harvest an arbitrary number
        # from reinforcement, elevation or opening text.
        if mark:
            remainder = _MARK_RE.sub("", value, count=1).strip(" :-：=")
            # PDF table extraction commonly emits a wall row as three words:
            # ``Q1`` + ``（3排）`` + ``500``.  The parenthetical is a
            # reinforcement-row/count annotation, not part of the wall
            # thickness.  Remove only balanced annotations here; the strict
            # full-match below still prevents arbitrary text/numbers from
            # becoming a drawing specification.
            remainder = re.sub(r"（[^（）]*）|\([^()]*\)", "", remainder).strip(" :-：=")
            compact_match = re.fullmatch(
                r"(?:厚(?:度)?\s*[:：=]?\s*)?"
                r"(\d{2,4}(?:\.\d+)?)\s*(?:MM|毫米)?",
                remainder,
                re.IGNORECASE,
            )
            if compact_match:
                thickness = float(compact_match.group(1))
                return {"thickness_mm": thickness} if 50.0 <= thickness <= 2000.0 else {}
        return {}

    if component_kind == "beam":
        match = _DIMENSIONS_RE.search(value)
        if not match or not mark:
            return {}
        first_raw, second_raw, _ = match.groups()
        first, second = float(first_raw), float(second_raw)
        if not (50.0 <= first <= 2000.0 and 50.0 <= second <= 5000.0):
            return {}
        # Beam notation is conventionally b×h: width is the smaller
        # horizontal dimension and depth/height is the larger vertical one.
        return {"width_mm": min(first, second), "depth_mm": max(first, second)}

    if component_kind != "column":
        return {}
    explicit_column_section = bool(_COLUMN_SECTION_LABEL_RE.search(value))
    if (
        any(token.casefold() in value.casefold()
            for token in _NON_COLUMN_SECTION_TOKENS)
        and not explicit_column_section
    ):
        return {}
    match = _DIMENSIONS_RE.search(value)
    if match:
        if not mark and not explicit_column_section:
            return {}
        first_raw, second_raw, third_raw = match.groups()
        first = float(first_raw)
        second = float(second_raw)
        third = float(third_raw) if third_raw is not None else None
        if not (100.0 <= first <= 5000.0 and 100.0 <= second <= 5000.0):
            return {}
        return {
            "width_mm": max(first, second),
            "depth_mm": min(first, second),
            **({"height_mm": third} if third is not None else {}),
        }
    dimension_value = _MARK_RE.sub("", value, count=1).strip(" :-：=")
    separated = _SEPARATED_DIMENSIONS_RE.fullmatch(dimension_value)
    if separated:
        first, second = (float(item) for item in separated.groups())
        if 100.0 <= first <= 5000.0 and 100.0 <= second <= 5000.0:
            return {"width_mm": max(first, second), "depth_mm": min(first, second)}
    if _COMPACT_DIMENSIONS_RE.fullmatch(dimension_value):
        # Try every 3/4-digit split instead of a greedy regex.  For example,
        # ``9001000`` must be read as 900 + 1000, while ``1400300`` is
        # 1400 + 300.  Reject candidates outside engineering dimensions.
        candidates = []
        for split in range(3, len(dimension_value) - 2):
            first = float(dimension_value[:split])
            second = float(dimension_value[split:])
            if 100.0 <= first <= 5000.0 and 100.0 <= second <= 5000.0:
                candidates.append((first, second))
        if candidates:
            # Prefer the split with the widest first value; this resolves
            # seven-digit OCR tokens such as 1400300 and 9001000 without
            # changing ordinary separated dimensions.
            first, second = max(candidates, key=lambda pair: pair[0])
            return {"width_mm": max(first, second), "depth_mm": min(first, second)}
    numbers = [float(item) for item in _NUMBER_RE.findall(value)]
    if not numbers:
        return {}
    if explicit_column_section and len(numbers) == 1 and 100.0 <= numbers[0] <= 5000.0:
        return {"width_mm": numbers[0], "depth_mm": numbers[0]}
    return {}


def parse_text(text: str, *, entity_ids: tuple[str, ...], source_file_id: str,
               frame_id: str, page_no: int | None,
               point_m: tuple[float, float], source_role: str = "") -> ParsedSpecification | None:
    """Parse one legend/schedule/detail row, without using coordinates."""

    cleaned = _clean(text)
    if not cleaned:
        return None
    mark_match = _MARK_RE.search(cleaned.upper())
    mark = re.sub(r"\s+", "", mark_match.group(1)).upper() if mark_match else None
    kind = _component_kind(cleaned, mark)
    dimensions = _dimension_map(cleaned, kind, mark)
    if not dimensions:
        return None
    return ParsedSpecification(
        component_kind=kind,
        mark=mark,
        dimensions_mm=dimensions,
        source_kind=_source_kind(cleaned, source_role),
        text=str(text).strip(),
        entity_ids=entity_ids,
        source_file_id=source_file_id,
        frame_id=frame_id,
        page_no=page_no,
        point_m=point_m,
    )


def _beam_mark(text: str) -> str | None:
    match = _MARK_RE.search(_clean(text).upper())
    if match is None:
        return None
    value = re.sub(r"\s+", "", match.group(1)).upper()
    return value if value.startswith(("LL", "KL")) else None


def _elevation_reference(text: str) -> Literal["top", "base"] | None:
    value = _clean(text)
    # Wall/column/slab schedules may use the same generic "顶/底标高" heading.
    # They are not coupling-beam elevations and must not enter this index.
    if re.search(r"(?:墙|墙体|柱|板|楼板|洞|管|门|窗|设备|COLUMN).*(?:顶|底).*标高", value, re.IGNORECASE) \
            and not re.search(r"(?:梁|BEAM|COUPLING)", value, re.IGNORECASE):
        return None
    if _BASE_ELEVATION_RE.search(value):
        return "base"
    if _TOP_ELEVATION_RE.search(value):
        return "top"
    h_match = _abbreviated_beam_elevation_match(value)
    if h_match is not None:
        # In Chinese structural notation a positive ``h+`` annotation denotes
        # the beam top relative elevation and a negative ``h-`` annotation
        # denotes the beam bottom.  This fallback is only used when no full
        # Chinese/English heading survived source extraction.
        return "top" if h_match.group(1) == "+" else "base"
    return None


def _parse_elevation_value(text: str) -> float | None:
    """Parse one explicit elevation value, normalising the result to metres."""

    value = _normalise_ocr_elevation_text(text)
    # A datum inside a parenthetical note (for example ``相对于±0.000``) is
    # not the beam elevation itself.  Removing balanced notes also keeps the
    # parser from mistaking that datum for a target value.
    value = _PARENTHETICAL_RE.sub(" ", value)
    match = _ELEVATION_VALUE_RE.search(value)
    if match is None:
        return None
    number = float(match.group(1))
    unit = str(match.group(2) or "m").lower()
    if unit in {"mm", "毫米"}:
        number /= 1000.0
    if not (-100.0 <= number <= 100.0):
        return None
    return number


def _parse_h_elevation_value(text: str) -> float | None:
    """Parse an abbreviated signed ``h+/-x.xxx`` elevation token."""

    match = _abbreviated_beam_elevation_match(text)
    if match is None:
        return None
    number = float("%s%s.%s" % (
        match.group(1), match.group(2), match.group(3),
    ))
    return number if -100.0 <= number <= 100.0 else None


def _abbreviated_beam_elevation_match(text: str) -> re.Match[str] | None:
    """Return a safe ``h+/-`` match, excluding pipe/duct annotations."""

    value = _normalise_ocr_elevation_text(text)
    for match in _H_ELEVATION_RE.finditer(value):
        prefix = value[max(0, match.start() - 24):match.start()]
        # MEP notes use the same ``h+`` token for pipe invert/center heights;
        # those rows are not structural-beam evidence.
        if re.search(
            r"(?:\bD?\s*N\s*\d{2,4}\b|\b[0O]\s*N\s*\d{2,4}\b|"
            r"PIPE|管|洞|门|窗|设备)",
            prefix,
            re.IGNORECASE,
        ):
            continue
        suffix = value[match.end():]
        if suffix and not re.fullmatch(r"[\s,;:：，；。）》)]*", suffix):
            continue
        return match
    return None


def _elevation_source_kind(
    row: TextRow, context: str = "",
) -> Literal["drawing", "schedule", "detail", "annotation"]:
    role = _clean(row.source_role).lower()
    value = (role + " " + _clean(context)).lower()
    if any(token in value for token in ("detail", "详图", "大样")):
        return "detail"
    if any(token in value for token in ("schedule", "构件表", "连梁表", "梁表")):
        return "schedule"
    if role in {"plan", "structural", "structure", "drawing"}:
        return "drawing"
    return "annotation"


def _direct_beam_elevation_evidence(row: TextRow) -> list[BeamElevationEvidence]:
    """Parse rows that contain a beam mark and an elevation heading/value."""

    cleaned = _PARENTHETICAL_RE.sub(" ", _clean(row.text))
    mark = _beam_mark(cleaned)
    records: list[BeamElevationEvidence] = []
    if mark is not None and _elevation_reference(cleaned) is not None:
        explicit_references: set[str] = set()
        for reference, pattern in (("top", _TOP_ELEVATION_RE), ("base", _BASE_ELEVATION_RE)):
            for match in pattern.finditer(cleaned):
                parsed = _parse_elevation_value(cleaned[match.end():])
                if parsed is None:
                    continue
                explicit_references.add(reference)
                records.append(BeamElevationEvidence(
                    reference=reference,
                    elevation_m=parsed,
                    mark=mark,
                    source_kind=_elevation_source_kind(row),
                    text=str(row.text).strip(),
                    entity_ids=(row.entity_id,),
                    source_file_id=row.source_file_id,
                    frame_id=row.frame_id,
                    page_no=row.page_no,
                    point_m=row.point_m,
                ))
        # A marked detail annotation often omits the Chinese heading and
        # writes ``LL16 h+5.000`` / ``LL16 h-0.200`` directly.  Keep this
        # evidence when that reference side was not already supplied by an
        # explicit 梁顶/梁底 heading, while still applying the pipe-context
        # exclusion in ``_abbreviated_beam_elevation_match``.
        h_match = _abbreviated_beam_elevation_match(cleaned)
        h_value = _parse_h_elevation_value(cleaned)
        h_reference = (
            "top" if h_match is not None and h_match.group(1) == "+" else "base"
            if h_match is not None else None
        )
        if h_match is not None and h_value is not None and h_reference not in explicit_references:
            records.append(BeamElevationEvidence(
                reference=h_reference,
                elevation_m=h_value,
                mark=mark,
                source_kind=_elevation_source_kind(row),
                text=str(row.text).strip(),
                entity_ids=(row.entity_id,),
                source_file_id=row.source_file_id,
                frame_id=row.frame_id,
                page_no=row.page_no,
                point_m=row.point_m,
            ))
    # When the source extractor keeps the abbreviated ``h+/-`` form but loses
    # the preceding Chinese heading, retain it as unmarked evidence.  The
    # resolver can still bind it by frame and proximity, but it cannot assign
    # a beam merely from this text row.
    if mark is None:
        heading_reference = _elevation_reference(cleaned)
        if heading_reference is not None:
            pattern = (
                _TOP_ELEVATION_RE
                if heading_reference == "top" else _BASE_ELEVATION_RE
            )
            for match in pattern.finditer(cleaned):
                parsed = _parse_elevation_value(cleaned[match.end():])
                if parsed is None:
                    continue
                records.append(BeamElevationEvidence(
                    reference=heading_reference,
                    elevation_m=parsed,
                    mark=None,
                    source_kind=_elevation_source_kind(row),
                    text=str(row.text).strip(),
                    entity_ids=(row.entity_id,),
                    source_file_id=row.source_file_id,
                    frame_id=row.frame_id,
                    page_no=row.page_no,
                    point_m=row.point_m,
                ))
        h_match = _abbreviated_beam_elevation_match(cleaned)
        h_value = _parse_h_elevation_value(cleaned)
        if h_match is not None and h_value is not None:
            records.append(BeamElevationEvidence(
                reference="top" if h_match.group(1) == "+" else "base",
                elevation_m=h_value,
                mark=None,
                source_kind=_elevation_source_kind(row),
                text=str(row.text).strip(),
                entity_ids=(row.entity_id,),
                source_file_id=row.source_file_id,
                frame_id=row.frame_id,
                page_no=row.page_no,
                point_m=row.point_m,
            ))
    return records


def _standalone_elevation_value(row: TextRow) -> float | None:
    cleaned = _normalise_ocr_elevation_text(row.text)
    if not _STANDALONE_ELEVATION_RE.fullmatch(cleaned):
        return None
    # A schedule may legitimately write the datum as ``0`` or ``-1``.  Bare
    # positive integers such as ``12`` are overwhelmingly OCR fragments from
    # detail dimensions, while repeated zeroes (``000``/``00``) are common
    # cropped-text artefacts.  Require a decimal, unit, or explicit sign for
    # non-zero values so those fragments cannot become beam elevations.
    if re.fullmatch(r"[+-]?\d+", cleaned):
        unsigned = cleaned.lstrip("+-")
        if len(unsigned) > 1 and set(unsigned) == {"0"}:
            return None
        if unsigned != "0" and not cleaned.startswith(("+", "-")):
            return None
    return _parse_elevation_value(cleaned)


def _schedule_elevation_value(
    row: TextRow, *, relative_heading: bool,
    infer_unsigned_relative_negative: bool = False,
) -> float | None:
    """Read a schedule cell, recovering a dropped minus glyph conservatively.

    Raster OCR occasionally returns the table's ``-0.150`` cell as
    ``0.150``.  Only a decimal cell under the explicit relative-elevation
    heading and an explicitly enabled structural-table context is eligible
    for this recovery; ordinary plan dimensions and unsigned integer
    fragments remain rejected by ``_standalone_elevation_value``.
    """

    value = _standalone_elevation_value(row)
    structural_source = _clean(row.source_role).lower() in {
        "structural_plan", "structural_detail", "structure", "structural",
    }
    if value is not None:
        cleaned = _normalise_ocr_elevation_text(row.text)
        if (
            relative_heading
            and (structural_source or infer_unsigned_relative_negative)
            and not cleaned.startswith(("+", "-"))
            and re.fullmatch(r"\d+\.\d+", cleaned)
        ):
            return -abs(value)
        return value
    if not relative_heading or not (structural_source or infer_unsigned_relative_negative):
        return None
    cleaned = _normalise_ocr_elevation_text(row.text)
    if not re.fullmatch(r"\d+\.\d+", cleaned):
        return None
    parsed = _parse_elevation_value(cleaned)
    return -abs(parsed) if parsed is not None else None


def _default_beam_top_note(rows: Iterable[TextRow], frame_id: str) -> TextRow | None:
    """Find the drawing note that binds unspecified beam tops to this level.

    The note does not create a numeric elevation by itself.  The caller must
    provide the user's explicit level top (for example the ``0`` in
    ``-6.4~0``); the returned text is retained as the audit reference.
    """
    candidates = [
        row for row in rows
        if row.frame_id == frame_id
        and _DEFAULT_BEAM_TOP_NOTE_RE.search(_clean(row.text)) is not None
    ]
    return sorted(candidates, key=lambda row: (row.page_no or 0, row.entity_id))[0] if candidates else None


def _is_beam_elevation_header(text: str) -> bool:
    """Return whether text is a real beam elevation column heading.

    This deliberately excludes default-level notes and abbreviated ``h``
    annotations.  The distinction is important because schedule values can
    be several metres away from the heading after a page rotation; accepting
    every elevation-like text as a header creates cross-table false matches.
    """

    cleaned = _clean(text)
    if not cleaned or _DEFAULT_BEAM_TOP_NOTE_RE.search(cleaned) is not None:
        return False
    if _H_ELEVATION_RE.search(cleaned) is not None:
        return False
    return _BEAM_ELEVATION_HEADER_RE.search(cleaned) is not None


def extract_beam_elevation_evidence(
    rows: Iterable[TextRow], *,
    association_distance_m: float = 25.0,
    alignment_tolerance_m: float = 0.20,
    column_tolerance_m: float = 4.0,
    infer_unsigned_relative_negative: bool = False,
) -> list[BeamElevationEvidence]:
    """Extract direct and table-based coupling-beam elevation evidence.

    Structural schedules frequently place the heading and numeric values on
    one row, while the LL/KL marks and beam sections are on another row.  The
    association is limited to the same frame/page and a bounded table axis;
    it never uses a free-floating number as a model elevation.  The heading
    and numeric cell must share an aligned table axis; the full association
    bound is only used along that axis so a dense detail sheet cannot
    cross-bind unrelated columns.
    """

    values = [row for row in rows if _clean(row.text)]
    records = (_direct_beam_elevation_evidence(row) for row in values)
    result: list[BeamElevationEvidence] = [item for group in records for item in group]
    # OCR may split a pipe note into separate rows (for example ``DN150`` on
    # one row and ``h+5.000`` on the next).  Remove an unmarked abbreviated
    # elevation only when a pipe/duct/opening clue is spatially adjacent; a
    # standalone structural h-notation remains available for beam association.
    filtered_result: list[BeamElevationEvidence] = []
    for item in result:
        if (
            _H_ELEVATION_RE.search(item.text)
            and not re.search(r"(?:梁|连梁|BEAM|COUPLING)", item.text, re.IGNORECASE)
        ):
            has_non_beam_context = any(
                candidate.frame_id == item.frame_id
                and candidate.page_no == item.page_no
                and math.dist(candidate.point_m, item.point_m)
                <= _ABBREVIATED_CONTEXT_DISTANCE_M
                and re.search(
                    r"(?:\bD?\s*N\s*\d{2,4}\b|\b[0O]\s*N\s*\d{2,4}\b|"
                    r"PIPE|管|洞|门|窗|设备)", candidate.text,
                    re.IGNORECASE,
                )
                for candidate in values
                if candidate.entity_id not in item.entity_ids
            )
            if has_non_beam_context:
                continue
        filtered_result.append(item)
    result = filtered_result
    headers = [
        (row, _elevation_reference(row.text))
        for row in values
        if _elevation_reference(row.text) is not None
        and _is_beam_elevation_header(row.text)
        and _beam_mark(row.text) is None
    ]
    beam_marks = [
        (mark, row) for mark, row in marks(values)
        if mark.startswith(("LL", "KL"))
    ]
    for header, reference in headers:
        # ``_clean`` also normalizes legacy OCR mojibake, so matching the
        # literal Chinese phrase is not reliable here.  The header has
        # already been accepted as the beam-top elevation column; treat that
        # top schedule as relative when its source is a structural sheet.
        relative_heading = reference == "top"
        numeric_rows = [
            row for row in values
            if row.frame_id == header.frame_id
            and row.page_no == header.page_no
            and _schedule_elevation_value(
                row,
                relative_heading=relative_heading,
                infer_unsigned_relative_negative=infer_unsigned_relative_negative,
            ) is not None
            and row.entity_id != header.entity_id
        ]
        nearby_context = " ".join(
            row.text for row in values
            if row.frame_id == header.frame_id
            and row.page_no == header.page_no
            and ((row.point_m[0] - header.point_m[0]) ** 2
                 + (row.point_m[1] - header.point_m[1]) ** 2) ** 0.5
            <= association_distance_m
        )
        source_kind = _elevation_source_kind(header, nearby_context)
        for numeric in numeric_rows:
            dx_header = abs(numeric.point_m[0] - header.point_m[0])
            dy_header = abs(numeric.point_m[1] - header.point_m[1])
            # After page rotation the heading and all cells in one column
            # share the same X (or Y) coordinate, while the table's row axis
            # can span much farther than ``column_tolerance_m``.  Constrain
            # the perpendicular axis tightly, but use the full bounded table
            # association distance along the column axis.
            if min(dx_header, dy_header) > alignment_tolerance_m \
                    or max(dx_header, dy_header) > association_distance_m:
                continue
            candidates: list[tuple[float, str, TextRow]] = []
            for mark, mark_row in beam_marks:
                if mark_row.frame_id != numeric.frame_id or mark_row.page_no != numeric.page_no:
                    continue
                dx = abs(mark_row.point_m[0] - numeric.point_m[0])
                dy = abs(mark_row.point_m[1] - numeric.point_m[1])
                if min(dx, dy) > alignment_tolerance_m or max(dx, dy) > association_distance_m:
                    continue
                score = min(dx, dy) + 0.05 * max(dx, dy)
                candidates.append((score, mark, mark_row))
            candidates.sort(key=lambda item: (item[0], item[1], item[2].entity_id))
            selected = candidates[0] if candidates else None
            mark = selected[1] if selected is not None else None
            entity_ids = [header.entity_id, numeric.entity_id]
            point = numeric.point_m
            text_parts = [header.text, numeric.text]
            if selected is not None:
                entity_ids.append(selected[2].entity_id)
                text_parts.insert(0, selected[2].text)
            schedule_value = _schedule_elevation_value(
                numeric,
                relative_heading=relative_heading,
                infer_unsigned_relative_negative=infer_unsigned_relative_negative,
            )
            if schedule_value is None:
                continue
            result.append(BeamElevationEvidence(
                reference=reference,
                elevation_m=float(schedule_value),
                mark=mark,
                source_kind=source_kind,
                text=" ".join(str(item).strip() for item in text_parts if str(item).strip()),
                entity_ids=tuple(dict.fromkeys(entity_ids)),
                source_file_id=numeric.source_file_id,
                frame_id=numeric.frame_id,
                page_no=numeric.page_no,
                point_m=point,
            ))
    unique: dict[tuple, BeamElevationEvidence] = {}
    for item in result:
        key = (
            item.reference, item.mark, round(item.elevation_m, 9),
            item.frame_id, item.page_no, item.entity_ids,
        )
        unique[key] = item
    return sorted(
        unique.values(),
        key=lambda item: (item.frame_id, item.page_no or 0, item.point_m, item.reference, item.mark or ""),
    )


def resolve_beam_elevation(
    *,
    element_mark: str | None,
    anchor_m: tuple[float, float],
    frame_id: str,
    rows: Iterable[TextRow],
    beam_depth_m: float,
    association_distance_m: float = 25.0,
    alignment_tolerance_m: float = 0.20,
    column_tolerance_m: float = 4.0,
    tolerance_m: float = 0.005,
    default_top_elevation_m: float | None = None,
    infer_unsigned_relative_negative: bool = False,
) -> BeamElevationResolution:
    """Resolve one beam's absolute project elevations from drawing evidence."""

    normalized_mark = re.sub(r"\s+", "", str(element_mark or "")).upper()
    # A marked LL/KL elevation may live on a separate detail/schedule sheet
    # from the plan geometry.  Keep exact mark matches cross-sheet (like
    # specification resolution); unmarked numeric annotations remain bound to
    # the geometry frame to avoid borrowing a nearby value from another view.
    all_evidence = extract_beam_elevation_evidence(
        rows,
        association_distance_m=association_distance_m,
        alignment_tolerance_m=alignment_tolerance_m,
        column_tolerance_m=column_tolerance_m,
        infer_unsigned_relative_negative=infer_unsigned_relative_negative,
    )
    evidence = [item for item in all_evidence if item.frame_id == frame_id]
    marked = [item for item in all_evidence if normalized_mark and item.mark == normalized_mark]
    if marked:
        candidates = marked
    else:
        candidates = [
            item for item in evidence
            if item.mark is None
            and ((item.point_m[0] - anchor_m[0]) ** 2
                 + (item.point_m[1] - anchor_m[1]) ** 2) ** 0.5
            <= association_distance_m
        ]
    if not candidates:
        default_note = _default_beam_top_note(rows, frame_id)
        if default_note is not None and default_top_elevation_m is not None:
            top_value = float(default_top_elevation_m)
            base_value = top_value - float(beam_depth_m)
            return BeamElevationResolution(
                status="level_default",
                top_elevation_m=round(top_value, 6),
                base_elevation_m=round(base_value, 6),
                source_kind="level_default",
                text=str(default_note.text).strip(),
                entity_ids=(default_note.entity_id,),
                reason="drawing note binds unspecified beam top to the supplied level top",
            )
        return BeamElevationResolution(
            status="unresolved",
            reason="no source-traceable top or base elevation is associated with beam",
        )

    priority = {"detail": 0, "schedule": 1, "drawing": 2, "annotation": 3}
    selected_by_reference: dict[str, BeamElevationEvidence] = {}
    for reference in ("top", "base"):
        grouped = [item for item in candidates if item.reference == reference]
        if not grouped:
            continue
        values = {round(item.elevation_m, 6) for item in grouped}
        if len(values) > 1:
            return BeamElevationResolution(
                status="conflict",
                reason="multiple drawing elevations conflict for " + reference,
                entity_ids=tuple(dict.fromkeys(
                    entity_id for item in grouped for entity_id in item.entity_ids
                )),
            )
        selected_by_reference[reference] = sorted(
            grouped,
            key=lambda item: (priority[item.source_kind], item.entity_ids),
        )[0]
    top = selected_by_reference.get("top")
    base = selected_by_reference.get("base")
    top_value = top.elevation_m if top else None
    base_value = base.elevation_m if base else None
    if top_value is not None and base_value is None:
        base_value = top_value - float(beam_depth_m)
    elif base_value is not None and top_value is None:
        top_value = base_value + float(beam_depth_m)
    if top_value is None or base_value is None:
        return BeamElevationResolution(
            status="unresolved",
            reason="beam elevation evidence has no usable top/base value",
        )
    if abs((top_value - base_value) - float(beam_depth_m)) > tolerance_m:
        return BeamElevationResolution(
            status="conflict",
            top_elevation_m=top_value,
            base_elevation_m=base_value,
            reason="beam top/base elevation does not match resolved beam depth",
            entity_ids=tuple(dict.fromkeys(
                entity_id
                for item in selected_by_reference.values()
                for entity_id in item.entity_ids
            )),
        )
    selected = sorted(
        selected_by_reference.values(),
        key=lambda item: (priority[item.source_kind], item.entity_ids),
    )
    return BeamElevationResolution(
        status="resolved",
        top_elevation_m=round(float(top_value), 6),
        base_elevation_m=round(float(base_value), 6),
        source_kind=selected[0].source_kind,
        text="；".join(dict.fromkeys(item.text for item in selected)),
        entity_ids=tuple(dict.fromkeys(
            entity_id for item in selected for entity_id in item.entity_ids
        )),
    )


def extract(rows: Iterable[TextRow], *, neighbour_radius_m: float = 0.80,
            baseline_tolerance_m: float = 0.20,
            schedule_alignment_tolerance_m: float = _SCHEDULE_ALIGNMENT_TOLERANCE_M,
            schedule_alignment_gap_m: float = _SCHEDULE_ALIGNMENT_GAP_M,
            column_association_radius_m: float = 1.50) -> list[ParsedSpecification]:
    """Extract direct and nearby-token specifications from source text.

    PDF word extraction frequently emits ``墙厚`` and ``267`` as separate
    words.  Parsing each word and a bounded same-frame combination covers that
    case without turning a distant table cell into a model coordinate.
    """

    values = [row for row in rows if row.text.strip()]
    result: dict[tuple, ParsedSpecification] = {}
    for row in values:
        parsed = parse_text(
            row.text,
            entity_ids=(row.entity_id,),
            source_file_id=row.source_file_id,
            frame_id=row.frame_id,
            page_no=row.page_no,
            point_m=row.point_m,
            source_role=row.source_role,
        )
        if parsed:
            result[(parsed.component_kind, parsed.mark, tuple(sorted(parsed.dimensions_mm.items())), parsed.entity_ids)] = parsed
        cluster = [
            other for other in values
            if other.frame_id == row.frame_id
            and other.page_no == row.page_no
            and abs(other.point_m[1] - row.point_m[1]) <= baseline_tolerance_m
            and ((other.point_m[0] - row.point_m[0]) ** 2
                 + (other.point_m[1] - row.point_m[1]) ** 2) ** 0.5 <= neighbour_radius_m
        ]
        if len(cluster) > 2:
            ordered_cluster = sorted(cluster, key=lambda item: item.point_m[0])
            joined_cluster = "".join(item.text for item in ordered_cluster)
            parsed = parse_text(
                joined_cluster,
                entity_ids=tuple(item.entity_id for item in ordered_cluster),
                source_file_id=row.source_file_id,
                frame_id=row.frame_id,
                page_no=row.page_no,
                point_m=(
                    sum(item.point_m[0] for item in ordered_cluster) / len(ordered_cluster),
                    sum(item.point_m[1] for item in ordered_cluster) / len(ordered_cluster),
                ),
                source_role=row.source_role,
            )
            if parsed:
                key = (parsed.component_kind, parsed.mark, tuple(sorted(parsed.dimensions_mm.items())), parsed.entity_ids)
                result[key] = parsed
        for other in values:
            if other.entity_id == row.entity_id or other.frame_id != row.frame_id:
                continue
            if other.page_no != row.page_no:
                continue
            dx = row.point_m[0] - other.point_m[0]
            dy = row.point_m[1] - other.point_m[1]
            if abs(dy) > baseline_tolerance_m:
                continue
            if (dx * dx + dy * dy) ** 0.5 > neighbour_radius_m:
                continue
            ordered = sorted((row, other), key=lambda item: item.point_m[0])
            joined = "".join(item.text for item in ordered)
            parsed = parse_text(
                joined,
                entity_ids=tuple(item.entity_id for item in ordered),
                source_file_id=row.source_file_id,
                frame_id=row.frame_id,
                page_no=row.page_no,
                point_m=(
                    sum(item.point_m[0] for item in ordered) / len(ordered),
                    sum(item.point_m[1] for item in ordered) / len(ordered),
                ),
                source_role=row.source_role,
            )
            if parsed:
                key = (parsed.component_kind, parsed.mark, tuple(sorted(parsed.dimensions_mm.items())), parsed.entity_ids)
                result[key] = parsed

        # Schedule columns are often rotated with the PDF page.  After the
        # source-frame transform, the mark and its value can therefore share
        # either X or Y while the other coordinate contains the table row
        # spacing.  Only pair a marked cell that carries a parenthetical row
        # count (``Q1（3排）``) with a standalone numeric cell.  This keeps
        # ordinary plan annotations from being interpreted as a table row.
        row_text = _clean(row.text)
        row_mark = _MARK_RE.search(row_text.upper())
        row_remainder = (
            _PARENTHETICAL_RE.sub("", _MARK_RE.sub("", row_text, count=1)).strip()
            if row_mark else ""
        )
        row_is_marked_schedule = bool(
            row_mark and _PARENTHETICAL_RE.search(row_text) and not row_remainder
        )
        if row_is_marked_schedule:
            for other in values:
                if other.entity_id == row.entity_id or other.frame_id != row.frame_id:
                    continue
                if other.page_no != row.page_no:
                    continue
                numeric_text = _clean(other.text)
                if not re.fullmatch(
                    r"\d{2,4}(?:\.\d+)?\s*(?:MM|毫米)?", numeric_text,
                    re.IGNORECASE,
                ):
                    continue
                dx = abs(row.point_m[0] - other.point_m[0])
                dy = abs(row.point_m[1] - other.point_m[1])
                if min(dx, dy) > schedule_alignment_tolerance_m:
                    continue
                if max(dx, dy) > schedule_alignment_gap_m:
                    continue
                parsed = parse_text(
                    f"{row.text} {other.text}",
                    entity_ids=(row.entity_id, other.entity_id),
                    source_file_id=row.source_file_id,
                    frame_id=row.frame_id,
                    page_no=row.page_no,
                    point_m=(
                        (row.point_m[0] + other.point_m[0]) / 2.0,
                        (row.point_m[1] + other.point_m[1]) / 2.0,
                    ),
                    source_role=row.source_role,
                )
                if parsed:
                    key = (
                        parsed.component_kind,
                        parsed.mark,
                        tuple(sorted(parsed.dimensions_mm.items())),
                        parsed.entity_ids,
                    )
                    result[key] = parsed

        # Structural column legends are also frequently laid out as a mark
        # cell next to a dimension cell without a parenthetical row count.
        # Pair only an explicitly column-like mark with a standalone,
        # engineering-sized dimension token.  This keeps ordinary plan
        # numbers out of the specification index while recovering OCR forms
        # such as ``GBZ42`` + ``9001000``.
        column_mark = bool(
            row_mark and row_mark.group(1).upper().replace(" ", "").startswith(
                ("GBZ", "YBZ", "KZ", "XZ", "GZ")
            )
        )
        if column_mark and not _PARENTHETICAL_RE.search(row_text):
            for other in values:
                if other.entity_id == row.entity_id or other.frame_id != row.frame_id:
                    continue
                if other.page_no != row.page_no:
                    continue
                numeric_text = _clean(other.text)
                if not (
                    _DIMENSION_TOKEN_RE.fullmatch(numeric_text)
                    # OCR can retain a short, unreadable legend prefix before
                    # an otherwise reliable ``1600x1100`` token.
                    or _DIMENSIONS_RE.search(numeric_text)
                ):
                    continue
                distance = ((row.point_m[0] - other.point_m[0]) ** 2
                            + (row.point_m[1] - other.point_m[1]) ** 2) ** 0.5
                if distance > column_association_radius_m:
                    continue
                parsed = parse_text(
                    f"{row.text} {other.text}",
                    entity_ids=(row.entity_id, other.entity_id),
                    source_file_id=row.source_file_id,
                    frame_id=row.frame_id,
                    page_no=row.page_no,
                    point_m=(
                        (row.point_m[0] + other.point_m[0]) / 2.0,
                        (row.point_m[1] + other.point_m[1]) / 2.0,
                    ),
                    source_role=row.source_role,
                )
                if parsed:
                    key = (
                        parsed.component_kind,
                        parsed.mark,
                        tuple(sorted(parsed.dimensions_mm.items())),
                        parsed.entity_ids,
                    )
                    result[key] = parsed

        # Coupling-beam schedules may export the LL/KL mark and its b×h cell
        # as separate PDF words/rows.  Pair only an explicit beam mark with a
        # standalone engineering dimension token; the aligned-axis and
        # bounded-gap checks prevent nearby plan dimensions from becoming a
        # beam specification.
        beam_mark = bool(
            row_mark and row_mark.group(1).upper().replace(" ", "").startswith(("LL", "KL"))
        )
        if beam_mark:
            for other in values:
                if other.entity_id == row.entity_id or other.frame_id != row.frame_id:
                    continue
                if other.page_no != row.page_no:
                    continue
                numeric_text = _clean(other.text)
                if not (_DIMENSION_TOKEN_RE.fullmatch(numeric_text) or _DIMENSIONS_RE.search(numeric_text)):
                    continue
                dx = abs(row.point_m[0] - other.point_m[0])
                dy = abs(row.point_m[1] - other.point_m[1])
                if min(dx, dy) > schedule_alignment_tolerance_m:
                    continue
                if max(dx, dy) > schedule_alignment_gap_m:
                    continue
                parsed = parse_text(
                    f"{row.text} {other.text}",
                    entity_ids=(row.entity_id, other.entity_id),
                    source_file_id=row.source_file_id,
                    frame_id=row.frame_id,
                    page_no=row.page_no,
                    point_m=(
                        (row.point_m[0] + other.point_m[0]) / 2.0,
                        (row.point_m[1] + other.point_m[1]) / 2.0,
                    ),
                    source_role=row.source_role,
                )
                if parsed:
                    key = (
                        parsed.component_kind,
                        parsed.mark,
                        tuple(sorted(parsed.dimensions_mm.items())),
                        parsed.entity_ids,
                    )
                    result[key] = parsed
    return sorted(result.values(), key=lambda item: (item.frame_id, item.page_no or 0, item.point_m, item.text))


def marks(rows: Iterable[TextRow]) -> list[tuple[str, TextRow]]:
    result = []
    for row in rows:
        match = _MARK_RE.search(_clean(row.text).upper())
        if match:
            result.append((re.sub(r"\s+", "", match.group(1)).upper(), row))
    return result


def resolve(*, component_kind: Literal["wall", "column", "beam"], anchor_m: tuple[float, float],
            frame_id: str, rows: Iterable[TextRow], parsed: Iterable[ParsedSpecification],
            association_radius_m: float = 1.50, geometry_mm: dict[str, float] | None = None,
            tolerance_mm: float = 5.0,
            element_mark: str | None = None) -> SpecificationResolution:
    """Resolve one model element using nearby annotation and mark→table links."""

    row_values = [row for row in rows if row.frame_id == frame_id]
    nearby = [
        row for row in row_values
        if ((row.point_m[0] - anchor_m[0]) ** 2 + (row.point_m[1] - anchor_m[1]) ** 2) ** 0.5 <= association_radius_m
    ]
    candidates = [
        item for item in parsed
        if item.component_kind in (component_kind, "unknown")
        and item.frame_id == frame_id
        and ((item.point_m[0] - anchor_m[0]) ** 2 + (item.point_m[1] - anchor_m[1]) ** 2) ** 0.5 <= association_radius_m
    ]
    by_mark: dict[str, list[ParsedSpecification]] = {}
    for item in parsed:
        if item.component_kind in (component_kind, "unknown") and item.mark:
            by_mark.setdefault(item.mark, []).append(item)
    for mark, row in marks(nearby):
        for item in by_mark.get(mark, []):
            candidates.append(item)
    normalized_element_mark = re.sub(r"\s+", "", str(element_mark or "")).upper()
    if normalized_element_mark:
        if normalized_element_mark in by_mark:
            # An existing deterministic component mark is stronger than page
            # proximity to another row in a dense schedule.  Restrict
            # candidates to that mark instead of mixing neighbouring table
            # specifications.
            candidates = list(by_mark[normalized_element_mark])
        else:
            # Geometry-generated marks (for example ``BM-C-800x800``) are
            # identifiers, not drawing marks.  They must not inherit an
            # unrelated GBZ/YBZ/KZ specification merely because a legend
            # happens to be close to the profile.  A labelled element whose
            # mark is absent from the specification index is explicitly
            # unresolved; its vector dimensions remain authoritative.
            candidates = [
                item for item in candidates
                if item.mark == normalized_element_mark
            ]
    if not candidates and geometry_mm:
        # Leadered legend/detail labels can sit well outside a wall midpoint.
        # Once parsing has proved that a text row is an explicit component
        # specification, exact dimensional agreement is a safe association:
        # geometry selects among real drawing values but never invents one.
        candidates = [
            item for item in parsed
            if item.component_kind in (component_kind, "unknown")
            and item.frame_id == frame_id
            and item.dimensions_mm
            and all(
                key in geometry_mm
                and abs(float(geometry_mm[key]) - float(value)) <= tolerance_mm
                for key, value in item.dimensions_mm.items()
            )
        ]
    # De-duplicate equivalent candidates and prefer detail/schedule/legend
    # over a plain annotation when the same mark is repeated.
    unique: dict[tuple, ParsedSpecification] = {}
    priority = {"detail": 0, "schedule": 1, "legend": 2, "annotation": 3}
    for item in candidates:
        key = (item.mark, tuple(sorted(item.dimensions_mm.items())))
        old = unique.get(key)
        if old is None or priority[item.source_kind] < priority[old.source_kind]:
            unique[key] = item
    candidates = list(unique.values())
    if not candidates:
        return SpecificationResolution(status="unresolved", source_kind="none", reason="no legend/schedule/detail specification is associated")
    signatures = {(tuple(sorted(item.dimensions_mm.items()))) for item in candidates}
    if len(signatures) > 1:
        return SpecificationResolution(status="conflict", source_kind="none", reason="multiple drawing specifications conflict")
    selected = sorted(candidates, key=lambda item: priority[item.source_kind])[0]
    if geometry_mm:
        for key, value in selected.dimensions_mm.items():
            measured = geometry_mm.get(key)
            if measured is not None and abs(float(measured) - float(value)) > tolerance_mm:
                return SpecificationResolution(
                status="conflict", source_kind="none", text=selected.text,
                    mark=selected.mark,
                    dimensions_mm=selected.dimensions_mm,
                    entity_ids=selected.entity_ids,
                    reason=f"drawing {key}={value:g}mm differs from geometric measurement {measured:g}mm",
                )
    return SpecificationResolution(
        status=f"resolved_from_{selected.source_kind}",
        source_kind=selected.source_kind,
        mark=selected.mark,
        text=selected.text,
        dimensions_mm=dict(selected.dimensions_mm),
        entity_ids=selected.entity_ids,
    )
