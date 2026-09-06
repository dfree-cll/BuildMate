import copy
import hashlib
import json

import pytest

from backend.engines.same_floor_fusion import fuse_same_floor_review_ir
from scripts.fuse_same_floor_review import (
    _validate_output_scope,
    _verify_declared_source,
)


ARCHITECTURE_SHA = "a" * 64
STRUCTURE_SHA = "b" * 64


def _reference(sha256, identifier):
    return {
        "drawing_identity": f"sha256:{sha256}",
        "source_occurrence_id": f"source-{identifier}",
        "placed_entity_id": f"placed-{identifier}",
        "structural_occurrence_id": f"structural-{identifier}",
        "source_segment_id": f"segment-{identifier}",
        "entity_handle": f"HANDLE-{identifier}",
        "source_record_index": 1,
        "segment_index": 0,
        "source_interval_m": [0.0, 1.0],
        "placement_path": [{
            "insert_handle": "INSERT-01",
            "block_name": "B01",
            "cumulative_transform": {
                "coordinate_system": "DXF_WCS",
                "affine_2d": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
            },
        }],
        "identity_is_complete": True,
        "identity_limitations": [],
    }


def _column(sha256, identifier, center, *, mark=None, grid_ref=None):
    corners = [
        [center[0] - 0.4, center[1] - 0.4],
        [center[0] + 0.4, center[1] - 0.4],
        [center[0] + 0.4, center[1] + 0.4],
        [center[0] - 0.4, center[1] + 0.4],
    ]
    references = []
    for index in range(4):
        reference = _reference(sha256, f"column-{identifier}-{index}")
        reference.update({
            "selected_structural_occurrence_id": "selected-column-plan",
            "source_structural_occurrence_id": "source-column-profile",
            "source_occurrence_id": f"source-column-{identifier}",
            "placed_entity_id": f"placed-column-{identifier}",
            "source_entity_handle": f"COLUMN-HANDLE-{identifier}",
            "start_local_m": corners[index],
            "end_local_m": corners[(index + 1) % 4],
        })
        references.append(reference)
    profile = {
        "drawing_identity": f"sha256:{sha256}",
        "selected_structural_occurrence_id": "selected-column-plan",
        "source_structural_occurrence_id": "source-column-profile",
        "source_occurrence_id": f"source-column-{identifier}",
        "placed_entity_id": f"placed-column-{identifier}",
        "source_entity_handle": f"COLUMN-HANDLE-{identifier}",
        "entity_closed": True,
        "vertex_count": 4,
        "center_local_m": list(center),
        "size_local_m": [0.8, 0.8],
        "source_segment_ids": [
            reference["source_segment_id"] for reference in references],
        "placement_path": copy.deepcopy(references[0]["placement_path"]),
    }
    result = {
        "id": f"column-{identifier}",
        "center": list(center),
        "size": [0.8, 0.8],
        "grid_ref": grid_ref,
        "confidence": 0.9,
        "source_profile_ref": profile,
        "source_segment_refs": references,
    }
    if mark is not None:
        result["mark"] = mark
    return result


def _wall(sha256, identifier, start, end, *, thickness=200.0):
    reference = _reference(sha256, f"wall-{identifier}")
    return {
        "id": f"wall-{identifier}",
        "start": list(start),
        "end": list(end),
        "thickness": thickness,
        "confidence": 0.9,
        "geometry_source": "DXF_CLOSED_WALL_STRIP",
        "source_segment_ids": [reference["source_segment_id"]],
        "source_segment_refs": [reference],
        "derivation": [{
            "operation": "merge_collinear_walls",
            "parent_source_segment_ids": [reference["source_segment_id"]],
        }],
    }


