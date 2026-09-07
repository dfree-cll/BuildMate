"""Deterministic WallModel -> Revit program compiler.

The compiler is intentionally kept separate from the HTTP/service layer.  It
accepts only the normalized, human-approved DTO produced by
``wall_model_contract`` and emits a small IronPython program with its own
coordinate, read-back, idempotency and topology gates.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from backend.domain.errors import ValidationFailure


# This is part of the delivery contract.  The deterministic dry-run and the
# Windows Bridge must agree on the compiler implementation before a write is
# allowed.  Bump it whenever the emitted Revit program's semantics change.
WALL_COMPILER_VERSION = "typed_wall_model_v41"
REVIT_DELIVERY_UNITS = "mm"

# Revit 2020 project parameters are backed by shared-parameter definitions.
# Keep the names ASCII and stable so the same RVT can be rebuilt idempotently
# on Chinese and English Revit installations.  Values are stored as TEXT on
# purpose: the engineering units and numeric precision remain governed by the
# immutable WallModel/quantity contract, while the Properties palette stays
# readable without locale-dependent unit conversion.
ELEMENT_PARAMETER_FIELDS = (
    ("BM_ElementId", "element_id"),
    ("BM_Category", "category"),
    ("BM_TypeName", "type_name"),
    ("BM_TypeMark", "type_mark"),
    ("BM_Material", "material_name"),
    ("BM_MaterialStatus", "material_status"),
    ("BM_Classification", "classification_code"),
    ("BM_ClassificationSystem", "classification_system"),
    ("BM_StandardProfile", "standard_profile"),
    ("BM_Units", "units"),
    ("BM_CoordinateFrame", "coordinate_frame"),
    ("BM_QuantityBasis", "quantity_basis"),
    ("BM_QuantityScope", "quantity_scope"),
    ("BM_Level", "level_id"),
    ("BM_ThicknessMm", "thickness_mm"),
    ("BM_HeightMm", "height_mm"),
    ("BM_WidthMm", "width_mm"),
    ("BM_DepthMm", "depth_mm"),
    ("BM_ProfileKind", "profile_kind"),
    ("BM_LengthM", "length_m"),
    ("BM_FootprintM2", "footprint_area_m2"),
    ("BM_SideAreaM2", "side_area_m2"),
    ("BM_GrossVolumeM3", "gross_volume_m3"),
    ("BM_SourceRefCount", "source_ref_count"),
    ("BM_Confidence", "confidence"),
    ("BM_OpeningMarks", "opening_marks"),
    # Append new fields so the GUIDs of existing parameters remain stable in
    # already-delivered RVT files.
    ("BM_FamilyName", "family_name"),
    ("BM_FamilyType", "family_type"),
    ("BM_Representation", "representation"),
    ("BM_SpecificationStatus", "specification_status"),
    ("BM_SpecificationSource", "specification_source"),
    ("BM_SpecificationText", "specification_text"),
    ("BM_SpecificationMm", "specification_mm"),
    # ``Walls`` is a single Revit category.  Persist the source-proven role
    # so shear walls are not delivered as non-structural architectural walls.
    ("BM_WallRole", "structural_role"),
    # Beam-specific vertical placement is kept as instance metadata as well
    # as geometry.  Appending these fields preserves the GUIDs of all
    # previously delivered parameters in existing Revit files.
    ("BM_BeamBaseElevationMm", "beam_base_elevation_mm"),
    ("BM_BeamTopElevationMm", "beam_top_elevation_mm"),
    ("BM_BeamElevationStatus", "beam_elevation_status"),
    ("BM_BeamElevationSource", "beam_elevation_source"),
    ("BM_BeamElevationText", "beam_elevation_text"),
    ("BM_BeamElevationRefCount", "beam_elevation_ref_count"),
)

# Revit shared parameter files use tab-separated records.  Stable GUIDs avoid
# duplicate parameters when a later build opens the same target model.
_ELEMENT_PARAMETER_GUIDS = (
    "8f5d7a24-2a26-4e9d-a4c0-000000000001",
    "8f5d7a24-2a26-4e9d-a4c0-000000000002",
    "8f5d7a24-2a26-4e9d-a4c0-000000000003",
    "8f5d7a24-2a26-4e9d-a4c0-000000000004",
    "8f5d7a24-2a26-4e9d-a4c0-000000000005",
    "8f5d7a24-2a26-4e9d-a4c0-000000000006",
    "8f5d7a24-2a26-4e9d-a4c0-000000000007",
    "8f5d7a24-2a26-4e9d-a4c0-000000000008",
    "8f5d7a24-2a26-4e9d-a4c0-000000000009",
    "8f5d7a24-2a26-4e9d-a4c0-000000000010",
    "8f5d7a24-2a26-4e9d-a4c0-000000000011",
    "8f5d7a24-2a26-4e9d-a4c0-000000000012",
    "8f5d7a24-2a26-4e9d-a4c0-000000000013",
    "8f5d7a24-2a26-4e9d-a4c0-000000000014",
    "8f5d7a24-2a26-4e9d-a4c0-000000000015",
    "8f5d7a24-2a26-4e9d-a4c0-000000000016",
    "8f5d7a24-2a26-4e9d-a4c0-000000000017",
    "8f5d7a24-2a26-4e9d-a4c0-000000000018",
    "8f5d7a24-2a26-4e9d-a4c0-000000000019",
    "8f5d7a24-2a26-4e9d-a4c0-000000000020",
    "8f5d7a24-2a26-4e9d-a4c0-000000000021",
    "8f5d7a24-2a26-4e9d-a4c0-000000000022",
    "8f5d7a24-2a26-4e9d-a4c0-000000000023",
    "8f5d7a24-2a26-4e9d-a4c0-000000000024",
    "8f5d7a24-2a26-4e9d-a4c0-000000000025",
    "8f5d7a24-2a26-4e9d-a4c0-000000000026",
    "8f5d7a24-2a26-4e9d-a4c0-000000000027",
    "8f5d7a24-2a26-4e9d-a4c0-000000000028",
    "8f5d7a24-2a26-4e9d-a4c0-000000000029",
    "8f5d7a24-2a26-4e9d-a4c0-000000000030",
    "8f5d7a24-2a26-4e9d-a4c0-000000000031",
    "8f5d7a24-2a26-4e9d-a4c0-000000000032",
    "8f5d7a24-2a26-4e9d-a4c0-000000000033",
    "8f5d7a24-2a26-4e9d-a4c0-000000000034",
    "8f5d7a24-2a26-4e9d-a4c0-000000000035",
    "8f5d7a24-2a26-4e9d-a4c0-000000000036",
    "8f5d7a24-2a26-4e9d-a4c0-000000000037",
    "8f5d7a24-2a26-4e9d-a4c0-000000000038",
    "8f5d7a24-2a26-4e9d-a4c0-000000000039",
    "8f5d7a24-2a26-4e9d-a4c0-000000000040",
)

SHARED_PARAMETER_FILE_NAME = "buildmate_shared_parameters.txt"


def shared_parameter_file_content() -> str:
    """Return the deterministic Revit shared-parameter file contents."""

    lines = [
        "# This is a Revit shared parameter file.",
        "# Do not edit manually.",
        "*META\tVERSION\tMINVERSION",
        "META\t2\t1",
        "*GROUP\tID\tNAME",
        "GROUP\t1\tBuildMate",
        # Revit 2020 only accepts the legacy seven-column shared-parameter
        # record.  Newer Revit versions tolerate the optional description and
        # user-modifiable columns, but Revit 2020 returns ``None`` from
        # OpenSharedParameterFile when those columns (or braced GUIDs) are
        # present.  Keep the file in the format emitted by the Revit 2020
        # template shipped with the application.
        "*PARAM\tGUID\tNAME\tDATATYPE\tDATACATEGORY\tGROUP\tVISIBLE",
    ]
    for index, (name, _key) in enumerate(ELEMENT_PARAMETER_FIELDS):
        lines.append(
            "PARAM\t%s\t%s\tTEXT\t\t1\t1"
            % (_ELEMENT_PARAMETER_GUIDS[index], name)
        )
    return "\n".join(lines) + "\n"


def _runtime_metadata(value: object) -> str:
    """Encode one element-property value for the size-bounded Revit payload."""

    return str(value or "").replace(";", "_").replace("=", "_")[:120]


def _source_dimension_mm(value: object) -> object:
    """Preserve a drawing dimension, using an integer only when it is one."""

    if value is None or value == "":
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if abs(numeric - round(numeric)) <= 1.0e-6:
        return int(round(numeric))
    return numeric


def _runtime_element_properties(element: dict) -> dict:
    """Project approved construction data onto one Revit element.

    The WallModel remains the complete, immutable source of truth.  This
    compact projection is copied into the element's native instance metadata
    (Comments/ApplicationDataId) so a modeller can inspect a selected wall or
    column directly in Revit without creating a schedule view.
    """

    construction = element.get("construction") or {}
    quantities = element.get("quantities") or {}
    standard = element.get("modeling_standard") or {}
    category = str(element.get("type") or "")
    type_name = construction.get("type_name") or element.get("wall_type_name") or ""
    family_name = construction.get("family_name") or (
        "Basic Wall" if category == "Wall" else "BuildMate - Profile DirectShape"
    )
    family_type = construction.get("family_type") or type_name
    representation = construction.get("representation") or (
        "system_family" if category == "Wall" else "profile_directshape"
    )
    raw_specification = construction.get("specification_mm") or {}
    # Keep the visible specification payload consistent with the individual
    # dimension parameters.  Pydantic serializes drawing integers as ``500.0``
    # internally; Revit users must see the authoritative schedule value as
    # ``500`` rather than a floating-point representation.
    specification = {
        key: _source_dimension_mm(value)
        for key, value in raw_specification.items()
    } if isinstance(raw_specification, dict) else {}
    def dimension(name):
        source_value = specification.get(name)
        return _source_dimension_mm(source_value if source_value is not None else element.get({
            "thickness_mm": "thickness",
            "height_mm": "height",
            "width_mm": "width",
            "depth_mm": "depth",
        }[name]))
    properties = {
        "element_id": str(element.get("id") or ""),
        "category": category,
        "type_name": type_name,
        "type_mark": construction.get("type_mark") or element.get("type_mark") or "",
        "family_name": family_name,
        "family_type": family_type,
        "representation": representation,
        "specification_status": construction.get("specification_status") or "unresolved",
        "specification_source": construction.get("specification_source") or "none",
        "specification_text": construction.get("specification_text") or "",
        "specification_mm": specification,
        "structural_role": construction.get("structural_role") or "unresolved",
        "material_name": construction.get("material_name") or "UNSPECIFIED",
        "material_status": construction.get("material_status") or "unspecified",
        "classification_code": construction.get("classification_code") or "",
        "classification_system": construction.get("classification_system") or standard.get("classification_system") or "",
        "standard_profile": standard.get("profile") or "",
        # WallModel coordinates are canonical metres, but all Revit delivery
        # properties are millimetres.  A value is integer-formatted only when
        # that integer came from drawing evidence; geometric fallback keeps
        # its measured decimal instead of pretending it is a schedule value.
        "units": standard.get("delivery_units") or REVIT_DELIVERY_UNITS,
        "coordinate_frame": standard.get("coordinate_frame") or "project_north",
        "quantity_basis": construction.get("quantity_basis") or "deterministic_geometry",
        "quantity_scope": "GROSS_NO_OPENINGS",
        "level_id": str(element.get("level_id") or element.get("level") or ""),
        "thickness_mm": dimension("thickness_mm"),
        "height_mm": dimension("height_mm"),
        "width_mm": dimension("width_mm"),
        "depth_mm": dimension("depth_mm"),
        "profile_kind": element.get("profile_kind"),
        "length_m": quantities.get("length_m"),
        "footprint_area_m2": quantities.get("footprint_area_m2"),
        "side_area_m2": quantities.get("side_area_m2"),
        "gross_volume_m3": quantities.get("gross_volume_m3"),
        "beam_base_elevation_mm": (
            _source_dimension_mm(element.get("base_z"))
            if category == "Beam" else None
        ),
        "beam_top_elevation_mm": (
            _source_dimension_mm(element.get("top_z"))
            if category == "Beam" else None
        ),
        "beam_elevation_status": (
            element.get("elevation_status") if category == "Beam" else None
        ),
        "beam_elevation_source": (
            element.get("elevation_source") if category == "Beam" else None
        ),
        "beam_elevation_text": (
            element.get("elevation_text") if category == "Beam" else None
        ),
        "beam_elevation_ref_count": (
            len(element.get("elevation_refs") or [])
            if category == "Beam" else None
        ),
        "source_ref_count": len(element.get("source_refs") or []),
        "confidence": element.get("confidence"),
    }
    return {
        key: _runtime_value(value)
        for key, value in properties.items()
        if value is not None and value != ""
    }


def _runtime_schedule_suffix(element: dict) -> str:
    """Keep quantity/take-off data without shipping its verbose JSON keys.

    The complete construction and quantity dictionaries remain in the
    approved WallModel and compiled-plan hash.  Revit receives a flattened
    string written to the selected element's native instance properties.
    """

    construction = element.get("construction") or {}
    quantities = element.get("quantities") or {}
    properties = _runtime_element_properties(element)
    return "".join((
        ";ELEMENT_ID=" + _runtime_metadata(properties.get("element_id")),
        ";CATEGORY=" + _runtime_metadata(properties.get("category")),
        ";TYPE=" + _runtime_metadata(construction.get("type_name")),
        ";FAMILY=" + _runtime_metadata(properties.get("family_name")),
        ";FAMILY_TYPE=" + _runtime_metadata(properties.get("family_type")),
        ";REPRESENTATION=" + _runtime_metadata(properties.get("representation")),
        ";SPEC_STATUS=" + _runtime_metadata(properties.get("specification_status")),
        ";SPEC_SOURCE=" + _runtime_metadata(properties.get("specification_source")),
        ";SPEC_TEXT=" + _runtime_metadata(properties.get("specification_text")),
        ";SPEC_MM=" + _runtime_metadata(json.dumps(
            properties.get("specification_mm") or {}, ensure_ascii=False,
            sort_keys=True,
        )),
        ";WALL_ROLE=" + _runtime_metadata(properties.get("structural_role")),
        ";MARK=" + _runtime_metadata(
            construction.get("type_mark") or element.get("type_mark")
        ),
        ";MAT=" + _runtime_metadata(
            construction.get("material_name") or "UNSPECIFIED"
        ),
        ";MAT_STATUS=" + _runtime_metadata(construction.get("material_status")),
        ";CLASS=" + _runtime_metadata(construction.get("classification_code")),
        ";CLASS_SYSTEM=" + _runtime_metadata(
            construction.get("classification_system")
        ),
        ";LEN_M=" + _runtime_metadata(quantities.get("length_m")),
        ";FOOTPRINT_M2=" + _runtime_metadata(quantities.get("footprint_area_m2")),
        ";SIDE_M2=" + _runtime_metadata(quantities.get("side_area_m2")),
        ";VOL_M3=" + _runtime_metadata(quantities.get("gross_volume_m3")),
        ";LEVEL_ID=" + _runtime_metadata(properties.get("level_id")),
        ";THICKNESS_MM=" + _runtime_metadata(properties.get("thickness_mm")),
        ";HEIGHT_MM=" + _runtime_metadata(properties.get("height_mm")),
        ";WIDTH_MM=" + _runtime_metadata(properties.get("width_mm")),
        ";DEPTH_MM=" + _runtime_metadata(properties.get("depth_mm")),
        ";BEAM_BASE_ELEV_MM=" + _runtime_metadata(
            properties.get("beam_base_elevation_mm")
        ),
        ";BEAM_TOP_ELEV_MM=" + _runtime_metadata(
            properties.get("beam_top_elevation_mm")
        ),
        ";BEAM_ELEV_STATUS=" + _runtime_metadata(
            properties.get("beam_elevation_status")
        ),
        ";BEAM_ELEV_SOURCE=" + _runtime_metadata(
            properties.get("beam_elevation_source")
        ),
        ";BEAM_ELEV_TEXT=" + _runtime_metadata(
            properties.get("beam_elevation_text")
        ),
        ";BEAM_ELEV_REF_COUNT=" + _runtime_metadata(
            properties.get("beam_elevation_ref_count")
        ),
        ";PROFILE_KIND=" + _runtime_metadata(properties.get("profile_kind")),
        ";QTY_BASIS=" + _runtime_metadata(properties.get("quantity_basis")),
        ";SOURCE_REF_COUNT=" + _runtime_metadata(properties.get("source_ref_count")),
        ";CONFIDENCE=" + _runtime_metadata(properties.get("confidence")),
    ))


def _runtime_value(value: object) -> object:
    """Remove floating-point parser noise below Revit's geometric precision."""

    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list):
        return [_runtime_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _runtime_value(item) for key, item in value.items()}
    return value


