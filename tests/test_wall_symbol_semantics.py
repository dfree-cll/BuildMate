from copy import deepcopy

import ezdxf
import pytest

from backend.engines.wall_symbol_semantics import (
    classify_door_leaf_swing_profiles,
)
from scripts.bim_pipeline import pipeline_std


def _symbol_plan(arcs=((0.0, 0.0, 762.5, 0.0, 90.0),)):
    document = ezdxf.new()
    symbol = document.blocks.new("MISLAYERED_DOOR_SYMBOL")
    symbol.add_lwpolyline(
        [(0.0, 0.0), (762.5, 0.0), (762.5, 40.0), (0.0, 40.0)],
        close=True, dxfattribs={"layer": "A-PART-S"})
    for center_x, center_y, radius, start_angle, end_angle in arcs:
        symbol.add_arc(
            (center_x, center_y), radius, start_angle, end_angle,
            dxfattribs={"layer": "A-PART-S"})
    plan = document.blocks.new("B1层平面图")
    plan.add_blockref("MISLAYERED_DOOR_SYMBOL", (0.0, 0.0))
    document.modelspace().add_blockref("B1层平面图", (0.0, 0.0))
    return document


def _step0_case(arcs=((0.0, 0.0, 762.5, 0.0, 90.0),)):
    cleaned = pipeline_std.step0_clean(_symbol_plan(arcs))
    profile_record = next(
        record for record in cleaned["placed_wall_source_records"]
        if record[0].dxftype() == "LWPOLYLINE")
    provenance = profile_record[2]
    profile = {
        "candidate_id": "closed_profile_test",
        "entity_handle": provenance["source_entity_handle"],
        "drawing_identity": provenance["drawing_identity"],
        "source_occurrence_id": provenance["source_occurrence_id"],
        "placed_entity_id": provenance["placed_entity_id"],
        "structural_occurrence_id": provenance["structural_occurrence_id"],
        "source_segment_ids": list(provenance["segment_ids"]),
        "identity_is_complete": True,
        "identity_limitations": [],
        "status": "NEEDS_REVIEW",
        "decision_reason": "thickness_outside_80_600mm",
        "points": [
            [0.0, 0.0], [0.7625, 0.0],
            [0.7625, 0.04], [0.0, 0.04],
        ],
        "centerline": {"start": [0.0, 0.02],
                       "end": [0.7625, 0.02]},
        "length_m": 0.7625,
        "thickness_mm": 40.0,
    }
    audit = {
        "status": "REVIEW",
        "candidate_count": 1,
        "approved_by_rule_count": 0,
        "needs_review_count": 1,
        "candidates": [profile],
    }
    return cleaned, audit


def _s1():
    return {"origin": [0.0, 0.0], "rot_deg": 0.0,
            "gcx": 0.0, "gcy": 0.0}


def test_step0_keeps_wall_layer_arcs_as_identity_complete_symbol_evidence():
    document = _symbol_plan()
    cleaned = pipeline_std.step0_clean(document)
    repeated = pipeline_std.step0_clean(document)

    assert len(cleaned["wall_symbol_source_records"]) == 1
    arc, layer, provenance = cleaned["wall_symbol_source_records"][0]
    assert arc.dxftype() == "ARC"
    assert layer == "A-PART-S"
    assert len(provenance["segment_ids"]) == 1
    assert provenance["segment_id"] == provenance["segment_ids"][0]
    assert provenance["drawing_identity"]
    assert provenance["source_occurrence_id"]
    assert provenance["placed_entity_id"]
    assert provenance["structural_occurrence_id"]
    assert repeated["wall_symbol_source_records"][0][2]["segment_ids"] == (
        provenance["segment_ids"])
    assert all(record[0].dxftype() != "ARC"
               for record in cleaned["placed_wall_source_records"])
    assert all(record[0].dxftype() != "ARC"
               for record in cleaned["wall_source_records"])
    assert all(record[0].dxftype() != "ARC"
               for record in cleaned["vector_source_records"])


