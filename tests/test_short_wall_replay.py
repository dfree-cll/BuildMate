import copy

import pytest

from backend.engines.short_wall_approval import (
    apply_short_wall_review_decision,
    proposal_integrity_sha256,
    review_ir_integrity_sha256,
    wall_precondition_sha256,
)
from backend.engines.short_wall_replay import (
    bind_short_wall_gate_base,
    replay_approved_short_wall_decisions,
    replay_approved_short_wall_bundle,
    short_wall_gate_base_sha256,
)


SOURCE_SHA256 = "c" * 64
DRAWING_IDENTITY = f"sha256:{SOURCE_SHA256}"
OCCURRENCE_ID = "architectural_occurrence_B01"
PLACEMENT_PATH = [{
    "block_name": "B01 plan",
    "insert_handle": "INSERT-B01",
    "array_index": None,
    "cumulative_transform": {
        "affine_2d": [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
    },
}]


def _reference(segment_id, handle, interval=(0.0, 0.4)):
    return {
        "drawing_identity": DRAWING_IDENTITY,
        "source_occurrence_id": f"source_{handle}",
        "placed_entity_id": f"placed_{handle}",
        "structural_occurrence_id": OCCURRENCE_ID,
        "source_segment_id": segment_id,
        "entity_handle": handle,
        "source_record_index": 1,
        "segment_index": 0,
        "placement_path": copy.deepcopy(PLACEMENT_PATH),
        "identity_is_complete": True,
        "identity_limitations": [],
        "source_interval_m": list(interval),
    }


def _review_unit(reference, unit_id):
    low, high = reference["source_interval_m"]
    return {
        **copy.deepcopy(reference),
        "review_unit_id": unit_id,
        "source_start": [0.0, 0.0],
        "source_end": [1.0, 0.0],
        "source_axis_start": [0.0, 0.0],
        "source_axis_end": [1.0, 0.0],
        "start": [float(low), 0.0],
        "end": [float(high), 0.0],
    }


def _strict_proposal(*, proposal_id="short_wall_proposal_strict"):
    references = [
        _reference("segment_a", "FACE-A"),
        _reference("segment_b", "FACE-B"),
    ]
    source_ids = [reference["source_segment_id"]
                  for reference in references]
    proposal = {
        "proposal_id": proposal_id,
        "pair_group_id": "pair_group_strict",
        "status": "PROPOSED_SHORT_WALL",
        "approval_status": "ELIGIBLE_FOR_STRICT_APPROVAL",
        "geometry_source": "DXF_VECTOR",
        "drawing_identity": DRAWING_IDENTITY,
        "structural_occurrence_id": OCCURRENCE_ID,
        "start": [0.0, 0.05],
        "end": [0.4, 0.05],
        "length_m": 0.4,
        "thickness_mm": 100.0,
        "source_review_unit_ids": ["unit_a", "unit_b"],
        "source_segment_ids": source_ids,
        "material_source_segment_refs": copy.deepcopy(references),
        "missing_material_source_segment_ids": [],
        "source_entity_handles": ["FACE-A", "FACE-B"],
        "raw_pair_ids": ["raw_pair_1"],
        "overlap_runs": [{
            "run_index": 0,
            "start": [0.0, 0.05],
            "end": [0.4, 0.05],
            "length_m": 0.4,
            "raw_pair_ids": ["raw_pair_1"],
        }],
        "junction_handoffs": [],
        "endpoint_support": [
            {
                "endpoint_index": index,
                "point": point,
                "face_points": [],
                "status": "EXACT_STRICT_CAP",
                "strict_cap_matches": [{
                    "review_unit_id": f"cap_{index}",
                    "entity_handle": f"CAP-{index}",
                    "source_segment_id": f"cap_segment_{index}",
                    "support_source_segment_ids": source_ids,
                    "endpoint_error_m": 0.0,
                }],
                "paired_wall_matches": [],
                "single_wall_matches": [],
            }
            for index, point in enumerate(([0.0, 0.05], [0.4, 0.05]))
        ],
        "blockers": [],
        "model_geometry_created": False,
        "auto_action": "NONE",
    }
    proposal["integrity_sha256"] = proposal_integrity_sha256(proposal)
    return proposal


def _review_ir(proposal, walls=None):
    units = [
        _review_unit(reference, unit_id)
        for reference, unit_id in zip(
            proposal["material_source_segment_refs"],
            proposal["source_review_unit_ids"])
    ]
    return {
        "schema_version": "buildmate.review-ir/1.0",
        "artifact_role": "REVIEW_ONLY",
        "artifact_status": "BLOCKED",
        "source": {"path": "hotel_b01.dxf", "sha256": SOURCE_SHA256},
        "quality_gate": {
            "allow_modeling": False,
            "structure": {"status": "PASS"},
            "architecture": {"status": "FAIL"},
        },
        "geometry": {"architectural_walls": copy.deepcopy(walls or [])},
        "review_candidates": {
            "short_wall_proposals": {"proposals": [proposal]},
            "semantic_wall_source_units": units,
        },
    }


def _decision(review_ir, proposal):
    return {
        "decision_id": "decision-strict-1",
        "decision": "approved",
        "operator": "reviewer-1",
        "comment": "strict vector evidence confirmed",
        "proposal_id": proposal["proposal_id"],
        "proposal_integrity_sha256": proposal["integrity_sha256"],
        "source_sha256": SOURCE_SHA256,
        "architectural_occurrence_id": OCCURRENCE_ID,
        "base_ir_sha256": review_ir_integrity_sha256(review_ir),
    }


def _approved_ir(proposal=None, walls=None):
    proposal = proposal or _strict_proposal()
    review_ir = _review_ir(proposal, walls)
    approved = apply_short_wall_review_decision(
        review_ir, _decision(review_ir, proposal))
    assert approved["status"] == "APPLIED_PREVIEW"
    return approved["review_ir"]


def _bound_ir(proposal=None, walls=None):
    proposal = proposal or _strict_proposal()
    review_ir = _review_ir(proposal, walls)
    review_ir.update({
        "plan_selection": {
            "selected": {
                "name": "B01 plan",
                "block_name": "B01 plan",
                "insert_handle": "INSERT-B01",
                "insert": [0.0, 0.0],
                "rotation": 0.0,
                "xscale": 1.0,
                "yscale": 1.0,
                "array_index": None,
                "cumulative_transform": copy.deepcopy(
                    PLACEMENT_PATH[0]["cumulative_transform"]),
            },
        },
        "coordinate_system": {
            "unit": "m",
            "origin_dxf_mm": [1000.0, 2000.0],
            "rotation_deg": 0.0,
            "grid_center_dxf_mm": [1000.0, 2000.0],
        },
        "traceability": {
            "selected_structural_occurrence_id": OCCURRENCE_ID,
        },
        "wall_occurrence_selection": {
            "valid": True,
            "selected_occurrence_id": OCCURRENCE_ID,
        },
        "architecture_occurrence_selection": {
            "valid": True,
            "selected_occurrence_id": OCCURRENCE_ID,
            "occurrence_ids": [OCCURRENCE_ID],
            "selected_plan_insert_handle": "INSERT-B01",
        },
    })
    return bind_short_wall_gate_base(review_ir)


def _bound_approved_ir(proposal=None, walls=None):
    proposal = proposal or _strict_proposal()
    review_ir = _bound_ir(proposal, walls)
    decision = _decision(review_ir, proposal)
    decision["gate_base_sha256"] = review_ir[
        "short_wall_gate_base"]["sha256"]
    approved = apply_short_wall_review_decision(review_ir, decision)
    assert approved["status"] == "APPLIED_PREVIEW"
    return approved["review_ir"]


def test_replay_recomputes_strict_wall_without_mutating_review_ir():
    approved_ir = _approved_ir()
    original = copy.deepcopy(approved_ir)

    result = replay_approved_short_wall_decisions(approved_ir)

    assert result["status"] == "REPLAYED"
    assert result["formal_model_eligible"] is False
    assert result["replayed_proposal_ids"] == [
        "short_wall_proposal_strict"]
    assert approved_ir == original
    assert result["architectural_walls"] == approved_ir[
        "approved_preview"]["geometry"]["architectural_walls"]
    wall = result["architectural_walls"][0]
    assert wall["source_segment_ids"] == ["segment_a", "segment_b"]
    assert wall["source_segment_refs"] == approved_ir[
        "review_candidates"]["short_wall_proposals"]["proposals"][0][
            "material_source_segment_refs"]
    assert wall["derivation"] == [{
        "operation": "review_approved_short_wall_preview",
        "proposal_id": "short_wall_proposal_strict",
        "proposal_integrity_sha256": wall["proposal_integrity_sha256"],
        "parent_source_segment_ids": ["segment_a", "segment_b"],
        "formal_model_eligible": False,
    }]
    assert wall["review_application"] == result["applications"][0]


def test_replay_is_deterministic_and_no_approval_is_a_pure_noop():
    approved_ir = _approved_ir()

    first = replay_approved_short_wall_decisions(approved_ir)
    second = replay_approved_short_wall_decisions(approved_ir)

    assert first == second
    unapproved = _review_ir(_strict_proposal())
    original_walls = unapproved["geometry"]["architectural_walls"]
    noop = replay_approved_short_wall_decisions(unapproved)
    assert noop["status"] == "NO_APPROVED_DECISIONS"
    assert noop["architectural_walls"] == original_walls
    assert noop["architectural_walls"] is not original_walls


@pytest.mark.parametrize(
    "mutate, expected_error",
    [
        (
            lambda proposal: proposal.update(
                {"approval_status": "NEEDS_REVIEW"}),
            "proposal_not_strict_approval_eligible",
        ),
        (
            lambda proposal: proposal.update({"endpoint_support": []}),
            "proposal_strict_endpoint_support_invalid",
        ),
        (
            lambda proposal: proposal.update({"overlap_runs": []}),
            "proposal_overlap_handoff_chain_invalid",
        ),
    ],
)
def test_replay_rejects_labels_that_do_not_satisfy_strict_contract(
        mutate, expected_error):
    proposal = _strict_proposal()
    mutate(proposal)
    proposal["integrity_sha256"] = proposal_integrity_sha256(proposal)
    approved_ir = _approved_ir(proposal)
    original = copy.deepcopy(approved_ir)

    result = replay_approved_short_wall_decisions(approved_ir)

    assert result["status"] == "BLOCKED"
    assert result["errors"] == [expected_error]
    assert result["architectural_walls"] == []
    assert approved_ir == original


def test_replay_rejects_tampered_integrity_protected_preview():
    approved_ir = _approved_ir()
    tampered = copy.deepcopy(approved_ir)
    tampered["approved_preview"]["geometry"]["architectural_walls"][0][
        "start"] = [999.0, 999.0]

    result = replay_approved_short_wall_decisions(tampered)

    assert result["status"] == "BLOCKED"
    assert result["errors"] == ["approved_preview_integrity_mismatch"]
    assert result["architectural_walls"] == []


def test_atomic_replacement_preview_is_not_replayed_for_gate():
    old_reference = _reference("segment_old", "OLD")
    existing = {
        "id": "wall_60",
        "start": [2.0, 0.1],
        "end": [2.0, 0.5],
        "thickness": 100.0,
        "paired": False,
        "source_segment_ids": ["segment_old"],
        "source_segment_refs": [copy.deepcopy(old_reference)],
    }
    references = [old_reference, _reference("segment_new", "NEW")]
    proposal = _strict_proposal(proposal_id="short_wall_upgrade")
    proposal.update({
        "status": "PROPOSED_WALL_UPGRADE",
        "source_review_unit_ids": ["unit_old", "unit_new"],
        "source_segment_ids": ["segment_new", "segment_old"],
        "material_source_segment_refs": copy.deepcopy(references),
        "source_entity_handles": ["NEW", "OLD"],
        "replacement_evidence": {
            "existing_wall_id": "wall_60",
            "existing_wall_paired": False,
            "existing_wall_source_segment_ids": ["segment_old"],
            "expected_existing_wall_fingerprint": (
                wall_precondition_sha256(existing)),
            "required_action": "ATOMIC_REPLACE_SINGLE_WALL",
        },
    })
    proposal["integrity_sha256"] = proposal_integrity_sha256(proposal)
    approved_ir = _approved_ir(proposal, [existing])

    result = replay_approved_short_wall_decisions(approved_ir)

    assert result["status"] == "BLOCKED"
    assert result["errors"] == [
        "short_wall_replacement_replay_not_supported"]
    assert result["architectural_walls"] == [existing]


def test_replay_is_transactional_when_authoritative_walls_changed():
    approved_ir = _approved_ir()
    changed = copy.deepcopy(approved_ir)
    proposal = changed["review_candidates"]["short_wall_proposals"][
        "proposals"][0]
    changed["geometry"]["architectural_walls"].append({
        "id": "wall_existing",
        "start": [10.0, 0.0],
        "end": [10.4, 0.0],
        "thickness": 100.0,
        "paired": True,
        "source_segment_ids": ["segment_a"],
        "source_segment_refs": [copy.deepcopy(
            proposal["material_source_segment_refs"][0])],
    })

    result = replay_approved_short_wall_decisions(changed)

    assert result["status"] == "BLOCKED"
    assert result["errors"] == ["proposal_material_source_already_owned"]
    assert result["architectural_walls"] == changed["geometry"][
        "architectural_walls"]


def test_gate_base_hash_ignores_output_paths_and_overlays_only():
    review_ir = _bound_ir()
    expected = short_wall_gate_base_sha256(review_ir)
    moved = copy.deepcopy(review_ir)
    moved["source"]["path"] = "D:/different/fresh/location.dxf"
    moved["artifacts"] = {"wall_endpoint_overlay": "D:/tmp/overlay.png"}
    moved["review_candidates"]["short_wall_proposals"][
        "review_overlay"] = "D:/tmp/proposals.png"

    assert short_wall_gate_base_sha256(moved) == expected
    changed_coordinate = copy.deepcopy(review_ir)
    changed_coordinate["coordinate_system"]["origin_dxf_mm"][0] += 1.0
    assert short_wall_gate_base_sha256(changed_coordinate) != expected


@pytest.mark.parametrize("scope", ["wall", "unit", "proposal"])
def test_gate_base_rejects_any_architecture_occurrence_mismatch(scope):
    wall = {
        "id": "wall_existing",
        "start": [5.0, 0.0],
        "end": [6.0, 0.0],
        "thickness": 100.0,
        "source_segment_ids": ["wall_segment"],
        "source_segment_refs": [
            _reference("wall_segment", "WALL-FACE")],
    }
    review_ir = _bound_ir(walls=[wall])
    if scope == "wall":
        target = review_ir["geometry"]["architectural_walls"][0][
            "source_segment_refs"][0]
    elif scope == "unit":
        target = review_ir["review_candidates"][
            "semantic_wall_source_units"][0]
    else:
        target = review_ir["review_candidates"][
            "short_wall_proposals"]["proposals"][0]
    target["structural_occurrence_id"] = "wrong_architecture_occurrence"

    with pytest.raises(
            ValueError,
            match="architectural occurrence scope does not match selection"):
        bind_short_wall_gate_base(review_ir)


def test_gate_base_rejects_architecture_evidence_outside_selected_plan():
    review_ir = _bound_ir()
    review_ir["review_candidates"]["semantic_wall_source_units"][0][
        "placement_path"][0]["insert_handle"] = "OTHER-PLAN"

    with pytest.raises(
            ValueError,
            match="architectural evidence is outside the selected plan"):
        bind_short_wall_gate_base(review_ir)


def test_bound_review_decision_requires_matching_gate_base_digest():
    proposal = _strict_proposal()
    review_ir = _bound_ir(proposal)
    decision = _decision(review_ir, proposal)

    missing = apply_short_wall_review_decision(review_ir, decision)
    assert missing["status"] == "BLOCKED"
    assert missing["errors"] == [
        "decision_fields_missing:gate_base_sha256"]

    decision["gate_base_sha256"] = "0" * 64
    mismatched = apply_short_wall_review_decision(review_ir, decision)
    assert mismatched["status"] == "BLOCKED"
    assert mismatched["errors"] == ["gate_base_sha256_mismatch"]


def test_bundle_replay_rebuilds_addition_from_equal_current_gate_base():
    proposal = _strict_proposal()
    approved_ir = _bound_approved_ir(proposal)
    current_ir = _bound_ir(copy.deepcopy(proposal))
    original = copy.deepcopy(current_ir)

    result = replay_approved_short_wall_bundle(current_ir, approved_ir)

    assert result["status"] == "REPLAYED"
    assert result["gate_base_sha256"] == current_ir[
        "short_wall_gate_base"]["sha256"]
    assert result["replayed_proposal_ids"] == [proposal["proposal_id"]]
    assert len(result["approved_additions"]) == 1
    assert result["approved_additions"][0]["approved_proposal_id"] == (
        proposal["proposal_id"])
    assert current_ir == original


def test_bundle_replay_fails_closed_for_old_or_stale_gate_base():
    proposal = _strict_proposal()
    current_ir = _bound_ir(copy.deepcopy(proposal))
    old_bundle = _approved_ir(copy.deepcopy(proposal))

    missing = replay_approved_short_wall_bundle(current_ir, old_bundle)
    assert missing["status"] == "BLOCKED"
    assert missing["errors"] == ["approved_bundle_gate_base_missing"]
    assert missing["architectural_walls"] == []

    approved_ir = _bound_approved_ir(copy.deepcopy(proposal))
    changed = copy.deepcopy(current_ir)
    changed["coordinate_system"]["origin_dxf_mm"][0] += 1.0
    changed = bind_short_wall_gate_base(changed)
    stale = replay_approved_short_wall_bundle(changed, approved_ir)
    assert stale["status"] == "BLOCKED"
    assert stale["errors"] == ["short_wall_gate_base_mismatch"]
    assert stale["architectural_walls"] == []
