from backend.engines.wall_pipeline.contracts import EvidenceSourceRef, ManifestSource
from backend.engines.wall_pipeline.geometry import (
    _wall_specification_roles,
    _wall_structural_role,
)
from workers.revit_bridge.wall_compiler import _runtime_element_properties


def _source(*, role="plan", path="plan.pdf"):
    return ManifestSource(
        source_file_id="source_0001",
        path=path,
        role=role,
        media_type="application/pdf",
        sha256="0" * 64,
        size_bytes=1,
    )


def _ref(layer):
    return EvidenceSourceRef(
        source_file_id="source_0001",
        entity_id="entity_0001",
        locator="page:1/entity:1",
        layer=layer,
    )


def test_s_wall_layer_is_structural_even_on_generic_plan_role():
    assert _wall_structural_role([_ref("xref|S-WALL")], [_source()]) == "shear_wall"


def test_a_wall_layer_is_architectural_by_default():
    assert _wall_structural_role([_ref("A-WALL")], [_source()]) == "architectural_wall"


def test_ambiguous_wall_suffix_is_not_resolved_from_sheet_discipline():
    assert _wall_structural_role(
        [_ref("A-WALL-S")],
        [_source(role="structural_supplement", path="structure-plan.pdf")],
    ) == "unresolved"
    assert _wall_structural_role([_ref("A-WALL-S")], [_source()]) == "unresolved"


def test_mixed_source_layers_are_unresolved_for_human_review():
    refs = [_ref("S-WALL"), _ref("A-WALL")]
    assert _wall_structural_role(refs, [_source()]) == "unresolved"


def test_custom_geometry_layer_is_not_silently_called_shear_wall():
    assert _wall_structural_role([_ref("GEOMETRY-WALL")], [_source()]) == "unresolved"


def test_flattened_structural_sheet_without_layers_remains_unresolved():
    assert _wall_structural_role(
        [_ref("")],
        [_source(role="plan", path="S-G50-01-墙柱配筋平面图.pdf")],
    ) == "unresolved"


def test_generic_wall_layer_does_not_prove_architectural_type():
    assert _wall_structural_role([_ref("WALL")], [_source()]) == "unresolved"


def test_wall_specific_structural_marks_are_not_inferred_from_column_marks():
    for mark in ("Q1", "Q8", "SW3"):
        assert _wall_specification_roles(mark, "墙厚400") == {"shear_wall"}
    for mark in (None, "GBZ7", "KZ1"):
        assert _wall_specification_roles(mark, "墙厚400") == set()


def test_explicit_architectural_and_structural_annotations_resolve_type():
    for text in ("建筑墙厚400", "后砌墙厚200", "200厚砌块填充墙"):
        assert _wall_specification_roles(None, text) == {"architectural_wall"}
    assert _wall_specification_roles(None, "剪力墙厚400") == {"shear_wall"}
    assert _wall_specification_roles(None, "墙厚400") == set()
    assert _wall_specification_roles("Q1", "后砌填充墙") == {
        "shear_wall", "architectural_wall",
    }


def test_explicit_nested_layer_names_remain_authoritative():
    source = _source(role="structural_supplement", path="structure-plan.pdf")
    assert _wall_structural_role([_ref("plan$0$S-WALL")], [source]) == "shear_wall"
    assert _wall_structural_role([_ref("plan$0$A-后砌")], [source]) == "architectural_wall"


def test_wall_and_column_delivery_dimensions_are_not_equalized_at_a_junction():
    wall = _runtime_element_properties({
        "type": "Wall",
        "id": "wall-495",
        "thickness": 495.0,
        "height": 3000.0,
        "construction": {
            "type_name": "BM-WALL-GEOMETRY",
            "structural_role": "architectural_wall",
            "specification_mm": {"thickness_mm": 495.0},
        },
    })
    column = _runtime_element_properties({
        "type": "Column",
        "id": "column-500",
        "width": 500.0,
        "depth": 500.0,
        "construction": {
            "type_name": "BM-C-500x500mm",
            "structural_role": "unresolved",
            "specification_mm": {
                "width_mm": 500.0,
                "depth_mm": 500.0,
            },
        },
    })
    assert wall["thickness_mm"] == 495
    assert column["width_mm"] == 500
    assert column["depth_mm"] == 500
    assert wall["thickness_mm"] != column["width_mm"]


def test_runtime_specification_payload_uses_integer_drawing_dimensions():
    wall = _runtime_element_properties({
        "type": "Wall",
        "id": "wall-q1",
        "thickness": 495.306,
        "height": 3000.0,
        "construction": {
            "type_name": "BM-WALL-500mm",
            "specification_status": "resolved_from_annotation",
            "specification_mm": {"thickness_mm": 500.0},
        },
    })
    assert wall["thickness_mm"] == 500
    assert wall["specification_mm"] == {"thickness_mm": 500}