def _traceability(sha256, path, geometry):
    def group(key, *, columns=False):
        elements = geometry[key]
        reference_count = sum(
            len(element.get("source_segment_refs") or [])
            for element in elements)
        result = {
            "status": "COMPLETE" if elements else "NOT_APPLICABLE",
            "element_count": len(elements),
            "traceable_element_count": len(elements),
            "incomplete_element_count": 0,
            "source_segment_ref_count": reference_count,
            "drawing_identity_mismatch_count": 0,
            "issues": [],
        }
        if columns:
            result.update({
                "source_profile_ref_count": len(elements),
                "one_to_one_complete": True,
            })
        else:
            result.update({
                "identity_incomplete_ref_count": 0,
                "occurrence_mismatch_count": 0,
                "lineage_incomplete_count": 0,
            })
        return result

    return {
        "schema_version": "buildmate.wall-column-traceability/1.0",
        "status": "COMPLETE",
        "gate_passed": True,
        "source": {
            "path": path,
            "sha256": sha256,
            "drawing_identity": f"sha256:{sha256}",
            "identity_status": "MATCH",
        },
        "groups": {
            "columns": group("columns", columns=True),
            "structural_walls": group("structural_walls"),
            "architectural_walls": group("architectural_walls"),
        },
        "identity_incomplete_ref_count": 0,
        "drawing_identity_mismatch_count": 0,
        "occurrence_mismatch_count": 0,
        "lineage_incomplete_count": 0,
        "issue_count": 0,
        "issues": [],
    }


def _review_ir(discipline, *, allow=True, architecture_status="PASS"):
    is_architecture = discipline == "architecture"
    sha256 = ARCHITECTURE_SHA if is_architecture else STRUCTURE_SHA
    y_offset = 0.0 if is_architecture else 27.0
    columns = [
        _column(
            sha256, "1", [0.0, y_offset],
            mark=None if is_architecture else "YBZ1",
            grid_ref="1-A" if is_architecture else "1-D",
        ),
        _column(
            sha256, "2", [10.0, y_offset],
            mark=None if is_architecture else "YBZ2",
            grid_ref="2-A" if is_architecture else "2-D",
        ),
    ]
    structural_walls = [
        _wall(sha256, "structure", [0.0, y_offset], [2.0, y_offset],
              thickness=300.0),
    ]
    architectural_walls = ([
        _wall(sha256, "partition", [0.0, 5.0], [2.0, 5.0]),
    ] if is_architecture else [])
    beams = ([] if is_architecture else [{
        "id": "beam-1",
        "start": [0.0, 27.0],
        "end": [10.0, 27.0],
        "confidence": 0.5,
        "source": ["drawing"],
        "warnings": ["section unverified"],
    }])
    blockers = ([] if allow else [f"{discipline} topology failed"])
    source_path = f"C:/{discipline}.dxf"
    geometry = {
        "columns": columns,
        "structural_walls": structural_walls,
        "architectural_walls": architectural_walls,
        "beams": beams,
    }
    return {
        "schema_version": "buildmate.review-ir/1.0",
        "artifact_role": "REVIEW_ONLY",
        "artifact_status": "READY_FOR_PREVIEW" if allow else "BLOCKED",
        "source": {
            "path": source_path,
            "sha256": sha256,
        },
        "coordinate_system": {
            "unit": "m",
            "origin_dxf_mm": [1000.0, 29000.0 if is_architecture else 2000.0],
            "rotation_deg": 0.0,
            "grid_center_dxf_mm": [0.0, 0.0],
        },
        "quality_gate": {
            "allow_modeling": allow,
            "blocking_reasons": blockers,
            "structure": {"status": "PASS", "checks": []},
            "architecture": {
                "status": architecture_status if is_architecture else "N/A",
                "checks": [],
            },
        },
        "traceability": _traceability(sha256, source_path, geometry),
        "geometry": geometry,
        "plan_selection": {
            "selected": {
                "name": f"{discipline} B01 (-6.5~-0.1m)",
            },
        },
        "review_candidates": {
            "short_wall_proposals": {
                "integrity_sha256": f"{discipline}-integrity",
                "proposals": [{"start": [1.0, 2.0], "end": [1.5, 2.0]}],
            },
        },
        "artifacts": {"review_overlay": f"{discipline}.png"},
    }


