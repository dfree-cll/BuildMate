import copy

import ezdxf

from backend.engines.wall_geometry import (
    audit_closed_wall_strips,
    extract_precise_walls,
    wall_source_coverage,
)
from tests.fixtures.dxf_entities import Line as _Line


def _complete_provenance(handle, *, segment_count=1):
    return {
        "drawing_identity": "sha256:review-unit-contract",
        "source_entity_handle": handle,
        "source_occurrence_id": f"source_{handle}",
        "placed_entity_id": f"placed_{handle}",
        "structural_occurrence_id": "structural_MAIN",
        "segment_ids": [
            f"segment_{handle}_{index}" for index in range(segment_count)
        ],
        "placement_path": [{
            "name": "B1",
            "block_name": "B1",
            "insert": [0, 0],
            "insert_handle": "INSERT-01",
            "array_index": None,
            "cumulative_transform": {
                "coordinate_system": "DXF_WCS",
                "affine_2d": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
            },
        }],
    }


def _closed_polyline(points, *, layer="A-WALL"):
    document = ezdxf.new()
    if layer not in document.layers:
        document.layers.add(layer)
    return document.modelspace().add_lwpolyline(
        points, format="xy", close=True, dxfattribs={"layer": layer})


def _review_units_by_start(coverage):
    return sorted(
        coverage["review_units"],
        key=lambda unit: (unit["start"][0], unit["start"][1]),
    )


def test_mixed_unmapped_ranges_become_stable_independent_review_units():
    provenance = _complete_provenance("MIXED-RANGES")
    records = [
        (_Line((0, 0), (10000, 0)), "A-WALL", provenance),
    ]
    walls = [
        {
            "start": [1.0, 0.1],
            "end": [4.0, 0.1],
            "thickness": 200.0,
            "paired": True,
        },
        {
            "start": [6.0, 0.1],
            "end": [10.0, 0.1],
            "thickness": 200.0,
            "paired": True,
        },
    ]
    opening_bridges = [{"start": [4.0, 0.1], "end": [6.0, 0.1]}]

    first = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0,
        opening_bridges=opening_bridges,
    )
    second = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0,
        opening_bridges=opening_bridges,
    )

    assert first["raw_source_review_count"] == 1
    assert first["logical_review_unit_count"] == 2
    assert first["review_units"] == second["review_units"]
    units = _review_units_by_start(first)
    assert len(units) == 2
    assert len({unit["review_unit_id"] for unit in units}) == 2

    candidate = first["partially_mapped_source_segments"][0]
    expected_identity = {
        "candidate_id": candidate["candidate_id"],
        "source_segment_id": "segment_MIXED-RANGES_0",
        "drawing_identity": "sha256:review-unit-contract",
        "source_occurrence_id": "source_MIXED-RANGES",
        "placed_entity_id": "placed_MIXED-RANGES",
        "structural_occurrence_id": "structural_MAIN",
        "identity_is_complete": True,
        "identity_limitations": [],
    }
    for unit in units:
        assert {key: unit[key] for key in expected_identity} == expected_identity
        assert unit["status"] == "NEEDS_REVIEW"

    assert {
        "start": units[0]["start"],
        "end": units[0]["end"],
        "length_m": units[0]["length_m"],
        "opening_bridge_supported": units[0]["opening_bridge_supported"],
        "decision_reason": units[0]["decision_reason"],
    } == {
        "start": [0.0, 0.0],
        "end": [1.0, 0.0],
        "length_m": 1.0,
        "opening_bridge_supported": False,
        "decision_reason": "semantic_wall_face_partially_mapped",
    }
    assert {
        "start": units[1]["start"],
        "end": units[1]["end"],
        "length_m": units[1]["length_m"],
        "opening_bridge_supported": units[1]["opening_bridge_supported"],
        "decision_reason": units[1]["decision_reason"],
    } == {
        "start": [4.0, 0.0],
        "end": [6.0, 0.0],
        "length_m": 2.0,
        "opening_bridge_supported": True,
        "decision_reason": "semantic_wall_face_partially_mapped",
    }
    assert units[1]["review_bucket"] == "POSSIBLE_OMISSION"
    assert units[1]["opening_bridge_semantic_authority"] is False


