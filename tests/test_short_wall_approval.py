import copy

import pytest

from backend.engines.short_wall_approval import (
    approved_preview_integrity_sha256,
    apply_short_wall_proposal_preview,
    apply_short_wall_review_decision,
    proposal_integrity_sha256,
    review_ir_integrity_sha256,
    source_reference_from_review_unit,
    wall_precondition_sha256,
)


SOURCE_SHA256 = "a" * 64
DRAWING_IDENTITY = f"sha256:{SOURCE_SHA256}"
ARCH_OCCURRENCE = "architectural_occurrence_B1"
PLACEMENT_PATH = [{
    "block_name": "B1",
    "insert_handle": "INSERT-B1",
    "array_index": None,
    "cumulative_transform": {
        "coordinate_system": "DXF_WCS",
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
        "structural_occurrence_id": ARCH_OCCURRENCE,
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
    axis_end = max(float(high), 1.0)
    return {
        **copy.deepcopy(reference),
        "review_unit_id": unit_id,
        "source_start": [0.0, 0.0],
        "source_end": [axis_end, 0.0],
        "source_axis_start": [0.0, 0.0],
        "source_axis_end": [axis_end, 0.0],
        "start": [float(low), 0.0],
        "end": [float(high), 0.0],
    }


def _proposal(*, proposal_id="short_wall_proposal_add",
              start=(0.0, 0.05), end=(0.4, 0.05), blockers=None,
              status="PROPOSED_SHORT_WALL", references=None,
              replacement_evidence=None):
    references = references or [
        _reference("segment_a", "FACE-A"),
        _reference("segment_b", "FACE-B"),
    ]
    proposal = {
        "proposal_id": proposal_id,
        "pair_group_id": "pair_group_1",
        "status": status,
        "approval_status": "ELIGIBLE_FOR_STRICT_APPROVAL",
        "geometry_source": "DXF_VECTOR",
        "drawing_identity": DRAWING_IDENTITY,
        "structural_occurrence_id": ARCH_OCCURRENCE,
        "start": list(start),
        "end": list(end),
        "length_m": 0.4,
        "thickness_mm": 100.0,
        "source_review_unit_ids": [
            f"unit_{index}" for index in range(len(references))],
        "source_segment_ids": sorted({
            reference["source_segment_id"] for reference in references
        }),
        "material_source_segment_refs": copy.deepcopy(references),
        "missing_material_source_segment_ids": [],
        "source_entity_handles": sorted({
            reference["entity_handle"] for reference in references
        }),
        "raw_pair_ids": ["pair_1"],
        "overlap_runs": [],
        "junction_handoffs": [],
        "endpoint_support": [],
        "blockers": list(blockers or []),
        "model_geometry_created": False,
        "auto_action": "NONE",
    }
    if replacement_evidence is not None:
        proposal["replacement_evidence"] = replacement_evidence
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


def _decision(review_ir, proposal, *, decision_id="decision-1"):
    return {
        "decision_id": decision_id,
        "decision": "approved",
        "operator": "reviewer-1",
        "comment": "confirmed wall",
        "proposal_id": proposal["proposal_id"],
        "proposal_integrity_sha256": proposal["integrity_sha256"],
        "source_sha256": SOURCE_SHA256,
        "architectural_occurrence_id": ARCH_OCCURRENCE,
        "base_ir_sha256": review_ir_integrity_sha256(review_ir),
    }


def test_exact_review_unit_interval_becomes_complete_material_reference():
    unit = {
        "drawing_identity": DRAWING_IDENTITY,
        "source_occurrence_id": "source_a",
        "placed_entity_id": "placed_a",
        "structural_occurrence_id": ARCH_OCCURRENCE,
        "source_segment_id": "segment_a",
        "entity_handle": "FACE-A",
        "source_record_index": 1,
        "segment_index": 0,
        "placement_path": copy.deepcopy(PLACEMENT_PATH),
        "identity_is_complete": True,
        "identity_limitations": [],
        "source_start": [0.0, 0.0],
        "source_end": [1.0, 0.0],
        "start": [0.2, 0.0],
        "end": [0.6, 0.0],
        "source_interval_m": [0.2, 0.6],
    }

    reference = source_reference_from_review_unit(unit)

    assert reference["source_interval_m"] == [0.2, 0.6]
    assert reference["placement_path"] == PLACEMENT_PATH
    assert reference["identity_is_complete"] is True
    missing_interval = copy.deepcopy(unit)
    missing_interval.pop("source_interval_m")
    assert source_reference_from_review_unit(missing_interval) is None

    reversed_source = copy.deepcopy(unit)
    reversed_source.update({
        "source_start": [1.0, 0.0],
        "source_end": [0.0, 0.0],
        "source_axis_start": [0.0, 0.0],
        "source_axis_end": [1.0, 0.0],
    })
    assert source_reference_from_review_unit(reversed_source)[
        "source_interval_m"] == [0.2, 0.6]

    incomplete_identity = copy.deepcopy(unit)
    incomplete_identity["identity_is_complete"] = False
    incomplete_identity["identity_limitations"] = ["source missing"]
    assert source_reference_from_review_unit(incomplete_identity) is None


def test_approved_add_creates_separate_review_preview_and_is_idempotent():
    proposal = _proposal()
    review_ir = _review_ir(proposal)
    original = copy.deepcopy(review_ir)
    decision = _decision(review_ir, proposal)

    result = apply_short_wall_review_decision(review_ir, decision)

    assert result["status"] == "APPLIED_PREVIEW"
    assert review_ir == original
    assert result["review_ir"] is not review_ir
    assert result["review_ir"]["geometry"] == review_ir["geometry"]
    preview = result["review_ir"]["approved_preview"]
    assert preview["status"] == "REVIEW_ONLY"
    assert preview["formal_model_eligible"] is False
    assert preview["revision"] == 1
    assert len(preview["geometry"]["architectural_walls"]) == 1
    wall = preview["geometry"]["architectural_walls"][0]
    assert wall["id"].startswith("wall_short_")
    assert wall["logical_id"] == wall["id"]
    assert wall["revision_number"] == 1
    assert wall["source_segment_ids"] == ["segment_a", "segment_b"]
    assert len(wall["source_segment_refs"]) == 2
    assert [reference["source_segment_id"] for reference in
            wall["source_segment_refs"]] == wall["source_segment_ids"]
    assert wall["formal_model_eligible"] is False
    assert wall["revit_execution_status"] == "NOT_EXECUTED"
    assert result["review_ir"]["quality_gate"]["allow_modeling"] is False

    repeated = apply_short_wall_review_decision(
        result["review_ir"], decision)
    assert repeated["status"] == "ALREADY_APPLIED"
    assert repeated["review_ir"] is result["review_ir"]

    conflicting = copy.deepcopy(decision)
    conflicting["comment"] = "different request with same key"
    conflict = apply_short_wall_review_decision(
        result["review_ir"], conflicting)
    assert conflict["status"] == "BLOCKED"
    assert conflict["errors"] == ["decision_idempotency_conflict"]


@pytest.mark.parametrize("mutation, expected", [
    (lambda preview: preview["geometry"]["architectural_walls"][0].update(
        {"start": [999.0, 999.0]}),
     "approved_preview_wall_integrity_mismatch"),
    (lambda preview: preview["geometry"]["architectural_walls"][0].update(
        {"revision_number": 99}),
     "approved_preview_wall_integrity_mismatch"),
    (lambda preview: preview["applications"][0].update(
        {"operation": "REPLACE_WALL"}),
     "approved_preview_application_integrity_mismatch"),
    (lambda preview: preview["geometry"]["architectural_walls"][0][
        "review_application"]["decision"].update({"operator": "tampered"}),
     "approved_preview_wall_application_mismatch"),
])
def test_idempotent_replay_rejects_tampered_preview(mutation, expected):
    proposal = _proposal()
    review_ir = _review_ir(proposal)
    decision = _decision(review_ir, proposal)
    applied = apply_short_wall_review_decision(review_ir, decision)
    tampered = copy.deepcopy(applied["review_ir"])
    mutation(tampered["approved_preview"])
    tampered["approved_preview"]["integrity_sha256"] = (
        approved_preview_integrity_sha256(tampered["approved_preview"]))
    original = copy.deepcopy(tampered)

    replay = apply_short_wall_review_decision(tampered, decision)

    assert replay["status"] == "BLOCKED"
    assert replay["errors"] == [expected]
    assert replay["review_ir"] is tampered
    assert tampered == original


def test_idempotent_replay_rejects_preview_digest_tampering():
    proposal = _proposal()
    review_ir = _review_ir(proposal)
    decision = _decision(review_ir, proposal)
    applied = apply_short_wall_review_decision(review_ir, decision)
    tampered = copy.deepcopy(applied["review_ir"])
    tampered["approved_preview"]["geometry"]["architectural_walls"][0][
        "start"] = [999.0, 999.0]

    replay = apply_short_wall_review_decision(tampered, decision)

    assert replay["status"] == "BLOCKED"
    assert replay["errors"] == ["approved_preview_integrity_mismatch"]


@pytest.mark.parametrize("mutation, expected", [
    (lambda decision, proposal: decision.update(
        {"base_ir_sha256": "0" * 64}), "stale_review_ir"),
    (lambda decision, proposal: decision.update(
        {"source_sha256": "b" * 64}), "review_ir_source_identity_mismatch"),
    (lambda decision, proposal: decision.update(
        {"architectural_occurrence_id": "other"}),
     "architectural_occurrence_mismatch"),
    (lambda decision, proposal: decision.update(
        {"proposal_integrity_sha256": "f" * 64}),
     "proposal_integrity_mismatch"),
])
def test_stale_or_cross_scope_decision_returns_original_ir(mutation,
                                                            expected):
    proposal = _proposal()
    review_ir = _review_ir(proposal)
    original = copy.deepcopy(review_ir)
    decision = _decision(review_ir, proposal)
    mutation(decision, proposal)

    result = apply_short_wall_review_decision(review_ir, decision)

    assert result["status"] == "BLOCKED"
    assert expected in result["errors"]
    assert result["review_ir"] is review_ir
    assert review_ir == original


@pytest.mark.parametrize("blocker", [
    "endpoint_support_ambiguous",
    "endpoint_supported_by_single_wall_only",
    "atomic_single_wall_replacement_not_implemented",
])
def test_any_proposal_blocker_prevents_preview_application(blocker):
    status = ("PROPOSED_WALL_UPGRADE" if blocker.startswith("atomic_")
              else "PROPOSED_SHORT_WALL")
    proposal = _proposal(status=status, blockers=[blocker])
    review_ir = _review_ir(proposal)

    result = apply_short_wall_review_decision(
        review_ir, _decision(review_ir, proposal))

    assert result["status"] == "BLOCKED"
    assert result["review_ir"] is review_ir
    assert result["errors"][0].startswith("proposal_has_blockers:")


def test_atomic_logical_replacement_preserves_id_and_records_revision():
    old_reference = _reference("segment_old", "OLD", (0.0, 0.5))
    existing = {
        "id": "wall_64",
        "start": [2.0, 0.1],
        "end": [2.0, 0.6],
        "thickness": 100.0,
        "paired": False,
        "source_segment_ids": ["segment_old"],
        "source_segment_refs": [copy.deepcopy(old_reference)],
    }
    evidence = {
        "existing_wall_id": "wall_64",
        "existing_wall_paired": False,
        "existing_wall_source_segment_ids": ["segment_old"],
        "expected_existing_wall_fingerprint": wall_precondition_sha256(
            existing),
        "required_action": "ATOMIC_REPLACE_SINGLE_WALL",
    }
    proposal = _proposal(
        proposal_id="short_wall_proposal_upgrade",
        status="PROPOSED_WALL_UPGRADE",
        start=(2.05, 0.0), end=(2.05, 0.5),
        references=[old_reference,
                    _reference("segment_new", "NEW", (0.0, 0.5))],
        replacement_evidence=evidence,
    )
    review_ir = _review_ir(proposal, [existing])
    original = copy.deepcopy(review_ir)

    result = apply_short_wall_review_decision(
        review_ir, _decision(review_ir, proposal))

    assert result["status"] == "APPLIED_PREVIEW"
    assert review_ir == original
    walls = result["review_ir"]["approved_preview"]["geometry"][
        "architectural_walls"]
    assert len(walls) == 1
    replacement = walls[0]
    assert replacement["id"] == "wall_64"
    assert replacement["logical_id"] == "wall_64"
    assert replacement["paired"] is True
    assert replacement["revision_number"] == 1
    assert replacement["supersedes_revision"] == 0
    assert replacement["supersedes_revision_id"].startswith(
        "wall_revision_")
    assert replacement["source_segment_ids"] == [
        "segment_new", "segment_old"]
    assert result["application"]["operation"] == "REPLACE_WALL"
    assert result["application"]["revit_action_required"] == (
        "ATOMIC_UPDATE_IN_PLACE")
    assert result["application"]["formal_model_eligible"] is False


@pytest.mark.parametrize("field, stale_value", [
    ("revision_number", 8),
    ("revision_id", "wall_revision_stale"),
    ("logical_id", "wall_logical_stale"),
    ("geometry_source", "CV_RASTER"),
])
def test_atomic_replacement_rejects_stale_version_identity(field,
                                                            stale_value):
    old_reference = _reference("segment_old", "OLD", (0.0, 0.5))
    existing = {
        "id": "wall_64",
        "logical_id": "wall_64",
        "start": [2.0, 0.1],
        "end": [2.0, 0.6],
        "thickness": 100.0,
        "paired": False,
        "geometry_source": "DXF_VECTOR",
        "revision": 2,
        "revision_number": 2,
        "revision_id": "wall_revision_current",
        "source_segment_ids": ["segment_old"],
        "source_segment_refs": [copy.deepcopy(old_reference)],
    }
    proposal = _proposal(
        status="PROPOSED_WALL_UPGRADE",
        references=[old_reference, _reference("segment_new", "NEW")],
        replacement_evidence={
            "existing_wall_id": "wall_64",
            "existing_wall_paired": False,
            "existing_wall_source_segment_ids": ["segment_old"],
            "expected_existing_wall_fingerprint": wall_precondition_sha256(
                existing),
            "required_action": "ATOMIC_REPLACE_SINGLE_WALL",
        },
    )
    stale = copy.deepcopy(existing)
    stale[field] = stale_value
    walls = [stale]
    review_ir = _review_ir(proposal, walls)
    decision = _decision(review_ir, proposal)
    original = copy.deepcopy(walls)

    result = apply_short_wall_proposal_preview(walls, proposal, decision)

    assert result["status"] == "BLOCKED"
    assert result["errors"] == ["replacement_wall_digest_mismatch"]
    assert result["walls"] is walls
    assert walls == original


def test_replacement_stale_geometry_and_material_ref_gaps_fail_closed():
    old_reference = _reference("segment_old", "OLD", (0.0, 0.5))
    existing = {
        "id": "wall_64", "start": [2.0, 0.1], "end": [2.0, 0.6],
        "thickness": 100.0, "paired": False,
        "source_segment_ids": ["segment_old"],
        "source_segment_refs": [old_reference],
    }
    proposal = _proposal(
        status="PROPOSED_WALL_UPGRADE",
        references=[old_reference, _reference("segment_new", "NEW")],
        replacement_evidence={
            "existing_wall_id": "wall_64",
            "existing_wall_paired": False,
            "existing_wall_source_segment_ids": ["segment_old"],
            "expected_existing_wall_fingerprint": wall_precondition_sha256(
                existing),
            "required_action": "ATOMIC_REPLACE_SINGLE_WALL",
        },
    )
    stale_wall = copy.deepcopy(existing)
    stale_wall["start"] = [2.0, 0.2]
    walls = [stale_wall]
    decision = {
        **_decision(_review_ir(proposal, walls), proposal),
    }

    stale = apply_short_wall_proposal_preview(walls, proposal, decision)

    assert stale["status"] == "BLOCKED"
    assert stale["walls"] is walls
    assert stale["errors"] == ["replacement_wall_digest_mismatch"]

    incomplete = copy.deepcopy(proposal)
    incomplete["material_source_segment_refs"] = [old_reference]
    incomplete["integrity_sha256"] = proposal_integrity_sha256(incomplete)
    incomplete_decision = copy.deepcopy(decision)
    incomplete_decision["proposal_integrity_sha256"] = incomplete[
        "integrity_sha256"]
    missing = apply_short_wall_proposal_preview(
        [existing], incomplete, incomplete_decision)
    assert missing["status"] == "BLOCKED"
    assert missing["walls"][0] is existing
    assert missing["errors"][0].startswith("proposal_source_refs_missing:")

    invalid_interval = copy.deepcopy(proposal)
    invalid_interval["material_source_segment_refs"][1][
        "source_interval_m"] = [0.4, 0.1]
    invalid_interval["integrity_sha256"] = proposal_integrity_sha256(
        invalid_interval)
    invalid_decision = copy.deepcopy(decision)
    invalid_decision["proposal_integrity_sha256"] = invalid_interval[
        "integrity_sha256"]
    invalid = apply_short_wall_proposal_preview(
        [existing], invalid_interval, invalid_decision)
    assert invalid["status"] == "BLOCKED"
    assert invalid["errors"] == ["proposal_source_refs_invalid"]


def test_reversed_endpoints_produce_same_stable_logical_wall_id():
    first = _proposal(proposal_id="proposal-forward")
    reverse = _proposal(
        proposal_id="proposal-reverse", start=(0.4, 0.05), end=(0.0, 0.05))
    first_ir, reverse_ir = _review_ir(first), _review_ir(reverse)

    first_result = apply_short_wall_review_decision(
        first_ir, _decision(first_ir, first, decision_id="forward"))
    reverse_result = apply_short_wall_review_decision(
        reverse_ir, _decision(reverse_ir, reverse, decision_id="reverse"))

    first_wall = first_result["review_ir"]["approved_preview"]["geometry"][
        "architectural_walls"][0]
    reverse_wall = reverse_result["review_ir"]["approved_preview"][
        "geometry"]["architectural_walls"][0]
    assert first_wall["id"] == reverse_wall["id"]
    assert first_wall["start"] == reverse_wall["start"]
    assert first_wall["end"] == reverse_wall["end"]


def test_persisted_material_refs_preserve_multiple_intervals_per_segment():
    references = [
        _reference("segment_shared", "FACE-A", (0.0, 0.2)),
        _reference("segment_shared", "FACE-A", (0.3, 0.5)),
    ]
    proposal = _proposal(references=references)
    review_ir = _review_ir(proposal)

    applied = apply_short_wall_review_decision(
        review_ir, _decision(review_ir, proposal))

    assert applied["status"] == "APPLIED_PREVIEW"
    wall = applied["review_ir"]["approved_preview"]["geometry"][
        "architectural_walls"][0]
    assert wall["source_segment_ids"] == ["segment_shared"]
    assert [reference["source_interval_m"] for reference in
            wall["source_segment_refs"]] == [[0.0, 0.2], [0.3, 0.5]]

    incomplete = copy.deepcopy(review_ir)
    incomplete["review_candidates"]["semantic_wall_source_units"].pop()
    blocked = apply_short_wall_review_decision(
        incomplete, _decision(incomplete, proposal))
    assert blocked["status"] == "BLOCKED"
    assert blocked["errors"] == ["proposal_material_refs_not_persisted"]

    incomplete_identity = copy.deepcopy(review_ir)
    unit = incomplete_identity["review_candidates"][
        "semantic_wall_source_units"][0]
    unit["identity_is_complete"] = False
    unit["identity_limitations"] = ["source missing"]
    identity_blocked = apply_short_wall_review_decision(
        incomplete_identity, _decision(incomplete_identity, proposal))
    assert identity_blocked["status"] == "BLOCKED"
    assert identity_blocked["errors"] == [
        "proposal_material_refs_not_persisted"]


def test_material_interval_already_owned_by_another_wall_is_blocked():
    proposal = _proposal()
    existing = {
        "id": "wall_existing",
        "start": [10.0, 0.0],
        "end": [10.4, 0.0],
        "thickness": 100.0,
        "paired": True,
        "source_segment_ids": ["segment_a"],
        "source_segment_refs": [copy.deepcopy(
            proposal["material_source_segment_refs"][0])],
    }
    review_ir = _review_ir(proposal, [existing])

    result = apply_short_wall_review_decision(
        review_ir, _decision(review_ir, proposal))

    assert result["status"] == "BLOCKED"
    assert result["errors"] == ["proposal_material_source_already_owned"]
    assert result["review_ir"] is review_ir


@pytest.mark.parametrize("source_refs", [
    None,
    [],
    [{"bad": "reference"}],
])
def test_incomplete_existing_wall_provenance_cannot_skip_source_ownership(
        source_refs):
    proposal = _proposal()
    existing = {
        "id": "wall_existing",
        "start": [10.0, 0.0],
        "end": [10.4, 0.0],
        "thickness": 100.0,
        "paired": True,
        "source_segment_ids": ["segment_a"],
    }
    if source_refs is not None:
        existing["source_segment_refs"] = copy.deepcopy(source_refs)
    review_ir = _review_ir(proposal, [existing])

    result = apply_short_wall_review_decision(
        review_ir, _decision(review_ir, proposal))

    assert result["status"] == "BLOCKED"
    assert result["errors"] == [
        "existing_wall_source_provenance_incomplete"]
    assert result["review_ir"] is review_ir


@pytest.mark.parametrize("invalid_blockers", [
    "endpoint_support_ambiguous",
    {"code": "endpoint_support_ambiguous"},
    [{"code": "endpoint_support_ambiguous"}],
    [None],
    [123],
])
def test_malformed_blockers_fail_closed_without_mutation(invalid_blockers):
    proposal = _proposal()
    proposal["blockers"] = invalid_blockers
    proposal["integrity_sha256"] = proposal_integrity_sha256(proposal)
    walls = []
    decision = _decision(_review_ir(proposal), proposal)

    result = apply_short_wall_proposal_preview(walls, proposal, decision)

    assert result["status"] == "BLOCKED"
    assert result["errors"] == ["proposal_blockers_invalid"]
    assert result["walls"] is walls


def test_malformed_replacement_evidence_fails_closed():
    proposal = _proposal(
        status="PROPOSED_WALL_UPGRADE", replacement_evidence=[])
    walls = []
    decision = _decision(_review_ir(proposal), proposal)

    result = apply_short_wall_proposal_preview(walls, proposal, decision)

    assert result["status"] == "BLOCKED"
    assert result["errors"] == ["replacement_evidence_invalid"]
    assert result["walls"] is walls


def test_noncanonical_nested_inputs_fail_closed_without_raising():
    proposal = _proposal()
    review_ir = _review_ir(proposal)
    decision = _decision(review_ir, proposal)

    invalid_proposal = copy.deepcopy(proposal)
    invalid_proposal["thickness_mm"] = float("nan")
    proposal_result = apply_short_wall_proposal_preview(
        [], invalid_proposal, decision)
    assert proposal_result["errors"] == ["proposal_not_canonical_json"]

    invalid_decision = copy.deepcopy(decision)
    invalid_decision["comment"] = float("nan")
    decision_result = apply_short_wall_proposal_preview(
        [], proposal, invalid_decision)
    assert decision_result["errors"] == ["decision_not_canonical_json"]

    invalid_walls = [{"id": "bad", "extra": {"not", "json"}}]
    wall_result = apply_short_wall_proposal_preview(
        invalid_walls, proposal, decision)
    assert wall_result["errors"] == [
        "architectural_walls_not_canonical_json"]
    assert wall_result["walls"] is invalid_walls

    invalid_ir = copy.deepcopy(review_ir)
    invalid_ir["extra"] = float("nan")
    ir_result = apply_short_wall_review_decision(invalid_ir, decision)
    assert ir_result["errors"] == ["review_ir_not_canonical_json"]
    assert ir_result["review_ir"] is invalid_ir


@pytest.mark.parametrize("field, invalid_value", [
    ("quality_gate", []),
    ("source", []),
    ("geometry", "invalid"),
    ("review_candidates", []),
])
def test_canonical_but_invalid_review_ir_shapes_fail_closed(field,
                                                             invalid_value):
    proposal = _proposal()
    review_ir = _review_ir(proposal)
    decision = _decision(review_ir, proposal)
    review_ir[field] = invalid_value
    original = copy.deepcopy(review_ir)

    result = apply_short_wall_review_decision(review_ir, decision)

    assert result["status"] == "BLOCKED"
    assert result["errors"] == ["review_ir_shape_invalid"]
    assert result["review_ir"] is review_ir
    assert review_ir == original