def _add_architecture_nested_elevation_reference(
        ir, *, marker_name="B1层墙柱(-6.5~-0.1m)", marker_affine=None):
    selected = ir["plan_selection"]["selected"]
    selected["insert_handle"] = "INSERT-01"
    marker = {
        "insert_handle": "NESTED-WALL-COLUMN-01",
        "block_name": marker_name,
        "cumulative_transform": {
            "coordinate_system": "DXF_WCS",
            "affine_2d": marker_affine or [
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
        },
    }
    occurrence_id = "architecture-selected-structural-occurrence"
    for column in ir["geometry"]["columns"]:
        profile = column["source_profile_ref"]
        profile["selected_structural_occurrence_id"] = occurrence_id
        profile["placement_path"][0]["block_name"] = selected["name"]
        profile["placement_path"].append(copy.deepcopy(marker))
        for reference in column["source_segment_refs"]:
            reference["selected_structural_occurrence_id"] = occurrence_id
    for wall in ir["geometry"]["structural_walls"]:
        for reference in wall["source_segment_refs"]:
            reference["structural_occurrence_id"] = occurrence_id
            reference["placement_path"][0]["block_name"] = selected["name"]
            reference["placement_path"].append(copy.deepcopy(marker))
    return ir


def _grid(discipline):
    if discipline == "architecture":
        return {
            "x_axes": [
                {"coord": 0.0, "label": "1"},
                {"coord": 10.0, "label": "2"},
            ],
            "y_axes": [
                {"coord": 0.0, "label": "A"},
                {"coord": 9.0, "label": "B"},
            ],
            "origin_mm": [1000.0, 29000.0],
            "meta": {"rot_deg": 0.0, "gcx": 0.0, "gcy": 0.0},
        }
    return {
        "x_axes": [
            {"coord": 0.0, "label": "1"},
            {"coord": 10.0, "label": "2"},
        ],
        "y_axes": [
            {"coord": 0.0, "label": "A"},
            {"coord": 27.0, "label": "D"},
            {"coord": 36.0, "label": "E"},
        ],
        "origin_mm": [1000.0, 2000.0],
        "meta": {"rot_deg": 0.0, "gcx": 0.0, "gcy": 0.0},
    }


def _fuse(architecture=None, structure=None):
    return fuse_same_floor_review_ir(
        architecture or _review_ir("architecture"),
        _grid("architecture"),
        structure or _review_ir("structure"),
        _grid("structure"),
        floor_code="B01",
    )


def test_fusion_deduplicates_same_floor_geometry_and_keeps_beams_for_review():
    result = _fuse()

    assert result["schema_version"] == (
        "buildmate.same-floor-fusion-review/1.0")
    assert result["artifact_role"] == "REVIEW_ONLY"
    assert len(result["geometry"]["columns"]) == 2
    assert len(result["geometry"]["structural_walls"]) == 1
    assert len(result["geometry"]["architectural_walls"]) == 1
    assert result["geometry"]["beams"] == []
    assert result["review_candidates"]["unverified_structure_beams"][
        "count"] == 1
    assert result["metrics"]["input_column_representations"] == 4
    assert result["metrics"]["fused_columns"] == 2
    assert result["metrics"][
        "suppressed_architecture_structural_wall_representations"] == 1

    alignment = result["alignments"][0]
    assert alignment["source_to_canonical_affine_m"] == [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, -27.0],
        [0.0, 0.0, 1.0],
    ]
    assert alignment["evidence"]["unique_match_count"] == 2
    assert alignment["evidence"]["max_center_residual_m"] == 0.0
    assert result["geometry"]["structural_walls"][0]["start"] == [0.0, 0.0]
    assert result["geometry"]["architectural_walls"][0]["start"] == [0.0, 5.0]
    assert [axis["coord"] for axis in result["geometry"]["grid"]["y_axes"]] == [
        -27.0, 0.0, 9.0]
    assert {column["mark"] for column in result["geometry"]["columns"]} == {
        "YBZ1", "YBZ2"}
    assert all(len(column["source_aliases"]) == 2
               for column in result["geometry"]["columns"])


