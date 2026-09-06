from copy import deepcopy

from PIL import Image

from scripts.audit_drawing_cleaning import (
    render_short_wall_proposal_overlay,
    render_wall_source_review_overlay,
)


def _candidate(handle, start_x, decision_reason, *, review_bucket=None):
    candidate = {
        "candidate_id": f"semantic_face_{handle}",
        "entity_handle": handle,
        "start": [start_x, 0.0],
        "end": [start_x + 0.1, 0.0],
        "decision_reason": decision_reason,
        "mapped_ratio": 0.0,
        "unmapped_ranges": [{
            "start": [start_x, 0.0],
            "end": [start_x + 0.1, 0.0],
            "length_m": 0.1,
            "opening_bridge_supported": decision_reason ==
            "possible_opening_gap",
        }],
    }
    if review_bucket:
        candidate["review_bucket"] = review_bucket
    return candidate


def test_wall_source_overlay_marks_strict_end_caps_evidence_resolved(
        tmp_path):
    source_coverage = {
        "raw_review_candidate_count": 126,
        "logical_review_unit_count": 100,
        "resolved_strict_topology_end_cap_count": 1,
        "uncovered_source_segments": [
            _candidate("OPENING", 0.0, "possible_opening_gap"),
            _candidate(
                "ENDCAP", 1.0, "strict_topology_end_cap",
                review_bucket="STRICT_TOPOLOGY_END_CAP"),
            _candidate("OMISSION", 2.0, "semantic_wall_face_unmapped"),
        ],
        "partially_mapped_source_segments": [],
    }
    original = deepcopy(source_coverage)
    output = tmp_path / "wall-source-review.png"

    result = render_wall_source_review_overlay([], source_coverage, output)

    assert result["candidate_count"] == 3
    assert result["review_bucket_counts"] == {
        "possible_opening": 1,
        "strict_topology_end_cap": 1,
        "known_non_wall": 0,
        "resolved_profile_edge": 0,
        "possible_omission": 1,
    }
    assert result["strict_topology_end_cap_count"] == 1
    assert result["resolved_strict_topology_end_cap_count"] == 1
    assert result["possible_omission_count"] == 1
    assert result["legend"]["purple"] == (
        "strict topology end cap; evidence resolved; no geometry action")
    assert source_coverage == original
    assert output.stat().st_size > 0
    with Image.open(output) as image:
        assert (186, 104, 255) in set(image.getdata())


def test_wall_source_overlay_recognises_strict_end_cap_decision_reason(
        tmp_path):
    source_coverage = {
        "uncovered_source_segments": [
            _candidate("ENDCAP", 0.0, "strict_topology_end_cap"),
        ],
        "partially_mapped_source_segments": [],
    }

    result = render_wall_source_review_overlay(
        [], source_coverage, tmp_path / "decision-fallback.png")

    assert result["strict_topology_end_cap_count"] == 1
    assert result["possible_omission_count"] == 0


def test_wall_source_overlay_keeps_exact_door_leaf_as_known_non_wall(
        tmp_path):
    candidate = _candidate(
        "DOOR-LEAF", 0.0, "exact_door_leaf_swing_topology",
        review_bucket="EXACT_DOOR_LEAF_SWING")
    candidate["status"] = "REJECTED_BY_RULE"

    result = render_wall_source_review_overlay([], {
        "uncovered_source_segments": [candidate],
        "partially_mapped_source_segments": [],
    }, tmp_path / "known-non-wall.png")

    assert result["known_non_wall_count"] == 1
    assert result["possible_omission_count"] == 0
    assert result["review_bucket_counts"]["known_non_wall"] == 1
    assert result["legend"]["green"].startswith("exact door leaf")


