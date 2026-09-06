"""Pure replay of strictly approved short walls for Gate recomputation.

The approval workflow intentionally stores review-only preview geometry.  This
module does not trust that geometry as an input to the modeling Gate.  Instead,
it validates every persisted decision through ``short_wall_approval``, replays
the decision from the authoritative architectural walls, and requires the
recomputed result to equal the integrity-protected preview exactly.

Only additive ``PROPOSED_SHORT_WALL`` decisions are supported.  Atomic wall
replacement remains outside this replay path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any

from backend.engines.short_wall_approval import (
    apply_short_wall_proposal_preview,
    apply_short_wall_review_decision,
    proposal_integrity_sha256,
    review_ir_integrity_sha256,
)


_SERIALIZATION_TOLERANCE_M = 1e-6
_MODE = "GATE_RECOMPUTATION_INPUT_ONLY"
GATE_BASE_SCHEMA_VERSION = "buildmate.short-wall-gate-base/1.0"
GATE_BASE_RULE_VERSION = "strict-additive-vector-replay/1.1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalised_sha256(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text if _SHA256_RE.fullmatch(text) else None


def _without_transient_artifacts(value: Any) -> Any:
    """Remove output-location metadata while retaining DXF placement paths."""
    if isinstance(value, dict):
        return {
            str(key): _without_transient_artifacts(item)
            for key, item in value.items()
            if (key not in {"path", "artifacts", "review_overlay"} and
                not str(key).endswith("_overlay"))
        }
    if isinstance(value, list):
        return [_without_transient_artifacts(item) for item in value]
    if isinstance(value, tuple):
        return [_without_transient_artifacts(item) for item in value]
    return value


def _sorted_objects(values: Any, identity_keys: tuple[str, ...]) -> list:
    if not isinstance(values, list):
        raise ValueError("gate base collection must be a list")
    normalised = [_without_transient_artifacts(item) for item in values]

    def sort_key(item: Any) -> tuple:
        if isinstance(item, dict):
            identity = tuple(str(item.get(key) or "")
                             for key in identity_keys)
        else:
            identity = tuple("" for _ in identity_keys)
        return (*identity, json.dumps(
            item, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False))

    return sorted(normalised, key=sort_key)


def _gate_base_wall(wall: Any) -> dict:
    if not isinstance(wall, dict):
        raise ValueError("gate base wall must be an object")
    fields = (
        "id", "start", "end", "thickness", "paired", "confidence",
        "source", "warnings", "geometry_source", "source_candidate_id",
        "profile_occurrence_id", "drawing_identity",
        "source_occurrence_id", "placed_entity_id",
        "structural_occurrence_id", "source_segment_ids",
        "source_segment_refs", "derivation", "endpoint_adjustments",
        "geometry_adjustments",
    )
    return {key: copy.deepcopy(wall[key]) for key in fields if key in wall}


def _gate_base_proposal_audit(audit: dict) -> dict:
    normalised = _without_transient_artifacts(audit)
    for key in ("proposals", "insufficient_support_candidates"):
        values = normalised.get(key)
        if isinstance(values, list):
            normalised[key] = _sorted_objects(
                values, ("proposal_id", "pair_group_id", "candidate_id"))
    return normalised


def short_wall_gate_base_payload(review_ir: dict) -> dict:
    """Return the path-free, reproducible state an approval is bound to.

    The payload intentionally excludes report/output paths and rendered
    overlays.  It includes only the selected placed plan and coordinate frame,
    occurrence scope, current authoritative wall geometry/provenance, exact
    semantic review units, and the complete short-wall proposal audit.
    """
    if not isinstance(review_ir, dict):
        raise ValueError("review IR must be an object")
    source = review_ir.get("source")
    plan_selection = review_ir.get("plan_selection")
    coordinate_system = review_ir.get("coordinate_system")
    traceability = review_ir.get("traceability")
    occurrence_selection = review_ir.get("wall_occurrence_selection")
    architecture_occurrence_selection = review_ir.get(
        "architecture_occurrence_selection")
    geometry = review_ir.get("geometry")
    candidates = review_ir.get("review_candidates")
    if not all(isinstance(value, dict) for value in (
            source, plan_selection, coordinate_system, traceability,
            occurrence_selection, architecture_occurrence_selection,
            geometry, candidates)):
        raise ValueError("review IR gate-base scope is incomplete")
    source_sha256 = _normalised_sha256(source.get("sha256"))
    selected_plan = plan_selection.get("selected")
    walls = geometry.get("architectural_walls")
    units = candidates.get("semantic_wall_source_units")
    proposal_audit = candidates.get("short_wall_proposals")
    if (source_sha256 is None or not isinstance(selected_plan, dict) or
            not isinstance(proposal_audit, dict)):
        raise ValueError("review IR gate-base evidence is incomplete")

    selected_occurrence = (
        occurrence_selection.get("selected_occurrence_id") or
        traceability.get("selected_structural_occurrence_id"))
    selected_architecture_occurrence = (
        architecture_occurrence_selection.get("selected_occurrence_id") or
        traceability.get("selected_architectural_occurrence_id"))
    if (occurrence_selection.get("valid") is not True or
            not str(selected_occurrence or "").strip()):
        raise ValueError("review IR occurrence selection is incomplete")
    if (architecture_occurrence_selection.get("valid") is not True or
            not str(selected_architecture_occurrence or "").strip()):
        raise ValueError(
            "review IR architectural occurrence selection is incomplete")

    selected_plan_handle = str(selected_plan.get("insert_handle") or "")
    selected_architecture_occurrence = str(
        selected_architecture_occurrence)
    if not selected_plan_handle:
        raise ValueError("selected plan placement identity is incomplete")

    def validate_reference(reference: Any) -> None:
        if (not isinstance(reference, dict) or
                str(reference.get("structural_occurrence_id") or "") !=
                selected_architecture_occurrence):
            raise ValueError(
                "architectural occurrence scope does not match selection")
        placement_path = reference.get("placement_path")
        if (not isinstance(placement_path, list) or not placement_path or
                not isinstance(placement_path[0], dict) or
                str(placement_path[0].get("insert_handle") or "") !=
                selected_plan_handle):
            raise ValueError(
                "architectural evidence is outside the selected plan")

    if (not isinstance(walls, list) or not isinstance(units, list) or
            not isinstance(proposal_audit.get("proposals"), list)):
        raise ValueError("review IR gate-base collections are invalid")
    for wall in walls:
        references = (wall.get("source_segment_refs")
                      if isinstance(wall, dict) else None)
        if not isinstance(references, list) or not references:
            raise ValueError("architectural wall source refs are incomplete")
        for reference in references:
            validate_reference(reference)
    for unit in units:
        validate_reference(unit)
    for proposal in proposal_audit["proposals"]:
        if (not isinstance(proposal, dict) or
                str(proposal.get("structural_occurrence_id") or "") !=
                selected_architecture_occurrence):
            raise ValueError(
                "architectural occurrence scope does not match selection")
        references = proposal.get("material_source_segment_refs")
        if not isinstance(references, list) or not references:
            raise ValueError("short-wall proposal source refs are incomplete")
        for reference in references:
            validate_reference(reference)

    return {
        "schema_version": GATE_BASE_SCHEMA_VERSION,
        "rule_version": GATE_BASE_RULE_VERSION,
        "review_ir_schema_version": review_ir.get("schema_version"),
        "source_sha256": source_sha256,
        "selected_plan": _without_transient_artifacts(selected_plan),
        "coordinate_system": _without_transient_artifacts(
            coordinate_system),
        "occurrence_scope": {
            "selected_structural_occurrence_id": str(selected_occurrence),
            "selected_architectural_occurrence_id":
                selected_architecture_occurrence,
            "architectural_occurrence_ids": [
                selected_architecture_occurrence],
        },
        "architectural_walls": _sorted_objects(
            [_gate_base_wall(wall) for wall in walls],
            ("id", "source_candidate_id")),
        "semantic_wall_source_units": _sorted_objects(
            units, ("review_unit_id", "source_segment_id")),
        "short_wall_proposal_audit": _gate_base_proposal_audit(
            proposal_audit),
    }


def short_wall_gate_base_sha256(review_ir: dict) -> str:
    """Hash the normalized architecture Gate base for approval replay."""
    return _canonical_sha256(short_wall_gate_base_payload(review_ir))


def bind_short_wall_gate_base(review_ir: dict) -> dict:
    """Return a copied review IR carrying its normalized Gate-base digest."""
    copied = copy.deepcopy(review_ir)
    digest = short_wall_gate_base_sha256(copied)
    copied["short_wall_gate_base"] = {
        "schema_version": GATE_BASE_SCHEMA_VERSION,
        "rule_version": GATE_BASE_RULE_VERSION,
        "sha256": digest,
    }
    return copied


def _authoritative_walls(review_ir: Any) -> list[dict]:
    if not isinstance(review_ir, dict):
        return []
    geometry = review_ir.get("geometry")
    if not isinstance(geometry, dict):
        return []
    walls = geometry.get("architectural_walls")
    if not isinstance(walls, list) or not all(
            isinstance(wall, dict) for wall in walls):
        return []
    return copy.deepcopy(walls)


def _result(status: str, walls: list[dict], *,
            proposal_ids: list[str] | None = None,
            applications: list[dict] | None = None,
            errors: list[str] | None = None,
            failed_proposal_id: str | None = None) -> dict:
    result = {
        "schema_version": "buildmate.short-wall-gate-replay/1.0",
        "status": status,
        "mode": _MODE,
        "formal_model_eligible": False,
        "architectural_walls": copy.deepcopy(walls),
        "replayed_proposal_ids": list(proposal_ids or []),
        "applications": copy.deepcopy(applications or []),
        "errors": list(errors or []),
    }
    if failed_proposal_id:
        result["failed_proposal_id"] = failed_proposal_id
    return result


def _point(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        point = [float(value[0]), float(value[1])]
    except (TypeError, ValueError, OverflowError):
        return None
    return point if all(math.isfinite(item) for item in point) else None


def _strict_contract_error(proposal: dict) -> str | None:
    """Recheck the evidence conditions used to label a strict proposal."""
    if proposal.get("status") != "PROPOSED_SHORT_WALL":
        return "short_wall_replacement_replay_not_supported"
    if proposal.get("approval_status") != "ELIGIBLE_FOR_STRICT_APPROVAL":
        return "proposal_not_strict_approval_eligible"
    if proposal.get("replacement_evidence") not in (None, {}):
        return "additive_proposal_contains_replacement_evidence"
    if (proposal.get("geometry_source") != "DXF_VECTOR" or
            proposal.get("model_geometry_created") is not False or
            proposal.get("auto_action") != "NONE"):
        return "proposal_not_strict_review_only_vector"
    if proposal.get("blockers") != []:
        return "proposal_has_strict_replay_blockers"
    if proposal.get("missing_material_source_segment_ids") != []:
        return "proposal_material_source_refs_incomplete"

    start = _point(proposal.get("start"))
    end = _point(proposal.get("end"))
    try:
        declared_length = float(proposal.get("length_m"))
    except (TypeError, ValueError, OverflowError):
        declared_length = math.nan
    if (start is None or end is None or not math.isfinite(declared_length) or
            declared_length <= 0.0 or
            abs(math.dist(start, end) - declared_length) >
            _SERIALIZATION_TOLERANCE_M):
        return "proposal_declared_geometry_mismatch"

    pair_group_id = proposal.get("pair_group_id")
    raw_pair_ids = proposal.get("raw_pair_ids")
    if (not str(pair_group_id or "").strip() or
            not isinstance(raw_pair_ids, list) or not raw_pair_ids or
            any(not str(item or "").strip() for item in raw_pair_ids) or
            len({str(item) for item in raw_pair_ids}) != len(raw_pair_ids)):
        return "proposal_pair_evidence_invalid"

    runs = proposal.get("overlap_runs")
    handoffs = proposal.get("junction_handoffs")
    if (not isinstance(runs, list) or not runs or
            not all(isinstance(item, dict) for item in runs) or
            not isinstance(handoffs, list) or
            not all(isinstance(item, dict) for item in handoffs) or
            len(runs) != len(handoffs) + 1):
        return "proposal_overlap_handoff_chain_invalid"
    if any(
            handoff.get("classification") !=
            "EXACT_PAIRED_JUNCTION_HANDOFF" or
            not isinstance(handoff.get("support_wall"), dict) or
            handoff["support_wall"].get("paired") is not True
            for handoff in handoffs):
        return "proposal_junction_handoff_not_exact"

    source_segment_ids = proposal.get("source_segment_ids")
    if (not isinstance(source_segment_ids, list) or not source_segment_ids or
            any(not str(item or "").strip() for item in source_segment_ids)):
        return "proposal_source_segment_ids_invalid"
    material_source_ids = {str(item) for item in source_segment_ids}

    endpoint_support = proposal.get("endpoint_support")
    if (not isinstance(endpoint_support, list) or len(endpoint_support) != 2 or
            not all(isinstance(item, dict) for item in endpoint_support)):
        return "proposal_strict_endpoint_support_invalid"
    by_index = {item.get("endpoint_index"): item
                for item in endpoint_support}
    if set(by_index) != {0, 1}:
        return "proposal_strict_endpoint_support_invalid"
    for endpoint_index, endpoint in enumerate((start, end)):
        support = by_index[endpoint_index]
        support_point = _point(support.get("point"))
        strict_caps = support.get("strict_cap_matches")
        if (support.get("status") != "EXACT_STRICT_CAP" or
                support_point is None or
                math.dist(endpoint, support_point) >
                _SERIALIZATION_TOLERANCE_M or
                not isinstance(strict_caps, list) or len(strict_caps) != 1 or
                support.get("paired_wall_matches") != [] or
                support.get("single_wall_matches") != []):
            return "proposal_strict_endpoint_support_invalid"
        cap = strict_caps[0]
        if not isinstance(cap, dict):
            return "proposal_strict_endpoint_support_invalid"
        cap_source_ids = cap.get("support_source_segment_ids")
        try:
            endpoint_error = float(cap.get("endpoint_error_m"))
        except (TypeError, ValueError, OverflowError):
            endpoint_error = math.nan
        if (not str(cap.get("review_unit_id") or "").strip() or
                not str(cap.get("entity_handle") or "").strip() or
                not str(cap.get("source_segment_id") or "").strip() or
                not isinstance(cap_source_ids, list) or not cap_source_ids or
                not {str(item) for item in cap_source_ids}.issubset(
                    material_source_ids) or
                not math.isfinite(endpoint_error) or endpoint_error < 0.0):
            return "proposal_strict_endpoint_support_invalid"
    return None


def _application_core_matches(generated: dict, persisted: dict) -> bool:
    """Compare fields deterministically produced by the approval engine."""
    return all(persisted.get(key) == value
               for key, value in generated.items())


def replay_approved_short_wall_decisions(review_ir: dict) -> dict:
    """Replay integrity-protected strict approvals into copied wall geometry.

    The returned walls are an input candidate for architecture Gate
    recomputation only.  This function never changes ``allow_modeling`` and
    never marks the geometry as formally model eligible.
    """
    base_walls = _authoritative_walls(review_ir)
    if not isinstance(review_ir, dict):
        return _result("BLOCKED", base_walls, errors=["review_ir_invalid"])
    try:
        review_ir_integrity_sha256(review_ir)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return _result(
            "BLOCKED", base_walls, errors=["review_ir_not_canonical_json"])
    geometry = review_ir.get("geometry")
    if (not isinstance(geometry, dict) or
            not isinstance(geometry.get("architectural_walls"), list) or
            not all(isinstance(wall, dict)
                    for wall in geometry["architectural_walls"])):
        return _result(
            "BLOCKED", base_walls,
            errors=["authoritative_architectural_walls_invalid"])

    preview = review_ir.get("approved_preview")
    if preview is None:
        return _result("NO_APPROVED_DECISIONS", base_walls)
    if not isinstance(preview, dict):
        return _result(
            "BLOCKED", base_walls, errors=["approved_preview_invalid"])
    applications = preview.get("applications")
    preview_geometry = preview.get("geometry")
    preview_walls = (preview_geometry.get("architectural_walls")
                     if isinstance(preview_geometry, dict) else None)
    if (not isinstance(applications, list) or not applications or
            not isinstance(preview_walls, list)):
        return _result(
            "BLOCKED", base_walls, errors=["approved_preview_state_invalid"])

    proposals = (((review_ir.get("review_candidates") or {}).get(
        "short_wall_proposals") or {}).get("proposals"))
    if not isinstance(proposals, list):
        return _result(
            "BLOCKED", base_walls, errors=["review_ir_proposals_invalid"])

    validated: list[tuple[dict, dict, dict]] = []
    for application in applications:
        if not isinstance(application, dict):
            return _result(
                "BLOCKED", base_walls,
                errors=["approved_application_invalid"])
        decision = application.get("decision")
        proposal_id = str(application.get("proposal_id") or "")
        if not isinstance(decision, dict) or not proposal_id:
            return _result(
                "BLOCKED", base_walls,
                errors=["approved_application_invalid"],
                failed_proposal_id=proposal_id or None)
        replay_decision = copy.deepcopy(decision)
        replay_decision["base_ir_sha256"] = application.get(
            "base_ir_sha256")
        approval_validation = apply_short_wall_review_decision(
            review_ir, replay_decision)
        if approval_validation.get("status") != "ALREADY_APPLIED":
            return _result(
                "BLOCKED", base_walls,
                errors=list(approval_validation.get("errors") or [
                    "approved_decision_integrity_validation_failed"]),
                failed_proposal_id=proposal_id)

        matches = [proposal for proposal in proposals
                   if isinstance(proposal, dict) and
                   str(proposal.get("proposal_id") or "") == proposal_id]
        if len(matches) != 1:
            return _result(
                "BLOCKED", base_walls,
                errors=["review_ir_proposal_not_unique"],
                failed_proposal_id=proposal_id)
        proposal = matches[0]
        strict_error = _strict_contract_error(proposal)
        if strict_error:
            return _result(
                "BLOCKED", base_walls, errors=[strict_error],
                failed_proposal_id=proposal_id)
        if application.get("operation") != "ADD_WALL":
            return _result(
                "BLOCKED", base_walls,
                errors=["short_wall_replacement_replay_not_supported"],
                failed_proposal_id=proposal_id)
        validated.append((application, proposal, replay_decision))

    replayed_walls = copy.deepcopy(base_walls)
    replayed_ids: list[str] = []
    for application, proposal, decision in validated:
        proposal_id = str(proposal["proposal_id"])
        applied = apply_short_wall_proposal_preview(
            replayed_walls, proposal, decision)
        if applied.get("status") != "APPLIED_PREVIEW":
            return _result(
                "BLOCKED", base_walls,
                errors=list(applied.get("errors") or [
                    "short_wall_replay_failed"]),
                failed_proposal_id=proposal_id)
        generated_application = applied.get("application")
        if (not isinstance(generated_application, dict) or
                not _application_core_matches(
                    generated_application, application)):
            return _result(
                "BLOCKED", base_walls,
                errors=["replayed_application_mismatch"],
                failed_proposal_id=proposal_id)
        replayed_walls = applied["walls"]
        applied_walls = [wall for wall in replayed_walls
                         if wall.get("approved_proposal_id") == proposal_id]
        if len(applied_walls) != 1:
            return _result(
                "BLOCKED", base_walls,
                errors=["replayed_wall_not_unique"],
                failed_proposal_id=proposal_id)
        applied_walls[0]["review_application"] = copy.deepcopy(application)
        replayed_ids.append(proposal_id)

    if replayed_walls != preview_walls:
        return _result(
            "BLOCKED", base_walls,
            errors=["replayed_preview_geometry_mismatch"])
    return _result(
        "REPLAYED", replayed_walls, proposal_ids=replayed_ids,
        applications=applications)


def _approved_review_ir(bundle: Any) -> dict | None:
    if not isinstance(bundle, dict):
        return None
    if bundle.get("schema_version") == "buildmate.review-ir/1.0":
        return bundle
    nested = bundle.get("review_ir")
    if (isinstance(nested, dict) and
            nested.get("schema_version") == "buildmate.review-ir/1.0"):
        return nested
    return None


def _validated_declared_gate_base(review_ir: dict) -> tuple[str | None,
                                                            list[str]]:
    declared = review_ir.get("short_wall_gate_base")
    if not isinstance(declared, dict):
        return None, ["approved_bundle_gate_base_missing"]
    digest = _normalised_sha256(declared.get("sha256"))
    if (declared.get("schema_version") != GATE_BASE_SCHEMA_VERSION or
            declared.get("rule_version") != GATE_BASE_RULE_VERSION or
            digest is None):
        return None, ["approved_bundle_gate_base_invalid"]
    try:
        actual = short_wall_gate_base_sha256(review_ir)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None, ["approved_bundle_gate_base_invalid"]
    if actual != digest:
        return None, ["approved_bundle_gate_base_integrity_mismatch"]
    return digest, []


def replay_approved_short_wall_bundle(current_review_ir: dict,
                                      approved_bundle: Any) -> dict:
    """Validate old decisions, then replay them only from current extraction.

    ``approved_bundle`` supplies decisions and immutable evidence only.  Its
    preview walls are validated for tampering but are never returned or copied
    into the current run.  The current and approved Gate bases must have the
    same normalized digest before any proposal is applied.
    """
    current_walls = _authoritative_walls(current_review_ir)
    if not isinstance(current_review_ir, dict):
        return _result(
            "BLOCKED", current_walls, errors=["current_gate_base_invalid"])
    approved_ir = _approved_review_ir(approved_bundle)
    if approved_ir is None:
        return _result(
            "BLOCKED", current_walls, errors=["approved_bundle_invalid"])

    approved_digest, approved_errors = _validated_declared_gate_base(
        approved_ir)
    if approved_errors:
        return _result("BLOCKED", current_walls, errors=approved_errors)
    current_digest, current_errors = _validated_declared_gate_base(
        current_review_ir)
    if current_errors:
        return _result(
            "BLOCKED", current_walls, errors=["current_gate_base_invalid"])
    if current_digest != approved_digest:
        return _result(
            "BLOCKED", current_walls,
            errors=["short_wall_gate_base_mismatch"])

    validated_old = replay_approved_short_wall_decisions(approved_ir)
    if validated_old.get("status") != "REPLAYED":
        errors = list(validated_old.get("errors") or [])
        if validated_old.get("status") == "NO_APPROVED_DECISIONS":
            errors = ["approved_bundle_has_no_decisions"]
        return _result(
            "BLOCKED", current_walls,
            errors=errors or ["approved_bundle_replay_validation_failed"],
            failed_proposal_id=validated_old.get("failed_proposal_id"))

    current_proposals = (((current_review_ir.get("review_candidates") or {})
                          .get("short_wall_proposals") or {})
                         .get("proposals"))
    if not isinstance(current_proposals, list):
        return _result(
            "BLOCKED", current_walls,
            errors=["current_gate_base_proposals_invalid"])
    by_id: dict[str, list[dict]] = {}
    for proposal in current_proposals:
        if isinstance(proposal, dict) and proposal.get("proposal_id"):
            by_id.setdefault(str(proposal["proposal_id"]), []).append(
                proposal)

    applications = validated_old.get("applications") or []
    replayed_walls = copy.deepcopy(current_walls)
    replayed_ids: list[str] = []
    for application in applications:
        proposal_id = str(application.get("proposal_id") or "")
        matches = by_id.get(proposal_id) or []
        if len(matches) != 1:
            return _result(
                "BLOCKED", current_walls,
                errors=["current_gate_base_proposal_not_unique"],
                failed_proposal_id=proposal_id or None)
        proposal = matches[0]
        strict_error = _strict_contract_error(proposal)
        if strict_error:
            return _result(
                "BLOCKED", current_walls, errors=[strict_error],
                failed_proposal_id=proposal_id)
        try:
            proposal_digest = proposal_integrity_sha256(proposal)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return _result(
                "BLOCKED", current_walls,
                errors=["current_proposal_not_canonical_json"],
                failed_proposal_id=proposal_id)
        if (proposal.get("integrity_sha256") != proposal_digest or
                application.get("proposal_integrity_sha256") !=
                proposal_digest or application.get("operation") !=
                "ADD_WALL"):
            return _result(
                "BLOCKED", current_walls,
                errors=["current_proposal_integrity_mismatch"],
                failed_proposal_id=proposal_id)
        decision = application.get("decision")
        if (not isinstance(decision, dict) or
                _normalised_sha256(decision.get("gate_base_sha256")) !=
                current_digest):
            return _result(
                "BLOCKED", current_walls,
                errors=["decision_gate_base_sha256_mismatch"],
                failed_proposal_id=proposal_id)
        applied = apply_short_wall_proposal_preview(
            replayed_walls, proposal, decision)
        if applied.get("status") != "APPLIED_PREVIEW":
            return _result(
                "BLOCKED", current_walls,
                errors=list(applied.get("errors") or [
                    "current_short_wall_replay_failed"]),
                failed_proposal_id=proposal_id)
        generated = applied.get("application")
        if (not isinstance(generated, dict) or
                not _application_core_matches(generated, application)):
            return _result(
                "BLOCKED", current_walls,
                errors=["current_replayed_application_mismatch"],
                failed_proposal_id=proposal_id)
        replayed_walls = applied["walls"]
        replayed_ids.append(proposal_id)

    if len(set(replayed_ids)) != len(replayed_ids):
        return _result(
            "BLOCKED", current_walls,
            errors=["duplicate_approved_proposal"])
    result = _result(
        "REPLAYED", replayed_walls, proposal_ids=replayed_ids,
        applications=applications)
    result["gate_base_sha256"] = current_digest
    result["approved_additions"] = [
        copy.deepcopy(wall) for wall in replayed_walls
        if wall.get("approved_proposal_id") in set(replayed_ids)
    ]
    return result