def test_fusion_is_deterministic_preserves_inputs_and_native_review_candidates():
    architecture = _review_ir("architecture")
    structure = _review_ir("structure")
    architecture_snapshot = copy.deepcopy(architecture)
    structure_snapshot = copy.deepcopy(structure)

    first = _fuse(architecture, structure)
    assert architecture == architecture_snapshot
    assert structure == structure_snapshot
    assert first["discipline_reviews"]["architecture"][
        "review_candidates"] == architecture["review_candidates"]
    assert first["discipline_reviews"]["architecture"][
        "coordinate_system"] == architecture["coordinate_system"]

    first["geometry"]["columns"][0]["source_aliases"][0][
        "source_segment_refs"][0]["entity_handle"] = "CHANGED"
    assert architecture == architecture_snapshot

    architecture_reordered = copy.deepcopy(architecture)
    structure_reordered = copy.deepcopy(structure)
    architecture_reordered["geometry"]["columns"].reverse()
    architecture_reordered["geometry"]["structural_walls"].reverse()
    structure_reordered["geometry"]["columns"].reverse()
    structure_reordered["geometry"]["structural_walls"].reverse()
    baseline = _fuse(architecture, structure)
    reordered = _fuse(architecture_reordered, structure_reordered)
    assert json.dumps(baseline, sort_keys=True) == json.dumps(reordered, sort_keys=True)


@pytest.mark.parametrize(
    ("architecture_allow", "structure_allow", "expected_status"),
    [
        (False, True, "BLOCKED"),
        (True, False, "BLOCKED"),
        (True, True, "READY_FOR_PREVIEW"),
    ],
)
def test_fusion_gate_is_conjunction_of_both_input_gates(
        architecture_allow, structure_allow, expected_status):
    architecture = _review_ir(
        "architecture",
        allow=architecture_allow,
        architecture_status="PASS" if architecture_allow else "FAIL",
    )
    structure = _review_ir("structure", allow=structure_allow)

    result = _fuse(architecture, structure)

    assert result["artifact_status"] == expected_status
    assert result["quality_gate"]["allow_modeling"] is False
    assert result["quality_gate"]["eligible_for_model_compilation"] is (
        architecture_allow and structure_allow)
    assert "model_elements" not in result
    if not architecture_allow:
        assert any(reason.startswith("architecture:") for reason in
                   result["quality_gate"]["blocking_reasons"])
    if not structure_allow:
        assert any(reason.startswith("structure:") for reason in
                   result["quality_gate"]["blocking_reasons"])


def test_fusion_rejects_ambiguous_column_alignment():
    architecture = _review_ir("architecture")
    structure = _review_ir("structure")
    architecture["geometry"]["columns"][1] = _column(
        ARCHITECTURE_SHA, "2", [20.0, 0.0], grid_ref="2-A")
    structure["geometry"]["columns"][1] = _column(
        STRUCTURE_SHA, "2", [0.0005, 27.0], mark="YBZ2", grid_ref="2-D")

    with pytest.raises(ValueError, match="not one-to-one"):
        _fuse(architecture, structure)


def test_fusion_keeps_unmatched_structural_wall_views_for_review():
    architecture = _review_ir("architecture")
    architecture["geometry"]["structural_walls"][0]["end"] = [3.0, 0.0]

    result = _fuse(architecture=architecture)

    differences = result["review_candidates"][
        "structural_wall_view_differences"]
    assert differences["status"] == "REVIEW"
    assert differences["exact_match_count"] == 0
    assert differences["unmatched_structure_count"] == 1
    assert differences["unmatched_architecture_count"] == 1
    assert result["geometry"]["structural_walls"][0]["fusion"][
        "architecture_duplicate_view_suppressed"] is False
    assert result["quality_gate"]["eligible_for_model_compilation"] is False
    assert result["quality_gate"]["checks"][
        "structural_wall_cross_source_alignment"] == "FAIL"
    assert result["alignments"][0]["status"] == "PARTIAL"
    assert result["alignments"][0]["coordinate_alignment_status"] == "PASS"
    assert result["alignments"][0][
        "structural_wall_alignment_status"] == "FAIL"


