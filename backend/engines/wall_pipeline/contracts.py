"""Versioned contracts for the six wall-pipeline artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    # Geometry and engineering measurements must never silently accept NaN or
    # infinity. They otherwise serialize successfully and fail only later in
    # NumPy/Revit, far from the source evidence.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _assert_finite_json(value: Any, field: str, *, depth: int = 0) -> Any:
    """Reject non-finite numbers hidden inside untyped geometry dictionaries.

    Pydantic's ``allow_inf_nan=False`` covers declared float fields, but source
    entity geometry intentionally keeps format-specific keys in a mapping.
    Validating that mapping here prevents a parser-provided NaN/Infinity from
    surviving serialization and failing much later in NumPy or Revit.
    """

    if depth > 64:
        raise ValueError(f"{field} nesting is too deep")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_finite_json(item, f"{field}.{key}", depth=depth + 1)
        return value
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_finite_json(item, f"{field}[{index}]", depth=depth + 1)
        return value
    return value


class SourceFileConfig(StrictModel):
    path: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1, max_length=64)
    page_no: int | None = Field(default=None, ge=1)


class ODAConverterConfig(StrictModel):
    executable: str | None = None
    output_version: str = "ACAD2018"
    timeout_seconds: int = Field(default=300, ge=1, le=1800)
    # Placeholders: input_dir, output_dir, output_version, output_type,
    # recursive and audit.  Keeping this configurable avoids coupling the
    # pipeline to one ODA release's command-line wrapper.
    command: list[str] | None = None


class SourceConfig(StrictModel):
    type: Literal["pdf", "dwg", "dxf"]
    files: list[SourceFileConfig] = Field(min_length=1)
    converter: ODAConverterConfig | None = None


class CalibrationPair(StrictModel):
    source_distance: float = Field(..., gt=0)
    model_distance_m: float = Field(..., gt=0)


class CoordinateConfig(StrictModel):
    origin: Literal[
        "source_origin", "revit_project_base_point", "revit_survey_point"
    ] = "source_origin"
    source_origin: tuple[float, float] = (0.0, 0.0)
    source_unit: Literal["auto", "mm", "cm", "m", "in", "ft", "pt"] = "auto"
    scale_to_m: float | None = Field(default=None, gt=0)
    calibration: list[CalibrationPair] = Field(default_factory=list)
    # PDF vector entities are emitted in the unrotated media-box frame while
    # users review the page with its /Rotate entry applied.  Preserve that
    # deterministic source transform by default; ``rotation_deg`` remains an
    # additional engineering calibration rather than a PDF-viewer workaround.
    apply_pdf_page_rotation: bool = True
    rotation_deg: float | Literal["auto"] = 0.0
    translation_m: tuple[float, float] = (0.0, 0.0)
    # A Project Base Point angle is only applied when the input frame is
    # explicitly declared compatible with Revit's true-north frame.  Merely
    # selecting the base-point origin must not rotate every drawing silently.
    source_frame: Literal["project_north", "true_north"] = "project_north"
    apply_base_point_rotation: bool = False
    # PDF pages and supplementary drawings are independent coordinate frames.
    # Explicit offsets are required before they can be combined safely.
    frame_offsets_m: dict[str, tuple[float, float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _single_scale_policy(self) -> "CoordinateConfig":
        if self.scale_to_m is not None and self.calibration:
            raise ValueError("use scale_to_m or calibration, not both")
        if any(not isinstance(key, str) or not key.strip() for key in self.frame_offsets_m):
            raise ValueError("frame_offsets_m keys must be non-empty frame identifiers")
        if self.apply_base_point_rotation and self.source_frame != "true_north":
            raise ValueError(
                "apply_base_point_rotation requires source_frame=true_north"
            )
        return self


class GridAxisConfig(StrictModel):
    label: str = Field(..., min_length=1, max_length=64)
    start: tuple[float, float]
    end: tuple[float, float]
    # Optional for axes shared by multiple source files.  Omitted means the
    # first source frame for backwards compatibility.
    source_file_id: str | None = None
    # Exact source coordinate frame (for example
    # ``source_0001:page:0002``).  This is needed when one PDF contains
    # multiple pages or a supplementary drawing reuses local coordinates.
    # ``source_file_id`` remains the backwards-compatible unit/provenance
    # selector; when both are supplied, frame_id controls the transform and
    # source_file_id controls unit lookup.
    frame_id: str | None = None


class GridConfig(StrictModel):
    axes: list[GridAxisConfig] = Field(default_factory=list)
    # Explicit axes remain supported, but production drawings normally carry
    # authoritative vector grid lines.  These scopes let the source adapter
    # keep those lines and the geometry engine derive the project frame before
    # any wall/column is built.
    include_layers: list[str] = Field(default_factory=list)
    exclude_layers: list[str] = Field(default_factory=list)
    min_length_m: float = Field(default=10.0, gt=0)
    cluster_tolerance_m: float = Field(default=0.40, gt=0, le=2.0)
    axis_tolerance_deg: float = Field(default=2.0, ge=0, le=15.0)
    normalize_origin: bool = False
    required: bool = False


class WallRecognitionConfig(StrictModel):
    include_layers: list[str] = Field(default_factory=list)
    exclude_layers: list[str] = Field(default_factory=list)
    min_thickness_m: float = Field(default=0.075, gt=0)
    max_thickness_m: float = Field(default=0.60, gt=0)
    # PDF/DWG unit conversion can leave sub-millimetre face spacing noise at
    # the configured engineering limit (for example 800.09 mm for an 800 mm
    # wall).  This bounded tolerance only affects acceptance of the measured
    # pair; the original value remains in evidence until a drawing
    # specification resolves it.
    thickness_tolerance_m: float = Field(default=0.005, ge=0, le=0.05)
    min_length_m: float = Field(default=0.30, gt=0)
    min_overlap_ratio: float = Field(default=0.45, ge=0, le=1)
    parallel_tolerance_deg: float = Field(default=2.0, ge=0, le=15)
    dedupe_tolerance_m: float = Field(default=0.02, ge=0, le=0.5)
    break_gap_m: float = Field(default=0.06, ge=0, le=3.0)
    junction_tolerance_m: float = Field(default=0.15, gt=0, le=1.0)
    # Table text may be emitted on an adjacent row after PDF page rotation.
    # Keep the association policy in the input contract rather than hiding
    # source-specific tolerances inside the specification parser.
    schedule_alignment_tolerance_m: float = Field(default=0.10, gt=0, le=1.0)
    schedule_alignment_gap_m: float = Field(default=8.0, gt=0, le=50.0)
    # A plan mark must be close to the modeled wall before it can link to a
    # mark-keyed legend/schedule entry.  Keeping this separate from the
    # schedule row spacing prevents a nearby table cell from being mistaken
    # for a wall annotation in dense structural sheets.
    specification_association_distance_m: float = Field(
        default=0.80, gt=0, le=5.0
    )
    allow_single_line_evidence: bool = False
    material_name: str | None = None
    classification_code: str = "IfcWall"

    @model_validator(mode="after")
    def _valid_thickness_range(self) -> "WallRecognitionConfig":
        if self.max_thickness_m <= self.min_thickness_m:
            raise ValueError("max_thickness_m must exceed min_thickness_m")
        return self


class ColumnRecognitionConfig(StrictModel):
    """Deterministic source-layer policy for structural column outlines.

    Columns are deliberately a separate scope from walls.  A drawing often
    contains column hatch/text layers that must not leak into wall pairing;
    keeping the policy separate also lets the same source adapter emit both
    evidence types without changing the downstream contracts.
    """

    include_layers: list[str] = Field(default_factory=list)
    exclude_layers: list[str] = Field(default_factory=list)
    # Boundary/irregular columns are accepted from an authoritative closed
    # vector profile.  A structural column label, when present, only
    # classifies the profile; it never supplies or moves coordinates.
    profile_layers: list[str] = Field(default_factory=list)
    label_layers: list[str] = Field(default_factory=list)
    label_pattern: str = r"\b(?:GBZ|YBZ)\s*-?\s*\d+[A-Z]?\b"
    profile_match_distance_m: float = Field(default=5.0, gt=0, le=20.0)
    # OCR column marks and their legend dimensions may be separated in both
    # axes after page rotation.  Keep this association radius configurable;
    # it classifies a profile only after vector geometry has been found.
    specification_association_distance_m: float = Field(default=1.5, gt=0, le=20.0)
    min_profile_area_m2: float = Field(default=0.05, gt=0)
    material_name: str | None = None
    classification_code: str = "IfcColumn"
    min_width_m: float = Field(default=0.20, gt=0)
    max_width_m: float = Field(default=5.0, gt=0)
    min_depth_m: float = Field(default=0.20, gt=0)
    max_depth_m: float = Field(default=5.0, gt=0)
    dedupe_tolerance_m: float = Field(default=0.02, ge=0, le=0.5)
    # Deployment-approved, content-addressed training supplement.  These
    # fields are never accepted from an API caller; the workflow resolves
    # them from an exact uploaded-source SHA-256 match.  The geometry engine
    # still aligns the reference to the current drawing frame and fails
    # closed when the grids/ordinary columns do not prove that alignment.
    approved_profile_model_path: str | None = None
    approved_profile_model_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    approved_profile_input_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    approved_profile_source_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    approved_profile_min_axis_match_ratio: float = Field(
        default=0.75, ge=0.5, le=1.0
    )

    @model_validator(mode="after")
    def _valid_dimension_ranges(self) -> "ColumnRecognitionConfig":
        if self.max_width_m <= self.min_width_m:
            raise ValueError("max_width_m must exceed min_width_m")
        if self.max_depth_m <= self.min_depth_m:
            raise ValueError("max_depth_m must exceed min_depth_m")
        try:
            re.compile(self.label_pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError("label_pattern must be a valid regular expression") from exc
        supplement = (
            self.approved_profile_model_path,
            self.approved_profile_model_sha256,
            self.approved_profile_input_sha256,
            self.approved_profile_source_sha256,
        )
        if any(supplement) and not all(supplement):
            raise ValueError(
                "approved profile model path, model SHA-256 and source SHA-256 "
                "must be configured together"
            )
        return self


class OpeningRecognitionConfig(StrictModel):
    """Semantic opening evidence that can explain masked wall geometry.

    Labels classify an opening; they never provide coordinates.  Coordinates
    must come from a closed vector boundary or a bounded vector marker in the
    configured source layer scope.
    """

    label_layers: list[str] = Field(default_factory=list)
    boundary_layers: list[str] = Field(default_factory=list)
    exclude_layers: list[str] = Field(default_factory=list)
    label_pattern: str = r"\b(?:JD|HOLE|OPENING)\s*-?\s*[0-9A-Z]+\b"
    association_distance_m: float = Field(default=2.0, gt=0, le=20.0)
    min_width_m: float = Field(default=0.05, gt=0, le=20.0)
    max_width_m: float = Field(default=20.0, gt=0, le=100.0)
    min_depth_m: float = Field(default=0.05, gt=0, le=20.0)
    max_depth_m: float = Field(default=20.0, gt=0, le=100.0)
    elevation_reference: Literal["unresolved", "project", "level"] = "unresolved"
    # Explicit operator corrections are persisted in the task/config hash;
    # never silently flip a positive extracted value for a basement drawing.
    sill_elevation_overrides_m: dict[str, float] = Field(default_factory=dict)
    schedule_alignment_tolerance_m: float = Field(default=0.25, gt=0, le=1)
    schedule_search_distance_m: float = Field(default=25.0, gt=0, le=50)

    @model_validator(mode="after")
    def _valid_ranges(self) -> "OpeningRecognitionConfig":
        for mark, value in self.sill_elevation_overrides_m.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,63}", mark) or not math.isfinite(value) or abs(value) > 1000:
                raise ValueError("opening elevation corrections require a mark and a finite elevation in metres")
        if self.max_width_m <= self.min_width_m:
            raise ValueError("max_width_m must exceed min_width_m")
        if self.max_depth_m <= self.min_depth_m:
            raise ValueError("max_depth_m must exceed min_depth_m")
        try:
            re.compile(self.label_pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError("label_pattern must be a valid regular expression") from exc
        return self


class BeamRecognitionConfig(StrictModel):
    """Deterministic coupling-beam scope for structural plan sheets."""

    line_layers: list[str] = Field(default_factory=list)
    label_layers: list[str] = Field(default_factory=list)
    exclude_layers: list[str] = Field(default_factory=list)
    label_pattern: str = r"\b(?:LL|KL)\s*-?\s*\d+[A-Z]?\b"
    association_distance_m: float = Field(default=2.0, gt=0, le=20.0)
    # Labels on dense structural plans are often placed beside (rather than
    # at the midpoint of) the two beam faces.  Keep face pairing strict while
    # allowing a separately bounded label lookup distance.
    label_association_distance_m: float = Field(default=8.0, gt=0, le=20.0)
    # Elevation tables often place the heading/value row apart from the LL/KL
    # mark row.  Keep the table association bounded and configurable instead
    # of treating every decimal annotation on the sheet as an elevation.
    # Beam schedules commonly put the LL/KL mark in the first column and the
    # top/base elevation in a later column.  The association bound therefore
    # covers a full A0 schedule row, while the aligned-axis tolerance remains
    # tight enough to prevent unrelated annotations from being attached.
    elevation_association_distance_m: float = Field(default=25.0, gt=0, le=50.0)
    elevation_alignment_tolerance_m: float = Field(default=0.20, gt=0, le=5.0)
    # The heading and numeric cell must remain in the same table column.  A
    # separate bound prevents another annotation that happens to share the
    # same baseline from being mistaken for an elevation value.
    elevation_column_tolerance_m: float = Field(default=4.0, gt=0, le=20.0)
    # Shorter marks on structural sheets are often annotation fragments, not
    # coupling beams.  Keep this threshold configurable, with a conservative
    # default that removes the 300 mm PDF line fragments seen in the sample.
    min_length_m: float = Field(default=0.50, gt=0)
    min_width_m: float = Field(default=0.075, gt=0)
    max_width_m: float = Field(default=0.80, gt=0)
    depth_m: float = Field(default=0.50, gt=0, le=5.0)

    @model_validator(mode="after")
    def _valid_ranges(self) -> "BeamRecognitionConfig":
        if self.max_width_m <= self.min_width_m:
            raise ValueError("max_width_m must exceed min_width_m")
        if self.label_association_distance_m < self.association_distance_m:
            raise ValueError(
                "label_association_distance_m must be at least association_distance_m"
            )
        if self.elevation_alignment_tolerance_m >= self.elevation_association_distance_m:
            raise ValueError(
                "elevation_alignment_tolerance_m must be below association distance"
            )
        if self.elevation_column_tolerance_m >= self.elevation_association_distance_m:
            raise ValueError(
                "elevation_column_tolerance_m must be below association distance"
            )
        try:
            re.compile(self.label_pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError("label_pattern must be a valid regular expression") from exc
        return self


class ReviewGateConfig(StrictModel):
    min_confidence: float = Field(default=0.70, ge=0, le=1)
    max_dangling_endpoint_ratio: float = Field(default=1.0, ge=0, le=1)


class ModelingStandardConfig(StrictModel):
    """Executable project profile for Chinese national BIM standards.

    GB/T documents describe a common information and delivery framework; they
    do not provide a drop-in wall-recognition algorithm.  This profile keeps
    the normative references explicit and turns the parts that can be checked
    deterministically (units, coordinates, classification, provenance and
    construction metadata) into pipeline gates.
    """

    profile: Literal["cn_gb_bim_delivery_v1"] = "cn_gb_bim_delivery_v1"
    references: list[str] = Field(
        default_factory=lambda: [
            "GB/T 51212-2016",
            "GB/T 51269-2017",
            "GB/T 51301-2018",
            "GB/T 51235-2017",
        ],
        min_length=3,
    )
    # Geometry contracts stay in metres for deterministic calculations.  The
    # Revit delivery surface and operator-facing quantities are explicitly
    # emitted in millimetres; keeping that distinction prevents a unit label
    # from silently changing the numeric coordinate contract.
    units: Literal["m"] = "m"
    delivery_units: Literal["mm"] = "mm"
    coordinate_frame: Literal["project_north", "true_north"] = "project_north"
    classification_system: Literal["GB/T 51269-2017"] = "GB/T 51269-2017"
    application_standard: Literal["GB/T 51212-2016"] = "GB/T 51212-2016"
    delivery_standard: Literal["GB/T 51301-2018"] = "GB/T 51301-2018"
    construction_standard: Literal["GB/T 51235-2017"] | None = "GB/T 51235-2017"
    naming_prefix: str = Field(default="BM", min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")
    wall_type_prefix: str = Field(default="BM-WALL", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    column_type_prefix: str = Field(default="BM-COLUMN", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    require_source_refs: bool = True
    require_classification_code: bool = True
    require_instance_parameters: bool = True
    require_level: bool = True
    require_material: bool = False
    require_quantities: bool = True

    @model_validator(mode="after")
    def _required_references(self) -> "ModelingStandardConfig":
        references = {item.strip() for item in self.references if item.strip()}
        required = {
            self.application_standard,
            self.classification_system,
            self.delivery_standard,
        }
        if self.construction_standard:
            required.add(self.construction_standard)
        missing = sorted(required - references)
        if missing:
            raise ValueError(
                "modeling_standard.references is missing: " + ", ".join(missing)
            )
        if any(not item.strip() for item in self.references):
            raise ValueError("modeling_standard.references cannot contain empty values")
        return self


_LEVEL_ELEVATION_RANGE_RE = re.compile(
    r"^\s*([+\-]?\d+(?:\.\d+)?|[+\-]?\.\d+)\s*(?:m|米)?"
    r"\s*(?:~|～|至|到)\s*"
    r"([+\-]?\d+(?:\.\d+)?|[+\-]?\.\d+)\s*(?:m|米)?\s*$",
    re.IGNORECASE,
)


def parse_level_elevation_range(value: str) -> tuple[float, float]:
    """Parse a user-facing ``bottom~top`` level range in metres.

    The range is deliberately explicit instead of accepting a hyphen: a
    hyphen is ambiguous when both endpoints may be negative.  The returned
    tuple is always ``(bottom, top)`` and is used as the authoritative level
    envelope for walls and columns.
    """

    normalized = str(value or "").replace("−", "-").strip()
    match = _LEVEL_ELEVATION_RANGE_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(
            "level elevation_range must use bottom~top metres, for example -6.4~0"
        )
    bottom = float(match.group(1))
    top = float(match.group(2))
    if not math.isfinite(bottom) or not math.isfinite(top) or top <= bottom:
        raise ValueError("level elevation_range top must be greater than bottom")
    if top - bottom > 30.0:
        raise ValueError("level elevation_range cannot exceed 30 metres")
    return round(bottom, 6), round(top, 6)


def infer_floor_code_from_elevation_range(
    bottom_m: float, top_m: float, *, tolerance_m: float = 0.01,
) -> str | None:
    """Infer only unambiguous common floor codes from a level envelope.

    ``-6.4~0`` is the conventional first-basement envelope and therefore
    maps to ``B1``.  Other negative ranges (for example ``-12.8~-6.4``)
    remain unresolved because their basement ordinal cannot be proven from a
    single range; the operator must provide the project-specific code.
    """

    bottom = float(bottom_m)
    top = float(top_m)
    if bottom < -tolerance_m and abs(top) <= tolerance_m:
        return "B1"
    if abs(bottom) <= tolerance_m and top > tolerance_m:
        return "1F"
    return None


class LevelConfig(StrictModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    # ``elevation_m`` is the bottom datum for backwards compatibility.  When
    # supplied, ``elevation_range`` is the user-facing source of both bottom
    # and top and deterministically sets ``wall_height_m``.
    elevation_m: float = 0.0
    top_elevation_m: float | None = None
    elevation_range: str | None = Field(default=None, max_length=64)
    # ``unresolved`` is used by the upload workflow when no explicit source
    # elevation was supplied.  It is a risk signal, not a geometry fallback:
    # Revit still resolves the named native level before writing.
    elevation_source: Literal["input", "drawing", "revit", "unresolved"] = "input"
    wall_height_m: float = Field(default=3.0, gt=0, le=30)

    @model_validator(mode="after")
    def _normalise_elevation_range(self) -> "LevelConfig":
        if self.elevation_range:
            bottom, top = parse_level_elevation_range(self.elevation_range)
            if (
                self.top_elevation_m is not None
                and not math.isclose(self.top_elevation_m, top, abs_tol=1.0e-6)
            ):
                raise ValueError(
                    "level top_elevation_m conflicts with elevation_range"
                )
            self.elevation_m = bottom
            self.top_elevation_m = top
            self.wall_height_m = round(top - bottom, 6)
        elif self.top_elevation_m is not None:
            top = float(self.top_elevation_m)
            if top <= self.elevation_m:
                raise ValueError("level top_elevation_m must be above elevation_m")
            self.wall_height_m = round(top - self.elevation_m, 6)
            self.elevation_range = (
                f"{self.elevation_m:g}~{self.top_elevation_m:g}"
            )
        else:
            self.top_elevation_m = round(
                float(self.elevation_m) + float(self.wall_height_m), 6
            )
        return self


class RevitConfig(StrictModel):
    target_model_path: str | None = None
    floor_code: str = Field(default="", max_length=32)
    wall_type_name: str | None = None


class WallPipelineConfig(StrictModel):
    schema_version: Literal["buildmate.wall-pipeline-config/1.0"] = (
        "buildmate.wall-pipeline-config/1.0"
    )
    tenant_id: str = Field(..., min_length=1, max_length=64)
    project_id: str = Field(..., min_length=1, max_length=64)
    source: SourceConfig
    coordinate: CoordinateConfig = Field(default_factory=CoordinateConfig)
    grid: GridConfig = Field(default_factory=GridConfig)
    wall: WallRecognitionConfig = Field(default_factory=WallRecognitionConfig)
    column: ColumnRecognitionConfig = Field(default_factory=ColumnRecognitionConfig)
    opening: OpeningRecognitionConfig = Field(default_factory=OpeningRecognitionConfig)
    beam: BeamRecognitionConfig = Field(default_factory=BeamRecognitionConfig)
    review_gate: ReviewGateConfig = Field(default_factory=ReviewGateConfig)
    modeling_standard: ModelingStandardConfig = Field(default_factory=ModelingStandardConfig)
    level: LevelConfig
    revit: RevitConfig = Field(default_factory=RevitConfig)
    output_dir: str = "wall_pipeline_output"

    @model_validator(mode="after")
    def _standard_frame_matches_coordinate_frame(self) -> "WallPipelineConfig":
        if self.modeling_standard.coordinate_frame != self.coordinate.source_frame:
            raise ValueError(
                "modeling_standard.coordinate_frame must match coordinate.source_frame"
            )
        return self


class ManifestSource(StrictModel):
    source_file_id: str
    path: str
    role: str
    media_type: str
    sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(..., ge=0)
    page_no: int | None = Field(default=None, ge=1)


class SourceManifest(StrictModel):
    schema_version: Literal["buildmate.source-manifest/1.0"] = (
        "buildmate.source-manifest/1.0"
    )
    artifact_type: Literal["source_manifest"] = "source_manifest"
    tenant_id: str
    project_id: str
    source_type: Literal["pdf", "dwg", "dxf"]
    sources: list[ManifestSource] = Field(min_length=1)
    coordinate: CoordinateConfig
    grid: GridConfig
    wall: WallRecognitionConfig
    column: ColumnRecognitionConfig = Field(default_factory=ColumnRecognitionConfig)
    opening: OpeningRecognitionConfig = Field(default_factory=OpeningRecognitionConfig)
    beam: BeamRecognitionConfig = Field(default_factory=BeamRecognitionConfig)
    modeling_standard: ModelingStandardConfig = Field(default_factory=ModelingStandardConfig)
    level: LevelConfig
    revit: RevitConfig
    config_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=utc_now)


class SourceEntity(StrictModel):
    entity_id: str
    source_file_id: str
    page_no: int | None = Field(default=None, ge=1)
    kind: Literal[
        "line", "polyline", "hatch_boundary", "mline", "text"
    ]
    layer: str = ""
    geometry: dict[str, Any]
    style: dict[str, Any] = Field(default_factory=dict)
    locator: str
    # Source/page frame identifier used to prevent accidental cross-sheet
    # geometry pairing.
    frame_id: str = ""

    @field_validator("geometry", "style")
    @classmethod
    def _geometry_values_are_valid(cls, value: dict[str, Any], info) -> dict[str, Any]:
        if info.field_name == "geometry" and not value:
            raise ValueError("source entity geometry cannot be empty")
        return _assert_finite_json(value, f"source entity {info.field_name}")


class SourceEntities(StrictModel):
    schema_version: Literal["buildmate.source-entities/1.0"] = (
        "buildmate.source-entities/1.0"
    )
    artifact_type: Literal["source_entities"] = "source_entities"
    tenant_id: str
    project_id: str
    manifest_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    entities: list[SourceEntity]
    source_units: dict[str, str]
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class EvidenceSourceRef(StrictModel):
    source_file_id: str
    entity_id: str
    page_no: int | None = Field(default=None, ge=1)
    locator: str
    layer: str = ""
    # Preserve the exact local page/placement frame through evidence and the
    # final WallModel.  Older hand-authored artifacts may omit it; adapters
    # always populate it for reproducible cross-sheet registration.
    frame_id: str | None = None


class WallEvidenceItem(StrictModel):
    evidence_id: str
    method: Literal[
        "parallel_vector_faces", "closed_vector_strip", "single_semantic_line"
    ]
    centerline_m: tuple[tuple[float, float], tuple[float, float]]
    thickness_m: float = Field(..., gt=0)
    source_refs: list[EvidenceSourceRef] = Field(min_length=1)
    confidence: float = Field(..., ge=0, le=1)
    transform_ref: str = "source_to_project_local_m"
    limitations: list[str] = Field(default_factory=list)


class WallEvidence(StrictModel):
    schema_version: Literal["buildmate.wall-evidence/1.0"] = (
        "buildmate.wall-evidence/1.0"
    )
    artifact_type: Literal["wall_evidence"] = "wall_evidence"
    tenant_id: str
    project_id: str
    source_entities_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    units: Literal["m"] = "m"
    transform_chain: list[dict[str, Any]]
    items: list[WallEvidenceItem]
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class GridAxis(StrictModel):
    axis_id: str
    label: str
    start_m: tuple[float, float]
    end_m: tuple[float, float]
    source_file_id: str | None = None
    frame_id: str | None = None


class TopologyJunction(StrictModel):
    junction_id: str
    kind: Literal["L", "T", "X", "Z", "straight"]
    point_m: tuple[float, float]
    wall_ids: list[str] = Field(min_length=2)


class ConstructionInfo(StrictModel):
    """Typed construction metadata projected onto each model element."""

    category: Literal["Wall", "Column", "Beam"]
    discipline: Literal["Structural"] = "Structural"
    # A Revit wall is still in the ``Walls`` category regardless of whether
    # it is a shear wall or an architectural partition.  Keep that semantic
    # distinction explicit instead of relying on geometry or the localized
    # native family name.  ``unresolved`` is intentional: an unproven role
    # must never be silently promoted to a structural wall.
    structural_role: Literal[
        "shear_wall", "architectural_wall", "unresolved"
    ] = "unresolved"
    type_name: str
    type_mark: str | None = None
    # Revit users need the native Family/Type identity in addition to the
    # project classification code.  These are descriptive contract fields;
    # the compiler still verifies the actual native type when one is used.
    family_name: str | None = None
    family_type: str | None = None
    representation: Literal["system_family", "loadable_family", "profile_directshape"] | None = None
    # The displayed engineering size must come from a drawing legend,
    # schedule or detail when one is available.  Geometry is retained as the
    # canonical model measurement, but it is not silently promoted to a
    # drawing specification (for example 264.7 must not become 265).
    specification_status: Literal[
        "resolved_from_legend",
        "resolved_from_schedule",
        "resolved_from_detail",
        "resolved_from_annotation",
        "unresolved",
        "conflict",
    ] = "unresolved"
    specification_source: Literal[
        "legend", "schedule", "detail", "annotation", "none"
    ] = "none"
    specification_text: str | None = None
    specification_mm: dict[str, float] = Field(default_factory=dict)
    specification_refs: list[EvidenceSourceRef] = Field(default_factory=list)
    classification_code: str
    classification_system: Literal["GB/T 51269-2017"] = "GB/T 51269-2017"
    material_name: str | None = None
    material_status: Literal["provided", "unspecified"] = "unspecified"
    quantity_basis: Literal["deterministic_geometry"] = "deterministic_geometry"


class QuantityTakeoff(StrictModel):
    length_m: float | None = Field(default=None, ge=0)
    footprint_area_m2: float | None = Field(default=None, ge=0)
    cross_section_area_m2: float | None = Field(default=None, ge=0)
    side_area_m2: float | None = Field(default=None, ge=0)
    gross_volume_m3: float = Field(..., ge=0)


class WallRecord(StrictModel):
    wall_id: str
    start_m: tuple[float, float]
    end_m: tuple[float, float]
    thickness_m: float = Field(..., gt=0)
    height_m: float = Field(..., gt=0)
    level_id: str
    evidence_ids: list[str] = Field(min_length=1)
    source_refs: list[EvidenceSourceRef] = Field(min_length=1)
    confidence: float = Field(..., ge=0, le=1)
    topology: list[str] = Field(default_factory=list)
    construction: ConstructionInfo | None = None
    quantities: QuantityTakeoff | None = None


class ColumnRecord(StrictModel):
    """A source-traceable column profile in the project-local metre frame."""

    column_id: str
    center_m: tuple[float, float]
    # A true irregular profile can be triangular; rectangles still carry
    # four vertices, while DirectShape can safely extrude any simple
    # three-or-more-sided polygon.
    profile_m: list[tuple[float, float]] = Field(min_length=3)
    width_m: float = Field(..., gt=0)
    depth_m: float = Field(..., gt=0)
    height_m: float = Field(..., gt=0)
    level_id: str
    source_refs: list[EvidenceSourceRef] = Field(min_length=1)
    confidence: float = Field(..., ge=0, le=1)
    profile_kind: Literal["rectangular", "irregular"] = "rectangular"
    type_mark: str | None = None
    construction: ConstructionInfo | None = None
    quantities: QuantityTakeoff | None = None

    @field_validator("profile_m")
    @classmethod
    def _profile_is_finite(cls, value: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(value) < 3:
            raise ValueError("column profile must contain at least three points")
        for point in value:
            if len(point) < 2 or not all(math.isfinite(float(item)) for item in point[:2]):
                raise ValueError("column profile contains an invalid point")
        return value


class BeamRecord(StrictModel):
    """A source-traceable coupling-beam centerline in metres."""

    beam_id: str
    start_m: tuple[float, float]
    end_m: tuple[float, float]
    width_m: float = Field(..., gt=0)
    depth_m: float = Field(..., gt=0)
    level_id: str
    source_refs: list[EvidenceSourceRef] = Field(min_length=1)
    confidence: float = Field(..., ge=0, le=1)
    type_mark: str | None = None
    construction: ConstructionInfo | None = None
    quantities: QuantityTakeoff | None = None
    # Absolute project elevations in metres.  A drawing may provide either
    # beam top or bottom; the missing side is derived from the resolved beam
    # depth and remains tied to the same evidence/status.
    top_elevation_m: float | None = None
    base_elevation_m: float | None = None
    elevation_status: Literal["resolved", "level_default", "unresolved", "conflict"] = "unresolved"
    elevation_source: Literal[
        "drawing", "schedule", "detail", "annotation", "level_default", "none"
    ] = "none"
    elevation_text: str | None = None
    elevation_refs: list[EvidenceSourceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_elevation(self) -> "BeamRecord":
        if self.elevation_status == "resolved" and (
            self.top_elevation_m is None or self.base_elevation_m is None
        ):
            raise ValueError("resolved beam elevation requires top and base values")
        if (
            self.top_elevation_m is not None
            and self.base_elevation_m is not None
            and self.top_elevation_m < self.base_elevation_m
        ):
            raise ValueError("beam top elevation cannot be below base elevation")
        return self


class OpeningRecord(StrictModel):
    """A source-traceable opening marker associated with candidate wall hosts."""

    opening_id: str
    mark: str
    # Drawing identifier plus plan dimensions for Revit metadata/schedules.
    type_name: str | None = None
    semantic_role: Literal["shear_wall_opening"] = "shear_wall_opening"
    center_m: tuple[float, float]
    # Empty when the semantic label has no paired vector boundary; this is
    # explicit review evidence, never a fabricated point geometry.
    boundary_m: list[tuple[float, float]] = Field(default_factory=list)
    width_m: float = Field(..., ge=0)
    depth_m: float = Field(..., ge=0)
    host_wall_ids: list[str] = Field(default_factory=list)
    source_refs: list[EvidenceSourceRef] = Field(min_length=1)
    confidence: float = Field(..., ge=0, le=1)
    status: Literal["matched", "review_required"]
    limitations: list[str] = Field(default_factory=list)
    # Plan marker depth is wall thickness, NEVER the vertical opening height.
    specification_mm: dict[str, float] = Field(default_factory=dict)
    specification_refs: list[EvidenceSourceRef] = Field(default_factory=list)
    cut_status: Literal["review_required", "ready"] = "review_required"
    elevation_source: Literal["drawing", "drawing_inferred_basement", "input", "unresolved"] = "unresolved"
    cut_start_m: tuple[float, float] | None = None
    cut_end_m: tuple[float, float] | None = None
    base_elevation_m: float | None = None
    top_elevation_m: float | None = None

    @model_validator(mode="after")
    def _valid_cut(self) -> "OpeningRecord":
        if self.cut_status != "ready":
            return self
        if self.status != "matched" or len(self.host_wall_ids) != 1 or len(self.boundary_m) < 3:
            raise ValueError("ready opening needs a boundary and one wall host")
        if self.cut_start_m is None or self.cut_end_m is None or self.base_elevation_m is None or self.top_elevation_m is None:
            raise ValueError("ready opening needs complete cut coordinates/elevations")
        values = (*self.cut_start_m, *self.cut_end_m, self.base_elevation_m, self.top_elevation_m)
        if not all(math.isfinite(value) for value in values) or self.top_elevation_m <= self.base_elevation_m:
            raise ValueError("opening cut coordinates/elevations are invalid")
        width = self.specification_mm.get("width_mm", 0)
        height = self.specification_mm.get("height_mm", 0)
        if width <= 0 or height <= 0 or not math.isclose(math.dist(self.cut_start_m, self.cut_end_m) * 1000, width, abs_tol=0.01):
            raise ValueError("opening cut width differs from drawing specification")
        if not math.isclose((self.top_elevation_m - self.base_elevation_m) * 1000, height, abs_tol=0.01):
            raise ValueError("opening cut height differs from drawing specification")
        if not self.specification_refs or any(ref not in self.source_refs for ref in self.specification_refs):
            raise ValueError("opening specification requires validated source references")
        return self


class ReviewGate(StrictModel):
    status: Literal["pass", "fail"]
    checks: dict[str, bool]
    errors: list[str] = Field(default_factory=list)
    metrics: dict[str, float | int]
    standard_violations: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class ApprovalRecord(StrictModel):
    status: Literal["approved", "rejected"]
    actor_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    decided_at: datetime = Field(default_factory=utc_now)


class WallModel(StrictModel):
    schema_version: Literal["buildmate.wall-model/1.0"] = (
        "buildmate.wall-model/1.0"
    )
    artifact_type: Literal["wall_model"] = "wall_model"
    tenant_id: str
    project_id: str
    wall_evidence_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    units: Literal["m"] = "m"
    coordinate_origin: str
    modeling_standard: ModelingStandardConfig = Field(default_factory=ModelingStandardConfig)
    # Replayable source→project transform metadata.  It is intentionally
    # separate from the wall coordinates so independent render/audit code can
    # apply the same transform without using WallModel as its sole source.
    transform_chain: list[dict[str, Any]] = Field(default_factory=list)
    # Bounds are measured from the immutable source entities after the
    # recorded source transform.  They are an audit-view input, not a
    # reconstruction of the source image; keeping them on the model prevents
    # the Revit renderer from deriving its crop from generated geometry and
    # accidentally hiding omitted walls.
    source_bounds_m: tuple[float, float, float, float] | None = None
    level: LevelConfig
    grid: list[GridAxis] = Field(default_factory=list)
    walls: list[WallRecord]
    columns: list[ColumnRecord] = Field(default_factory=list)
    beams: list[BeamRecord] = Field(default_factory=list)
    openings: list[OpeningRecord] = Field(default_factory=list)
    junctions: list[TopologyJunction] = Field(default_factory=list)
    gate: ReviewGate
    review_status: Literal["pending", "approved", "rejected"] = "pending"
    approval: ApprovalRecord | None = None
    revit: RevitConfig = Field(default_factory=RevitConfig)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _approval_matches_status(self) -> "WallModel":
        if self.review_status == "approved":
            if self.gate.status != "pass":
                raise ValueError("a failed gate cannot be approved")
            if self.approval is None or self.approval.status != "approved":
                raise ValueError("approved wall model requires approval record")
        if self.review_status == "rejected" and (
            self.approval is None or self.approval.status != "rejected"
        ):
            raise ValueError("rejected wall model requires rejection record")
        return self


class RevitResult(StrictModel):
    schema_version: Literal["buildmate.revit-result/1.0"] = (
        "buildmate.revit-result/1.0"
    )
    artifact_type: Literal["revit_result"] = "revit_result"
    tenant_id: str
    project_id: str
    wall_model_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    status: Literal[
        "pending_approval", "ready_for_dry_run", "dry_run_passed",
        "write_approved", "succeeded", "failed", "rolled_back"
    ]
    transaction_id: str | None = None
    created_element_ids: list[str] = Field(default_factory=list)
    created_column_ids: list[str] = Field(default_factory=list)
    created_beam_ids: list[str] = Field(default_factory=list)
    readback: dict[str, Any] = Field(default_factory=dict)
    actual_view_path: str | None = None
    actual_view_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    approval: ApprovalRecord | None = None
    errors: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class AuditReport(StrictModel):
    schema_version: Literal["buildmate.audit-report/1.0"] = (
        "buildmate.audit-report/1.0"
    )
    artifact_type: Literal["audit_report"] = "audit_report"
    tenant_id: str
    project_id: str
    wall_model_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    revit_result_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    status: Literal["blocked", "pass", "fail"]
    independent_sources: bool
    source_render_path: str | None = None
    source_render_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    revit_render_path: str | None = None
    revit_render_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    overlay_path: str | None = None
    metrics: dict[str, float | int | str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