def test_single_unmapped_range_remains_one_review_unit():
    records = [(
        _Line((0, 0), (2000, 0)),
        "A-PART-S",
        _complete_provenance("SINGLE-RANGE"),
    )]

    coverage = wall_source_coverage(records, [], 0, 0, 0, 0, 0)

    assert coverage["raw_source_review_count"] == 1
    assert coverage["logical_review_unit_count"] == 1
    units = coverage["review_units"]
    assert len(units) == 1
    assert units[0]["candidate_id"] == coverage[
        "uncovered_source_segments"][0]["candidate_id"]
    assert units[0]["source_segment_id"] == "segment_SINGLE-RANGE_0"
    assert units[0]["start"] == [0.0, 0.0]
    assert units[0]["end"] == [2.0, 0.0]
    assert units[0]["length_m"] == 2.0
    assert units[0]["decision_reason"] == "semantic_wall_face_unmapped"


def test_resolved_profile_ranges_do_not_create_review_units():
    entity = _closed_polyline([
        (0, 0), (10000, 0), (10000, 200), (0, 200),
    ])
    provenance = _complete_provenance("APPROVED-PROFILE", segment_count=4)
    records = [(entity, "A-WALL", provenance)]
    audit = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))
    walls = extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    coverage = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0,
        closed_profile_audit=audit,
    )

    assert coverage["resolved_by_profile_source_segment_count"] == 2
    assert coverage["logical_review_unit_count"] == 0
    assert coverage["review_units"] == []


def test_rejected_profile_ranges_do_not_create_review_units():
    entity = _closed_polyline([
        (0, 0), (762.5, 0), (762.5, 40), (0, 40),
    ], layer="A-PART-S")
    provenance = _complete_provenance("REJECTED-PROFILE", segment_count=4)
    records = [(entity, "A-PART-S", provenance)]
    audit = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))
    audit = copy.deepcopy(audit)
    audit["candidates"][0].update({
        "status": "REJECTED_BY_RULE",
        "decision_reason": "exact_door_leaf_swing_topology",
        "review_bucket": "EXACT_DOOR_LEAF_SWING",
        "auto_action": "NONE",
    })
    audit.update({
        "status": "PASS",
        "needs_review_count": 0,
        "rejected_by_rule_count": 1,
    })

    coverage = wall_source_coverage(
        records, [], 0, 0, 0, 0, 0,
        closed_profile_audit=audit,
    )

    assert coverage["rejected_by_rule_source_segment_count"] == 4
    assert coverage["logical_review_unit_count"] == 0
    assert coverage["review_units"] == []


def _junction_records(*, mismatched_support_occurrence=False):
    support_b = _complete_provenance("SUPPORT-B")
    if mismatched_support_occurrence:
        support_b["structural_occurrence_id"] = "structural_OTHER"
    return [
        (_Line((0, 0), (800, 0)), "A-WALL",
         _complete_provenance("OWNER-A")),
        (_Line((100, 100), (800, 100)), "A-WALL",
         _complete_provenance("OWNER-B")),
        (_Line((0, -1000), (0, 0)), "A-WALL",
         _complete_provenance("SUPPORT-A")),
        (_Line((100, -1000), (100, 100)), "A-WALL", support_b),
    ]


def _junction_walls():
    return [
        {
            "id": "owner",
            "start": [0.1, 0.05],
            "end": [0.8, 0.05],
            "thickness": 100.0,
            "paired": True,
            "source_segment_ids": [
                "segment_OWNER-A_0", "segment_OWNER-B_0"],
        },
        {
            "id": "support",
            "start": [0.05, -1.0],
            "end": [0.05, 0.0],
            "thickness": 100.0,
            "paired": True,
            "source_segment_ids": [
                "segment_SUPPORT-A_0", "segment_SUPPORT-B_0"],
        },
    ]