def test_fusion_accepts_strict_one_to_many_wall_segmentation_equivalence():
    architecture = _review_ir("architecture")
    structure = _review_ir("structure")
    architecture["geometry"]["structural_walls"] = [
        _wall(ARCHITECTURE_SHA, "a-whole", [0.0, 0.0], [10.0, 0.0],
              thickness=300.0),
        _wall(ARCHITECTURE_SHA, "a-left", [20.0, 0.0], [24.0, 0.0],
              thickness=300.0),
        _wall(ARCHITECTURE_SHA, "a-right", [24.0, 0.0], [30.0, 0.0],
              thickness=300.0),
    ]
    structure["geometry"]["structural_walls"] = [
        _wall(STRUCTURE_SHA, "s-left", [0.0, 27.0], [4.0, 27.0],
              thickness=300.0),
        _wall(STRUCTURE_SHA, "s-right", [4.0, 27.0], [10.0, 27.0],
              thickness=300.0),
        _wall(STRUCTURE_SHA, "s-whole", [20.0, 27.0], [30.0, 27.0],
              thickness=300.0),
    ]
    for ir, sha256 in (
        (architecture, ARCHITECTURE_SHA),
        (structure, STRUCTURE_SHA),
    ):
        ir["traceability"] = _traceability(
            sha256, ir["source"]["path"], ir["geometry"])

    result = _fuse(architecture=architecture, structure=structure)

    differences = result["review_candidates"][
        "structural_wall_view_differences"]
    assert differences["status"] == "PASS"
    assert differences["exact_match_count"] == 0
    assert differences["segmentation_equivalence_group_count"] == 2
    assert differences["unmatched_structure_count"] == 0
    assert differences["unmatched_architecture_count"] == 0
    assert {group["relation"] for group in
            differences["segmentation_equivalence_groups"]} == {
        "MANY_STRUCTURE_TO_ONE_ARCHITECTURE",
        "ONE_STRUCTURE_TO_MANY_ARCHITECTURE",
    }
    assert all(
        wall["fusion"]["corroboration_status"] ==
        "SEGMENTATION_EQUIVALENT_GROUP"
        for wall in result["geometry"]["structural_walls"]
    )
    assert result["metrics"][
        "suppressed_architecture_structural_wall_representations"] == 3
    assert result["quality_gate"][
        "eligible_for_model_compilation"] is True


def test_fusion_does_not_upgrade_a_contradictory_blocked_input():
    architecture = _review_ir("architecture")
    architecture["artifact_status"] = "BLOCKED"
    architecture["quality_gate"]["blocking_reasons"] = ["still blocked"]

    result = _fuse(architecture=architecture)

    assert result["artifact_status"] == "BLOCKED"
    assert result["quality_gate"]["eligible_for_model_compilation"] is False
    assert "architecture: still blocked" in result["quality_gate"][
        "blocking_reasons"]
    assert any("conflicts" in reason for reason in result["quality_gate"][
        "blocking_reasons"])


def test_fusion_rejects_a_different_selected_floor():
    structure = _review_ir("structure")
    structure["plan_selection"]["selected"]["name"] = "structure B02"

    with pytest.raises(ValueError, match="does not match both"):
        _fuse(structure=structure)


def test_fusion_blocks_when_only_one_plan_declares_elevation():
    architecture = _review_ir("architecture")
    architecture["plan_selection"]["selected"]["name"] = "architecture B01"

    result = _fuse(architecture=architecture)

    assert result["floor"]["status"] == "PARTIAL"
    assert result["floor"]["elevation_evidence"] == "ONE_SELECTED_PLAN"
    assert result["quality_gate"]["checks"]["same_floor_contract"] == "PARTIAL"
    assert result["quality_gate"]["eligible_for_model_compilation"] is False