def compiled_plan_sha256(compiled: dict) -> str:
    """Hash the deterministic engineering instructions.

    The generated script contains host-local paths, and the normalized DTO
    carries operational approval/build metadata.  Neither should change the
    compiler plan: target binding and the human approval digest are signed
    separately by the Bridge capability.  Hashing only the engineering
    instructions keeps dry-run/replay deterministic while the compiler
    version binds the code itself.
    """

    import copy

    identity = copy.deepcopy(compiled)
    build = identity.get("build")
    if isinstance(build, dict):
        for key in (
            "build_id",
            "wall_model_sha256",
            "approval",
            "target_model_path",
        ):
            build.pop(key, None)
    coordinate = identity.get("coordinate_system")
    if isinstance(coordinate, dict):
        # A future compiler may add an image/export prefix here; keep this
        # explicit so a host-local operational value cannot enter the plan
        # identity accidentally.
        coordinate.pop("actual_prefix", None)
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compile_wall_model_script(
    compiled: dict,
    *,
    actual_prefix: Path,
    expected_wall_count: int,
    expected_column_count: int | None = None,
    expected_beam_count: int | None = None,
) -> str:
    """Generate the typed Revit program for an approved WallModel."""

    elements = compiled.get("model_elements") or []
    openings = compiled.get("openings") or []
    opening_marks_by_host: dict[str, list[str]] = {}
    opening_type_names_by_host: dict[str, list[str]] = {}
    for opening in openings:
        if (opening or {}).get("status") != "matched":
            continue
        mark = _runtime_metadata((opening or {}).get("mark"))
        type_name = _runtime_metadata(
            (opening or {}).get("type_name") or (opening or {}).get("mark")
        )
        for host_id in (opening or {}).get("host_wall_ids") or []:
            host_key = str(host_id or "")
            if not host_key or not mark:
                continue
            values = opening_marks_by_host.setdefault(host_key, [])
            if mark not in values:
                values.append(mark)
            type_values = opening_type_names_by_host.setdefault(host_key, [])
            if type_name and type_name not in type_values:
                type_values.append(type_name)
    # Source references and engineering metadata are validated against
    # immutable artifacts before the compiler is called.  Revit only consumes
    # geometry plus a compact element-property projection.  Shipping the verbose DTO
    # through pyRevit Routes exceeds its local request-body limit on floors
    # with many profiled columns, so keep the complete data in the approved
    # WallModel/compiled-plan hash and emit a size-bounded execution DTO here.
    runtime_elements = []
    modeling_standard = dict(compiled.get("modeling_standard") or {})
    delivery_units = str(
        modeling_standard.get("delivery_units") or REVIT_DELIVERY_UNITS
    ).strip().lower()
    if delivery_units != REVIT_DELIVERY_UNITS:
        raise ValidationFailure(
            "Revit delivery units must be millimetres (mm), got " + delivery_units
        )
    modeling_standard["delivery_units"] = REVIT_DELIVERY_UNITS
    for element in elements:
        runtime_element = dict(element or {})
        runtime_source = dict(element or {})
        runtime_source["modeling_standard"] = modeling_standard
        for key in (
            "source_refs",
            "elevation_refs",
            "source_evidence_ids",
            "confidence",
            "topology",
            "construction",
            "quantities",
            "modeling_standard",
        ):
            runtime_element.pop(key, None)
        construction = (element or {}).get("construction") or {}
        material_name = construction.get("material_name")
        if material_name:
            runtime_element["material_name"] = material_name
        element_properties = _runtime_element_properties(runtime_source)
        schedule_suffix = _runtime_schedule_suffix(runtime_source)
        if (element or {}).get("type") == "Wall":
            marks = opening_marks_by_host.get(str((element or {}).get("id") or ""), [])
            schedule_suffix += ";OPENINGS=" + _runtime_metadata(
                ",".join(sorted(marks))
            )
            element_properties["opening_marks"] = ",".join(sorted(marks))
            element_properties["opening_type_names"] = ",".join(
                sorted(opening_type_names_by_host.get(str((element or {}).get("id") or ""), []))
            )
        runtime_element["element_properties"] = element_properties
        schedule_suffix += ";QTY_SCOPE=GROSS_NO_OPENINGS"
        runtime_element["schedule_suffix"] = schedule_suffix
        runtime_elements.append(_runtime_value(runtime_element))
    runtime_openings = []
    for opening in openings:
        runtime_opening = dict(opening or {})
        # Full immutable evidence remains in WallModel/compiled-plan identity;
        # Revit only needs the typed host/mark association for element metadata.
        runtime_opening.pop("source_refs", None)
        runtime_opening.pop("limitations", None)
        runtime_openings.append(_runtime_value(runtime_opening))
    grids = compiled.get("grids") or []
    level = ((compiled.get("project") or {}).get("levels") or [{}])[0]
    coordinate_system = compiled.get("coordinate_system") or {}
    prefix = str(Path(actual_prefix).resolve())
    shared_parameter_path = str(
        (Path(actual_prefix).resolve().parent / SHARED_PARAMETER_FILE_NAME).resolve()
    )
    payload = {
        "project_id": (compiled.get("project") or {}).get("project_id", ""),
        "tenant_id": (compiled.get("project") or {}).get("tenant_id", ""),
        "level_id": level.get("id", ""),
        "level_name": level.get("name", ""),
        "level_elevation_mm": float(level.get("elevation") or 0.0),
        "floor_code": (compiled.get("build") or {}).get("floor_code", ""),
        "build_id": (compiled.get("build") or {}).get("build_id", ""),
        "wall_model_sha256": (compiled.get("build") or {}).get(
            "wall_model_sha256", ""
        ),
        "shared_parameters_path": shared_parameter_path,
        "element_parameter_fields": [list(item) for item in ELEMENT_PARAMETER_FIELDS],
        "coordinate_system": {
            "offset_policy": str(
                coordinate_system.get("offset_policy") or "local_origin"
            ),
            "project_offset_mm": coordinate_system.get("project_offset_mm"),
            "apply_base_point_rotation": bool(
                coordinate_system.get("apply_base_point_rotation")
            ),
            "source_frame": str(
                coordinate_system.get("source_frame") or "project_north"
            ),
            "transform_chain": coordinate_system.get("transform_chain") or [],
            "source_bounds_m": coordinate_system.get("source_bounds_m"),
        },
        "elements": runtime_elements,
        "grids": grids,
        "junctions": compiled.get("junctions") or [],
        # A plan marker has no sill/head elevation, so it is persisted as
        # host-element semantics rather than an invented Revit void cut.
        "openings": runtime_openings,
        "modeling_standard": modeling_standard,
    }
    literal = repr(payload)
    if len(literal) > 1_500_000:
        raise ValidationFailure("compiled WallModel script exceeds 1.5 MB")
    grid_count = sum(
        len((item or {}).get("x_axes") or [])
        + len((item or {}).get("y_axes") or [])
        for item in grids
    )
    column_count = sum(1 for item in elements if (item or {}).get("type") == "Column")
    beam_count = sum(1 for item in elements if (item or {}).get("type") == "Beam")
    if expected_column_count is None:
        expected_column_count = column_count
    if expected_beam_count is None:
        expected_beam_count = beam_count
    # A plain template plus replacement is used instead of an f-string so the
    # emitted IronPython dictionaries and exception messages need no escaping.
    template = r'''# -*- coding: utf-8 -*-
from Autodesk.Revit.DB import *
from Autodesk.Revit.DB.Structure import StructuralType
from System.Collections.Generic import List
import clr
import math
import os

DATA = __DATA__
MODELING_STANDARD = DATA.get("modeling_standard") or {}
MM_PER_FOOT = 304.8
DELIVERY_UNITS = "mm"
PROJECT_ID = __PROJECT_ID__
TENANT_ID = __TENANT_ID__
FLOOR_CODE = __FLOOR_CODE__
BUILD_ID = __BUILD_ID__
WALL_MODEL_SHA256 = __WALL_MODEL_SHA256__
ACTUAL_PREFIX = __ACTUAL_PREFIX__
EXPECTED_WALL_COUNT = __EXPECTED_WALL_COUNT__
EXPECTED_COLUMN_COUNT = __EXPECTED_COLUMN_COUNT__
EXPECTED_BEAM_COUNT = __EXPECTED_BEAM_COUNT__
EXPECTED_GRID_COUNT = __EXPECTED_GRID_COUNT__
MATERIAL_ID_CACHE = {}
WALL_TYPE_CACHE = {}
DELIVERY_MATERIAL_ID = None

# Revit 2020 runs the emitted program under IronPython 2.7.  That runtime has
# ``math.isnan``/``math.isinf`` but does not provide the Python 3
# ``math.isfinite`` helper, so keep the finite-value check compatible with
# both the Bridge's Python tests and the in-process Revit runner.
def _finite(value):
    try:
        value = float(value)
        return not math.isnan(value) and not math.isinf(value)
    except Exception:
        return False

def _name(item):
    try:
        return Element.Name.GetValue(item)
    except Exception:
        return ""

def _comments(item):
    try:
        parameter = item.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        return (parameter.AsString() or "") if parameter is not None else ""
    except Exception:
        return ""

def _set_comments(item, value):
    parameter = item.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if parameter is None or parameter.IsReadOnly:
        raise Exception("BuildMate marker parameter is unavailable")
    parameter.Set(value)

def _configure_metric_units(doc):
    """Set Revit 2020 project display units to millimetres before delivery."""
    units = doc.GetUnits()
    format_specs = (
        (UnitType.UT_Length, DisplayUnitType.DUT_MILLIMETERS),
        (UnitType.UT_Area, DisplayUnitType.DUT_SQUARE_MILLIMETERS),
        (UnitType.UT_Volume, DisplayUnitType.DUT_CUBIC_MILLIMETERS),
    )
    for unit_type, display_unit in format_specs:
        options = units.GetFormatOptions(unit_type)
        options.DisplayUnits = display_unit
        # Do not force a global precision.  The drawing legend/detail is the
        # authority for integer specifications; changing FormatOptions here
        # would hide a conflict or round an unresolved geometric measurement.
        units.SetFormatOptions(unit_type, options)
    doc.SetUnits(units)
    print("BUILDMATE_DISPLAY_UNITS_APPLIED length=mm area=mm2 volume=mm3")

def _try_set_comments(item, value):
    try:
        _set_comments(item, value)
        return True
    except Exception:
        return False

def _safe_metadata(value):
    text = str(value or "").replace(";", "_").replace("=", "_")
    return text[:120]

def _schedule_suffix(data):
    # Backward-compatible accessor for the compact element-property payload.
    # The approved contract retains the explicit ";MAT=", ";FOOTPRINT_M2=" and
    # ";VOL_M3=" fields because downstream opening checks parse those tokens.
    return str(data.get("schedule_suffix") or "")

def _element_property_suffix(data):
    """Return the persisted, inspectable property string for one element."""
    suffix = _schedule_suffix(data)
    properties = data.get("element_properties") or {}
    if not properties:
        return suffix
    values = []
    for key in (
            "element_id", "category", "type_name", "type_mark",
            "family_name", "family_type", "representation",
            "structural_role",
            "specification_status", "specification_source", "specification_text",
            "specification_mm",
            "material_name", "material_status", "classification_code",
            "classification_system", "standard_profile", "units", "coordinate_frame",
            "quantity_basis", "quantity_scope", "level_id", "thickness_mm",
            "height_mm", "width_mm", "depth_mm", "profile_kind",
            "source_ref_count", "beam_elevation_ref_count", "confidence",
            "opening_marks", "opening_type_names"):
        if key not in properties:
            continue
        values.append(";PROP_%s=%s" % (
            str(key).upper(), _safe_metadata(properties.get(key))))
    return suffix + "".join(values)

def _shared_parameter_definition(app, definition_file, name):
    group = definition_file.Groups.get_Item("BuildMate")
    if group is None:
        group = definition_file.Groups.Create("BuildMate")
    definition = group.Definitions.get_Item(name)
    if definition is None:
        raise Exception("BuildMate shared parameter is missing: " + str(name))
    return definition

def _bind_element_parameters(doc):
    """Bind the stable BM_* fields to walls, columns and beams."""
    path = str(DATA.get("shared_parameters_path") or "")
    if not path:
        raise Exception("BuildMate shared parameter file path is missing")
    app = doc.Application
    previous = app.SharedParametersFilename
    app.SharedParametersFilename = path
    try:
        definition_file = app.OpenSharedParameterFile()
        if definition_file is None:
            raise Exception("BuildMate shared parameter file cannot be opened")
        categories = app.Create.NewCategorySet()
        walls_category = getattr(BuiltInCategory, "OST_Walls", None)
        columns_category = getattr(BuiltInCategory, "OST_StructuralColumns", None)
        framing_category = getattr(BuiltInCategory, "OST_StructuralFraming", None)
        if walls_category is None or columns_category is None:
            raise Exception("Revit wall/structural-column categories are unavailable")
        categories.Insert(doc.Settings.Categories.get_Item(walls_category))
        categories.Insert(doc.Settings.Categories.get_Item(columns_category))
        if framing_category is not None:
            categories.Insert(doc.Settings.Categories.get_Item(framing_category))
        group = getattr(BuiltInParameterGroup, "PG_IDENTITY_DATA", None)
        if group is None:
            raise Exception("Revit identity parameter group is unavailable")
        for field in DATA.get("element_parameter_fields") or []:
            if len(field) < 1:
                continue
            name = str(field[0])
            definition = _shared_parameter_definition(app, definition_file, name)
            binding = app.Create.NewInstanceBinding(categories)
            inserted = doc.ParameterBindings.Insert(definition, binding, group)
            if not inserted:
                reinserter = getattr(doc.ParameterBindings, "ReInsert", None)
                if reinserter is None or not reinserter(definition, binding, group):
                    raise Exception("BuildMate shared parameter binding failed: " + name)
    finally:
        app.SharedParametersFilename = previous

def _set_element_parameters(item, data):
    global ELEMENT_PARAMETER_WRITE_COUNT
    properties = data.get("element_properties") or {}
    written = 0
    for field in DATA.get("element_parameter_fields") or []:
        if len(field) < 2:
            continue
        name, key = str(field[0]), str(field[1])
        parameter = item.LookupParameter(name)
        if parameter is None or parameter.IsReadOnly:
            raise Exception("BuildMate element parameter is unavailable: " + name)
        value = properties.get(key, "")
        parameter.Set(_safe_metadata(value))
        written += 1
    ELEMENT_PARAMETER_WRITE_COUNT += written
    return written

def _set_schedule_mark(item, value):
    try:
        parameter = item.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
        if parameter is not None and not parameter.IsReadOnly:
            parameter.Set(str(value or "")[:120])
            return True
    except Exception:
        pass
    return False

def _set_opening_marker(opening, item):
    """Persist an opening identity wherever Revit 2020 exposes one.

    Native wall ``Opening`` elements are not regular family instances.  In
    some Revit 2020 templates they expose neither instance Comments nor Mark.
    The opening remains traceable through the already-verified host-wall
    ``OPENINGS`` metadata and the Bridge's created-element receipt, so the
    absence of an optional native identity parameter must not invalidate an
    otherwise verified physical cut.
    """
    marker = "BUILDMATE_AUTO_OPENING:T=%s;P=%s;F=%s;I=%s;TYPE=%s;ELEVATION_SOURCE=%s" % (
        TENANT_ID, PROJECT_ID, FLOOR_CODE, item["id"],
        item.get("type_name") or item["mark"], item.get("elevation_source") or "unresolved")
    if _try_set_comments(opening, marker):
        return "comments"
    if _set_schedule_mark(opening, item.get("type_name") or item.get("mark") or item["id"]):
        return "mark"
    return "host_and_receipt"

def _set_wall_base_offset(wall, target_z, host_level_z):
    """Place a hosted wall at the requested physical bottom elevation.

    The selected native Revit Level is only the host/semantic floor.  It may
    have a different elevation from the user-provided ``-6.4~0`` range, so
    the wall's base constraint must carry the signed difference explicitly.
    """
    parameter_id = getattr(BuiltInParameter, "WALL_BASE_OFFSET", None)
    parameter = wall.get_Parameter(parameter_id) if parameter_id is not None else None
    delta_ft = (float(target_z) - float(host_level_z)) / MM_PER_FOOT
    if parameter is None or parameter.IsReadOnly:
        if abs(delta_ft) > 1.0e-8:
            raise Exception("wall base offset parameter is unavailable")
        return
    parameter.Set(delta_ft)

def _level(doc):
    wanted_name = DATA.get("level_name") or ""
    wanted_elevation = float(DATA.get("level_elevation_mm") or 0.0)
    wanted_code = str(DATA.get("floor_code") or "").strip().upper()
    levels = [item for item in FilteredElementCollector(doc).OfClass(Level)]

    def _key(value):
        # Revit templates use names such as ``Level 1``, ``1F`` and ``B1``
        # interchangeably.  Normalize only for matching; the selected native
        # Level object remains the source of truth for model creation.
        return "".join(ch for ch in str(value or "").strip().upper() if ch.isalnum())

    def _digits(value):
        return "".join(ch for ch in str(value or "") if ch.isdigit())

    def _code_match(name, code):
        name_raw = str(name or "").strip().upper()
        code_raw = str(code or "").strip().upper()
        name_key = _key(name_raw)
        code_key = _key(code_raw)
        if not code_key:
            return False
        if name_key == code_key or code_key in name_key:
            return True
        digits = _digits(code_key)
        if not digits:
            return False
        if code_key.startswith("B"):
            # Do not map B1 to an above-ground ``Level 1`` merely because the
            # numeric suffix is equal; require an explicit basement marker.
            aliases = ("B" + digits, "BASEMENT" + digits, "地下" + digits,
                       "LEVEL-" + digits)
            if "BASEMENT" in name_raw or "地下" in name_raw or ("-" + digits) in name_raw:
                return True
        elif code_key.startswith("L") or code_key.startswith("F"):
            aliases = ("L" + digits, "F" + digits, digits + "F",
                       "LEVEL" + digits, digits + "FL")
        else:
            aliases = (code_key, "LEVEL" + digits, digits + "F")
        return any(_key(alias) in name_key for alias in aliases)

    def _closest(items):
        best = None
        best_delta = None
        for item in items:
            delta = abs(float(item.Elevation) * MM_PER_FOOT - wanted_elevation)
            if best is None or delta < best_delta:
                best, best_delta = item, delta
        return best, best_delta

    exact = [item for item in levels if _name(item) == wanted_name]
    if exact:
        candidate, delta = _closest(exact)
        if delta <= 5.0:
            return candidate
        raise Exception("Revit level elevation mismatch: %s" % wanted_name)

    coded = [item for item in levels if _code_match(_name(item), wanted_code)]
    if coded:
        candidate, _ = _closest(coded)
        return candidate

    near = [
        item for item in levels
        if abs(float(item.Elevation) * MM_PER_FOOT - wanted_elevation) <= 5.0
    ]
    if len(near) == 1:
        return near[0]
    if len(levels) == 1:
        # A single-level template has no ambiguity.  Let that native level
        # host the approved floor while keeping its real name for read-back.
        return levels[0]
    available = ", ".join(
        "%s@%.1fmm" % (_name(item), float(item.Elevation) * MM_PER_FOOT)
        for item in levels
    )
    raise Exception(
        "Revit level not found: %s (floor_code=%s; available=%s)"
        % (wanted_name, wanted_code, available)
    )

def _base_point_transform(doc):
    coordinate = DATA.get("coordinate_system") or {}
    policy = str(coordinate.get("offset_policy") or "local_origin")
    explicit = coordinate.get("project_offset_mm")
    base = None
    if policy == "local_origin":
        return 0.0, 0.0, 0.0
    if policy not in (
            "revit_project_base_point", "revit_survey_point", "source_origin"):
        raise Exception("unsupported coordinate offset policy: " + policy)
    if policy == "source_origin":
        return 0.0, 0.0, 0.0
    if isinstance(explicit, (list, tuple)) and len(explicit) >= 2:
        offset_x = float(explicit[0])
        offset_y = float(explicit[1])
    else:
        category_name = (
            "OST_SharedBasePoint" if policy == "revit_survey_point"
            else "OST_ProjectBasePoint"
        )
        category = getattr(BuiltInCategory, category_name, None)
        if category is None:
            raise Exception("Revit base-point category is unavailable: " + category_name)
        for candidate in FilteredElementCollector(doc).OfCategory(
                category).WhereElementIsNotElementType():
            base = candidate
            break
        if base is None:
            raise Exception("active Revit Project Base Point is unavailable")
        try:
            position = base.Position
            offset_x = float(position.X) * MM_PER_FOOT
            offset_y = float(position.Y) * MM_PER_FOOT
        except Exception:
            east_west = base.get_Parameter(BuiltInParameter.BASEPOINT_EASTWEST_PARAM)
            north_south = base.get_Parameter(BuiltInParameter.BASEPOINT_NORTHSOUTH_PARAM)
            if east_west is None or north_south is None:
                raise Exception("cannot read Revit Project Base Point coordinates")
            offset_x = float(east_west.AsDouble()) * MM_PER_FOOT
            offset_y = float(north_south.AsDouble()) * MM_PER_FOOT
    angle = 0.0
    if coordinate.get("apply_base_point_rotation"):
        if base is None:
            raise Exception("base-point rotation requested without a readable base point")
        angle_found = False
        for parameter_name in ("BASEPOINT_ANGLETON_PARAM", "BASEPOINT_ANGLE_PARAM"):
            parameter_id = getattr(BuiltInParameter, parameter_name, None)
            if parameter_id is None:
                continue
            parameter = base.get_Parameter(parameter_id)
            if parameter is not None and parameter.HasValue:
                # Revit reports the angle from project north to true north;
                # source coordinates declared as true-north need the inverse
                # rotation to land in the project frame.
                angle = -float(parameter.AsDouble())
                angle_found = True
                break
        if not angle_found:
            raise Exception("cannot read the requested base-point rotation")
    return offset_x, offset_y, angle

OFFSET_X, OFFSET_Y, BASE_ANGLE = _base_point_transform(doc)
print("BUILDMATE_COORDINATE_APPLIED offset_x_mm=%.6f offset_y_mm=%.6f angle_rad=%.12f" % (
    OFFSET_X, OFFSET_Y, BASE_ANGLE))

def _project_xy(point):
    x = float(point[0])
    y = float(point[1])
    cosine = math.cos(BASE_ANGLE)
    sine = math.sin(BASE_ANGLE)
    return [cosine * x - sine * y + OFFSET_X,
            sine * x + cosine * y + OFFSET_Y]

def _line(item, z_mm=0.0):
    start = _project_xy(item["start"])
    end = _project_xy(item["end"])
    start_z = (float(item["start"][2]) + z_mm) / MM_PER_FOOT
    end_z = (float(item["end"][2]) + z_mm) / MM_PER_FOOT
    return Line.CreateBound(
        XYZ(start[0] / MM_PER_FOOT, start[1] / MM_PER_FOOT, start_z),
        XYZ(end[0] / MM_PER_FOOT, end[1] / MM_PER_FOOT, end_z),
    )

def _material_id(doc, material_name):
    name = str(material_name or "").strip()
    if name in MATERIAL_ID_CACHE:
        return MATERIAL_ID_CACHE[name]
    if not name:
        MATERIAL_ID_CACHE[name] = ElementId.InvalidElementId
        return MATERIAL_ID_CACHE[name]
    for item in FilteredElementCollector(doc).OfClass(Material):
        if _name(item) == name:
            MATERIAL_ID_CACHE[name] = item.Id
            return MATERIAL_ID_CACHE[name]
    MATERIAL_ID_CACHE[name] = Material.Create(doc, name)
    return MATERIAL_ID_CACHE[name]

def _delivery_material_id(doc):
    """Return a dedicated neutral-gray material for exact-profile columns.

    DirectShape geometry is shaded from its material in 3D views.  A source
    concrete material may legitimately be black in a Revit template, and a
    view override cannot reliably replace that material color in Revit 2020.
    Keep the source material in BM_Material metadata, but use this delivery
    material for presentation so irregular columns remain visible and match
    the gray delivery convention.
    """
    global DELIVERY_MATERIAL_ID
    if DELIVERY_MATERIAL_ID is not None:
        return DELIVERY_MATERIAL_ID
    material_name = "BuildMate - Delivery Gray"
    material = None
    for candidate in FilteredElementCollector(doc).OfClass(Material):
        if _name(candidate) == material_name:
            material = candidate
            break
    if material is None:
        material_id = Material.Create(doc, material_name)
        material = doc.GetElement(material_id)
    if material is None:
        return ElementId.InvalidElementId
    gray = Color(180, 180, 180)
    try:
        material.Color = gray
    except Exception:
        pass
    for attribute in ("SurfaceForegroundPatternColor", "CutForegroundPatternColor"):
        try:
            setattr(material, attribute, gray)
        except Exception:
            pass
    DELIVERY_MATERIAL_ID = material.Id
    return DELIVERY_MATERIAL_ID

def _wall_type(doc, thickness_mm, preferred_name, material_name):
    target = float(thickness_mm or 0.0) / MM_PER_FOOT
    cache_key = "%s|%s|%s" % (
        round(float(thickness_mm or 0.0), 6),
        str(preferred_name or ""),
        str(material_name or ""),
    )
    if cache_key in WALL_TYPE_CACHE:
        return WALL_TYPE_CACHE[cache_key]
    basic = []

    def _is_basic_wall_type(item):
        # FamilyName is localized in Revit (and in Revit 2020 it can be an
        # IronPython byte/unicode value whose comparison with an English
        # literal is false).  WallKind is the stable API contract; keep the
        # name check only as a compatibility fallback for older hosts.
        try:
            if item.Kind == WallKind.Basic:
                return True
        except Exception:
            pass
        try:
            family = item.FamilyName
            return family in ("Basic Wall", "基本墙")
        except Exception:
            return False

    for item in FilteredElementCollector(doc).OfClass(WallType):
        try:
            if not _is_basic_wall_type(item):
                continue
            basic.append(item)
            if preferred_name and _name(item) == preferred_name:
                if target <= 0.0 or abs(float(item.Width) - target) <= (5.0 / MM_PER_FOOT):
                    WALL_TYPE_CACHE[cache_key] = item
                    return WALL_TYPE_CACHE[cache_key]
                raise Exception("configured wall type thickness mismatch: " + preferred_name)
        except Exception:
            if preferred_name and _name(item) == preferred_name:
                raise
    if not basic:
        raise Exception("no Basic Wall type exists in target model")
    base = basic[0]
    standard_prefix = str(
        MODELING_STANDARD.get("wall_type_prefix") or "BM-WALL"
    ).replace(";", "_").replace("=", "_")
    if preferred_name:
        # A legend/detail-derived type name is a requested output identity,
        # not a prerequisite that must already exist in the target template.
        duplicate_name = str(preferred_name)
    else:
        numeric = float(thickness_mm)
        display = (
            str(int(round(numeric)))
            if abs(numeric - round(numeric)) <= 1.0e-6
            else ("%.3f" % numeric).rstrip("0").rstrip(".")
        )
        duplicate_name = "%s-%smm" % (standard_prefix, display)
    candidate = None
    for item in basic:
        if _name(item) == duplicate_name:
            candidate = item
            break
    if candidate is None:
        candidate = base.Duplicate(duplicate_name)
    compound = candidate.GetCompoundStructure()
    if compound is None or compound.LayerCount <= 0:
        raise Exception("wall type has no editable compound structure")
    widths = [compound.GetLayerWidth(index) for index in range(compound.LayerCount)]
    layer = max(range(len(widths)), key=lambda index: widths[index])
    new_width = widths[layer] + target - sum(widths)
    if new_width <= 0.001:
        raise Exception("requested wall thickness is incompatible with wall layers")
    compound.SetLayerWidth(layer, new_width)
    material_id = _material_id(doc, material_name)
    if material_id != ElementId.InvalidElementId:
        compound.SetMaterialId(layer, material_id)
    candidate.SetCompoundStructure(compound)
    doc.Regenerate()
    if abs(float(candidate.Width) - target) > (5.0 / MM_PER_FOOT):
        raise Exception("wall type thickness read-back mismatch")
    WALL_TYPE_CACHE[cache_key] = candidate
    return WALL_TYPE_CACHE[cache_key]

def _delete_previous_build(doc):
    wall_prefix = "BUILDMATE_AUTO:T=%s;P=%s;F=%s;" % (TENANT_ID, PROJECT_ID, FLOOR_CODE)
    grid_prefix = "BUILDMATE_AUTO_GRID:T=%s;P=%s;F=%s;" % (TENANT_ID, PROJECT_ID, FLOOR_CODE)
    wall_ids = []
    grid_ids = []
    column_ids = []
    beam_ids = []
    for wall in FilteredElementCollector(doc).OfClass(Wall):
        if _comments(wall).startswith(wall_prefix):
            wall_ids.append(wall.Id)
    for grid in FilteredElementCollector(doc).OfClass(Grid):
        if _comments(grid).startswith(grid_prefix):
            grid_ids.append(grid.Id)
    # DirectShape columns are isolated by the application marker as well as
    # the comments parameter because Revit 2020 templates differ in whether
    # ALL_MODEL_INSTANCE_COMMENTS is exposed for a DirectShape instance.
    try:
        column_category = getattr(BuiltInCategory, "OST_StructuralColumns")
        column_app_id = "BuildMate:%s:%s:%s" % (
            TENANT_ID, PROJECT_ID, FLOOR_CODE)
        for column in FilteredElementCollector(doc).OfCategory(
                column_category).WhereElementIsNotElementType():
            marker = _comments(column)
            app_id = str(getattr(column, "ApplicationId", "") or "")
            if marker.startswith(wall_prefix.replace("BUILDMATE_AUTO:", "BUILDMATE_AUTO_COLUMN:")) \
                    or app_id == column_app_id:
                column_ids.append(column.Id)
    except Exception:
        # A host without the structural-column category is handled by the
        # normal wall/grid path; column creation will emit a clear error.
        pass
    try:
        framing_category = getattr(BuiltInCategory, "OST_StructuralFraming")
        beam_app_id = "BuildMate:%s:%s:%s" % (
            TENANT_ID, PROJECT_ID, FLOOR_CODE)
        for beam in FilteredElementCollector(doc).OfCategory(
                framing_category).WhereElementIsNotElementType():
            marker = _comments(beam)
            app_id = str(getattr(beam, "ApplicationId", "") or "")
            if marker.startswith(wall_prefix.replace("BUILDMATE_AUTO:", "BUILDMATE_AUTO_BEAM:")) \
                    or app_id == beam_app_id:
                beam_ids.append(beam.Id)
    except Exception:
        pass
    for element_id in wall_ids:
        doc.Delete(element_id)
    for element_id in grid_ids:
        doc.Delete(element_id)
    for element_id in column_ids:
        doc.Delete(element_id)
    for element_id in beam_ids:
        doc.Delete(element_id)
    return len(wall_ids), len(grid_ids), len(column_ids), len(beam_ids)


def _column_marker(column, item):
    column_id = str(item.get("id") or "")
    marker = "BUILDMATE_AUTO_COLUMN:T=%s;P=%s;F=%s;I=%s" % (
        TENANT_ID, PROJECT_ID, FLOOR_CODE, column_id)
    property_marker = marker + _element_property_suffix(item)
    comments_written = False
    try:
        _set_comments(column, property_marker)
        comments_written = True
    except Exception:
        # Revit 2020 templates differ in whether DirectShape exposes the
        # Comments parameter.  ApplicationDataId is still persisted on the
        # element, so retain the complete quantity/material payload there
        # instead of silently falling back to an ID-only marker.
        pass
    try:
        column.ApplicationId = "BuildMate:%s:%s:%s" % (
            TENANT_ID, PROJECT_ID, FLOOR_CODE)
        column.ApplicationDataId = property_marker
    except Exception:
        if not comments_written:
            # IronPython 2.7 (Revit 2020) does not implement Python 3's
            # exception-chaining syntax.  Preserve the explicit failure
            # without emitting ``raise ... from`` in the host script.
            raise Exception("BuildMate column schedule marker is unavailable")
    _set_schedule_mark(column, column_id)
    _set_element_parameters(column, item)

def _beam_marker(beam, item):
    beam_id = str(item.get("id") or "")
    marker = "BUILDMATE_AUTO_BEAM:T=%s;P=%s;F=%s;I=%s" % (
        TENANT_ID, PROJECT_ID, FLOOR_CODE, beam_id)
    property_marker = marker + _element_property_suffix(item)
    _set_comments(beam, property_marker)
    try:
        beam.ApplicationId = "BuildMate:%s:%s:%s" % (
            TENANT_ID, PROJECT_ID, FLOOR_CODE)
        beam.ApplicationDataId = property_marker
    except Exception:
        pass
    _set_schedule_mark(beam, beam_id)
    _set_element_parameters(beam, item)

def _element_metadata(item):
    comments = _comments(item)
    if comments:
        return comments
    try:
        return str(getattr(item, "ApplicationDataId", "") or "")
    except Exception:
        return ""

def _verify_element_properties(item, expected_id):
    value = _element_metadata(item)
    required = (
        ";ELEMENT_ID=", ";CATEGORY=", ";TYPE=", ";FAMILY=",
        ";FAMILY_TYPE=", ";REPRESENTATION=", ";MAT=",
        ";SPEC_STATUS=", ";SPEC_SOURCE=", ";SPEC_MM=",
        ";MAT_STATUS=", ";CLASS=", ";VOL_M3=",
        ";QTY_SCOPE=GROSS_NO_OPENINGS",
    )
    if not all(token in value for token in required):
        raise Exception("BuildMate element properties are incomplete: " + str(expected_id))
    return value

# Compatibility aliases keep previously generated scripts and old approval
# snapshots readable while new deliveries use the element-property wording.
_schedule_metadata = _element_metadata
_verify_schedule_metadata = _verify_element_properties

def _metadata_field(value, name):
    prefix = str(name) + "="
    for part in str(value or "").split(";"):
        if part.startswith(prefix):
            return part[len(prefix):]
    return ""


def _column_property(item, key):
    return str((item.get("element_properties") or {}).get(key) or "").strip()

def _family_name_matches(actual, desired):
    left = str(actual or "").replace(" ", "").replace("-", "").lower()
    right = str(desired or "").replace(" ", "").replace("-", "").lower()
    return bool(left and right and (left == right or right in left or left in right))

def _profile_is_rectangle(profile):
    """Recognize a four-edge rectangular profile without Shapely.

    A GBZ/YBZ label can classify a source profile as ``irregular`` even when
    the exported vector boundary is a true rectangle.  Revit 2020 can then
    use the native structural-column family (and expose a real Family/Type)
    while genuinely non-rectangular profiles continue through the exact
    DirectShape fallback.  This check is intentionally geometric and does
    not promote a text-only label into a shape.
    """
    if not isinstance(profile, (list, tuple)) or len(profile) != 4:
        return False
    points = []
    for value in profile:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return False
        points.append((float(value[0]), float(value[1])))
    edges = []
    for index in range(4):
        first = points[index]
        second = points[(index + 1) % 4]
        vector = (second[0] - first[0], second[1] - first[1])
        length = math.hypot(vector[0], vector[1])
        if length <= 1.0e-9:
            return False
        edges.append((vector, length))
    for index in range(4):
        left = edges[index][0]
        right = edges[(index + 1) % 4][0]
        if abs(left[0] * right[0] + left[1] * right[1]) > max(
                1.0e-6, edges[index][1] * edges[(index + 1) % 4][1] * 1.0e-3):
            return False
        if abs(edges[index][1] - edges[(index + 2) % 4][1]) > max(
                0.1, edges[index][1] * 1.0e-3):
            return False
    return True

def _set_family_dimension(symbol, names, value_mm):
    for name in names:
        try:
            parameter = symbol.LookupParameter(name)
            if parameter is not None and not parameter.IsReadOnly:
                parameter.Set(float(value_mm) / MM_PER_FOOT)
                return True
        except Exception:
            pass
    return False

def _column_family_symbol(doc, item):
    """Resolve a real structural-column FamilySymbol for rectangular columns.

    The source profile remains authoritative.  A loadable family is used only
    when its dimensions can be set and its committed bounding box matches that
    profile; otherwise the exact DirectShape path below remains explicit.
    """
    desired_family = _column_property(item, "family_name")
    desired_type = _column_property(item, "family_type") or _column_property(item, "type_name")
    aliases = [desired_family, "混凝土 - 矩形 - 柱", "Concrete - Rectangular Column"]
    symbols = []
    def collect():
        for symbol in FilteredElementCollector(doc).OfClass(FamilySymbol):
            try:
                category = symbol.Category
                family = symbol.Family
                if category is None or family is None:
                    continue
                if int(category.Id.IntegerValue) != int(BuiltInCategory.OST_StructuralColumns):
                    continue
                family_name = _name(family)
                if any(_family_name_matches(family_name, alias) for alias in aliases if alias):
                    symbols.append(symbol)
            except Exception:
                pass
    collect()
    if not symbols:
        # Revit 2020 Chinese installs normally keep this family in the stock
        # library but do not load it into a blank project template.  Loading
        # the known stock family is deterministic and avoids fabricating a
        # family name in the delivered model.
        program_data = os.environ.get("ProgramData", r"C:\\ProgramData")
        family_paths = (
            os.path.join(program_data, "Autodesk", "RVT 2020", "Libraries", "China", "结构", "柱", "混凝土", "混凝土 - 矩形 - 柱.rfa"),
            os.path.join(program_data, "Autodesk", "RVT 2020", "Libraries", "English", "Structural Columns", "Concrete-Rectangular-Column.rfa"),
        )
        for family_path in family_paths:
            if not os.path.isfile(family_path):
                continue
            try:
                family_ref = clr.Reference[Family]()
                doc.LoadFamily(family_path, family_ref)
                collect()
                if symbols:
                    break
            except Exception:
                continue
    if not symbols:
        return None
    selected = None
    for symbol in symbols:
        if desired_type and _name(symbol) == desired_type:
            selected = symbol
            break
    selected = selected or symbols[0]
    try:
        if desired_type and _name(selected) != desired_type:
            selected = selected.Duplicate(desired_type)
        width = float(item.get("width") or 0.0)
        depth = float(item.get("depth") or 0.0)
        if width <= 0.0 or depth <= 0.0:
            return None
        if not _set_family_dimension(selected, ("b", "B", "宽度", "Width"), width):
            return None
        if not _set_family_dimension(selected, ("h", "H", "高度", "深度", "Height", "Depth"), depth):
            return None
        if not selected.IsActive:
            selected.Activate()
        doc.Regenerate()
        return selected
    except Exception:
        return None

def _create_family_column(doc, item, level_z):
    profile = item.get("profile") or []
    construction = item.get("construction") or {}
    representation = str(construction.get("representation") or "").strip().lower()
    # An irregular/exact-profile column must stay a DirectShape.  Falling
    # through to a stock rectangular family can appear to work in plan but
    # changes the vertical extent (and loses the approved profile semantics),
    # which then makes the deterministic read-back gate roll the transaction
    # back.  Only an explicitly loadable rectangular representation may use a
    # native family symbol.
    if representation in ("profile_directshape", "directshape", "exact_profile"):
        return None
    if str(item.get("profile_kind") or "") != "rectangular":
        return None
    symbol = _column_family_symbol(doc, item)
    if symbol is None or level is None:
        return None
    center = item.get("center") or []
    if len(center) < 2 or len(profile) < 4:
        return None
    projected_center = _project_xy((float(center[0]), float(center[1])))
    host_level_z = float(level.Elevation) * MM_PER_FOOT
    point = XYZ(projected_center[0] / MM_PER_FOOT,
                projected_center[1] / MM_PER_FOOT,
                host_level_z / MM_PER_FOOT)
    column = None
    try:
        column = doc.Create.NewFamilyInstance(point, symbol, level, StructuralType.Column)
        base_level = column.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
        if base_level is not None and not base_level.IsReadOnly:
            base_level.Set(level.Id)
        top_level = column.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
        if top_level is not None and not top_level.IsReadOnly:
            # Structural columns default to the next available level in some
            # Revit 2020 templates.  Bind both ends to the requested source
            # level so the offsets below express an exact, self-contained
            # column height instead of adding height to an unrelated level.
            top_level.Set(level.Id)
        base_offset_ft = (float(level_z) - host_level_z) / MM_PER_FOOT
        base_offset = column.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)
        if base_offset is not None and not base_offset.IsReadOnly:
            base_offset.Set(base_offset_ft)
        top_offset = column.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)
        if top_offset is not None and not top_offset.IsReadOnly:
            top_offset.Set(
                base_offset_ft + float(item.get("height") or 0.0) / MM_PER_FOOT
            )
        # Align a rotated rectangular source profile with the family instance.
        longest = None
        for index in range(len(profile)):
            first = profile[index]
            second = profile[(index + 1) % len(profile)]
            length = math.hypot(float(second[0]) - float(first[0]),
                                float(second[1]) - float(first[1]))
            if longest is None or length > longest[0]:
                longest = (length, math.atan2(float(second[1]) - float(first[1]),
                                              float(second[0]) - float(first[0])))
        if longest is not None and abs(longest[1]) > 1.0e-8:
            axis = Line.CreateBound(point, XYZ(point.X, point.Y, point.Z + 1.0))
            ElementTransformUtils.RotateElement(doc, column.Id, axis, longest[1])
        doc.Regenerate()
        box = column.get_BoundingBox(None)
        if box is None:
            doc.Delete(column.Id)
            return None
        expected_height = float(item.get("height") or 0.0)
        actual_height = (float(box.Max.Z) - float(box.Min.Z)) * MM_PER_FOOT
        if expected_height <= 0.0 or abs(actual_height - expected_height) > 5.0:
            # A target family whose level/offset contract cannot represent the
            # approved height is not a compatible family for this delivery.
            # Delete the instance and preserve the geometry through the exact
            # DirectShape fallback instead of rolling back all other elements.
            doc.Delete(column.Id)
            return None
        expected = [
            _project_xy((float(center[0]) + float(point[0]),
                         float(center[1]) + float(point[1])))
            for point in profile
        ]
        expected_bounds = (
            min(point[0] for point in expected), min(point[1] for point in expected),
            max(point[0] for point in expected), max(point[1] for point in expected),
        )
        actual_bounds = (
            float(box.Min.X) * MM_PER_FOOT, float(box.Min.Y) * MM_PER_FOOT,
            float(box.Max.X) * MM_PER_FOOT, float(box.Max.Y) * MM_PER_FOOT,
        )
        if max(abs(actual_bounds[index] - expected_bounds[index]) for index in range(4)) > 5.0:
            doc.Delete(column.Id)
            return None
        item.setdefault("element_properties", {})["representation"] = "loadable_family"
        _column_marker(column, item)
        return column
    except Exception:
        if column is not None:
            try:
                doc.Delete(column.Id)
            except Exception:
                pass
        return None

def _create_directshape_column(doc, item, level_z):
    if not hasattr(DirectShape, "CreateElement"):
        raise Exception("Revit DirectShape API is unavailable for columns")
    center = item.get("center") or []
    profile = item.get("profile") or []
    if len(center) < 2 or len(profile) < 3:
        raise Exception("column profile is incomplete: " + str(item.get("id")))
    points = []
    for point in profile:
        if len(point) < 2:
            raise Exception("column profile point is invalid: " + str(item.get("id")))
        projected = _project_xy((float(center[0]) + float(point[0]),
                                 float(center[1]) + float(point[1])))
        points.append(XYZ(projected[0] / MM_PER_FOOT,
                          projected[1] / MM_PER_FOOT,
                          level_z / MM_PER_FOOT))
    if points[0].DistanceTo(points[-1]) <= 1.0e-9:
        points = points[:-1]
    if len(points) < 3:
        raise Exception("column profile is degenerate: " + str(item.get("id")))
    loop = CurveLoop()
    for index in range(len(points)):
        loop.Append(Line.CreateBound(points[index], points[(index + 1) % len(points)]))
    height_ft = float(item.get("height") or 0.0) / MM_PER_FOOT
    if height_ft <= 0.0:
        raise Exception("column height is not positive: " + str(item.get("id")))
    material_id = _delivery_material_id(doc)
    if material_id == ElementId.InvalidElementId:
        solid = GeometryCreationUtilities.CreateExtrusionGeometry(
            [loop], XYZ.BasisZ, height_ft
        )
    else:
        solid = GeometryCreationUtilities.CreateExtrusionGeometry(
            [loop], XYZ.BasisZ, height_ft,
            SolidOptions(material_id, ElementId.InvalidElementId),
        )
    category = getattr(BuiltInCategory, "OST_StructuralColumns", None)
    if category is None:
        raise Exception("Revit structural-column category is unavailable")
    column = DirectShape.CreateElement(doc, ElementId(category))
    # Revit 2020 exposes DirectShape instances as generic elements in the
    # Properties palette.  Preserve the approved drawing mark as the element
    # name as well as the BM_FamilyType shared parameter so schedules and
    # downstream exports still show a stable, human-readable family/type
    # identity when no compatible loadable irregular-column family is loaded.
    desired_name = _column_property(item, "family_type") or _column_property(
        item, "type_name"
    )
    if desired_name:
        try:
            column.Name = desired_name
        except Exception:
            pass
    shape = List[GeometryObject]()
    shape.Add(solid)
    column.SetShape(shape)
    _column_marker(column, item)
    return column

def _create_column(doc, item, level_z):
    family_column = _create_family_column(doc, item, level_z)
    if family_column is not None:
        return family_column
    # A DirectShape is retained only as an explicit, exact-profile fallback
    # for irregular columns or templates without a compatible loaded family.
    item.setdefault("element_properties", {})["representation"] = "profile_directshape"
    return _create_directshape_column(doc, item, level_z)

def _beam_family_symbol(doc, item):
    """Resolve and size a native Revit 2020 structural framing type."""
    desired_family = _column_property(item, "family_name")
    desired_type = _column_property(item, "family_type") or _column_property(item, "type_name")
    aliases = [desired_family, "混凝土 - 矩形 - 梁", "Concrete - Rectangular Beam"]
    symbols = []
    category = getattr(BuiltInCategory, "OST_StructuralFraming", None)
    if category is None:
        return None
    def collect(require_family_match):
        symbols[:] = []
        for symbol in FilteredElementCollector(doc).OfClass(FamilySymbol):
            try:
                if symbol.Category is None or int(symbol.Category.Id.IntegerValue) != int(category):
                    continue
                family = symbol.Family
                if family is None:
                    continue
                if require_family_match and not any(
                        _family_name_matches(_name(family), alias)
                        for alias in aliases if alias):
                    continue
                symbols.append(symbol)
            except Exception:
                pass
    collect(True)
    if not symbols:
        # A blank Revit 2020 template does not necessarily contain a
        # structural-framing symbol.  Load Autodesk's installed rectangular
        # concrete beam family before falling back to another already-loaded
        # framing family.  The Chinese path is the stock RVT 2020 library;
        # the English alternatives keep the same workflow portable.
        program_data = os.environ.get("ProgramData", r"C:\\ProgramData")
        family_paths = (
            os.path.join(program_data, "Autodesk", "RVT 2020", "Libraries", "China", "结构", "框架", "混凝土", "混凝土 - 矩形梁.rfa"),
            os.path.join(program_data, "Autodesk", "RVT 2020", "Libraries", "English", "Structural Framing", "Concrete", "Concrete - Rectangular Beam.rfa"),
            os.path.join(program_data, "Autodesk", "RVT 2020", "Libraries", "US Metric", "Structural Framing", "Concrete", "Concrete-Rectangular Beam.rfa"),
        )
        for family_path in family_paths:
            if not os.path.isfile(family_path):
                continue
            try:
                family_ref = clr.Reference[Family]()
                doc.LoadFamily(family_path, family_ref)
                collect(True)
                if symbols:
                    break
            except Exception:
                continue
    if not symbols:
        # Preserve compatibility with a project template that supplies a
        # custom parametric structural-framing family.  It is accepted only
        # if the width/depth parameters below can actually be written.
        collect(False)
    if not symbols:
        return None
    selected = None
    for symbol in symbols:
        if desired_type and _name(symbol) == desired_type:
            selected = symbol
            break
    selected = selected or symbols[0]
    try:
        if desired_type and _name(selected) != desired_type:
            selected = selected.Duplicate(desired_type)
        width = float(item.get("width") or 0.0)
        depth = float(item.get("depth") or 0.0)
        if width <= 0.0 or depth <= 0.0:
            return None
        if not _set_family_dimension(selected, ("b", "B", "宽度", "Width"), width):
            return None
        if not _set_family_dimension(selected, ("h", "H", "高度", "深度", "Height", "Depth"), depth):
            return None
        if not selected.IsActive:
            selected.Activate()
        doc.Regenerate()
        return selected
    except Exception:
        return None

def _beam_vertical_target(item, level_z):
    """Return the requested physical bottom/top elevations in millimetres.

    Revit structural-framing families are not consistent about whether their
    insertion line represents the section origin, centre or bottom.  The
    delivery contract is about the physical beam envelope, so the compiler
    keeps the drawing-derived bottom/top values separate from that insertion
    line and aligns the created element after regeneration.
    """
    raw_base = item.get("base_z")
    raw_top = item.get("top_z")
    base_z = level_z if raw_base is None else float(raw_base)
    depth = float(item.get("depth") or 0.0)
    if depth <= 0.0:
        raise Exception("beam depth is not positive: " + str(item.get("id")))
    top_z = base_z + depth if raw_top is None else float(raw_top)
    if top_z <= base_z:
        raise Exception("beam top elevation is not above its base: " + str(item.get("id")))
    return base_z, top_z

def _align_beam_vertical_extent(doc, beam, base_z, top_z, beam_id):
    """Align a native beam's physical bounding box to the approved elevations."""
    doc.Regenerate()
    box = beam.get_BoundingBox(None)
    if box is None:
        raise Exception("beam vertical read-back unavailable: " + str(beam_id))
    actual_min_z = float(box.Min.Z) * MM_PER_FOOT
    actual_max_z = float(box.Max.Z) * MM_PER_FOOT
    delta_mm = float(base_z) - actual_min_z
    if abs(delta_mm) > 0.001:
        ElementTransformUtils.MoveElement(
            doc, beam.Id, XYZ(0.0, 0.0, delta_mm / MM_PER_FOOT)
        )
        doc.Regenerate()
    box = beam.get_BoundingBox(None)
    if box is None:
        raise Exception("beam vertical read-back unavailable after alignment: " + str(beam_id))
    bottom_error = abs(float(box.Min.Z) * MM_PER_FOOT - float(base_z))
    top_error = abs(float(box.Max.Z) * MM_PER_FOOT - float(top_z))
    if bottom_error > 5.0 or top_error > 5.0:
        raise Exception(
            "beam vertical envelope mismatch: %s expected=[%.6f, %.6f]mm "
            "actual=[%.6f, %.6f]mm"
            % (
                str(beam_id), float(base_z), float(top_z),
                float(box.Min.Z) * MM_PER_FOOT,
                float(box.Max.Z) * MM_PER_FOOT,
            )
        )
    return bottom_error, top_error

def _create_beam(doc, item, level, level_z):
    symbol = _beam_family_symbol(doc, item)
    if symbol is None:
        raise Exception("no structural framing family/type available for beam: " + str(item.get("id")))
    base_z, top_z = _beam_vertical_target(item, level_z)
    # Create from the host level first.  Revit families disagree about whether
    # the insertion line is their centre, top or bottom; the deterministic
    # read-back alignment below moves the physical envelope to ``base_z`` /
    # ``top_z``.  Keeping the initial line on the level also avoids applying
    # a drawing-relative offset twice when a beam carries an explicit base_z.
    line = _line(item, level_z)
    beam = doc.Create.NewFamilyInstance(line, symbol, level, StructuralType.Beam)
    item.setdefault("element_properties", {})["representation"] = "loadable_family"
    _align_beam_vertical_extent(doc, beam, base_z, top_z, item.get("id"))
    _beam_marker(beam, item)
    return beam

def _grid_name(label, axis, index):
    if label is not None and str(label).strip():
        return str(label).strip()
    return "BM-%s-%s-%d" % (PROJECT_ID, axis, index + 1)

def _mark_grid(grid, marker):
    # Revit 2020 does not expose ALL_MODEL_INSTANCE_COMMENTS on Grid in every
    # template.  Axis names are themselves native, schedulable datum data, so
    # retain the requested label and use a visible BM prefix only when the
    # comments parameter is unavailable.  This is an explicit fallback, not
    # a silent loss of provenance.
    if _try_set_comments(grid, marker):
        return False
    name = _name(grid)
    if not name.startswith("BM-"):
        grid.Name = "BM-" + name
    return True

def _distance_point_segment(point, start, end):
    px, py = point.X, point.Y
    sx, sy = start.X, start.Y
    ex, ey = end.X, end.Y
    dx, dy = ex - sx, ey - sy
    length_sq = dx * dx + dy * dy
    if length_sq <= 1.0e-12:
        return math.sqrt((px - sx) ** 2 + (py - sy) ** 2)
    ratio = ((px - sx) * dx + (py - sy) * dy) / length_sq
    ratio = max(0.0, min(1.0, ratio))
    qx, qy = sx + ratio * dx, sy + ratio * dy
    return math.sqrt((px - qx) ** 2 + (py - qy) ** 2)

def _segments_intersect(left_start, left_end, right_start, right_end, tolerance_ft):
    """Return true when two wall centerline segments cross in plan.

    X/T junctions commonly meet through the middle of a wall rather than at
    an endpoint.  Endpoint-only proximity therefore rejects valid topology
    (and used to abort otherwise valid full-floor builds).  Keep the test
    purely 2-D and numerically bounded so it remains compatible with Revit
    2020 IronPython; the endpoint-distance checks below still cover parallel
    near joins.
    """
    ax, ay = left_start.X, left_start.Y
    bx, by = left_end.X, left_end.Y
    cx, cy = right_start.X, right_start.Y
    dx, dy = right_end.X, right_end.Y
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    cross = rx * sy - ry * sx
    qpx, qpy = cx - ax, cy - ay
    if abs(cross) <= 1.0e-12:
        # Collinear/parallel segments are considered joinable only when an
        # endpoint is within the caller's engineering tolerance.
        return min(
            _distance_point_segment(left_start, right_start, right_end),
            _distance_point_segment(left_end, right_start, right_end),
            _distance_point_segment(right_start, left_start, left_end),
            _distance_point_segment(right_end, left_start, left_end),
        ) <= tolerance_ft
    t = (qpx * sy - qpy * sx) / cross
    u = (qpx * ry - qpy * rx) / cross
    slack = max(1.0e-9, tolerance_ft / max(
        math.sqrt(rx * rx + ry * ry), math.sqrt(sx * sx + sy * sy), 1.0
    ))
    return -slack <= t <= 1.0 + slack and -slack <= u <= 1.0 + slack

def _walls_are_near(left, right, tolerance_ft):
    left_curve = left.Location.Curve
    right_curve = right.Location.Curve
    left_start, left_end = left_curve.GetEndPoint(0), left_curve.GetEndPoint(1)
    right_start, right_end = right_curve.GetEndPoint(0), right_curve.GetEndPoint(1)
    distances = [
        _distance_point_segment(left_start, right_start, right_end),
        _distance_point_segment(left_end, right_start, right_end),
        _distance_point_segment(right_start, left_start, left_end),
        _distance_point_segment(right_end, left_start, left_end),
    ]
    return min(distances) <= tolerance_ft or _segments_intersect(
        left_start, left_end, right_start, right_end, tolerance_ft
    )

def _native_join_elements(location, end):
    # Revit exposes ElementsAtJoin as an indexed property.  IronPython builds
    # have used both the CLR get_ accessor and indexer syntax, so support both
    # without weakening the final result: failure to read either form simply
    # means the pair is not verified as a native wall join.
    try:
        return list(location.get_ElementsAtJoin(end))
    except Exception:
        try:
            return list(location.ElementsAtJoin[end])
        except Exception:
            return []

def _native_wall_pair_joined(left, right):
    right_id = int(right.Id.IntegerValue)
    left_id = int(left.Id.IntegerValue)
    for end in (0, 1):
        for element in _native_join_elements(left.Location, end):
            if int(element.Id.IntegerValue) == right_id:
                return True
        for element in _native_join_elements(right.Location, end):
            if int(element.Id.IntegerValue) == left_id:
                return True
    return False

def _wall_pair_joined(doc, left, right):
    try:
        if JoinGeometryUtils.AreElementsJoined(doc, left, right):
            return True
    except Exception:
        pass
    return _native_wall_pair_joined(left, right)

def _infinite_line_intersection(left_curve, right_curve):
    left_start = left_curve.GetEndPoint(0)
    left_end = left_curve.GetEndPoint(1)
    right_start = right_curve.GetEndPoint(0)
    right_end = right_curve.GetEndPoint(1)
    rx, ry = left_end.X - left_start.X, left_end.Y - left_start.Y
    sx, sy = right_end.X - right_start.X, right_end.Y - right_start.Y
    cross = rx * sy - ry * sx
    if abs(cross) <= 1.0e-12:
        return None
    qx = right_start.X - left_start.X
    qy = right_start.Y - left_start.Y
    ratio = (qx * sy - qy * sx) / cross
    return XYZ(
        left_start.X + ratio * rx,
        left_start.Y + ratio * ry,
        (left_start.Z + right_start.Z) / 2.0,
    )

def _snap_near_wall_endpoint(wall, point, tolerance_ft):
    location = wall.Location
    curve = location.Curve
    start, end = curve.GetEndPoint(0), curve.GetEndPoint(1)
    distances = [start.DistanceTo(point), end.DistanceTo(point)]
    end_index = 0 if distances[0] <= distances[1] else 1
    distance = distances[end_index]
    if distance > tolerance_ft:
        return 0.0
    if distance <= 0.1 / MM_PER_FOOT:
        return 0.0
    replacement = XYZ(point.X, point.Y, start.Z if end_index == 0 else end.Z)
    points = [start, end]
    points[end_index] = replacement
    if points[0].DistanceTo(points[1]) <= 1.0 / MM_PER_FOOT:
        raise Exception("wall topology snap would create a zero-length wall")
    location.Curve = Line.CreateBound(points[0], points[1])
    return distance

def _connect_wall_centerlines(left, right, tolerance_ft):
    left_curve = left.Location.Curve
    right_curve = right.Location.Curve
    intersection = _infinite_line_intersection(left_curve, right_curve)
    if intersection is not None:
        if (
            _distance_point_segment(
                intersection, left_curve.GetEndPoint(0), left_curve.GetEndPoint(1)
            ) > tolerance_ft
            or _distance_point_segment(
                intersection, right_curve.GetEndPoint(0), right_curve.GetEndPoint(1)
            ) > tolerance_ft
        ):
            return []
        return [
            value for value in (
                _snap_near_wall_endpoint(left, intersection, tolerance_ft),
                _snap_near_wall_endpoint(right, intersection, tolerance_ft),
            ) if value > 0.0
        ]

    # A collinear continuation has no unique infinite-line intersection.  It
    # is safe to bridge only a truly collinear endpoint gap; parallel wall
    # faces must never be converted into a wall junction.
    left_start, left_end = left_curve.GetEndPoint(0), left_curve.GetEndPoint(1)
    right_start, right_end = right_curve.GetEndPoint(0), right_curve.GetEndPoint(1)
    dx, dy = left_end.X - left_start.X, left_end.Y - left_start.Y
    length = math.sqrt(dx * dx + dy * dy)
    if length <= 1.0e-12:
        return []
    line_offset = abs(
        (right_start.X - left_start.X) * dy
        - (right_start.Y - left_start.Y) * dx
    ) / length
    if line_offset > 5.0 / MM_PER_FOOT:
        return []
    endpoint_pairs = [
        (left_start, right_start), (left_start, right_end),
        (left_end, right_start), (left_end, right_end),
    ]
    first, second = min(endpoint_pairs, key=lambda pair: pair[0].DistanceTo(pair[1]))
    if first.DistanceTo(second) > tolerance_ft:
        return []
    midpoint = XYZ(
        (first.X + second.X) / 2.0,
        (first.Y + second.Y) / 2.0,
        (first.Z + second.Z) / 2.0,
    )
    return [
        value for value in (
            _snap_near_wall_endpoint(left, midpoint, tolerance_ft),
            _snap_near_wall_endpoint(right, midpoint, tolerance_ft),
        ) if value > 0.0
    ]

def _apply_topology(doc, wall_by_id):
    junctions = DATA.get("junctions") or []
    candidate_pairs = {}
    junction_pairs = []
    snap_distances = []

    def pair_tolerance(left, right):
        try:
            half_widths = (float(left.WallType.Width) + float(right.WallType.Width)) / 2.0
        except Exception:
            half_widths = 0.0
        # Net-cut L junctions have two centreline endpoints separated by the
        # diagonal of the two half-thickness sums.  The solids touch at the
        # corner even though their centreline gap is larger than a T branch.
        return max(
            150.0 / MM_PER_FOOT,
            math.sqrt(2.0) * half_widths + 20.0 / MM_PER_FOOT,
        )

    for junction in junctions:
        refs = [str(item) for item in (junction.get("wall_ids") or [])]
        if len(refs) < 2:
            raise Exception("topology junction has fewer than two walls")
        if any(item not in wall_by_id for item in refs):
            raise Exception("topology junction references a missing wall")
        pairs_here = []
        for index in range(len(refs)):
            for other_index in range(index + 1, len(refs)):
                left_id, right_id = refs[index], refs[other_index]
                pair = tuple(sorted((left_id, right_id)))
                left, right = wall_by_id[left_id], wall_by_id[right_id]
                tolerance_ft = pair_tolerance(left, right)
                if not _walls_are_near(left, right, tolerance_ft):
                    continue
                candidate_pairs[pair] = (left, right, tolerance_ft)
                pairs_here.append(pair)
        if not pairs_here:
            raise Exception("topology junction has no geometrically joinable wall pair")
        junction_pairs.append((
            str(junction.get("id") or ""),
            str(junction.get("kind") or ""),
            pairs_here,
        ))

    # Net-cut WallModel solids deliberately stop at the adjacent wall face so
    # approved geometry contains no collision.  Revit's native wall cleanup,
    # however, requires the corresponding location curves to reach the true
    # L/T/X intersection.  Extend only endpoints inside the bounded junction
    # tolerance, preserving the wall direction and never sweeping unrelated
    # wall pairs.
    for pair in sorted(candidate_pairs):
        left, right, tolerance_ft = candidate_pairs[pair]
        snap_distances.extend(
            _connect_wall_centerlines(left, right, tolerance_ft)
        )
        allow_count = 0
        for wall in (left, right):
            for end in (0, 1):
                try:
                    WallUtils.AllowWallJoinAtEnd(wall, end)
                    if WallUtils.IsWallJoinAllowedAtEnd(wall, end):
                        allow_count += 1
                except Exception:
                    pass
        if allow_count <= 0:
            raise Exception("wall endpoint join permission was rejected")

    doc.Regenerate()
    for pair in sorted(candidate_pairs):
        left, right, _ = candidate_pairs[pair]
        if _wall_pair_joined(doc, left, right):
            continue
        try:
            JoinGeometryUtils.JoinGeometry(doc, left, right)
        except Exception:
            # Revit 2020 often rejects solid JoinGeometry for walls that use
            # its native endpoint cleanup.  That host-specific exception is
            # acceptable only if the native join read-back succeeds below.
            pass
    doc.Regenerate()

    joined_pairs = set()
    for pair in sorted(candidate_pairs):
        left, right, _ = candidate_pairs[pair]
        if _wall_pair_joined(doc, left, right):
            joined_pairs.add(pair)

    junction_results = []
    for junction_id, kind, pairs_here in junction_pairs:
        joined_here = sum(1 for pair in pairs_here if pair in joined_pairs)
        if joined_here <= 0:
            raise Exception(
                "Revit did not persist a native or solid join for topology junction "
                + junction_id
            )
        junction_results.append((junction_id, kind, joined_here))

    # The approved topology list is the complete deterministic candidate set.
    # Do not sweep every wall pair here: Revit 2020's optional solid join
    # operation is expensive even for distant elements, and the all-pairs
    # sweep used to turn a 100-wall delivery into a multi-hour transaction.
    # Collinear continuations are healed before this stage; every remaining
    # physical L/T/X/Z connection is represented by a junction above.
    return joined_pairs, junction_results, snap_distances

level = None
created_walls = []
created_columns = []
created_beams = []
created_grids = []
wall_by_id = {}
deleted_walls = 0
deleted_grids = 0
deleted_columns = 0
deleted_beams = 0
grid_marker_fallbacks = 0
max_wall_xy_error_mm = 0.0
max_wall_thickness_error_mm = 0.0
max_column_xy_error_mm = 0.0
max_column_height_error_mm = 0.0
max_beam_base_elevation_error_mm = 0.0
ELEMENT_PARAMETER_WRITE_COUNT = 0
transaction = Transaction(doc, "BuildMate approved WallModel")
transaction.Start()
try:
    _configure_metric_units(doc)
    _bind_element_parameters(doc)
    doc.Regenerate()
    level = _level(doc)
    host_level_z = float(level.Elevation) * MM_PER_FOOT
    # The native Level is the Revit host, while the user-provided level range
    # is the physical delivery datum.  Keep these values separate: a project
    # may contain a B1 level at -4.5 m while the uploaded drawing explicitly
    # declares the modeled storey as -6.4~0 m.
    model_level_z = float(DATA.get("level_elevation_mm") or host_level_z)
    deleted_walls, deleted_grids, deleted_columns, deleted_beams = _delete_previous_build(doc)
    for grid_set in DATA.get("grids") or []:
        axis_lines = grid_set.get("axis_lines") or []
        labels_x = grid_set.get("x_axis_labels") or []
        labels_y = grid_set.get("y_axis_labels") or []
        x_values = [float(value) for value in (grid_set.get("x_axes") or [])]
        y_values = [float(value) for value in (grid_set.get("y_axes") or [])]
        if axis_lines:
            for index, item in enumerate(axis_lines):
                axis = str(item.get("axis") or "")
                if axis not in ("X", "Y"):
                    raise Exception("unsupported grid axis direction")
                grid = Grid.Create(doc, _line(item, model_level_z))
                grid.Name = _grid_name(item.get("label"), axis, index)
                if _mark_grid(grid, "BUILDMATE_AUTO_GRID:T=%s;P=%s;F=%s;M=%s;I=%s%d" % (
                        TENANT_ID, PROJECT_ID, FLOOR_CODE, WALL_MODEL_SHA256, axis, index + 1)):
                    grid_marker_fallbacks += 1
                created_grids.append(grid)
            continue
        x_min = (min(x_values) - 1000.0) if x_values else -1000.0
        x_max = (max(x_values) + 1000.0) if x_values else 1000.0
        y_min = (min(y_values) - 1000.0) if y_values else -1000.0
        y_max = (max(y_values) + 1000.0) if y_values else 1000.0
        for index, value in enumerate(x_values):
            item = {"start": [value, y_min, 0.0], "end": [value, y_max, 0.0]}
            grid = Grid.Create(doc, _line(item, model_level_z))
            grid.Name = _grid_name(labels_x[index] if index < len(labels_x) else None, "X", index)
            if _mark_grid(grid, "BUILDMATE_AUTO_GRID:T=%s;P=%s;F=%s;M=%s;I=X%d" % (
                    TENANT_ID, PROJECT_ID, FLOOR_CODE, WALL_MODEL_SHA256, index + 1)):
                grid_marker_fallbacks += 1
            created_grids.append(grid)
        for index, value in enumerate(y_values):
            item = {"start": [x_min, value, 0.0], "end": [x_max, value, 0.0]}
            grid = Grid.Create(doc, _line(item, model_level_z))
            grid.Name = _grid_name(labels_y[index] if index < len(labels_y) else None, "Y", index)
            if _mark_grid(grid, "BUILDMATE_AUTO_GRID:T=%s;P=%s;F=%s;M=%s;I=Y%d" % (
                    TENANT_ID, PROJECT_ID, FLOOR_CODE, WALL_MODEL_SHA256, index + 1)):
                grid_marker_fallbacks += 1
            created_grids.append(grid)
    if len(created_grids) != EXPECTED_GRID_COUNT:
        raise Exception("grid count mismatch: expected %d actual %d" % (EXPECTED_GRID_COUNT, len(created_grids)))
    print("BUILDMATE_GRID_MARKER_FALLBACK count=%d" % grid_marker_fallbacks)
    for item in DATA.get("elements") or []:
        if item.get("type") != "Wall":
            continue
        wall_id = str(item.get("id") or "")
        if not wall_id or wall_id in wall_by_id:
            raise Exception("duplicate or empty wall id: " + wall_id)
        # Wall.Create hosts the element on the native Level.  The signed base
        # offset below moves the physical wall to the requested datum without
        # changing the project's existing level object.
        line = _line(item, host_level_z)
        if line.Length <= 0.01:
            raise Exception("degenerate wall: " + wall_id)
        wall_role = str(
            (item.get("element_properties") or {}).get("structural_role")
            or "unresolved"
        ).strip().lower()
        if wall_role not in ("shear_wall", "architectural_wall", "unresolved"):
            raise Exception("unsupported wall structural role: " + wall_role)
        # Revit's final boolean is the structural-wall switch.  Do not infer
        # it from thickness or geometry: only an evidence-backed role may set
        # it.  Unknown roles remain non-structural and visible for review.
        wall = Wall.Create(doc, line, level.Id, wall_role == "shear_wall")
        # Wall.Create's structural flag is the authoritative switch, but a
        # few Revit 2020 templates expose the instance parameter separately
        # and otherwise display the wall as an ordinary Basic Wall.  Mirror
        # the same evidence-backed role when that parameter is available;
        # never infer it from thickness or geometry.
        structural_flag = wall.get_Parameter(
            BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT
        )
        if structural_flag is not None and not structural_flag.IsReadOnly:
            structural_flag.Set(1 if wall_role == "shear_wall" else 0)
        # Keep the first transaction an exact geometry gate.  Revit can
        # automatically trim/extend a newly-created wall as soon as its join
        # ends are enabled, which makes the source-coordinate read-back fail.
        # The following topology transaction restores both join ends before
        # joining, and does not reset curves after those joins.
        WallUtils.DisallowWallJoinAtEnd(wall, 0)
        WallUtils.DisallowWallJoinAtEnd(wall, 1)
        wall.Location.Curve = line
        _set_wall_base_offset(wall, model_level_z, host_level_z)
        wall.ChangeTypeId(_wall_type(
            doc,
            item.get("thickness"),
            item.get("wall_type_name"),
            item.get("material_name"),
        ).Id)
        height = float(item.get("height") or 0.0) / MM_PER_FOOT
        parameter = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)
        if parameter is None or parameter.IsReadOnly:
            raise Exception("wall height parameter is unavailable: " + wall_id)
        parameter.Set(height)
        _set_comments(wall, "BUILDMATE_AUTO:T=%s;P=%s;F=%s;M=%s;I=%s" % (
            TENANT_ID, PROJECT_ID, FLOOR_CODE, WALL_MODEL_SHA256, wall_id)
            + _element_property_suffix(item))
        _set_schedule_mark(wall, wall_id)
        _set_element_parameters(wall, item)
        created_walls.append((wall, item))
        wall_by_id[wall_id] = wall
    if len(created_walls) != EXPECTED_WALL_COUNT:
        raise Exception("wall count mismatch: expected %d actual %d" % (EXPECTED_WALL_COUNT, len(created_walls)))
    for item in DATA.get("elements") or []:
        if item.get("type") != "Column":
            continue
        column = _create_column(doc, item, model_level_z)
        created_columns.append((column, item))
    if len(created_columns) != EXPECTED_COLUMN_COUNT:
        raise Exception("column count mismatch: expected %d actual %d" % (EXPECTED_COLUMN_COUNT, len(created_columns)))
    # Keep the explicit level_z name in the generated script: it documents
    # that beam placement uses the approved model datum, not the host Level's
    # potentially different elevation.
    level_z = model_level_z
    for item in DATA.get("elements") or []:
        if item.get("type") != "Beam":
            continue
        beam = _create_beam(doc, item, level, level_z)
        created_beams.append((beam, item))
    if len(created_beams) != EXPECTED_BEAM_COUNT:
        raise Exception("beam count mismatch: expected %d actual %d" % (EXPECTED_BEAM_COUNT, len(created_beams)))
    # DirectShape geometry is finalized lazily by Revit 2020; regenerate once
    # before the deterministic bounding-box/read-back gate below.
    doc.Regenerate()
    for wall, item in created_walls:
        vertical_box = wall.get_BoundingBox(None)
        if vertical_box is None:
            raise Exception("wall vertical read-back unavailable: " + str(item.get("id")))
        expected_wall_base_z = model_level_z + float((item.get("start") or [0.0, 0.0, 0.0])[2])
        expected_wall_top_z = expected_wall_base_z + float(item.get("height") or 0.0)
        wall_base_error = abs(float(vertical_box.Min.Z) * MM_PER_FOOT - expected_wall_base_z)
        wall_top_error = abs(float(vertical_box.Max.Z) * MM_PER_FOOT - expected_wall_top_z)
        if wall_base_error > 5.0 or wall_top_error > 5.0:
            raise Exception(
                "wall vertical envelope mismatch: %s expected=[%.6f, %.6f]mm "
                "actual=[%.6f, %.6f]mm"
                % (
                    str(item.get("id")), expected_wall_base_z, expected_wall_top_z,
                    float(vertical_box.Min.Z) * MM_PER_FOOT,
                    float(vertical_box.Max.Z) * MM_PER_FOOT,
                )
            )
        curve = wall.Location.Curve
        actual_start = curve.GetEndPoint(0)
        actual_end = curve.GetEndPoint(1)
        expected_start = _project_xy(item["start"])
        expected_end = _project_xy(item["end"])
        direct_error = max(
            abs(actual_start.X * MM_PER_FOOT - expected_start[0]),
            abs(actual_start.Y * MM_PER_FOOT - expected_start[1]),
            abs(actual_end.X * MM_PER_FOOT - expected_end[0]),
            abs(actual_end.Y * MM_PER_FOOT - expected_end[1]),
        )
        reverse_error = max(
            abs(actual_start.X * MM_PER_FOOT - expected_end[0]),
            abs(actual_start.Y * MM_PER_FOOT - expected_end[1]),
            abs(actual_end.X * MM_PER_FOOT - expected_start[0]),
            abs(actual_end.Y * MM_PER_FOOT - expected_start[1]),
        )
        wall_xy_error = min(direct_error, reverse_error)
        max_wall_xy_error_mm = max(max_wall_xy_error_mm, wall_xy_error)
        if wall_xy_error > 5.0:
            raise Exception("wall geometry read-back mismatch: " + str(item.get("id")))
        expected_width = float(item.get("thickness") or 0.0)
        thickness_error = abs(
            float(wall.WallType.Width) * MM_PER_FOOT - expected_width
        )
        max_wall_thickness_error_mm = max(
            max_wall_thickness_error_mm, thickness_error
        )
        if expected_width > 0.0 and thickness_error > 5.0:
            raise Exception("wall thickness read-back mismatch: " + str(item.get("id")))
    for column, item in created_columns:
        box = column.get_BoundingBox(None)
        if box is None:
            raise Exception("column geometry read-back unavailable: " + str(item.get("id")))
        expected_height = float(item.get("height") or 0.0)
        expected_base_z = (
            float(item.get("base_z"))
            if item.get("base_z") is not None else model_level_z
        )
        expected_top_z = expected_base_z + expected_height
        column_base_error = abs(float(box.Min.Z) * MM_PER_FOOT - expected_base_z)
        column_top_error = abs(float(box.Max.Z) * MM_PER_FOOT - expected_top_z)
        if column_base_error > 5.0 or column_top_error > 5.0:
            raise Exception(
                "column vertical envelope mismatch: %s expected=[%.6f, %.6f]mm "
                "actual=[%.6f, %.6f]mm"
                % (
                    str(item.get("id")), expected_base_z, expected_top_z,
                    float(box.Min.Z) * MM_PER_FOOT,
                    float(box.Max.Z) * MM_PER_FOOT,
                )
            )
        actual_height = (float(box.Max.Z) - float(box.Min.Z)) * MM_PER_FOOT
        height_error = abs(actual_height - expected_height)
        max_column_height_error_mm = max(
            max_column_height_error_mm, height_error
        )
        if height_error > 5.0:
            raise Exception(
                "column height read-back mismatch: %s expected_mm=%.6f actual_mm=%.6f"
                % (str(item.get("id")), expected_height, actual_height)
            )
        center = item.get("center") or []
        profile = item.get("profile") or []
        expected_points = [
            _project_xy((
                float(center[0]) + float(point[0]),
                float(center[1]) + float(point[1]),
            ))
            for point in profile
        ]
        expected_bounds = (
            min(point[0] for point in expected_points),
            min(point[1] for point in expected_points),
            max(point[0] for point in expected_points),
            max(point[1] for point in expected_points),
        )
        actual_bounds = (
            float(box.Min.X) * MM_PER_FOOT,
            float(box.Min.Y) * MM_PER_FOOT,
            float(box.Max.X) * MM_PER_FOOT,
            float(box.Max.Y) * MM_PER_FOOT,
        )
        column_xy_error = max(
            abs(actual_bounds[index] - expected_bounds[index])
            for index in range(4)
        )
        max_column_xy_error_mm = max(
            max_column_xy_error_mm, column_xy_error
        )
        if column_xy_error > 5.0:
            raise Exception("column footprint read-back mismatch: " + str(item.get("id")))
    for beam, item in created_beams:
        curve = getattr(beam.Location, "Curve", None)
        if curve is None or curve.Length <= 0.01:
            raise Exception("beam geometry read-back unavailable: " + str(item.get("id")))
        expected_base_z, expected_top_z = _beam_vertical_target(item, model_level_z)
        box = beam.get_BoundingBox(None)
        if box is None:
            raise Exception("beam vertical read-back unavailable: " + str(item.get("id")))
        actual_base_z = float(box.Min.Z) * MM_PER_FOOT
        actual_top_z = float(box.Max.Z) * MM_PER_FOOT
        elevation_error = max(
            abs(actual_base_z - expected_base_z),
            abs(actual_top_z - expected_top_z),
        )
        max_beam_base_elevation_error_mm = max(
            max_beam_base_elevation_error_mm, elevation_error
        )
        if elevation_error > 5.0:
            raise Exception(
                "beam elevation read-back mismatch: %s expected=[%.6f, %.6f]mm "
                "actual=[%.6f, %.6f]mm"
                % (
                    str(item.get("id")), expected_base_z, expected_top_z,
                    actual_base_z, actual_top_z,
                )
            )
        _verify_element_properties(beam, str(item.get("id") or ""))
    print("BUILDMATE_GEOMETRY_READBACK wall_xy_mm=%.6f wall_thickness_mm=%.6f column_xy_mm=%.6f column_height_mm=%.6f" % (
        max_wall_xy_error_mm,
        max_wall_thickness_error_mm,
        max_column_xy_error_mm,
        max_column_height_error_mm,
    ))
    print("BUILDMATE_BEAM_ELEVATION_READBACK max_base_z_mm=%.6f" % (
        max_beam_base_elevation_error_mm,
    ))
    wall_properties = {}
    for wall, item in created_walls:
        wall_id = str(item.get("id") or "")
        wall_properties[wall_id] = _verify_element_properties(wall, wall_id)
    for column, item in created_columns:
        _verify_element_properties(column, str(item.get("id") or ""))
    for beam, item in created_beams:
        _verify_element_properties(beam, str(item.get("id") or ""))
    matched_openings = []
    opening_hosts = set()
    opening_marks = []
    for opening in DATA.get("openings") or []:
        if str(opening.get("status") or "") != "matched":
            continue
        opening_id = str(opening.get("id") or "")
        mark = str(opening.get("mark") or "")
        hosts = [str(value) for value in (opening.get("host_wall_ids") or [])]
        if not opening_id or not mark or not hosts:
            raise Exception("matched opening semantics are incomplete")
        for host_id in hosts:
            if host_id not in wall_by_id or host_id not in wall_properties:
                raise Exception("opening references an unavailable Revit wall host: " + host_id)
            host_marks = [
                value for value in _metadata_field(
                    wall_properties[host_id], "OPENINGS"
                ).split(",") if value
            ]
            if mark not in host_marks:
                raise Exception("opening mark was not persisted on its Revit wall host: " + mark)
            opening_hosts.add(host_id)
        matched_openings.append(opening_id)
        if mark not in opening_marks:
            opening_marks.append(mark)
    print("BUILDMATE_ELEMENT_PROPERTIES_APPLIED elements=%d walls=%d columns=%d beams=%d named_parameters=%d" % (
        len(created_walls) + len(created_columns) + len(created_beams),
        len(created_walls),
        len(created_columns),
        len(created_beams),
        ELEMENT_PARAMETER_WRITE_COUNT,
    ))
    # Retain the legacy marker for older Bridge/service readers and approval
    # snapshots.  It describes the same per-element properties; no Revit
    # schedule view is created by this workflow.
    print("BUILDMATE_SCHEDULE_DATA_APPLIED elements=%d walls=%d columns=%d beams=%d" % (
        len(created_walls) + len(created_columns) + len(created_beams),
        len(created_walls),
        len(created_columns),
        len(created_beams),
    ))
    print("BUILDMATE_OPENING_SEMANTICS_APPLIED openings=%d matched=%d hosts=%d marks=%s" % (
        len(DATA.get("openings") or []),
        len(matched_openings),
        len(opening_hosts),
        ",".join(sorted(opening_marks)),
    ))
    transaction.Commit()
except Exception:
    try:
        transaction.RollBack()
    except Exception:
        pass
    raise

# Revit 2020 can reject a standalone ``Document.Regenerate`` immediately
# after the first transaction has committed (the document is briefly
# considered non-modifiable while the external command is returning).  The
# topology transaction below regenerates after it starts, which is the only
# regeneration needed before joining the walls.
topology_transaction = Transaction(doc, "BuildMate Wall Topology")
topology_transaction.Start()
try:
    doc.Regenerate()
    print("BUILDMATE_TOPOLOGY_START walls=%d" % len(created_walls))
    joined_pairs, junction_results, topology_snap_distances = _apply_topology(
        doc, wall_by_id
    )
    doc.Regenerate()
    topology_transaction.Commit()
except Exception:
    try:
        topology_transaction.RollBack()
    except Exception:
        pass
    raise

def _create_opening_cuts(doc, wall_by_id):
    created = []
    own_marker_count = 0
    for item in DATA.get("openings") or []:
        if item.get("cut_status") != "ready":
            continue
        hosts = item.get("host_wall_ids") or []
        if len(hosts) != 1 or hosts[0] not in wall_by_id:
            raise Exception("opening cut requires one existing wall host")
        host = wall_by_id[hosts[0]]
        start, end = _project_xy(item["cut_start"]), _project_xy(item["cut_end"])
        bottom, top = float(item["base_elevation"]), float(item["top_elevation"])
        if top <= bottom:
            raise Exception("opening height must be positive: " + str(item["id"]))
        # Wall overload, not a generic void/DirectShape.  Z is already the
        # project datum and must NOT have the host level added a second time.
        opening = doc.Create.NewOpening(
            host, XYZ(start[0] / MM_PER_FOOT, start[1] / MM_PER_FOOT, bottom / MM_PER_FOOT),
            XYZ(end[0] / MM_PER_FOOT, end[1] / MM_PER_FOOT, top / MM_PER_FOOT))
        if opening is None:
            raise Exception("Revit returned no physical opening: " + str(item["id"]))
        marker_storage = _set_opening_marker(opening, item)
        if marker_storage != "host_and_receipt":
            own_marker_count += 1
        created.append((opening, item))
    doc.Regenerate()
    for opening, item in created:
        points = list(opening.BoundaryRect)
        if len(points) != 2 or opening.Host.Id != wall_by_id[item["host_wall_ids"][0]].Id:
            raise Exception("opening host/boundary read-back failed: " + str(item["id"]))
        actual_width = math.hypot(points[1].X - points[0].X, points[1].Y - points[0].Y) * MM_PER_FOOT
        actual_bottom = min(point.Z for point in points) * MM_PER_FOOT
        actual_top = max(point.Z for point in points) * MM_PER_FOOT
        expected_start, expected_end = _project_xy(item["cut_start"]), _project_xy(item["cut_end"])
        actual_center = [(points[0].X + points[1].X) * MM_PER_FOOT / 2,
                         (points[0].Y + points[1].Y) * MM_PER_FOOT / 2]
        if (abs(actual_width - float(item["cut_width"])) > 1
                or abs(actual_bottom - float(item["base_elevation"])) > 1
                or abs(actual_top - float(item["top_elevation"])) > 1
                or math.hypot(actual_center[0] - (expected_start[0] + expected_end[0]) / 2,
                              actual_center[1] - (expected_start[1] + expected_end[1]) / 2) > 1):
            raise Exception("opening cut position/size read-back mismatch: " + str(item["id"]))
    return created, own_marker_count


opening_transaction = Transaction(doc, "BuildMate physical wall openings")
opening_transaction.Start()
try:
    created_openings, opening_own_marker_count = _create_opening_cuts(doc, wall_by_id)
    opening_transaction.Commit()
except Exception:
    opening_transaction.RollBack()
    raise
print("BUILDMATE_OPENING_CUTS_APPLIED count=%d ids=%s" % (
    len(created_openings), ",".join(str(opening.Id.IntegerValue) for opening, item in created_openings)))
print("BUILDMATE_OPENING_MARKERS_APPLIED own=%d host_receipt=%d" % (
    opening_own_marker_count, len(created_openings) - opening_own_marker_count))

view = None
view_name = "BM-ACTUAL-" + FLOOR_CODE
for candidate in FilteredElementCollector(doc).OfClass(ViewPlan):
    if candidate.IsTemplate:
        continue
    if _name(candidate) != view_name:
        continue
    # A floor-plan name is not a sufficient identity: users may have copied
    # a BuildMate view to another level or a template may contain duplicate
    # names.  Reusing the wrong view would produce a convincing but unrelated
    # PNG and make the independent overlay audit meaningless.
    try:
        candidate_level = candidate.GenLevel
        if candidate_level is None or candidate_level.Id != level.Id:
            continue
    except Exception:
        continue
    view = candidate
    break
if view is None:
    view_type = None
    for candidate in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if candidate.ViewFamily == ViewFamily.FloorPlan:
            view_type = candidate
            break
    if view_type is None:
        raise Exception("no floor-plan view type for actual audit")
    view_transaction = Transaction(doc, "BuildMate actual audit view")
    view_transaction.Start()
    try:
        view = ViewPlan.Create(doc, view_type.Id, level.Id)
        view.Name = view_name
        view_transaction.Commit()
    except Exception:
        try:
            view_transaction.RollBack()
        except Exception:
            pass
        raise

# A newly-created Revit floor plan has a host/template-dependent crop (and in
# the Revit 2020 sample template it can be effectively unbounded).  Exporting
# that view with FitToPage then produces a page-sized frame in which the
# generated walls occupy only a few pixels.  The audit image must be framed by
# the source geometry frame, not by a template default or a generated-model
# extent.  The source bounds are computed from immutable source entities and
# carried through WallModel; using them here prevents missing generated walls
# from being cropped out of the independent comparison.  The generated-bbox
# fallback is retained only for legacy hand-authored payloads that predate the
# source-bounds field.
audit_grid_bubbles = []

def _actual_view_crop(view, level, walls, columns, beams, grids, openings):
    bounds = [None, None, None, None]

    def include_box(box):
        # BoundingBoxXYZ values are in the document's model coordinates.  A
        # wall's model-space box includes its actual type width, which is what
        # the source-side wall-face comparison needs.
        if box is None:
            return
        try:
            left = float(box.Min.X)
            bottom = float(box.Min.Y)
            right = float(box.Max.X)
            top = float(box.Max.Y)
        except Exception:
            return
        if bounds[0] is None:
            bounds[0], bounds[1], bounds[2], bounds[3] = left, bottom, right, top
        else:
            bounds[0] = min(bounds[0], left)
            bounds[1] = min(bounds[1], bottom)
            bounds[2] = max(bounds[2], right)
            bounds[3] = max(bounds[3], top)

    source_bounds_m = (DATA.get("coordinate_system") or {}).get("source_bounds_m")
    if isinstance(source_bounds_m, (list, tuple)) and len(source_bounds_m) == 4:
        # WallModel coordinates are metres in the project/source frame.  Revit
        # creates each wall through ``_project_xy`` (which applies the active
        # Project Base Point/Survey Point offset and angle), so the source
        # frame must pass through that same rigid transform before becoming a
        # document-space crop.  Transform all four corners and then take an
        # axis-aligned envelope; transforming only the min/max pair would
        # under-bound a rotated frame.
        source_bounds_mm = [float(value) * 1000.0 for value in source_bounds_m]
        source_corners_mm = [
            (source_bounds_mm[0], source_bounds_mm[1]),
            (source_bounds_mm[0], source_bounds_mm[3]),
            (source_bounds_mm[2], source_bounds_mm[1]),
            (source_bounds_mm[2], source_bounds_mm[3]),
        ]
        projected_corners_mm = [_project_xy(point) for point in source_corners_mm]
        source_bounds_ft = [
            min(float(point[0]) for point in projected_corners_mm) / MM_PER_FOOT,
            min(float(point[1]) for point in projected_corners_mm) / MM_PER_FOOT,
            max(float(point[0]) for point in projected_corners_mm) / MM_PER_FOOT,
            max(float(point[1]) for point in projected_corners_mm) / MM_PER_FOOT,
        ]
        if (
            all(_finite(value) for value in source_bounds_ft)
            and source_bounds_ft[2] > source_bounds_ft[0]
            and source_bounds_ft[3] > source_bounds_ft[1]
        ):
            bounds = source_bounds_ft

    if bounds[0] is None:
        for wall, item in walls:
            include_box(wall.get_BoundingBox(None))
        for column, item in columns:
            include_box(column.get_BoundingBox(None))
        for beam, item in beams:
            include_box(beam.get_BoundingBox(None))
        for opening, item in openings:
            include_box(opening.get_BoundingBox(None))
        for grid in grids:
            include_box(grid.get_BoundingBox(None))
    min_x, min_y, max_x, max_y = bounds
    if min_x is None or min_y is None or max_x is None or max_y is None:
        raise Exception("cannot derive actual audit view crop from generated geometry")
    if max_x <= min_x or max_y <= min_y:
        raise Exception("generated geometry has an invalid actual audit view crop")

    span_x = max_x - min_x
    span_y = max_y - min_y

    # Keep the Revit raster in the same deterministic frame as the independent
    # source renderer.  The source renderer maps the observed source bounds to
    # a 2400x1600 canvas with a 40-pixel left/bottom margin and chooses the
    # smaller axis scale.  Reconstructing that *frame geometry* from the
    # generated Revit bounding boxes is not reconstructing source pixels: it
    # simply makes both independent renderers use the same engineering
    # coordinate-to-page transform.  In particular, the top/right margins may
    # be larger when the source aspect ratio leaves letterbox space; a
    # symmetric crop would shift every wall by tens of pixels.
    canvas_width = 2400.0
    canvas_height = 1600.0
    source_margin_px = 40.0
    source_scale = min(
        (canvas_width - 2.0 * source_margin_px) / max(span_x, 1.0e-9),
        (canvas_height - 2.0 * source_margin_px) / max(span_y, 1.0e-9),
    )
    if source_scale <= 0.0:
        raise Exception("generated geometry has an invalid actual audit scale")
    crop_width = canvas_width / source_scale
    crop_height = canvas_height / source_scale
    min_x = min_x - source_margin_px / source_scale
    min_y = min_y - source_margin_px / source_scale
    max_x = min_x + crop_width
    max_y = min_y + crop_height

    crop = BoundingBoxXYZ()
    # Floor-plan crop boxes use model X/Y and tolerate a shallow Z range.  The
    # Z range is deliberately wider than the cut plane so Revit 2020 does not
    # clip the wall solids before rasterization.
    level_z = float(level.Elevation)
    crop.Min = XYZ(min_x, min_y, level_z - 10.0)
    crop.Max = XYZ(max_x, max_y, level_z + 20.0)
    view_transaction = Transaction(doc, "BuildMate configure actual audit view")
    view_transaction.Start()
    try:
        view.CropBoxActive = True
        view.CropBox = crop
        try:
            view.CropBoxVisible = False
        except Exception:
            pass
        try:
            view.DisplayStyle = DisplayStyle.HLR
        except Exception:
            pass
        try:
            view.DetailLevel = ViewDetailLevel.Coarse
        except Exception:
            pass
        # Templates may hide structural categories in their default floor
        # plan.  Temporary element isolation does not override a category
        # visibility switch, so explicitly show the categories produced by
        # this delivery before exporting and before handing the view to the
        # operator.  Unsupported/non-hideable categories are harmless.
        for category_name in (
            "OST_Walls", "OST_StructuralColumns", "OST_StructuralFraming",
            "OST_Grids",
        ):
            category = getattr(BuiltInCategory, category_name, None)
            if category is None:
                continue
            try:
                view.SetCategoryHidden(ElementId(category), False)
            except Exception:
                pass
        # A Revit floor plan's default cut plane is often below the physical
        # sill/head range of a wall opening (especially for a basement range
        # such as -6.4~0m).  In that case the opening is real but looks like a
        # continuous wall in plan.  Pick a deterministic midpoint from the
        # approved cuts so the operator-facing plan intersects their voids;
        # the independent source/Revit comparison still uses this same actual
        # view and therefore measures what is delivered.
        opening_midpoints_mm = []
        for opening, item in openings:
            try:
                if str(item.get("cut_status") or "") != "ready":
                    continue
                bottom = float(item.get("base_elevation") or 0.0)
                top = float(item.get("top_elevation") or 0.0)
                if top > bottom:
                    opening_midpoints_mm.append((bottom + top) / 2.0)
            except Exception:
                continue
        if opening_midpoints_mm:
            try:
                opening_midpoints_mm.sort()
                cut_z_mm = opening_midpoints_mm[len(opening_midpoints_mm) // 2]
                view_range = view.GetViewRange()
                view_range.SetOffset(
                    PlanViewPlane.CutPlane,
                    (cut_z_mm / MM_PER_FOOT) - float(level.Elevation),
                )
                view.SetViewRange(view_range)
                print("BUILDMATE_OPENING_CUT_PLANE z_mm=%.3f count=%d" % (
                    cut_z_mm, len(opening_midpoints_mm)))
            except Exception as exc:
                # A template may expose a non-editable view range.  Physical
                # cuts and their read-back remain valid; keep delivery alive
                # and let the 3D view show the complete vertical result.
                print("BUILDMATE_OPENING_CUT_PLANE_SKIPPED " + str(exc))
        # A target model may already contain walls, levels, or imported CAD.
        # Isolate only the elements created by this delivery so unrelated
        # geometry cannot pollute the independent Revit raster.  Temporary
        # isolation is a view state, not a model edit, and is therefore safe
        # to apply in the same transaction as the crop configuration.
        scope_ids = List[ElementId]()
        for wall, _ in walls:
            scope_ids.Add(wall.Id)
        for column, _ in columns:
            scope_ids.Add(column.Id)
        for beam, _ in beams:
            scope_ids.Add(beam.Id)
        for opening, _ in openings:
            scope_ids.Add(opening.Id)
        for grid in grids:
            scope_ids.Add(grid.Id)
        if scope_ids.Count <= 0:
            raise Exception("actual audit view has no generated elements to isolate")
        try:
            if view.IsInTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate):
                view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
        except Exception:
            pass
        view.IsolateElementsTemporary(scope_ids)
        if not view.IsInTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate):
            raise Exception("Revit did not activate temporary audit-view isolation")
        # Grid heads are useful in the delivered RVT but they are annotations,
        # not source geometry.  The independent source raster intentionally
        # contains axis lines without Revit-generated BM-* bubble text.  Hide
        # only the two grid bubbles during export, retain the grid lines, and
        # restore the bubbles immediately after the audit image is written.
        for grid in grids:
            for datum_end in (DatumEnds.End0, DatumEnds.End1):
                if grid.IsBubbleVisibleInView(datum_end, view):
                    grid.HideBubbleInView(datum_end, view)
                    audit_grid_bubbles.append((grid, datum_end))
        # Use the thinnest available projection/cut line for the audit view.
        # This keeps the independent Revit raster from turning a CAD hairline
        # into a multi-pixel stroke; geometry itself is unchanged and the raw
        # raster IoU remains reported alongside the normalized score.
        line_overrides = OverrideGraphicSettings()
        try:
            line_overrides.SetProjectionLineWeight(1)
        except Exception:
            pass
        try:
            line_overrides.SetCutLineWeight(1)
        except Exception:
            pass
        for element_id in scope_ids:
            try:
                view.SetElementOverrides(element_id, line_overrides)
            except Exception:
                pass
        starting_view = StartingViewSettings.GetStartingViewSettings(doc)
        if not starting_view.IsAcceptableStartingView(view.Id):
            raise Exception("BuildMate delivery view cannot be used as the RVT starting view")
        starting_view.ViewId = view.Id
        view_transaction.Commit()
    except Exception:
        try:
            view_transaction.RollBack()
        except Exception:
            pass
        raise
    print("BUILDMATE_ACTUAL_VIEW_CROP min_x_ft=%.6f min_y_ft=%.6f max_x_ft=%.6f max_y_ft=%.6f scale_px_per_ft=%.6f" % (
        min_x, min_y, max_x, max_y, source_scale))

_actual_view_crop(
    view, level, created_walls, created_columns, created_beams, created_grids,
    created_openings,
)
print("BUILDMATE_AUDIT_GRID_BUBBLES_HIDDEN count=%d" % len(audit_grid_bubbles))

# Persist before rendering so the PNG corresponds to the saved model state.
doc.Save()
print("BUILDMATE_PERSISTED_BEFORE_RENDER")
options = ImageExportOptions()
options.ExportRange = ExportRange.SetOfViews
view_ids = List[ElementId]()
view_ids.Add(view.Id)
options.SetViewsAndSheets(view_ids)
options.FilePath = ACTUAL_PREFIX
options.HLRandWFViewsFileType = ImageFileType.PNG
options.ImageResolution = ImageResolution.DPI_150
options.ZoomType = ZoomFitType.FitToPage
options.PixelSize = 2400
doc.ExportImage(options)
print("BUILDMATE_WALL_TOPOLOGY_APPLIED junctions=%d joined_pairs=%d kinds=%s" % (
    len(DATA.get("junctions") or []), len(joined_pairs),
    ",".join(sorted(set([str(item.get("kind") or "") for item in (DATA.get("junctions") or [])])))))
print("BUILDMATE_WALL_TOPOLOGY_VERIFIED joined_pairs=%d snapped_endpoints=%d max_snap_mm=%.6f" % (
    len(joined_pairs), len(topology_snap_distances),
    max([value * MM_PER_FOOT for value in topology_snap_distances] or [0.0])))
# Leave the delivered RVT on the generated, permanently cropped floor plan
# instead of the template's empty 3D/Level 1 view.  Temporary isolation and
# hidden grid bubbles are audit-only view state; restore the operator-facing
# view in a transaction after the independent PNG has already been exported.
def _apply_delivery_gray(doc, elements):
    solid = next((pattern for pattern in FilteredElementCollector(doc).OfClass(FillPatternElement)
                  if pattern.GetFillPattern().IsSolidFill), None)
    if solid is None:
        raise Exception("delivery gray requires a solid fill pattern")
    settings = OverrideGraphicSettings()
    settings.SetProjectionLineColor(Color(100, 100, 100))
    settings.SetCutLineColor(Color(100, 100, 100))
    settings.SetSurfaceForegroundPatternId(solid.Id)
    settings.SetSurfaceForegroundPatternColor(Color(180, 180, 180))
    settings.SetSurfaceForegroundPatternVisible(True)
    settings.SetCutForegroundPatternId(solid.Id)
    settings.SetCutForegroundPatternColor(Color(180, 180, 180))
    settings.SetCutForegroundPatternVisible(True)
    settings.SetSurfaceTransparency(0)
    # Keep plan views as black linework without a surface fill.
    plan_settings = OverrideGraphicSettings()
    plan_settings.SetProjectionLineColor(Color(0, 0, 0))
    plan_settings.SetCutLineColor(Color(0, 0, 0))
    plan_settings.SetSurfaceForegroundPatternVisible(False)
    plan_settings.SetSurfaceBackgroundPatternVisible(False)
    plan_settings.SetCutForegroundPatternVisible(False)
    plan_settings.SetCutBackgroundPatternVisible(False)
    count = 0
    for candidate in FilteredElementCollector(doc).OfClass(View):
        if candidate.IsTemplate or not isinstance(candidate, (ViewPlan, View3D)):
            continue
        view_settings = plan_settings if isinstance(candidate, ViewPlan) else settings
        if isinstance(candidate, ViewPlan):
            try:
                candidate.ViewTemplateId = ElementId.InvalidElementId
            except Exception:
                pass
            try:
                candidate.DisplayStyle = DisplayStyle.Wireframe
            except Exception:
                pass
            for category_name in (
                "OST_Walls", "OST_StructuralColumns", "OST_StructuralFraming",
                "OST_Grids",
            ):
                category = getattr(globals().get("BuiltInCategory"), category_name, None)
                if category is None:
                    continue
                try:
                    candidate.SetCategoryHidden(ElementId(category), False)
                except Exception:
                    pass
        for element, item in elements:
            candidate.SetElementOverrides(element.Id, view_settings)
        count += 1
    return count


def _delivery_3d_view(doc, elements):
    name = "BM-3D-" + FLOOR_CODE
    target = next((candidate for candidate in FilteredElementCollector(doc).OfClass(View3D)
                   if not candidate.IsTemplate and _name(candidate) == name), None)
    if target is None:
        family_type = next((item for item in FilteredElementCollector(doc).OfClass(ViewFamilyType)
                            if item.ViewFamily == ViewFamily.ThreeDimensional), None)
        if family_type is None:
            raise Exception("Revit template has no three-dimensional view family")
        target = View3D.CreateIsometric(doc, family_type.Id)
        target.Name = name
    target.ViewTemplateId = ElementId.InvalidElementId
    target.IsSectionBoxActive = False
    target.DisplayStyle = DisplayStyle.ShadingWithEdges
    for category_name in (
        "OST_Walls", "OST_StructuralColumns", "OST_StructuralFraming",
        "OST_Grids",
    ):
        category = getattr(BuiltInCategory, category_name, None)
        if category is None:
            continue
        try:
            target.SetCategoryHidden(ElementId(category), False)
        except Exception:
            pass
    ids = List[ElementId]()
    for element, item in elements:
        ids.Add(element.Id)
    if ids.Count:
        target.IsolateElementsTemporary(ids)
        target.ConvertTemporaryHideIsolateToPermanent()
    return target


delivery_view_transaction = Transaction(doc, "BuildMate restore delivery view")
delivery_view_transaction.Start()
try:
    if view.IsInTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate):
        view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
    for grid, datum_end in audit_grid_bubbles:
        grid.ShowBubbleInView(datum_end, view)
    # Style only operator-facing views AFTER the independent raster export.
    # Changing presentation must never increase the audit similarity score.
    # Keep one complete presentation set. Openings are native hosted
    # elements and irregular columns are DirectShapes; both must be included
    # in the final operator-facing views and gray overrides.
    delivery_elements = (
        created_walls + created_columns + created_beams + created_openings
    )
    delivery_3d = _delivery_3d_view(doc, delivery_elements)
    gray_view_count = _apply_delivery_gray(doc, delivery_elements)
    doc.Regenerate()
    delivery_view_transaction.Commit()
except Exception:
    try:
        delivery_view_transaction.RollBack()
    except Exception:
        pass
    raise
print("BUILDMATE_DELIVERY_GRID_BUBBLES_RESTORED count=%d" % len(audit_grid_bubbles))
print("BUILDMATE_DELIVERY_GRAY_APPLIED views=%d" % gray_view_count)
print("BUILDMATE_DELIVERY_3D_VIEW name=%s" % delivery_3d.Name)
try:
    uidoc.ActiveView = view
    print("BUILDMATE_DELIVERY_VIEW_ACTIVE name=%s" % view.Name)
except Exception as exc:
    print("BUILDMATE_DELIVERY_VIEW_ACTIVE_SKIPPED " + str(exc))
doc.Save()
print("BUILDMATE_DELIVERY_VIEW_PERSISTED")
print("BUILDMATE_WALL_TOPOLOGY_REFS refs=%s" % (
    ",".join(["%s:%s:%d" % item for item in junction_results])))
print("BUILDMATE_WALL_MODEL_APPLIED walls=%d grids=%d element_ids=%s" % (
    len(created_walls), len(created_grids),
    ",".join([str(wall.Id.IntegerValue) for wall, item in created_walls])))
print("BUILDMATE_COLUMN_MODEL_APPLIED columns=%d element_ids=%s" % (
    len(created_columns),
    ",".join([str(column.Id.IntegerValue) for column, item in created_columns])))
print("BUILDMATE_BEAM_MODEL_APPLIED beams=%d element_ids=%s" % (
    len(created_beams),
    ",".join([str(beam.Id.IntegerValue) for beam, item in created_beams])))
'''
    replacements = {
        "__DATA__": literal,
        "__PROJECT_ID__": repr(payload["project_id"]),
        "__TENANT_ID__": repr(payload["tenant_id"]),
        "__FLOOR_CODE__": repr(payload["floor_code"]),
        "__BUILD_ID__": repr(payload["build_id"]),
        "__WALL_MODEL_SHA256__": repr(payload["wall_model_sha256"]),
        "__ACTUAL_PREFIX__": repr(prefix),
        "__EXPECTED_WALL_COUNT__": str(expected_wall_count),
        "__EXPECTED_COLUMN_COUNT__": str(expected_column_count),
        "__EXPECTED_BEAM_COUNT__": str(expected_beam_count),
        "__EXPECTED_GRID_COUNT__": str(grid_count),
    }
    return re.sub(
        r"__(?:DATA|PROJECT_ID|TENANT_ID|FLOOR_CODE|BUILD_ID|"
        r"WALL_MODEL_SHA256|ACTUAL_PREFIX|EXPECTED_WALL_COUNT|"
        r"EXPECTED_COLUMN_COUNT|"
        r"EXPECTED_BEAM_COUNT|"
        r"EXPECTED_GRID_COUNT)__",
        lambda match: replacements[match.group(0)],
        template,
    )


__all__ = [
    "ELEMENT_PARAMETER_FIELDS",
    "SHARED_PARAMETER_FILE_NAME",
    "shared_parameter_file_content",
    "WALL_COMPILER_VERSION",
    "compiled_plan_sha256",
    "compile_wall_model_script",
]