def test_exact_terminal_face_tail_is_resolved_by_unique_paired_junction():
    walls = _junction_walls()
    original_walls = copy.deepcopy(walls)

    coverage = wall_source_coverage(
        _junction_records(), walls, 0, 0, 0, 0, 0)

    assert coverage["raw_source_review_count"] == 1
    assert coverage["resolved_modeled_junction_edge_count"] == 1
    assert coverage["logical_review_unit_count"] == 0
    unit = coverage["review_units"][0]
    assert unit["status"] == "RESOLVED_MODELED_JUNCTION_EDGE"
    assert unit["decision_reason"] == "exact_modeled_wall_junction_edge"
    assert unit["resolution_evidence"]["owner_wall_id"] == "owner"
    assert unit["resolution_evidence"]["support_wall_id"] == "support"
    assert unit["auto_action"] == "NONE"
    assert walls == original_walls


def test_inferred_opening_does_not_suppress_exact_junction_resolution():
    opening = wall_source_coverage(
        _junction_records(), _junction_walls(), 0, 0, 0, 0, 0,
        opening_bridges=[{"start": [0.0, 0.0], "end": [0.1, 0.0]}])
    assert opening["resolved_modeled_junction_edge_count"] == 1
    assert opening["logical_review_unit_count"] == 0
    assert opening["review_units"][0]["status"] == (
        "RESOLVED_MODELED_JUNCTION_EDGE")
    assert opening["review_units"][0]["opening_bridge_supported"] is True


def test_junction_tail_stays_in_review_with_identity_mismatch():
    mismatch = wall_source_coverage(
        _junction_records(mismatched_support_occurrence=True),
        _junction_walls(), 0, 0, 0, 0, 0)
    assert mismatch["resolved_modeled_junction_edge_count"] == 0
    assert mismatch["logical_review_unit_count"] == 1
    assert mismatch["review_units"][0]["status"] == "NEEDS_REVIEW"


def test_endpoint_adjustment_resolves_supported_terminal_face_tail():
    face_a_provenance = _complete_provenance("ADJUSTED-A")
    face_b_provenance = _complete_provenance("ADJUSTED-B")
    constraint_provenance = _complete_provenance("ADJUSTED-CONSTRAINT")
    records = [
        (_Line((0, 0), (2000, 0)), "A-WALL-S", face_a_provenance),
        (_Line((250, 200), (2000, 200)), "A-WALL-S", face_b_provenance),
    ]
    constraint_ref = {
        "drawing_identity": constraint_provenance["drawing_identity"],
        "source_occurrence_id": constraint_provenance[
            "source_occurrence_id"],
        "placed_entity_id": constraint_provenance["placed_entity_id"],
        "structural_occurrence_id": constraint_provenance[
            "structural_occurrence_id"],
        "source_segment_id": constraint_provenance["segment_ids"][0],
        "entity_handle": "ADJUSTED-CONSTRAINT",
        "identity_is_complete": True,
        "identity_limitations": [],
    }
    walls = [{
        "id": "adjusted_wall",
        "start": [0.25, 0.1],
        "end": [2.0, 0.1],
        "thickness": 200.0,
        "paired": True,
        "source_segment_ids": [
            face_a_provenance["segment_ids"][0],
            face_b_provenance["segment_ids"][0],
        ],
        "endpoint_adjustments": [{
            "operation": "heal_wall_junctions",
            "endpoint": "start",
            "before": [0.30, 0.1],
            "after": [0.25, 0.1],
            "distance_m": 0.05,
            "constraint_source_segment_ids": [
                constraint_provenance["segment_ids"][0]],
            "constraint_source_refs": [constraint_ref],
        }],
    }]
    original_walls = copy.deepcopy(walls)

    coverage = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0)

    assert coverage["raw_source_review_count"] == 1
    assert coverage["resolved_endpoint_adjustment_count"] == 1
    assert coverage["logical_review_unit_count"] == 0
    unit = coverage["review_units"][0]
    assert unit["status"] == "RESOLVED_ENDPOINT_ADJUSTMENT"
    assert unit["decision_reason"] == "modeled_wall_endpoint_adjustment"
    assert unit["resolution_type"] == "MODELED_ENDPOINT_ADJUSTMENT"
    assert unit["resolution_evidence"]["wall_id"] == "adjusted_wall"
    assert unit["auto_action"] == "NONE"
    assert walls == original_walls


