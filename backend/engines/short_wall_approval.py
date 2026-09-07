"""Safe, review-only application of short-wall proposal decisions.

The functions in this module are deliberately pure.  They never write a BIM
artifact or call Revit.  A successful application produces a separate preview
geometry inside a copied review IR; authoritative extracted geometry and the
formal modeling Gate remain unchanged.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_POINT_TOLERANCE_M = 0.005
_SUPPORTED_PROPOSAL_STATUSES = {
    "PROPOSED_SHORT_WALL",
    "PROPOSED_WALL_UPGRADE",
}


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_canonical_sha256(value: Any) -> str | None:
    try:
        return _canonical_sha256(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def application_integrity_sha256(application: dict) -> str:
    """Hash one persisted preview application, excluding its own digest."""
    payload = copy.deepcopy(application)
    payload.pop("application_integrity_sha256", None)
    return _canonical_sha256(payload)


def approved_preview_integrity_sha256(preview: dict) -> str:
    """Hash an approved-preview artifact, excluding its own digest."""
    payload = copy.deepcopy(preview)
    payload.pop("integrity_sha256", None)
    return _canonical_sha256(payload)


def proposal_integrity_sha256(proposal: dict) -> str:
    """Hash the immutable evidence and geometry of one proposal."""
    fields = (
        "proposal_id", "pair_group_id", "status", "approval_status",
        "geometry_source", "drawing_identity", "structural_occurrence_id",
        "start", "end", "length_m", "thickness_mm",
        "source_review_unit_ids", "source_segment_ids",
        "material_source_segment_refs",
        "missing_material_source_segment_ids",
        "source_entity_handles", "raw_pair_ids", "overlap_runs",
        "junction_handoffs", "endpoint_support", "blockers",
        "replacement_evidence", "model_geometry_created", "auto_action",
    )
    return _canonical_sha256({key: proposal.get(key) for key in fields})


def wall_precondition_sha256(wall: dict) -> str:
    """Hash the wall state that an atomic logical replacement expects."""
    fields = (
        "id", "logical_id", "start", "end", "thickness", "paired",
        "geometry_source",
        "drawing_identity", "structural_occurrence_id",
        "source_segment_ids", "source_segment_refs", "revision",
        "revision_number", "revision_id",
    )
    return _canonical_sha256({key: wall.get(key) for key in fields})


def review_ir_integrity_sha256(review_ir: dict) -> str:
    """Canonical hash used as the optimistic-lock token for a review IR."""
    return _canonical_sha256(review_ir)


def _normalised_sha256(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text if _SHA256_RE.fullmatch(text) else None


def _point(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        point = [float(value[0]), float(value[1])]
    except (TypeError, ValueError, OverflowError):
        return None
    return point if all(math.isfinite(item) for item in point) else None


def source_reference_from_review_unit(unit: dict) -> dict | None:
    """Build a material reference only from an exact persisted interval."""
    if (not isinstance(unit, dict) or
            unit.get("identity_is_complete") is not True or
            unit.get("identity_limitations") not in ([], ())):
        return None
    source_start = _point(
        unit.get("source_axis_start") or unit.get("source_start"))
    source_end = _point(
        unit.get("source_axis_end") or unit.get("source_end"))
    range_start = _point(unit.get("start"))
    range_end = _point(unit.get("end"))
    if None in (source_start, source_end, range_start, range_end):
        return None
    dx = source_end[0] - source_start[0]
    dy = source_end[1] - source_start[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return None
    ux, uy = dx / length, dy / length
    interval = unit.get("source_interval_m")
    if not isinstance(interval, (list, tuple)) or len(interval) != 2:
        return None
    try:
        low, high = sorted((float(interval[0]), float(interval[1])))
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(low) or not math.isfinite(high) or
            low < -1e-9 or high > length + 1e-6 or high - low <= 1e-9):
        return None

    def projection(point: list[float]) -> tuple[float, float]:
        rx, ry = point[0] - source_start[0], point[1] - source_start[1]
        along = rx * ux + ry * uy
        perpendicular = abs(rx * -uy + ry * ux)
        return along, perpendicular

    first, first_error = projection(range_start)
    second, second_error = projection(range_end)
    if max(first_error, second_error) > _POINT_TOLERANCE_M:
        return None
    projected = sorted((first, second))
    if max(abs(projected[0] - low), abs(projected[1] - high)) > (
            _POINT_TOLERANCE_M):
        return None
    reference = {
        "drawing_identity": unit.get("drawing_identity"),
        "source_occurrence_id": unit.get("source_occurrence_id"),
        "placed_entity_id": unit.get("placed_entity_id"),
        "structural_occurrence_id": unit.get("structural_occurrence_id"),
        "source_segment_id": unit.get("source_segment_id"),
        "entity_handle": unit.get("entity_handle"),
        "source_record_index": unit.get("source_record_index"),
        "segment_index": unit.get("segment_index"),
        "placement_path": copy.deepcopy(unit.get("placement_path") or []),
        "identity_is_complete": True,
        "identity_limitations": [],
        "source_interval_m": [round(low, 6), round(high, 6)],
    }
    return reference if _valid_source_reference(
        reference, str(reference.get("drawing_identity") or ""),
        str(reference.get("structural_occurrence_id") or "")) else None


def _valid_source_reference(reference: Any, drawing_identity: str,
                            occurrence_id: str) -> bool:
    if not isinstance(reference, dict):
        return False
    interval = reference.get("source_interval_m")
    try:
        interval_valid = bool(
            isinstance(interval, (list, tuple)) and len(interval) == 2 and
            all(math.isfinite(float(value)) for value in interval) and
            float(interval[0]) >= 0.0 and
            float(interval[1]) > float(interval[0]))
    except (TypeError, ValueError, OverflowError):
        interval_valid = False
    placement_path = reference.get("placement_path")
    placement_valid = bool(isinstance(placement_path, list) and
                           placement_path)
    if placement_valid:
        for marker in placement_path:
            if (not isinstance(marker, dict) or
                    not marker.get("insert_handle") or
                    not (marker.get("block_name") or marker.get("name")) or
                    "array_index" not in marker):
                placement_valid = False
                break
            transform = marker.get("cumulative_transform")
            matrix = (transform.get("affine_2d")
                      if isinstance(transform, dict) else None)
            try:
                matrix_valid = bool(
                    isinstance(matrix, (list, tuple)) and len(matrix) == 3 and
                    all(isinstance(row, (list, tuple)) and len(row) == 3
                        for row in matrix) and
                    all(math.isfinite(float(value))
                        for row in matrix for value in row))
                if matrix_valid:
                    numeric = [[float(value) for value in row]
                               for row in matrix]
                    determinant = (numeric[0][0] * numeric[1][1] -
                                   numeric[0][1] * numeric[1][0])
                    matrix_valid = bool(
                        abs(numeric[2][0]) <= 1e-9 and
                        abs(numeric[2][1]) <= 1e-9 and
                        abs(numeric[2][2] - 1.0) <= 1e-9 and
                        abs(determinant) > 1e-12)
            except (TypeError, ValueError, OverflowError):
                matrix_valid = False
            if not matrix_valid:
                placement_valid = False
                break
    return bool(
        reference.get("identity_is_complete") is True and
        reference.get("identity_limitations") in ([], ()) and
        reference.get("drawing_identity") == drawing_identity and
        reference.get("structural_occurrence_id") == occurrence_id and
        all(reference.get(key) for key in (
            "source_occurrence_id", "placed_entity_id",
            "source_segment_id", "entity_handle")) and
        isinstance(reference.get("source_record_index"), int) and
        not isinstance(reference.get("source_record_index"), bool) and
        reference.get("source_record_index") >= 0 and
        isinstance(reference.get("segment_index"), int) and
        not isinstance(reference.get("segment_index"), bool) and
        reference.get("segment_index") >= 0 and
        interval_valid and placement_valid
    )


def _proposal_source_references(proposal: dict) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    drawing_identity = str(proposal.get("drawing_identity") or "")
    occurrence_id = str(proposal.get("structural_occurrence_id") or "")
    declared_ids = proposal.get("source_segment_ids")
    if not isinstance(declared_ids, (list, tuple)) or any(
            not str(value or "").strip() for value in declared_ids):
        return [], ["proposal_source_segment_ids_invalid"]
    expected_ids = {
        str(value) for value in declared_ids
    }
    if not expected_ids:
        return [], ["proposal_source_segment_ids_missing"]

    declared_references = proposal.get("material_source_segment_refs")
    if not isinstance(declared_references, (list, tuple)) or not (
            declared_references):
        return [], ["proposal_source_refs_missing"]
    if any(not _valid_source_reference(
            reference, drawing_identity, occurrence_id)
            for reference in declared_references):
        return [], ["proposal_source_refs_invalid"]
    references = copy.deepcopy(list(declared_references))

    unique: dict[str, dict] = {}
    for reference in references:
        unique[_canonical_sha256(reference)] = reference
    if len(unique) != len(references):
        errors.append("proposal_source_refs_duplicated")
    references = sorted(
        unique.values(),
        key=lambda reference: (
            str(reference.get("source_segment_id") or ""),
            tuple(float(value) for value in
                  reference.get("source_interval_m") or ()),
            _canonical_sha256(reference),
        ),
    )
    actual_ids = {
        str(reference.get("source_segment_id")) for reference in references
        if reference.get("source_segment_id")
    }
    missing_ids = sorted(expected_ids - actual_ids)
    extra_ids = sorted(actual_ids - expected_ids)
    if missing_ids:
        errors.append("proposal_source_refs_missing:" + ",".join(missing_ids))
    if extra_ids:
        errors.append("proposal_source_refs_unexpected:" + ",".join(extra_ids))
    if proposal.get("missing_material_source_segment_ids"):
        errors.append("proposal_declares_incomplete_material_sources")
    return references, errors


def _deduplicated_references(references: list[dict]) -> list[dict]:
    unique = {_canonical_sha256(reference): reference
              for reference in references}
    return [unique[key] for key in sorted(unique)]


def _complete_wall_source_references(wall: dict) -> list[dict] | None:
    declared_ids = wall.get("source_segment_ids")
    references = wall.get("source_segment_refs")
    if (not isinstance(declared_ids, list) or not declared_ids or
            any(not str(value or "").strip() for value in declared_ids) or
            not isinstance(references, list) or not references):
        return None
    reference_ids: list[str] = []
    for reference in references:
        if (not isinstance(reference, dict) or
                not _valid_source_reference(
                    reference,
                    str(reference.get("drawing_identity") or ""),
                    str(reference.get("structural_occurrence_id") or ""))):
            return None
        source_id = str(reference.get("source_segment_id") or "")
        if source_id not in reference_ids:
            reference_ids.append(source_id)
    return references if [str(value) for value in declared_ids] == (
        reference_ids) else None


def _proposal_refs_persisted_in_review_ir(
        review_ir: dict, proposal: dict) -> list[str]:
    """Bind proposal material refs to exact evidence persisted in this IR."""
    proposal_references, errors = _proposal_source_references(proposal)
    if errors:
        return errors
    unit_ids = proposal.get("source_review_unit_ids")
    if (not isinstance(unit_ids, (list, tuple)) or not unit_ids or
            any(not str(value or "").strip() for value in unit_ids) or
            len({str(value) for value in unit_ids}) != len(unit_ids)):
        return ["proposal_material_refs_not_persisted"]
    units = ((review_ir.get("review_candidates") or {}).get(
        "semantic_wall_source_units"))
    if not isinstance(units, list):
        return ["proposal_material_refs_not_persisted"]
    units_by_id: dict[str, list[dict]] = {}
    for unit in units:
        if not isinstance(unit, dict) or not unit.get("review_unit_id"):
            continue
        units_by_id.setdefault(str(unit["review_unit_id"]), []).append(unit)

    expected_ids = {str(value) for value in proposal["source_segment_ids"]}
    persisted_references: list[dict] = []
    for unit_id in unit_ids:
        matches = units_by_id.get(str(unit_id)) or []
        if len(matches) != 1:
            return ["proposal_material_refs_not_persisted"]
        unit = matches[0]
        if str(unit.get("source_segment_id") or "") not in expected_ids:
            continue
        reference = source_reference_from_review_unit(unit)
        if (reference is None or not _valid_source_reference(
                reference, str(proposal.get("drawing_identity") or ""),
                str(proposal.get("structural_occurrence_id") or ""))):
            return ["proposal_material_refs_not_persisted"]
        persisted_references.append(reference)

    if proposal.get("status") == "PROPOSED_WALL_UPGRADE":
        replacement_evidence = proposal.get("replacement_evidence")
        if not isinstance(replacement_evidence, dict):
            return ["proposal_material_refs_not_persisted"]
        target_id = str(replacement_evidence.get("existing_wall_id") or "")
        authoritative_walls = ((review_ir.get("geometry") or {}).get(
            "architectural_walls"))
        if not isinstance(authoritative_walls, list):
            return ["proposal_material_refs_not_persisted"]
        targets = [wall for wall in authoritative_walls
                   if isinstance(wall, dict) and
                   str(wall.get("id") or "") == target_id]
        if len(targets) != 1:
            return ["proposal_material_refs_not_persisted"]
        target_refs = targets[0].get("source_segment_refs")
        if not isinstance(target_refs, list):
            return ["proposal_material_refs_not_persisted"]
        for reference in target_refs:
            if (not isinstance(reference, dict) or
                    str(reference.get("source_segment_id") or "") not in
                    expected_ids):
                continue
            if not _valid_source_reference(
                    reference, str(proposal.get("drawing_identity") or ""),
                    str(proposal.get("structural_occurrence_id") or "")):
                return ["proposal_material_refs_not_persisted"]
            persisted_references.append(copy.deepcopy(reference))

    try:
        persisted = _deduplicated_references(persisted_references)
        proposed = _deduplicated_references(proposal_references)
        persisted_hashes = [_canonical_sha256(item) for item in persisted]
        proposed_hashes = [_canonical_sha256(item) for item in proposed]
    except (TypeError, ValueError, OverflowError):
        return ["proposal_material_refs_not_persisted"]
    if persisted_hashes != proposed_hashes:
        return ["proposal_material_refs_not_persisted"]
    return []


def _same_geometry(wall: dict, proposal: dict) -> bool:
    wall_start, wall_end = _point(wall.get("start")), _point(wall.get("end"))
    start, end = _point(proposal.get("start")), _point(proposal.get("end"))
    if None in (wall_start, wall_end, start, end):
        return False
    direct = max(math.dist(wall_start, start), math.dist(wall_end, end))
    reverse = max(math.dist(wall_start, end), math.dist(wall_end, start))
    try:
        thickness_error = abs(
            float(wall.get("thickness")) -
            float(proposal.get("thickness_mm")))
    except (TypeError, ValueError, OverflowError):
        return False
    return min(direct, reverse) <= 1e-6 and thickness_error <= 1e-6


def _references_overlap(first: dict, second: dict) -> bool:
    if any(first.get(key) != second.get(key) for key in (
            "drawing_identity", "structural_occurrence_id",
            "source_segment_id")):
        return False
    first_interval = first.get("source_interval_m")
    second_interval = second.get("source_interval_m")
    try:
        first_low, first_high = sorted((float(first_interval[0]),
                                        float(first_interval[1])))
        second_low, second_high = sorted((float(second_interval[0]),
                                          float(second_interval[1])))
    except (IndexError, TypeError, ValueError, OverflowError):
        return True
    return min(first_high, second_high) - max(
        first_low, second_low) > 1e-9


def _ordered_endpoints(first: list[float],
                       second: list[float]) -> tuple[list[float], list[float]]:
    ordered = sorted((tuple(first), tuple(second)))
    return list(ordered[0]), list(ordered[1])


def _logical_wall_id(proposal: dict, references: list[dict],
                     start: list[float], end: list[float]) -> str:
    signature = {
        "drawing_identity": proposal.get("drawing_identity"),
        "architectural_occurrence_id": proposal.get(
            "structural_occurrence_id"),
        "start": start,
        "end": end,
        "thickness_mm": float(proposal.get("thickness_mm")),
        "material_sources": [{
            "source_segment_id": reference.get("source_segment_id"),
            "source_interval_m": reference.get("source_interval_m"),
        } for reference in references],
    }
    return "wall_short_" + _canonical_sha256(signature)[:16]


def _wall_revision_id(wall: dict) -> str:
    payload = copy.deepcopy(wall)
    payload.pop("revision_id", None)
    return "wall_revision_" + wall_precondition_sha256(payload)[:16]


def _blocked(walls: list[dict], *errors: str) -> dict:
    return {
        "status": "BLOCKED",
        "mode": "IR_PREVIEW_ONLY",
        "walls": walls,
        "application": None,
        "errors": list(errors),
    }


def _review_blocked(review_ir: dict, *errors: str) -> dict:
    return {"status": "BLOCKED", "review_ir": review_ir,
            "application": None, "errors": list(errors)}


def _review_ir_shape_valid(review_ir: dict) -> bool:
    source = review_ir.get("source")
    quality_gate = review_ir.get("quality_gate")
    geometry = review_ir.get("geometry")
    candidates = review_ir.get("review_candidates")
    if not all(isinstance(value, dict) for value in (
            source, quality_gate, geometry, candidates)):
        return False
    short_walls = candidates.get("short_wall_proposals")
    return bool(
        isinstance(short_walls, dict) and
        isinstance(short_walls.get("proposals"), list) and
        isinstance(candidates.get("semantic_wall_source_units"), list) and
        isinstance(geometry.get("architectural_walls"), list)
    )


def _validate_approved_preview(preview: Any, review_ir: dict) -> list[str]:
    """Validate the whole persisted preview before reuse or extension."""
    if not isinstance(preview, dict):
        return ["approved_preview_invalid"]
    if _safe_canonical_sha256(preview) is None:
        return ["approved_preview_not_canonical_json"]
    try:
        actual_preview_digest = approved_preview_integrity_sha256(preview)
    except (TypeError, ValueError, OverflowError):
        return ["approved_preview_not_canonical_json"]
    if preview.get("integrity_sha256") != actual_preview_digest:
        return ["approved_preview_integrity_mismatch"]
    source_sha256 = _normalised_sha256(
        (review_ir.get("source") or {}).get("sha256"))
    applications = preview.get("applications")
    walls = ((preview.get("geometry") or {}).get("architectural_walls"))
    revision = preview.get("revision")
    if (preview.get("schema_version") !=
            "buildmate.approved-preview/1.0" or
            preview.get("status") != "REVIEW_ONLY" or
            preview.get("formal_model_eligible") is not False or
            _normalised_sha256(preview.get("source_sha256")) !=
            source_sha256 or
            isinstance(revision, bool) or not isinstance(revision, int) or
            revision <= 0 or not isinstance(applications, list) or
            revision != len(applications) or
            not isinstance(walls, list) or
            not all(isinstance(wall, dict) for wall in walls)):
        return ["approved_preview_state_invalid"]

    proposals = ((review_ir.get("review_candidates") or {}).get(
        "short_wall_proposals") or {}).get("proposals")
    if not isinstance(proposals, list):
        return ["approved_preview_application_integrity_mismatch"]
    proposals_by_id: dict[str, list[dict]] = {}
    for proposal in proposals:
        if isinstance(proposal, dict) and proposal.get("proposal_id"):
            proposals_by_id.setdefault(
                str(proposal["proposal_id"]), []).append(proposal)

    decision_ids: set[str] = set()
    latest_by_wall: dict[str, dict] = {}
    for application in applications:
        if not isinstance(application, dict):
            return ["approved_preview_application_integrity_mismatch"]
        try:
            application_digest = application_integrity_sha256(application)
        except (TypeError, ValueError, OverflowError):
            return ["approved_preview_application_not_canonical_json"]
        if application.get(
                "application_integrity_sha256") != application_digest:
            return ["approved_preview_application_integrity_mismatch"]
        decision = application.get("decision")
        if not isinstance(decision, dict):
            return ["approved_preview_application_integrity_mismatch"]
        decision_id = str(decision.get("decision_id") or "")
        proposal_id = str(application.get("proposal_id") or "")
        wall_id = str(application.get("wall_id") or "")
        operation = application.get("operation")
        previous_wall_digest = application.get(
            "previous_wall_precondition_sha256")
        if (not decision_id or decision_id in decision_ids or
                not proposal_id or not wall_id or
                application.get("schema_version") !=
                "buildmate.short-wall-application/1.0" or
                application.get("mode") != "IR_PREVIEW_ONLY" or
                operation not in {"ADD_WALL", "REPLACE_WALL"} or
                application.get("formal_model_eligible") is not False or
                application.get("revit_execution_status") !=
                "NOT_EXECUTED" or decision.get("decision") != "approved" or
                not str(decision.get("operator") or "").strip() or
                decision.get("proposal_id") != proposal_id or
                decision.get("proposal_integrity_sha256") !=
                application.get("proposal_integrity_sha256") or
                _normalised_sha256(decision.get("source_sha256")) !=
                source_sha256):
            return ["approved_preview_application_integrity_mismatch"]
        gate_base = review_ir.get("short_wall_gate_base")
        if gate_base is not None:
            expected_gate_base = (_normalised_sha256(
                gate_base.get("sha256"))
                if isinstance(gate_base, dict) else None)
            if (expected_gate_base is None or
                    _normalised_sha256(decision.get(
                        "gate_base_sha256")) != expected_gate_base):
                return ["approved_preview_application_integrity_mismatch"]
        decision_ids.add(decision_id)
        matching_proposals = proposals_by_id.get(proposal_id) or []
        if len(matching_proposals) != 1:
            return ["approved_preview_application_integrity_mismatch"]
        try:
            proposal_digest = proposal_integrity_sha256(
                matching_proposals[0])
        except (TypeError, ValueError, OverflowError):
            return ["approved_preview_application_integrity_mismatch"]
        if (matching_proposals[0].get("integrity_sha256") !=
                proposal_digest or application.get(
                    "proposal_integrity_sha256") != proposal_digest):
            return ["approved_preview_application_integrity_mismatch"]
        if (decision.get("architectural_occurrence_id") !=
                matching_proposals[0].get("structural_occurrence_id") or
                _normalised_sha256(application.get("base_ir_sha256")) is
                None or _normalised_sha256(application.get(
                    "new_wall_precondition_sha256")) is None or
                operation == "ADD_WALL" and (
                    previous_wall_digest is not None or application.get(
                        "revit_action_required") != "CREATE") or
                operation == "REPLACE_WALL" and (
                    _normalised_sha256(previous_wall_digest) is None or
                    application.get("revit_action_required") !=
                    "ATOMIC_UPDATE_IN_PLACE")):
            return ["approved_preview_application_integrity_mismatch"]
        request_payload = {
            "decision_id": decision.get("decision_id"),
            "decision": decision.get("decision"),
            "operator": decision.get("operator"),
            "comment": decision.get("comment"),
            "proposal_id": decision.get("proposal_id"),
            "proposal_integrity_sha256": decision.get(
                "proposal_integrity_sha256"),
            "source_sha256": decision.get("source_sha256"),
            "architectural_occurrence_id": decision.get(
                "architectural_occurrence_id"),
            "base_ir_sha256": application.get("base_ir_sha256"),
        }
        if gate_base is not None:
            request_payload["gate_base_sha256"] = decision.get(
                "gate_base_sha256")
        stored_request_digest = _safe_canonical_sha256(request_payload)
        if (stored_request_digest is None or stored_request_digest !=
                application.get("decision_request_sha256")):
            return ["approved_preview_application_integrity_mismatch"]
        previous = latest_by_wall.get(wall_id)
        if (previous is not None and application.get(
                "previous_wall_precondition_sha256") != previous.get(
                    "new_wall_precondition_sha256")):
            return ["approved_preview_wall_integrity_mismatch"]
        latest_by_wall[wall_id] = application

    if (not applications or preview.get("parent_ir_sha256") !=
            applications[-1].get("base_ir_sha256")):
        return ["approved_preview_state_invalid"]
    for wall_id, application in latest_by_wall.items():
        matching_walls = [wall for wall in walls
                          if str(wall.get("id") or "") == wall_id]
        if len(matching_walls) != 1:
            return ["approved_preview_wall_integrity_mismatch"]
        wall = matching_walls[0]
        try:
            wall_digest = wall_precondition_sha256(wall)
        except (TypeError, ValueError, OverflowError):
            return ["approved_preview_wall_integrity_mismatch"]
        revision = wall.get("revision")
        revision_number = wall.get("revision_number")
        if (wall_digest != application.get(
                "new_wall_precondition_sha256") or
                wall.get("approved_proposal_id") !=
                application.get("proposal_id") or
                wall.get("proposal_integrity_sha256") !=
                application.get("proposal_integrity_sha256") or
                wall.get("formal_model_eligible") is not False or
                wall.get("revit_execution_status") != "NOT_EXECUTED" or
                wall.get("geometry_source") != "DXF_VECTOR" or
                not str(wall.get("logical_id") or "").strip() or
                isinstance(revision, bool) or not isinstance(revision, int) or
                revision <= 0 or revision_number != revision or
                wall.get("revision_id") != _wall_revision_id(wall) or
                _complete_wall_source_references(wall) is None):
            return ["approved_preview_wall_integrity_mismatch"]
        if wall.get("review_application") != application:
            return ["approved_preview_wall_application_mismatch"]
    return []


def apply_short_wall_proposal_preview(
        walls: list[dict], proposal: dict, decision: dict) -> dict:
    """Apply one explicit decision to copied wall geometry for review only."""
    if not isinstance(walls, list) or not all(
            isinstance(wall, dict) for wall in walls):
        return _blocked(walls, "architectural_walls_invalid")
    if not isinstance(proposal, dict) or not isinstance(decision, dict):
        return _blocked(walls, "proposal_or_decision_invalid")
    if _safe_canonical_sha256(walls) is None:
        return _blocked(walls, "architectural_walls_not_canonical_json")
    if _safe_canonical_sha256(proposal) is None:
        return _blocked(walls, "proposal_not_canonical_json")
    if _safe_canonical_sha256(decision) is None:
        return _blocked(walls, "decision_not_canonical_json")
    if decision.get("decision") != "approved":
        return _blocked(walls, "decision_must_be_approved")
    required_decision_fields = (
        "decision_id", "operator", "proposal_id",
        "proposal_integrity_sha256", "source_sha256",
        "architectural_occurrence_id",
    )
    missing = [field for field in required_decision_fields
               if not str(decision.get(field) or "").strip()]
    if missing:
        return _blocked(walls, "decision_fields_missing:" + ",".join(missing))
    if decision.get("proposal_id") != proposal.get("proposal_id"):
        return _blocked(walls, "decision_proposal_id_mismatch")
    expected_integrity = proposal.get("integrity_sha256")
    try:
        actual_integrity = proposal_integrity_sha256(proposal)
    except (TypeError, ValueError, OverflowError):
        return _blocked(walls, "proposal_not_canonical_json")
    if (not expected_integrity or expected_integrity != actual_integrity or
            decision.get("proposal_integrity_sha256") != actual_integrity):
        return _blocked(walls, "proposal_integrity_mismatch")
    source_sha256 = _normalised_sha256(decision.get("source_sha256"))
    proposal_sha256 = _normalised_sha256(proposal.get("drawing_identity"))
    if source_sha256 is None or source_sha256 != proposal_sha256:
        return _blocked(walls, "drawing_identity_mismatch")
    gate_base_sha256 = decision.get("gate_base_sha256")
    if (gate_base_sha256 is not None and
            _normalised_sha256(gate_base_sha256) is None):
        return _blocked(walls, "gate_base_sha256_invalid")
    if (decision.get("architectural_occurrence_id") !=
            proposal.get("structural_occurrence_id")):
        return _blocked(walls, "architectural_occurrence_mismatch")
    if (proposal.get("status") not in _SUPPORTED_PROPOSAL_STATUSES or
            proposal.get("geometry_source") != "DXF_VECTOR" or
            proposal.get("auto_action") != "NONE" or
            proposal.get("model_geometry_created") is not False or
            not proposal.get("structural_occurrence_id")):
        return _blocked(walls, "proposal_not_review_applicable")
    start, end = _point(proposal.get("start")), _point(proposal.get("end"))
    try:
        thickness_mm = float(proposal.get("thickness_mm"))
    except (TypeError, ValueError, OverflowError):
        thickness_mm = math.nan
    if (start is None or end is None or math.dist(start, end) <= 1e-6 or
            not math.isfinite(thickness_mm) or
            not 80.0 <= thickness_mm <= 600.0):
        return _blocked(walls, "proposal_geometry_invalid")
    start, end = _ordered_endpoints(start, end)

    proposal_id = str(proposal["proposal_id"])
    for wall in walls:
        if wall.get("approved_proposal_id") == proposal_id:
            if wall.get("proposal_integrity_sha256") == actual_integrity:
                return {
                    "status": "ALREADY_APPLIED",
                    "mode": "IR_PREVIEW_ONLY",
                    "walls": walls,
                    "application": copy.deepcopy(
                        wall.get("review_application")),
                    "errors": [],
                }
            return _blocked(walls, "proposal_id_reused_with_new_integrity")

    status = proposal.get("status")
    raw_blockers = proposal.get("blockers")
    if (not isinstance(raw_blockers, list) or
            any(not isinstance(item, str) or not item.strip()
                for item in raw_blockers)):
        return _blocked(walls, "proposal_blockers_invalid")
    blockers = set(raw_blockers)
    existing_wall = None
    wall_index = None
    operation = "ADD_WALL"
    if status == "PROPOSED_SHORT_WALL":
        if blockers:
            return _blocked(walls, "proposal_has_blockers:" +
                            ",".join(sorted(str(item) for item in blockers)))
        if any(_same_geometry(wall, proposal) for wall in walls):
            return _blocked(walls, "proposal_geometry_already_present")
    else:
        operation = "REPLACE_WALL"
        if blockers:
            return _blocked(walls, "proposal_has_blockers:" +
                            ",".join(sorted(str(item) for item in blockers)))
        evidence = proposal.get("replacement_evidence")
        if not isinstance(evidence, dict):
            return _blocked(walls, "replacement_evidence_invalid")
        if evidence.get("required_action") != "ATOMIC_REPLACE_SINGLE_WALL":
            return _blocked(walls, "replacement_action_invalid")
        wall_id = str(evidence.get("existing_wall_id") or "")
        matches = [(index, wall) for index, wall in enumerate(walls)
                   if str(wall.get("id") or "") == wall_id]
        if len(matches) != 1:
            return _blocked(walls, "replacement_wall_not_unique")
        wall_index, existing_wall = matches[0]
        evidence_source_ids = evidence.get(
            "existing_wall_source_segment_ids")
        if (not isinstance(evidence_source_ids, list) or
                any(not str(value or "").strip()
                    for value in evidence_source_ids)):
            return _blocked(walls, "replacement_evidence_invalid")
        if (existing_wall.get("paired") is not False or
                sorted(str(value) for value in
                       existing_wall.get("source_segment_ids") or []) !=
                sorted(str(value) for value in
                       evidence_source_ids)):
            return _blocked(walls, "replacement_wall_source_precondition_failed")
        expected_wall_digest = evidence.get(
            "expected_existing_wall_fingerprint")
        if (not expected_wall_digest or
                wall_precondition_sha256(existing_wall) !=
                expected_wall_digest):
            return _blocked(walls, "replacement_wall_digest_mismatch")

    source_references, source_errors = _proposal_source_references(proposal)
    if source_errors:
        return _blocked(walls, *source_errors)
    for wall in walls:
        wall_references = _complete_wall_source_references(wall)
        if wall_references is None:
            return _blocked(
                walls, "existing_wall_source_provenance_incomplete")
        if existing_wall is not None and wall is existing_wall:
            continue
        if any(_references_overlap(reference, wall_reference)
               for reference in source_references
               for wall_reference in wall_references):
            return _blocked(walls, "proposal_material_source_already_owned")

    decision_record = {
        "decision_id": str(decision["decision_id"]),
        "decision": "approved",
        "operator": str(decision["operator"]),
        "comment": str(decision.get("comment") or ""),
        "proposal_id": proposal_id,
        "proposal_integrity_sha256": actual_integrity,
        "source_sha256": source_sha256,
        "architectural_occurrence_id": proposal.get(
            "structural_occurrence_id"),
    }
    if gate_base_sha256 is not None:
        decision_record["gate_base_sha256"] = _normalised_sha256(
            gate_base_sha256)
    new_walls = copy.deepcopy(walls)
    if existing_wall is None:
        wall_id = _logical_wall_id(
            proposal, source_references, start, end)
        if any(str(wall.get("id") or "") == wall_id for wall in walls):
            return _blocked(walls, "generated_wall_id_collision")
        new_wall = {
            "id": wall_id,
            "start": start,
            "end": end,
            "thickness": thickness_mm,
            "paired": True,
            "geometry_source": "DXF_VECTOR",
            "drawing_identity": proposal.get("drawing_identity"),
            "structural_occurrence_id": proposal.get(
                "structural_occurrence_id"),
            "source_segment_ids": sorted({
                str(value) for value in proposal.get("source_segment_ids") or []
            }),
            "source_segment_refs": source_references,
            "revision": 1,
            "revision_number": 1,
            "supersedes_revision": None,
            "supersedes_revision_id": None,
            "logical_id": wall_id,
        }
        new_wall["revision_id"] = _wall_revision_id(new_wall)
        new_walls.append(new_wall)
        previous_wall_digest = None
        revit_action = "CREATE"
    else:
        new_wall = copy.deepcopy(existing_wall)
        previous_revision = existing_wall.get(
            "revision_number", existing_wall.get("revision", 0))
        declared_revision = existing_wall.get("revision")
        if (isinstance(previous_revision, bool) or
                not isinstance(previous_revision, int) or
                previous_revision < 0 or
                declared_revision is not None and (
                    isinstance(declared_revision, bool) or
                    not isinstance(declared_revision, int) or
                    declared_revision != previous_revision)):
            return _blocked(walls, "replacement_wall_revision_invalid")
        new_wall.update({
            "start": start,
            "end": end,
            "thickness": thickness_mm,
            "paired": True,
            "geometry_source": "DXF_VECTOR",
            "drawing_identity": proposal.get("drawing_identity"),
            "structural_occurrence_id": proposal.get(
                "structural_occurrence_id"),
            "source_segment_ids": sorted({
                str(value) for value in proposal.get("source_segment_ids") or []
            }),
            "source_segment_refs": source_references,
            "revision": previous_revision + 1,
            "revision_number": previous_revision + 1,
            "supersedes_revision": previous_revision,
            "supersedes_revision_id": (
                existing_wall.get("revision_id") or
                _wall_revision_id(existing_wall)),
            "logical_id": existing_wall.get("logical_id") or
            existing_wall.get("id"),
        })
        new_wall["revision_id"] = _wall_revision_id(new_wall)
        new_walls[wall_index] = new_wall
        previous_wall_digest = wall_precondition_sha256(existing_wall)
        revit_action = "ATOMIC_UPDATE_IN_PLACE"

    application = {
        "schema_version": "buildmate.short-wall-application/1.0",
        "mode": "IR_PREVIEW_ONLY",
        "operation": operation,
        "proposal_id": proposal_id,
        "proposal_integrity_sha256": actual_integrity,
        "wall_id": new_wall["id"],
        "previous_wall_precondition_sha256": previous_wall_digest,
        "new_wall_precondition_sha256": wall_precondition_sha256(new_wall),
        "decision": decision_record,
        "formal_model_eligible": False,
        "revit_action_required": revit_action,
        "revit_execution_status": "NOT_EXECUTED",
    }
    derivation = list(new_wall.get("derivation") or [])
    derivation.append({
        "operation": "review_approved_short_wall_preview",
        "proposal_id": proposal_id,
        "proposal_integrity_sha256": actual_integrity,
        "parent_source_segment_ids": copy.deepcopy(
            new_wall["source_segment_ids"]),
        "formal_model_eligible": False,
    })
    new_wall.update({
        "approved_proposal_id": proposal_id,
        "proposal_integrity_sha256": actual_integrity,
        "derivation": derivation,
        "application_trace": copy.deepcopy(decision_record),
        "review_application": copy.deepcopy(application),
        "formal_model_eligible": False,
        "revit_execution_status": "NOT_EXECUTED",
    })
    if existing_wall is None:
        new_walls[-1] = new_wall
    else:
        new_walls[wall_index] = new_wall
    return {
        "status": "APPLIED_PREVIEW",
        "mode": "IR_PREVIEW_ONLY",
        "walls": new_walls,
        "application": application,
        "errors": [],
    }


def apply_short_wall_review_decision(review_ir: dict,
                                     decision: dict) -> dict:
    """Attach a safe preview to a copied review IR without opening the Gate."""
    if not isinstance(review_ir, dict) or not isinstance(decision, dict):
        return _review_blocked(review_ir, "review_ir_invalid")
    if _safe_canonical_sha256(decision) is None:
        return _review_blocked(review_ir, "decision_not_canonical_json")
    if _safe_canonical_sha256(review_ir) is None:
        return _review_blocked(review_ir, "review_ir_not_canonical_json")
    if not _review_ir_shape_valid(review_ir):
        return _review_blocked(review_ir, "review_ir_shape_invalid")
    if (review_ir.get("artifact_role") != "REVIEW_ONLY" or
            review_ir.get("quality_gate", {}).get("allow_modeling") is not
            False):
        return _review_blocked(
            review_ir, "review_ir_not_blocked_review_artifact")
    required_decision_fields = (
        "decision_id", "operator", "proposal_id",
        "proposal_integrity_sha256", "source_sha256",
        "architectural_occurrence_id", "base_ir_sha256",
    )
    gate_base = review_ir.get("short_wall_gate_base")
    if gate_base is not None:
        required_decision_fields += ("gate_base_sha256",)
    missing = [field for field in required_decision_fields
               if not str(decision.get(field) or "").strip()]
    if missing:
        return _review_blocked(
            review_ir, "decision_fields_missing:" + ",".join(missing))
    decision_id = str(decision.get("decision_id") or "")
    request_payload = {
        key: decision.get(key) for key in (
            "decision_id", "decision", "operator", "comment",
            "proposal_id", "proposal_integrity_sha256", "source_sha256",
            "architectural_occurrence_id", "base_ir_sha256",
        )
    }
    if gate_base is not None:
        request_payload["gate_base_sha256"] = decision.get(
            "gate_base_sha256")
    request_sha256 = _safe_canonical_sha256(request_payload)
    if request_sha256 is None:
        return _review_blocked(review_ir, "decision_not_canonical_json")
    source_sha256 = _normalised_sha256(
        (review_ir.get("source") or {}).get("sha256"))
    decision_sha256 = _normalised_sha256(decision.get("source_sha256"))
    if source_sha256 is None or source_sha256 != decision_sha256:
        return _review_blocked(
            review_ir, "review_ir_source_identity_mismatch")
    if gate_base is not None:
        declared_gate_base_sha256 = (_normalised_sha256(
            gate_base.get("sha256"))
            if isinstance(gate_base, dict) else None)
        if declared_gate_base_sha256 is None:
            return _review_blocked(review_ir, "gate_base_sha256_invalid")
        if (_normalised_sha256(decision.get("gate_base_sha256")) !=
                declared_gate_base_sha256):
            return _review_blocked(review_ir, "gate_base_sha256_mismatch")

    audit = ((review_ir.get("review_candidates") or {}).get(
        "short_wall_proposals") or {})
    proposal_id = str(decision.get("proposal_id") or "")
    audit_proposals = audit.get("proposals")
    if not isinstance(audit_proposals, list):
        return _review_blocked(review_ir, "review_ir_proposal_not_unique")
    proposals = [proposal for proposal in audit_proposals
                  if isinstance(proposal, dict) and
                  str(proposal.get("proposal_id") or "") == proposal_id]
    if len(proposals) != 1:
        return _review_blocked(review_ir, "review_ir_proposal_not_unique")
    proposal = proposals[0]
    try:
        proposal_digest = proposal_integrity_sha256(proposal)
    except (TypeError, ValueError, OverflowError):
        return _review_blocked(review_ir, "proposal_not_canonical_json")
    if (proposal.get("integrity_sha256") != proposal_digest or
            decision.get("proposal_integrity_sha256") != proposal_digest):
        return _review_blocked(review_ir, "proposal_integrity_mismatch")
    raw_blockers = proposal.get("blockers")
    if (not isinstance(raw_blockers, list) or
            any(not isinstance(item, str) or not item.strip()
                for item in raw_blockers)):
        return _review_blocked(review_ir, "proposal_blockers_invalid")
    if raw_blockers:
        return _review_blocked(
            review_ir, "proposal_has_blockers:" +
            ",".join(sorted(set(raw_blockers))))
    source_errors = _proposal_refs_persisted_in_review_ir(
        review_ir, proposal)
    if source_errors:
        return _review_blocked(review_ir, *source_errors)

    previous_applications: list[dict] = []
    previous_revision = 0
    preview = review_ir.get("approved_preview")
    if preview is not None:
        preview_errors = _validate_approved_preview(preview, review_ir)
        if preview_errors:
            return _review_blocked(review_ir, *preview_errors)
        previous_applications = list(preview["applications"])
        previous_revision = int(preview["revision"])
        matching_decisions = [
            application for application in previous_applications
            if str((application.get("decision") or {}).get(
                "decision_id") or "") == decision_id
        ]
        if matching_decisions:
            application = matching_decisions[0]
            if (len(matching_decisions) == 1 and
                    application.get("decision_request_sha256") ==
                    request_sha256 and
                    application.get("base_ir_sha256") ==
                    decision.get("base_ir_sha256")):
                return {"status": "ALREADY_APPLIED",
                        "review_ir": review_ir,
                        "application": copy.deepcopy(application),
                        "errors": []}
            return _review_blocked(
                review_ir, "decision_idempotency_conflict")
        base_walls = preview["geometry"]["architectural_walls"]
    else:
        base_walls = ((review_ir.get("geometry") or {}).get(
            "architectural_walls"))
    if not isinstance(base_walls, list):
        return _review_blocked(review_ir, "architectural_walls_invalid")

    try:
        base_ir_sha256 = review_ir_integrity_sha256(review_ir)
    except (TypeError, ValueError, OverflowError):
        return _review_blocked(review_ir, "review_ir_not_canonical_json")
    if decision.get("base_ir_sha256") != base_ir_sha256:
        return _review_blocked(review_ir, "stale_review_ir")
    result = apply_short_wall_proposal_preview(
        base_walls, proposal, decision)
    if result["status"] not in {"APPLIED_PREVIEW", "ALREADY_APPLIED"}:
        return {"status": result["status"], "review_ir": review_ir,
                "application": result.get("application"),
                "errors": result.get("errors", [])}
    if result["status"] == "ALREADY_APPLIED":
        return _review_blocked(
            review_ir, "proposal_already_applied_by_other_decision")

    copied = copy.deepcopy(review_ir)
    application = copy.deepcopy(result["application"])
    application["base_ir_sha256"] = base_ir_sha256
    application["decision_request_sha256"] = request_sha256
    application["application_integrity_sha256"] = (
        application_integrity_sha256(application))
    previous_applications.append(application)
    preview_walls = copy.deepcopy(result["walls"])
    applied_walls = [wall for wall in preview_walls
                     if wall.get("id") == application["wall_id"] and
                     wall.get("approved_proposal_id") ==
                     application["proposal_id"]]
    if len(applied_walls) != 1:
        return _review_blocked(
            review_ir, "approved_preview_wall_integrity_mismatch")
    applied_walls[0]["review_application"] = copy.deepcopy(application)
    approved_preview = {
        "schema_version": "buildmate.approved-preview/1.0",
        "status": "REVIEW_ONLY",
        "formal_model_eligible": False,
        "source_sha256": source_sha256,
        "revision": previous_revision + 1,
        "parent_ir_sha256": base_ir_sha256,
        "applications": previous_applications,
        "geometry": {
            "architectural_walls": preview_walls,
        },
    }
    approved_preview["integrity_sha256"] = (
        approved_preview_integrity_sha256(approved_preview))
    copied["approved_preview"] = approved_preview
    return {"status": "APPLIED_PREVIEW", "review_ir": copied,
            "application": application, "errors": []}