def test_exact_unique_leaf_swing_rejects_wall_without_mutating_input():
    cleaned, audit = _step0_case()
    original = deepcopy(audit)

    enriched, statistics = classify_door_leaf_swing_profiles(
        audit, cleaned["wall_symbol_source_records"], _s1(), [])

    assert audit == original
    profile = enriched["candidates"][0]
    assert profile["status"] == "REJECTED_BY_RULE"
    assert profile["decision_reason"] == "exact_door_leaf_swing_topology"
    assert profile["prior_status"] == "NEEDS_REVIEW"
    assert profile["prior_decision_reason"] == (
        "thickness_outside_80_600mm")
    assert profile["auto_action"] == "NONE"
    assert profile["wall_semantics_confirmed"] is False
    evidence = profile["door_leaf_swing_evidence"]
    assert evidence["arc_radius_m"] == 0.7625
    assert evidence["arc_sweep_deg"] == 90.0
    assert evidence["hinge_error_m"] == 0.0
    assert evidence["free_endpoint_error_m"] == 0.0
    assert evidence["arc_source_segment_id"] == (
        cleaned["wall_symbol_source_records"][0][2]["segment_ids"][0])
    assert statistics["status"] == "COMPLETE"
    assert statistics["eligible_profile_count"] == 1
    assert statistics["matched_profile_count"] == 1
    assert statistics["missing_arc_match_count"] == 0
    assert statistics["ambiguous_arc_match_count"] == 0
    assert enriched["needs_review_count"] == 0
    assert enriched["rejected_by_rule_count"] == 1
    assert enriched["status"] == "PASS"


@pytest.mark.parametrize("arc", [
    (0.0, 0.0, 700.0, 0.0, 90.0),
    (0.0, 0.0, 762.5, 0.0, 80.0),
    (10.0, 0.0, 762.5, 0.0, 90.0),
    (0.0, 0.0, 762.5, 10.0, 100.0),
])
def test_nearby_non_topological_arcs_stay_in_review(arc):
    cleaned, audit = _step0_case((arc,))

    enriched, statistics = classify_door_leaf_swing_profiles(
        audit, cleaned["wall_symbol_source_records"], _s1(), [])

    assert enriched["candidates"][0]["status"] == "NEEDS_REVIEW"
    assert statistics["matched_profile_count"] == 0
    assert statistics["missing_arc_match_count"] == 1


def test_duplicate_exact_arcs_are_ambiguous_and_stay_in_review():
    exact = (0.0, 0.0, 762.5, 0.0, 90.0)
    cleaned, audit = _step0_case((exact, exact))

    enriched, statistics = classify_door_leaf_swing_profiles(
        audit, cleaned["wall_symbol_source_records"], _s1(), [])

    assert enriched["candidates"][0]["status"] == "NEEDS_REVIEW"
    assert statistics["matched_profile_count"] == 0
    assert statistics["ambiguous_arc_match_count"] == 1


def test_cross_occurrence_arc_identity_is_not_accepted():
    cleaned, audit = _step0_case()
    entity, layer, provenance = cleaned["wall_symbol_source_records"][0]
    other_provenance = dict(provenance)
    other_provenance["structural_occurrence_id"] = "other_occurrence"

    enriched, statistics = classify_door_leaf_swing_profiles(
        audit, [(entity, layer, other_provenance)], _s1(), [])

    assert enriched["candidates"][0]["status"] == "NEEDS_REVIEW"
    assert statistics["missing_arc_match_count"] == 1


def test_two_ended_wall_network_support_blocks_symbol_rejection():
    cleaned, audit = _step0_case()
    walls = [
        {"start": [0.0, -1.0], "end": [0.0, 1.0]},
        {"start": [0.7625, -1.0], "end": [0.7625, 1.0]},
    ]

    enriched, statistics = classify_door_leaf_swing_profiles(
        audit, cleaned["wall_symbol_source_records"], _s1(), walls)

    assert enriched["candidates"][0]["status"] == "NEEDS_REVIEW"
    assert statistics["matched_profile_count"] == 0
    assert statistics["two_ended_wall_network_blocked_count"] == 1


def test_incomplete_profile_or_arc_identity_fails_closed():
    cleaned, audit = _step0_case()
    incomplete_profile = deepcopy(audit)
    incomplete_profile["candidates"][0]["identity_is_complete"] = False

    enriched, statistics = classify_door_leaf_swing_profiles(
        incomplete_profile, cleaned["wall_symbol_source_records"], _s1(), [])

    assert enriched["candidates"][0]["status"] == "NEEDS_REVIEW"
    assert statistics["identity_incomplete_profile_count"] == 1

    entity, layer, provenance = cleaned["wall_symbol_source_records"][0]
    incomplete_arc = dict(provenance)
    incomplete_arc["segment_ids"] = []
    enriched, statistics = classify_door_leaf_swing_profiles(
        audit, [(entity, layer, incomplete_arc)], _s1(), [])

    assert enriched["candidates"][0]["status"] == "NEEDS_REVIEW"
    assert statistics["invalid_arc_record_count"] == 1
    assert statistics["missing_arc_match_count"] == 1