def test_fusion_accepts_verified_architecture_nested_elevation_reference():
    architecture = _review_ir("architecture")
    architecture["plan_selection"]["selected"]["name"] = "architecture B01"
    _add_architecture_nested_elevation_reference(architecture)

    result = _fuse(architecture=architecture)

    assert result["floor"]["status"] == "PASS"
    assert result["floor"]["elevation_range_m"] == [-6.5, -0.1]
    assert result["floor"]["elevation_evidence"] == (
        "STRUCTURE_PLAN_PLUS_ARCH_NESTED_REFERENCE")
    nested = result["floor"]["architecture_nested_reference"]
    assert nested["source_sha256"] == ARCHITECTURE_SHA
    assert nested["selected_plan_insert_handle"] == "INSERT-01"
    assert nested["marker_insert_handle"] == "NESTED-WALL-COLUMN-01"
    assert nested["marker_name"] == "B1层墙柱(-6.5~-0.1m)"
    assert nested["structural_occurrence_id"] == (
        "architecture-selected-structural-occurrence")
    assert nested["parser"] == "BLOCK_NAME_RANGE"
    assert nested["scope"] == "STRUCTURAL_WALL_COLUMN_VERTICAL_RANGE"
    assert result["quality_gate"]["checks"]["same_floor_contract"] == "PASS"
    assert result["quality_gate"]["eligible_for_model_compilation"] is True


def test_fusion_rejects_conflicting_architecture_nested_elevation_reference():
    architecture = _review_ir("architecture")
    architecture["plan_selection"]["selected"]["name"] = "architecture B01"
    _add_architecture_nested_elevation_reference(
        architecture, marker_name="B1层墙柱(-6.4~-0.1m)")

    with pytest.raises(ValueError, match="different elevation ranges"):
        _fuse(architecture=architecture)


def test_fusion_rejects_nested_reference_conflicting_with_architecture_plan():
    architecture = _review_ir("architecture")
    _add_architecture_nested_elevation_reference(
        architecture, marker_name="B1层墙柱(-6.4~-0.1m)")

    with pytest.raises(
            ValueError, match="selected plan and nested reference"):
        _fuse(architecture=architecture)


