import copy

import ezdxf

from backend.engines.column_provenance import (
    enrich_columns_with_provenance,
)


SELECTED_OCCURRENCE = "structural_occurrence_selected"


def _marker(handle="PARENT"):
    return {
        "name": "B1 wall-column plan",
        "block_name": "B1 wall-column plan",
        "insert_handle": handle,
        "layer": "A-XREF",
        "insert": [0.0, 0.0],
        "rotation": 0.0,
        "xscale": 1.0,
        "yscale": 1.0,
        "array_index": None,
        "cumulative_transform": {
            "coordinate_system": "DXF_WCS",
            "affine_2d": [
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
        },
    }


def _child_marker(handle):
    item = _marker(handle)
    item["name"] = f"column profile {handle}"
    item["block_name"] = item["name"]
    item["layer"] = "S-COLU"
    return item


def _provenance(identifier, parent, *, segment_ids=None):
    return {
        "drawing_identity": "sha256:test-drawing",
        "root_plan": "B1 plan",
        "placement_path": [copy.deepcopy(parent),
                           _child_marker(f"CHILD-{identifier}")],
        "source_entity_handle": f"ENTITY-{identifier}",
        "source_occurrence_id": f"source_occurrence_{identifier}",
        "placed_entity_id": f"placed_entity_{identifier}",
        "structural_occurrence_id": f"leaf_occurrence_{identifier}",
        "segment_ids": (segment_ids if segment_ids is not None else
                        [f"segment_{identifier}_{index}"
                         for index in range(4)]),
    }


def _records_with_anchor(profiles):
    doc = profiles[0].doc
    anchor = doc.modelspace().add_line((0, 0), (1, 0))
    parent = _marker()
    anchor_provenance = {
        "drawing_identity": "sha256:test-drawing",
        "placement_path": [copy.deepcopy(parent)],
        "source_entity_handle": "ANCHOR",
        "source_occurrence_id": "source_occurrence_anchor",
        "placed_entity_id": "placed_entity_anchor",
        "structural_occurrence_id": SELECTED_OCCURRENCE,
        "segment_ids": ["segment_anchor"],
    }
    records = [(anchor, "S-WALL-HATC", anchor_provenance)]
    for index, profile in enumerate(profiles):
        records.append((profile, "B1$0$S-COLU",
                        _provenance(str(index), parent)))
    return records


def test_mirrored_ocs_profile_matches_in_wcs_and_keeps_inputs_unchanged():
    doc = ezdxf.new()
    # extrusion -Z maps OCS x=-2 to WCS x=+2. Reading raw xy would miss.
    profile = doc.modelspace().add_lwpolyline(
        [(-1500, 2600), (-2500, 2600), (-2500, 3400), (-1500, 3400)],
        close=True,
        dxfattribs={"layer": "S-COLU", "extrusion": (0, 0, -1)},
    )
    assert sum(point[0] for point in profile.get_points("xy")) / 4 == -2000.0
    columns = [{"center": [2.0, 3.0], "size": [1.0, 0.8]}]
    original_columns = copy.deepcopy(columns)
    records = _records_with_anchor([profile])
    original_provenance = copy.deepcopy(records[1][2])

    enriched, audit = enrich_columns_with_provenance(
        columns, records,
        origin_dxf_mm=[0.0, 0.0], rotation_deg=0.0,
        grid_center_dxf_mm=[0.0, 0.0],
        selected_structural_occurrence_id=SELECTED_OCCURRENCE,
    )

    assert columns == original_columns
    assert records[1][2] == original_provenance
    assert audit["status"] == "PASS"
    assert audit["selected_candidate_count"] == 1
    assert audit["valid_geometry_candidate_count"] == 1
    assert audit["matched_count"] == 1
    assert audit["missing_count"] == 0
    assert audit["ambiguous_count"] == 0
    assert audit["identity_complete_count"] == 1
    assert audit["one_to_one_complete"] is True
    assert enriched[0]["source_profile_ref"]["center_local_m"] == [2.0, 3.0]
    assert len(enriched[0]["source_segment_refs"]) == 4
    assert {item["source_segment_id"] for item in
            enriched[0]["source_segment_refs"]} == {
        f"segment_0_{index}" for index in range(4)}


def test_ambiguous_profiles_are_reported_and_not_attached():
    doc = ezdxf.new()
    profiles = [
        doc.modelspace().add_lwpolyline(
            [(0, 0), (1, 0), (1, 1), (0, 1)], close=True,
            dxfattribs={"layer": "S-COLU"})
        for _index in range(2)
    ]
    columns = [{"center": [0.0005, 0.0005],
                "size": [0.001, 0.001]}]

    enriched, audit = enrich_columns_with_provenance(
        columns, _records_with_anchor(profiles),
        origin_dxf_mm=[0.0, 0.0], rotation_deg=0.0,
        grid_center_dxf_mm=[0.0, 0.0],
        selected_structural_occurrence_id=SELECTED_OCCURRENCE,
    )

    assert audit["status"] == "REVIEW"
    assert audit["selected_candidate_count"] == 2
    assert audit["matched_count"] == 0
    assert audit["ambiguous_count"] == 1
    assert audit["ambiguous_candidate_count"] == 2
    assert "column_profile_match_ambiguous" in audit["blockers"]
    assert "source_profile_ref" not in enriched[0]


def test_incomplete_profile_identity_fails_closed_without_mutating_column():
    doc = ezdxf.new()
    profile = doc.modelspace().add_lwpolyline(
        [(0, 0), (1000, 0), (1000, 800), (0, 800)], close=True,
        dxfattribs={"layer": "COLUMN"})
    parent = _marker()
    records = _records_with_anchor([profile])
    records[1] = (
        profile, "COLUMN",
        _provenance("broken", parent, segment_ids=["one", "two", "three"]),
    )
    columns = [{"center": [0.5, 0.4], "size": [1.0, 0.8]}]
    before = copy.deepcopy(columns)

    enriched, audit = enrich_columns_with_provenance(
        columns, records,
        origin_dxf_mm=[0.0, 0.0], rotation_deg=0.0,
        grid_center_dxf_mm=[0.0, 0.0],
        selected_structural_occurrence_id=SELECTED_OCCURRENCE,
    )

    assert columns == before
    assert enriched == before
    assert audit["status"] == "REVIEW"
    assert audit["identity_complete_count"] == 0
    assert audit["identity_incomplete_count"] == 1
    assert audit["matched_count"] == 0
    assert audit["missing_count"] == 1
    assert "four_segment_ids_required" in (
        audit["identity_issues"][0]["limitations"])
    assert "column_profile_identity_incomplete" in audit["blockers"]
