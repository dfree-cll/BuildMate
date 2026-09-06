import copy

from backend.engines.wall_geometry import wall_source_coverage
from tests.fixtures.dxf_entities import Line as _Line


def _provenance(handle, *, occurrence="structural_MAIN"):
    return {
        "drawing_identity": "sha256:short-wall-pair-audit",
        "source_entity_handle": handle,
        "source_occurrence_id": f"source_{handle}",
        "placed_entity_id": f"placed_{handle}",
        "structural_occurrence_id": occurrence,
        "segment_ids": [f"segment_{handle}_0"],
        "placement_path": [{
            "name": "B1",
            "block_name": "B1",
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


def _record(handle, start, end, *, occurrence="structural_MAIN",
            layer="B1_ROOM$0$A-PART-S"):
    return (
        _Line(start, end),
        layer,
        _provenance(handle, occurrence=occurrence),
    )


def _source_ref(handle, interval):
    provenance = _provenance(handle)
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


_EMPTY_PROFILE_AUDIT = {"candidate_count": 0, "candidates": []}


def test_short_parallel_faces_are_one_stable_evidence_group_only():
    records = [
        _record("FACE-A", (0, 0), (400, 0)),
        _record("FACE-B", (0, 100), (400, 100)),
    ]

    first = wall_source_coverage(records, [], 0, 0, 0, 0, 0)
    second = wall_source_coverage(records, [], 0, 0, 0, 0, 0)

    audit = first["short_wall_pair_audit"]
    assert audit == second["short_wall_pair_audit"]
    assert audit["status"] == "REVIEW"
    assert audit["eligible_review_unit_count"] == 2
    assert audit["raw_pair_count"] == 1
    assert audit["pair_group_count"] == 1
    assert audit["approved_proposal_count"] == 0
    group = audit["groups"][0]
    assert group["classification"] == "DOUBLE_FACE_EVIDENCE"
    assert group["thickness_mm"] == 100.0
    assert group["effective_overlap_length_m"] == 0.4
    assert group["overlap_run_count"] == 1
    assert group["inter_run_gaps"] == []
    assert group["model_geometry_created"] is False
    assert group["auto_action"] == "NONE"
    assert all(unit["short_wall_pair_group_ids"] == [
        group["pair_group_id"]] for unit in first["review_units"])


def test_collinear_fragments_roll_up_but_gap_is_not_filled():
    records = [
        _record("CONTINUOUS", (0, 0), (500, 0)),
        _record("FRAGMENT-A", (0, 100), (200, 100)),
        _record("FRAGMENT-B", (300, 100), (500, 100)),
    ]

    coverage = wall_source_coverage(records, [], 0, 0, 0, 0, 0)

    audit = coverage["short_wall_pair_audit"]
    assert audit["eligible_review_unit_count"] == 3
    assert audit["raw_pair_count"] == 2
    assert audit["pair_group_count"] == 1
    group = audit["groups"][0]
    assert group["raw_pair_count"] == 2
    assert group["overlap_run_count"] == 2
    assert [run["length_m"] for run in group["overlap_runs"]] == [
        0.2, 0.2]
    assert group["effective_overlap_length_m"] == 0.4
    assert group["face_extent_hull"]["length_m"] == 0.5
    assert group["inter_run_gaps"] == [{
        "after_run_index": 0,
        "start": [0.2, 0.05],
        "end": [0.3, 0.05],
        "length_m": 0.1,
        "classification": "UNRESOLVED_BETWEEN_OVERLAP_RUNS",
    }]
    assert group["requires_gap_classification"] is True
    assert audit["rule"]["fill_face_extent_hull"] is False


def test_inferred_opening_does_not_suppress_exact_pair_audit():
    opening_records = [
        _record("OPEN-A", (0, 0), (400, 0)),
        _record("OPEN-B", (0, 100), (400, 100)),
    ]
    opening = wall_source_coverage(
        opening_records, [], 0, 0, 0, 0, 0,
        opening_bridges=[{"start": [0.0, 0.05], "end": [0.4, 0.05]}],
    )
    assert opening["short_wall_pair_audit"]["pair_group_count"] == 1
    assert all(unit["review_bucket"] == "POSSIBLE_OMISSION"
               for unit in opening["review_units"])
    assert all(unit["opening_bridge_supported"] is True
               for unit in opening["review_units"])


def test_occurrence_mismatch_and_subminimum_ranges_do_not_pair():
    mismatch = wall_source_coverage([
        _record("OTHER-A", (0, 0), (400, 0), occurrence="structural_A"),
        _record("OTHER-B", (0, 100), (400, 100), occurrence="structural_B"),
    ], [], 0, 0, 0, 0, 0)
    assert mismatch["short_wall_pair_audit"]["raw_pair_count"] == 0

    too_short = wall_source_coverage([
        _record("SHORT-A", (0, 0), (149, 0)),
        _record("SHORT-B", (0, 100), (149, 100)),
    ], [], 0, 0, 0, 0, 0)
    assert too_short["short_wall_pair_audit"][
        "eligible_review_unit_count"] == 0


def test_invalid_external_wall_is_ignored_by_junction_audit():
    records = [
        _record("FACE-A", (0, 0), (400, 0)),
        _record("FACE-B", (0, 100), (400, 100)),
    ]
    walls = [
        {"id": "missing_end", "start": [0.0, 0.0]},
        {"id": "degenerate", "start": [0.0, 0.0], "end": [0.0, 0.0]},
    ]
    original = copy.deepcopy(walls)

    coverage = wall_source_coverage(records, walls, 0, 0, 0, 0, 0)

    assert coverage["resolved_junction_edge_count"] == 0
    assert coverage["l_junction_closure_edge_rule"]["auto_action"] == "NONE"
    assert coverage["short_wall_pair_audit"]["pair_group_count"] == 1
    assert walls == original


def test_two_exact_caps_make_one_strict_but_non_executable_proposal():
    records = [
        _record("FACE-A", (0, 0), (400, 0)),
        _record("FACE-B", (0, 100), (400, 100)),
        _record("CAP-A", (0, 0), (0, 100)),
        _record("CAP-B", (400, 0), (400, 100)),
    ]

    coverage = wall_source_coverage(
        records, [], 0, 0, 0, 0, 0,
        closed_profile_audit=copy.deepcopy(_EMPTY_PROFILE_AUDIT),
    )

    audit = coverage["short_wall_proposal_audit"]
    assert audit["proposed_short_wall_count"] == 1
    assert audit["strict_approval_eligible_count"] == 1
    assert audit["insufficient_support_count"] == 0
    proposal = audit["proposals"][0]
    assert proposal["status"] == "PROPOSED_SHORT_WALL"
    assert proposal["approval_status"] == "ELIGIBLE_FOR_STRICT_APPROVAL"
    assert [item["status"] for item in proposal["endpoint_support"]] == [
        "EXACT_STRICT_CAP", "EXACT_STRICT_CAP"]
    assert proposal["start"] == [0.0, 0.05]
    assert proposal["end"] == [0.4, 0.05]
    assert proposal["missing_material_source_segment_ids"] == []
    assert {item["entity_handle"] for item in
            proposal["material_source_segment_refs"]} == {
        "FACE-A", "FACE-B"}
    assert all(item["source_interval_m"] == [0.0, 0.4]
               for item in proposal["material_source_segment_refs"])
    assert len(proposal["integrity_sha256"]) == 64
    assert proposal["model_geometry_created"] is False
    assert proposal["auto_action"] == "NONE"


def test_one_open_end_stays_as_insufficient_evidence_not_a_proposal():
    records = [
        _record("FACE-A", (0, 0), (400, 0)),
        _record("FACE-B", (0, 100), (400, 100)),
        _record("CAP-A", (0, 0), (0, 100)),
    ]

    coverage = wall_source_coverage(
        records, [], 0, 0, 0, 0, 0,
        closed_profile_audit=copy.deepcopy(_EMPTY_PROFILE_AUDIT),
    )

    audit = coverage["short_wall_proposal_audit"]
    assert audit["proposed_short_wall_count"] == 0
    assert audit["insufficient_support_count"] == 1
    candidate = audit["insufficient_support_candidates"][0]
    assert candidate["status"] == "INSUFFICIENT_SUPPORT"
    assert "UNSUPPORTED" in [
        item["status"] for item in candidate["endpoint_support"]]
    assert candidate["model_geometry_created"] is False


def test_exact_paired_junction_merges_one_face_gap_without_filling_blindly():
    records = [
        _record("CONTINUOUS", (0, 0), (500, 0)),
        _record("FRAGMENT-A", (0, 100), (200, 100)),
        _record("FRAGMENT-B", (300, 100), (500, 100)),
        _record("CAP-A", (0, 0), (0, 100)),
        _record("CAP-B", (500, 0), (500, 100)),
        _record("SUPPORT-A", (200, 100), (200, 800)),
        _record("SUPPORT-B", (300, 100), (300, 800)),
    ]
    support_wall = {
        "id": "support",
        "start": [0.25, 0.1],
        "end": [0.25, 0.8],
        "thickness": 100.0,
        "paired": True,
        "source_segment_ids": [
            "segment_SUPPORT-A_0", "segment_SUPPORT-B_0"],
        "source_segment_refs": [
            _source_ref("SUPPORT-A", (0.0, 0.7)),
            _source_ref("SUPPORT-B", (0.0, 0.7)),
        ],
    }
    original_wall = copy.deepcopy(support_wall)

    coverage = wall_source_coverage(
        records, [support_wall], 0, 0, 0, 0, 0,
        closed_profile_audit=copy.deepcopy(_EMPTY_PROFILE_AUDIT),
    )

    audit = coverage["short_wall_proposal_audit"]
    assert audit["proposed_short_wall_count"] == 1
    assert audit["strict_approval_eligible_count"] == 1
    proposal = audit["proposals"][0]
    assert proposal["start"] == [0.0, 0.05]
    assert proposal["end"] == [0.5, 0.05]
    assert len(proposal["overlap_runs"]) == 2
    assert proposal["junction_handoffs"][0][
        "support_wall"]["wall_id"] == "support"
    assert support_wall == original_wall


def test_fragmented_support_faces_are_two_logical_junction_baselines():
    records = [
        _record("CONTINUOUS", (0, 0), (500, 0)),
        _record("FRAGMENT-A", (0, 100), (200, 100)),
        _record("FRAGMENT-B", (300, 100), (500, 100)),
        _record("CAP-A", (0, 0), (0, 100)),
        _record("CAP-B", (500, 0), (500, 100)),
        _record("SUPPORT-A1", (200, 100), (200, 500)),
        _record("SUPPORT-A2", (200, 100), (200, 800)),
        _record("SUPPORT-B1", (300, 100), (300, 500)),
        _record("SUPPORT-B2", (300, 100), (300, 800)),
    ]
    support_wall = {
        "id": "fragmented_support",
        "start": [0.25, 0.1],
        "end": [0.25, 0.8],
        "thickness": 100.0,
        "paired": True,
        "source_segment_ids": [
            "segment_SUPPORT-A1_0", "segment_SUPPORT-A2_0",
            "segment_SUPPORT-B1_0", "segment_SUPPORT-B2_0",
        ],
        "source_segment_refs": [
            _source_ref("SUPPORT-A1", (0.0, 0.4)),
            _source_ref("SUPPORT-A2", (0.0, 0.7)),
            _source_ref("SUPPORT-B1", (0.0, 0.4)),
            _source_ref("SUPPORT-B2", (0.0, 0.7)),
        ],
    }

    coverage = wall_source_coverage(
        records, [support_wall], 0, 0, 0, 0, 0,
        closed_profile_audit=copy.deepcopy(_EMPTY_PROFILE_AUDIT),
    )

    audit = coverage["short_wall_proposal_audit"]
    assert audit["proposed_short_wall_count"] == 1
    assert audit["insufficient_support_count"] == 0
    proposal = audit["proposals"][0]
    assert proposal["blockers"] == []
    assert proposal["junction_handoffs"][0]["support_wall"]["wall_id"] == (
        "fragmented_support")
    assert proposal["junction_handoffs"][0]["support_wall"][
        "source_segment_ids"] == [
            "segment_SUPPORT-A1_0", "segment_SUPPORT-A2_0",
            "segment_SUPPORT-B1_0", "segment_SUPPORT-B2_0",
        ]


def test_partial_opposing_face_creates_atomic_single_wall_upgrade_proposal():
    records = [
        _record("OLD-FACE", (0, 100), (0, 600)),
        _record("OPPOSING", (100, 0), (100, 500)),
        _record("BRIDGE", (0, 0), (0, 100)),
        _record("BOTTOM-CAP", (0, 0), (100, 0)),
        _record("SUPPORT-A", (100, 500), (1000, 500)),
        _record("SUPPORT-B", (0, 600), (1000, 600)),
    ]
    walls = [
        {
            "id": "old_single",
            "start": [0.0, 0.1],
            "end": [0.0, 0.6],
            "thickness": 100.0,
            "paired": False,
            "source_segment_ids": ["segment_OLD-FACE_0"],
            "source_segment_refs": [
                _source_ref("OLD-FACE", (0.0, 0.5))],
        },
        {
            "id": "top_support",
            "start": [0.05, 0.55],
            "end": [1.0, 0.55],
            "thickness": 100.0,
            "paired": True,
            "source_segment_ids": [
                "segment_SUPPORT-A_0", "segment_SUPPORT-B_0"],
            "source_segment_refs": [
                _source_ref("SUPPORT-A", (0.0, 0.9)),
                _source_ref("SUPPORT-B", (0.0, 1.0)),
            ],
        },
    ]
    original_walls = copy.deepcopy(walls)

    coverage = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0,
        closed_profile_audit=copy.deepcopy(_EMPTY_PROFILE_AUDIT),
    )

    audit = coverage["short_wall_proposal_audit"]
    assert audit["single_wall_upgrade_proposal_count"] == 1
    upgrades = [item for item in audit["proposals"]
                if item["status"] == "PROPOSED_WALL_UPGRADE"]
    assert len(upgrades) == 1
    proposal = upgrades[0]
    assert proposal["start"] == [0.05, 0.0]
    assert proposal["end"] == [0.05, 0.5]
    assert proposal["replacement_evidence"]["existing_wall_id"] == (
        "old_single")
    assert proposal["replacement_evidence"]["required_action"] == (
        "ATOMIC_REPLACE_SINGLE_WALL")
    assert proposal["replacement_evidence"]["existing_wall_geometry"] == {
        "start": [0.0, 0.1], "end": [0.0, 0.6],
        "thickness_mm": 100.0, "paired": False,
    }
    assert len(proposal["replacement_evidence"][
        "expected_existing_wall_fingerprint"]) == 64
    assert proposal["missing_material_source_segment_ids"] == []
    assert {item["entity_handle"] for item in
            proposal["material_source_segment_refs"]} == {
        "OLD-FACE", "OPPOSING", "BRIDGE"}
    assert [item["status"] for item in proposal["endpoint_support"]] == [
        "EXACT_SOURCE_CAP", "EXACT_PAIRED_WALL_JUNCTION"]
    assert proposal["blockers"] == []
    assert proposal["approval_status"] == "NEEDS_REVIEW"
    assert proposal["model_geometry_created"] is False
    assert proposal["auto_action"] == "NONE"
    assert walls == original_walls


def test_three_parallel_faces_are_explicitly_ambiguous():
    coverage = wall_source_coverage([
        _record("FACE-A", (0, 0), (400, 0)),
        _record("FACE-B", (0, 100), (400, 100)),
        _record("FACE-C", (0, 200), (400, 200)),
    ], [], 0, 0, 0, 0, 0)

    audit = coverage["short_wall_pair_audit"]
    assert audit["raw_pair_count"] == 3
    assert audit["pair_group_count"] == 3
    assert audit["ambiguous_review_unit_count"] == 3
    assert all(group["pairing_ambiguous"] for group in audit["groups"])
    assert all(group["pairing_ambiguity_reason"] ==
               "source_face_participates_in_multiple_pair_groups"
               for group in audit["groups"])


def test_same_wall_group_can_pair_across_leaf_layers_but_is_downgraded():
    coverage = wall_source_coverage([
        _record("FACE-A", (0, 0), (400, 0),
                layer="B1_ROOM$0$A-PART-S"),
        _record("FACE-B", (0, 100), (400, 100),
                layer="B1_ROOM$0$A-WALL"),
    ], [], 0, 0, 0, 0, 0)

    group = coverage["short_wall_pair_audit"]["groups"][0]
    assert group["wall_group"] == "A"
    assert group["leaf_layers"] == ["A-PART-S", "A-WALL"]
    assert group["semantic_layer_mismatch"] is True