def test_wall_source_overlay_separates_approved_profile_end_cap(
        tmp_path):
    candidate = _candidate(
        "PROFILE-CAP", 0.0, "approved_closed_wall_strip_end_cap",
        review_bucket="RESOLVED_PROFILE_EDGE")
    candidate["status"] = "RESOLVED_BY_PROFILE"

    result = render_wall_source_review_overlay([], {
        "uncovered_source_segments": [candidate],
        "partially_mapped_source_segments": [],
    }, tmp_path / "resolved-profile-cap.png")

    assert result["resolved_profile_edge_count"] == 1
    assert result["possible_omission_count"] == 0
    assert result["legend"]["blue"].startswith("approved closed wall-strip")


def test_wall_source_overlay_reports_resolved_junction_ranges(tmp_path):
    candidate = _candidate(
        "JUNCTION", 0.0, "semantic_wall_face_partially_mapped")
    candidate.update({
        "range_review_units": [{
            "review_unit_id": "range_1",
            "range_index": 0,
            "status": "RESOLVED_MODELED_JUNCTION_EDGE",
            "decision_reason": "exact_modeled_wall_junction_edge",
            "review_bucket": "RESOLVED_MODELED_JUNCTION_EDGE",
        }],
    })
    review_unit = {
        "review_unit_id": "range_1",
        "status": "RESOLVED_MODELED_JUNCTION_EDGE",
        "decision_reason": "exact_modeled_wall_junction_edge",
        "review_bucket": "RESOLVED_MODELED_JUNCTION_EDGE",
    }

    result = render_wall_source_review_overlay([], {
        "uncovered_source_segments": [],
        "partially_mapped_source_segments": [candidate],
        "review_units": [review_unit],
        "logical_review_unit_count": 0,
    }, tmp_path / "resolved-junction.png")

    assert result["candidate_count"] == 1
    assert result["resolved_modeled_junction_edge_count"] == 1
    assert result["needs_review_unit_count"] == 0
    with Image.open(result["path"]) as image:
        assert (52, 190, 184) in set(image.getdata())