def test_fusion_rejects_transformed_architecture_nested_elevation_reference():
    architecture = _review_ir("architecture")
    architecture["plan_selection"]["selected"]["name"] = "architecture B01"
    _add_architecture_nested_elevation_reference(
        architecture,
        marker_affine=[
            [1.0, 0.0, 0.01],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
    )

    with pytest.raises(ValueError, match="identity-aligned"):
        _fuse(architecture=architecture)


def test_fusion_rejects_partial_architecture_nested_elevation_reference():
    architecture = _review_ir("architecture")
    architecture["plan_selection"]["selected"]["name"] = "architecture B01"
    _add_architecture_nested_elevation_reference(architecture)
    architecture["geometry"]["columns"][0]["source_profile_ref"][
        "placement_path"].pop()

    with pytest.raises(ValueError, match="not present on every"):
        _fuse(architecture=architecture)


def test_fusion_does_not_trust_an_internally_inconsistent_traceability_gate():
    structure = _review_ir("structure")
    structure["traceability"]["groups"]["columns"]["status"] = "INCOMPLETE"
    structure["traceability"]["issue_count"] = 1
    structure["traceability"]["issues"] = ["forced inconsistency"]

    result = _fuse(structure=structure)

    assert result["artifact_status"] == "BLOCKED"
    assert result["quality_gate"]["checks"]["structure_input_gate"] == "FAIL"
    assert any("traceability" in reason for reason in
               result["quality_gate"]["blocking_reasons"])


def test_fusion_rejects_incomplete_formal_provenance():
    structure = _review_ir("structure")
    structure["geometry"]["structural_walls"][0][
        "source_segment_refs"][0]["identity_is_complete"] = False

    with pytest.raises(ValueError, match="identity is incomplete"):
        _fuse(structure=structure)


def test_fusion_rejects_nonrectangular_column_profile():
    structure = _review_ir("structure")
    structure["geometry"]["columns"][0]["source_profile_ref"][
        "vertex_count"] = 5

    with pytest.raises(ValueError, match="not a verified rectangular"):
        _fuse(structure=structure)


def test_fusion_rejects_column_profile_geometry_that_disagrees_with_output():
    structure = _review_ir("structure")
    profile = structure["geometry"]["columns"][0]["source_profile_ref"]
    profile["center_local_m"] = [999.0, 999.0]

    with pytest.raises(ValueError, match="profile center disagrees"):
        _fuse(structure=structure)


def test_fusion_rejects_rotated_rectangles_without_an_orientation_contract():
    structure = _review_ir("structure")
    column = structure["geometry"]["columns"][0]
    corners = [[0.0, 26.6], [0.4, 27.0], [0.0, 27.4], [-0.4, 27.0]]
    for index, reference in enumerate(column["source_segment_refs"]):
        reference["start_local_m"] = corners[index]
        reference["end_local_m"] = corners[(index + 1) % 4]

    with pytest.raises(ValueError, match="not axis-aligned"):
        _fuse(structure=structure)


def test_fusion_rejects_grid_that_is_not_a_subset_after_alignment():
    architecture_grid = _grid("architecture")
    architecture_grid["y_axes"][1]["coord"] = 8.0

    with pytest.raises(ValueError, match="not a unique subset"):
        fuse_same_floor_review_ir(
            _review_ir("architecture"),
            architecture_grid,
            _review_ir("structure"),
            _grid("structure"),
            floor_code="B01",
        )


def test_fusion_rejects_grid_from_a_different_coordinate_contract():
    structure_grid = _grid("structure")
    structure_grid["origin_mm"] = [1000.0, 3000.0]

    with pytest.raises(ValueError, match="origin does not match"):
        fuse_same_floor_review_ir(
            _review_ir("architecture"),
            _grid("architecture"),
            _review_ir("structure"),
            structure_grid,
            floor_code="B01",
        )


@pytest.mark.parametrize("mutation", ["schema", "sha", "same_source"])
def test_fusion_rejects_invalid_source_contracts(mutation):
    architecture = _review_ir("architecture")
    structure = _review_ir("structure")
    if mutation == "schema":
        architecture["schema_version"] = "buildmate.review-ir/0.9"
    elif mutation == "sha":
        structure["source"]["sha256"] = "not-a-sha"
    else:
        structure["source"]["sha256"] = ARCHITECTURE_SHA

    with pytest.raises(ValueError):
        _fuse(architecture, structure)


def test_wall_stable_id_does_not_depend_on_endpoint_direction():
    baseline = _fuse()
    structure = _review_ir("structure")
    wall = structure["geometry"]["structural_walls"][0]
    wall["start"], wall["end"] = wall["end"], wall["start"]

    reversed_result = _fuse(structure=structure)

    assert baseline["geometry"]["structural_walls"][0]["id"] == (
        reversed_result["geometry"]["structural_walls"][0]["id"])
    assert baseline["geometry"]["structural_walls"][0]["start"] == (
        reversed_result["geometry"]["structural_walls"][0]["start"])


def test_cli_verifies_the_declared_drawing_hash(tmp_path):
    source_path = tmp_path / "fresh.dxf"
    source_path.write_bytes(b"fresh drawing")
    actual_sha256 = hashlib.sha256(b"fresh drawing").hexdigest()
    ir = {"source": {"path": str(source_path), "sha256": actual_sha256}}

    _verify_declared_source(ir, "architecture")
    ir["source"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        _verify_declared_source(ir, "architecture")


def test_cli_restricts_output_to_the_same_fusion_review_directory(tmp_path):
    fresh_root = tmp_path / "fresh_20260828_example"
    inputs = [
        fresh_root / "architecture" / "review.json",
        fresh_root / "architecture" / "grid.json",
        fresh_root / "structure" / "review.json",
        fresh_root / "structure" / "grid.json",
    ]
    _validate_output_scope(
        fresh_root / "fusion_v1" / "same_floor_review.json", inputs)

    with pytest.raises(ValueError, match="fusion review directory"):
        _validate_output_scope(fresh_root / "json" / "model.json", inputs)
    with pytest.raises(ValueError, match="same fresh run"):
        _validate_output_scope(
            tmp_path / "elsewhere" / "fusion" / "review.json", inputs)