def _source_ref(handle, interval):
    provenance = _complete_provenance(handle)
    return {
        "drawing_identity": provenance["drawing_identity"],
        "source_occurrence_id": provenance["source_occurrence_id"],
        "placed_entity_id": provenance["placed_entity_id"],
        "structural_occurrence_id": provenance[
            "structural_occurrence_id"],
        "source_segment_id": provenance["segment_ids"][0],
        "entity_handle": handle,
        "source_record_index": 0,
        "segment_index": 0,
        "source_interval_m": list(interval),
        "placement_path": copy.deepcopy(provenance["placement_path"]),
        "identity_is_complete": True,
        "identity_limitations": [],
    }


def _l_closure_records():
    return [
        (_Line((0, 0), (100, 0)), "A-WALL",
         _complete_provenance("CLOSURE")),
        (_Line((-1000, 0), (0, 0)), "A-WALL",
         _complete_provenance("COLLINEAR")),
        (_Line((100, 0), (100, 1000)), "A-WALL",
         _complete_provenance("PERPENDICULAR")),
    ]


def _l_closure_walls(*, perpendicular_interval=(0.0, 1.0)):
    return [
        {
            "id": "collinear_wall",
            "start": [-1.0, 0.0],
            "end": [0.0, 0.0],
            "thickness": 100.0,
            "paired": False,
            "source_segment_ids": ["segment_COLLINEAR_0"],
            "source_segment_refs": [
                _source_ref("COLLINEAR", (0.0, 1.0))],
        },
        {
            "id": "perpendicular_wall",
            "start": [-0.2, 0.0],
            "end": [-0.2, 1.0],
            "thickness": 600.0,
            "paired": False,
            "source_segment_ids": ["segment_PERPENDICULAR_0"],
            "source_segment_refs": [
                _source_ref("PERPENDICULAR", perpendicular_interval)],
        },
    ]


def test_unique_l_junction_closure_edge_is_resolved_without_new_geometry():
    walls = _l_closure_walls()
    original_walls = copy.deepcopy(walls)

    coverage = wall_source_coverage(
        _l_closure_records(), walls, 0, 0, 0, 0, 0)

    assert coverage["raw_source_review_count"] == 1
    assert coverage["resolved_l_junction_closure_edge_count"] == 1
    assert coverage["logical_review_unit_count"] == 0
    unit = coverage["review_units"][0]
    assert unit["status"] == "RESOLVED_MODELED_JUNCTION_EDGE"
    assert unit["decision_reason"] == "exact_l_junction_closure_edge"
    assert unit["resolution_type"] == "L_JUNCTION_CLOSURE_EDGE"
    assert unit["resolution_evidence"]["collinear_wall_id"] == (
        "collinear_wall")
    assert unit["resolution_evidence"]["perpendicular_wall_id"] == (
        "perpendicular_wall")
    assert unit["auto_action"] == "NONE"
    assert walls == original_walls


def test_l_junction_closure_requires_exact_interval_touch():
    interval_gap = wall_source_coverage(
        _l_closure_records(),
        _l_closure_walls(perpendicular_interval=(0.1, 1.0)),
        0, 0, 0, 0, 0)
    assert interval_gap["resolved_l_junction_closure_edge_count"] == 0
    assert interval_gap["logical_review_unit_count"] == 1

    opening = wall_source_coverage(
        _l_closure_records(), _l_closure_walls(), 0, 0, 0, 0, 0,
        opening_bridges=[{"start": [0.0, 0.0], "end": [0.1, 0.0]}])
    assert opening["resolved_l_junction_closure_edge_count"] == 1
    assert opening["logical_review_unit_count"] == 0
    assert opening["review_units"][0]["opening_bridge_supported"] is True