def test_short_wall_proposal_overlay_separates_review_actions(tmp_path):
    strict = {
        "proposal_id": "strict",
        "status": "PROPOSED_SHORT_WALL",
        "approval_status": "ELIGIBLE_FOR_STRICT_APPROVAL",
        "source_entity_handles": ["FACE-A", "FACE-B"],
        "start": [0.0, 0.05],
        "end": [0.4, 0.05],
        "length_m": 0.4,
        "thickness_mm": 100.0,
        "endpoint_support": [
            {"point": [0.0, 0.05], "status": "EXACT_STRICT_CAP",
             "face_points": [[0.0, 0.0], [0.0, 0.1]]},
            {"point": [0.4, 0.05], "status": "EXACT_STRICT_CAP",
             "face_points": [[0.4, 0.0], [0.4, 0.1]]},
        ],
        "blockers": [],
    }
    upgrade = {
        "proposal_id": "upgrade",
        "status": "PROPOSED_WALL_UPGRADE",
        "approval_status": "NEEDS_REVIEW",
        "source_entity_handles": ["OLD", "OPPOSING"],
        "start": [2.05, 0.0],
        "end": [2.05, 0.5],
        "length_m": 0.5,
        "thickness_mm": 100.0,
        "endpoint_support": [
            {"point": [2.05, 0.0], "status": "EXACT_SOURCE_CAP",
             "face_points": [[2.0, 0.0], [2.1, 0.0]]},
            {"point": [2.05, 0.5],
             "status": "EXACT_PAIRED_WALL_JUNCTION",
             "face_points": [[2.0, 0.5], [2.1, 0.5]]},
        ],
        "replacement_evidence": {"existing_wall_id": "old_wall"},
        "blockers": ["atomic_single_wall_replacement_not_implemented"],
    }
    ordinary = {
        **deepcopy(strict),
        "proposal_id": "ordinary",
        "approval_status": "NEEDS_REVIEW",
        "source_entity_handles": ["ORD-A", "ORD-B"],
        "start": [1.0, 0.05],
        "end": [1.4, 0.05],
        "endpoint_support": [
            {"point": [1.0, 0.05], "status": "EXACT_STRICT_CAP",
             "face_points": [[1.0, 0.0], [1.0, 0.1]]},
            {"point": [1.4, 0.05],
             "status": "EXACT_PAIRED_WALL_JUNCTION",
             "face_points": [[1.4, 0.0], [1.4, 0.1]]},
        ],
    }
    ambiguous = {
        **deepcopy(ordinary),
        "proposal_id": "ambiguous",
        "source_entity_handles": ["AMB-A", "AMB-B"],
        "start": [3.0, 0.05],
        "end": [3.4, 0.05],
        "endpoint_support": [
            {"point": [3.0, 0.05], "status": "EXACT_STRICT_CAP",
             "face_points": [[3.0, 0.0], [3.0, 0.1]]},
            {"point": [3.4, 0.05], "status": "AMBIGUOUS_SUPPORT",
             "face_points": [[3.4, 0.0], [3.4, 0.1]]},
        ],
        "blockers": ["endpoint_support_ambiguous"],
    }
    single = {
        **deepcopy(ordinary),
        "proposal_id": "single",
        "source_entity_handles": ["ONE-A", "ONE-B"],
        "start": [5.0, 0.05],
        "end": [5.4, 0.05],
        "endpoint_support": [
            {"point": [5.0, 0.05], "status": "EXACT_STRICT_CAP",
             "face_points": [[5.0, 0.0], [5.0, 0.1]]},
            {"point": [5.4, 0.05], "status": "SINGLE_WALL_JUNCTION",
             "face_points": [[5.4, 0.0], [5.4, 0.1]]},
        ],
        "blockers": ["endpoint_supported_by_single_wall_only"],
    }
    insufficient = {
        "proposal_id": "insufficient",
        "status": "INSUFFICIENT_SUPPORT",
        "approval_status": "NEEDS_REVIEW",
        "source_entity_handles": ["OPEN-A", "OPEN-B"],
        "start": [4.0, 0.05],
        "end": [4.4, 0.05],
        "length_m": 0.4,
        "thickness_mm": 100.0,
        "endpoint_support": [
            {"point": [4.0, 0.05], "status": "UNSUPPORTED",
             "face_points": [[4.0, 0.0], [4.0, 0.1]]},
            {"point": [4.4, 0.05], "status": "UNSUPPORTED",
             "face_points": [[4.4, 0.0], [4.4, 0.1]]},
        ],
        "blockers": ["endpoint_unsupported"],
    }
    audit = {
        "proposals": [strict, ordinary, ambiguous, single, upgrade],
        "insufficient_support_candidates": [insufficient],
        "strict_approval_eligible_count": 1,
        "single_wall_upgrade_proposal_count": 1,
    }
    walls = [{
        "id": "old_wall", "start": [2.0, 0.1], "end": [2.0, 0.6],
        "thickness": 100.0, "paired": False,
    }]
    original_audit = deepcopy(audit)
    original_walls = deepcopy(walls)
    output = tmp_path / "short-wall-proposals.png"

    result = render_short_wall_proposal_overlay(walls, audit, output)

    assert result["proposal_count"] == 5
    assert result["strict_candidate_count"] == 1
    assert result["ordinary_proposal_count"] == 1
    assert result["ambiguous_support_count"] == 1
    assert result["single_wall_support_count"] == 1
    assert result["single_wall_upgrade_count"] == 1
    assert result["insufficient_support_count"] == 1
    assert result["auto_model_action_count"] == 0
    assert audit == original_audit
    assert walls == original_walls
    with Image.open(output) as image:
        colours = set(image.getdata())
        assert (72, 205, 132) in colours
        assert (70, 150, 245) in colours
        assert (244, 164, 66) in colours
        assert (244, 210, 82) in colours
        assert (190, 105, 245) in colours
        assert (246, 78, 74) in colours
