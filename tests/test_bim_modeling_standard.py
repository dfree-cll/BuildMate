from pathlib import Path

from backend.engines.wall_pipeline.contracts import (
    BeamRecord,
    ColumnRecord,
    ConstructionInfo,
    EvidenceSourceRef,
    LevelConfig,
    ModelingStandardConfig,
    QuantityTakeoff,
    WallRecord,
)
from backend.engines.wall_pipeline.standards import validate_modeling_standard
from workers.revit_bridge.wall_compiler import (
    ELEMENT_PARAMETER_FIELDS,
    compile_wall_model_script,
)


def _source_ref() -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_file_id="source_0001",
        entity_id="entity_0001",
        locator="page:1/entity:1",
    )


def _standard() -> ModelingStandardConfig:
    return ModelingStandardConfig()


def test_standard_profile_contains_core_national_references():
    standard = _standard()
    assert standard.profile == "cn_gb_bim_delivery_v1"
    assert set(standard.references) >= {
        "GB/T 51212-2016",
        "GB/T 51269-2017",
        "GB/T 51301-2018",
        "GB/T 51235-2017",
    }
    assert standard.units == "m"
    assert standard.delivery_units == "mm"
    assert standard.naming_prefix == "BM"


def test_construction_info_carries_revit_family_identity():
    info = ConstructionInfo(
        category="Wall",
        type_name="BM-WALL-267mm",
        family_name="Basic Wall",
        family_type="BM-WALL-267mm",
        representation="system_family",
        classification_code="IfcWall",
    )
    assert info.family_name == "Basic Wall"
    assert info.family_type == "BM-WALL-267mm"
    assert info.representation == "system_family"


def test_standard_profile_drives_revit_instance_metadata():
    script = compile_wall_model_script(
        {
            "project": {"project_id": "p", "tenant_id": "t", "levels": [{"id": "l", "name": "L1", "elevation": 0}]},
            "build": {"floor_code": "L1", "build_id": "b", "wall_model_sha256": "h"},
            "coordinate_system": {"offset_policy": "local_origin", "source_frame": "project_north"},
            "modeling_standard": {
                "profile": "cn_gb_bim_delivery_v1",
                "classification_system": "GB/T 51269-2017",
                "units": "m",
                "coordinate_frame": "project_north",
            },
            "model_elements": [{
                "type": "Wall",
                "id": "wall_0001",
                "start": [0, 0, 0],
                "end": [1000, 0, 0],
                "thickness": 200,
                "height": 3000,
                "level": "L1",
                "construction": {
                    "category": "Wall",
                    "structural_role": "shear_wall",
                    "type_name": "BM-WALL-200mm",
                    "type_mark": "BM-W-200",
                    "classification_code": "IfcWall",
                    "classification_system": "GB/T 51269-2017",
                    "material_name": "C40",
                    "material_status": "provided",
                    "quantity_basis": "deterministic_geometry",
                },
                "quantities": {"length_m": 1, "footprint_area_m2": 0.2, "side_area_m2": 3, "gross_volume_m3": 0.6},
            }],
            "junctions": [],
            "openings": [],
        },
        actual_prefix=Path("buildmate-standard-test"),
        expected_wall_count=1,
        expected_column_count=0,
    )
    parameter_names = {name for name, _key in ELEMENT_PARAMETER_FIELDS}
    assert {"BM_ClassificationSystem", "BM_StandardProfile", "BM_Units", "BM_CoordinateFrame"} <= parameter_names
    assert {"BM_FamilyName", "BM_FamilyType", "BM_Representation"} <= parameter_names
    assert "BM_ClassificationSystem" in script
    assert "BM_WallRole" in script
    assert "wall_role == \"shear_wall\"" in script
    assert "cn_gb_bim_delivery_v1" in script
    assert "BUILDMATE_DISPLAY_UNITS_APPLIED length=mm" in script
    assert "BUILDMATE_INTEGER_DISPLAY_ACCURACY" not in script
    assert "DUT_MILLIMETERS" in script
    assert "BUILDMATE_AUDIT_GRID_BUBBLES_HIDDEN" in script
    assert "BUILDMATE_DELIVERY_GRID_BUBBLES_RESTORED" in script
    assert "grid.HideBubbleInView" in script
    assert "grid.ShowBubbleInView" in script
    assert "def _create_beam(doc, item, level, level_z):" in script
    assert "line = _line(item, level_z)" in script
    assert "beam = _create_beam(doc, item, level, level_z)" in script


def test_standard_validator_reports_missing_metadata_without_fabricating_it():
    level = LevelConfig(id="level-1", name="一层")
    ref = _source_ref()
    wall = WallRecord(
        wall_id="wall_0001",
        start_m=(0, 0),
        end_m=(1, 0),
        thickness_m=0.2,
        height_m=3,
        level_id="level-1",
        evidence_ids=["evidence-1"],
        source_refs=[ref],
        confidence=1,
        construction=None,
        quantities=None,
    )
    violations = validate_modeling_standard(
        standard=_standard(),
        units="m",
        coordinate_origin="source_origin",
        transform_chain=[{"operation": "identity"}],
        level=level,
        walls=[wall],
        columns=[],
    )
    assert {item.code for item in violations} == {"missing_construction_info", "missing_quantities"}


def test_standard_validator_covers_coupling_beam_metadata():
    level = LevelConfig(id="level-1", name="一层")
    ref = _source_ref()
    beam = BeamRecord(
        beam_id="beam_0001",
        start_m=(0, 0), end_m=(2, 0), width_m=0.2, depth_m=0.5,
        level_id="level-1", source_refs=[ref], confidence=1,
        construction=ConstructionInfo(
            category="Beam", type_name="BM-BEAM-LL1", type_mark="LL1",
            family_name="Concrete - Rectangular Beam",
            family_type="BM-BEAM-LL1", representation="loadable_family",
            classification_code="IfcBeam",
        ),
        quantities=QuantityTakeoff(
            length_m=2, footprint_area_m2=0.4, side_area_m2=1,
            gross_volume_m3=0.2,
        ),
    )
    violations = validate_modeling_standard(
        standard=_standard(), units="m", coordinate_origin="source_origin",
        transform_chain=[{"operation": "identity"}], level=level,
        walls=[], columns=[], beams=[beam],
    )
    assert violations == []
