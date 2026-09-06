"""Durable workflow adapter for the deterministic PDF/DWG wall pipeline.

The geometry engine remains synchronous and deterministic.  This module only
adapts it to the v2 task runtime: resolve an uploaded source artifact, persist
the six pipeline artifacts, pause for the model approval, run the static Revit
dry-run, and pause once more before a potential external Revit write.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from backend.application.artifact_service import get_artifact_service
from backend.adapters.task_repository import TaskRepository
from backend.application.revit_bridge_client import RevitBridgeClient
from backend.application.workflow_runtime import ExecutionBudget
from backend.config import get_settings
from backend.domain.approval_token import (
    approval_digest,
    issue_approval_token,
    normalize_target_path,
)
from backend.domain.contracts import AgentResult, EvidenceRef, RequestContext, TaskEnvelope
from backend.domain.errors import DependencyFailure, PolicyFailure, ValidationFailure
from backend.engines.wall_pipeline.contracts import (
    AuditReport,
    ManifestSource,
    RevitResult,
    SourceEntities,
    SourceManifest,
    WallEvidence,
    WallModel,
    infer_floor_code_from_elevation_range,
    parse_level_elevation_range,
)
from backend.engines.wall_pipeline.io import (
    canonical_sha256,
    file_sha256,
    read_artifact,
    write_artifact,
)
from backend.engines.wall_pipeline.pipeline import (
    ARTIFACT_FILENAMES,
    approve_revit_write,
    approve_wall_model,
    dry_run_wall_model,
    finalize_audit,
)
from backend.engines.wall_pipeline.audit import REQUIRED_SIMILARITY

_SOURCE_EXTENSIONS = {".pdf": "pdf", ".dwg": "dwg", ".dxf": "dxf"}
_WALL_SOURCE_SUFFIXES = frozenset(_SOURCE_EXTENSIONS)
_APPROVED_PROFILE_REGISTRY = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "wall_pipeline_trained_profiles.json"
)
_APPROVED_PROFILE_OPTION_KEYS = frozenset({
    "approved_profile_model_path",
    "approved_profile_model_sha256",
    "approved_profile_input_sha256",
    "approved_profile_source_sha256",
    "approved_profile_min_axis_match_ratio",
})
# Layer names exported by AutoCAD PDFs retain their source CAD layer, often
# prefixed by an XREF name separated with ``|``.  These patterns deliberately
# match the leaf layer only: S-WALL-TEXT / HATC / BEAM must not become wall
# coordinates merely because their name contains S-WALL.
_PDF_STRUCTURAL_AUTO_SCOPES: dict[str, dict[str, Any]] = {
    "wall": {
        "include_layers": [
            r"(?:^|\|)(?:S-WALL|A-WALL(?:-S|-CONC)?|A-PART(?:-S)?|WALL)$",
        ],
    },
    "column": {
        "include_layers": [
            r"(?:^|\|)(?:S-COLU|S-COLS|COLUMN|COLU)$",
        ],
        # Some PDF exports flatten the structural-column hatch/profile into
        # independent LINE records.  Keep that authoritative profile layer in
        # the source scope so closed irregular columns can be polygonized
        # before the generic rectangular outlines are deduplicated.
        "profile_layers": [
            r"(?:^|\|)(?:S-COLU-HATC|S-COLS-HATC|COLU-HATC)$",
        ],
        "label_layers": [
            r"^OCR$|^$|(?:^|\|)(?:S-COLU|S-COLS|COLUMN|COLU)(?:-TEXT)?$",
        ],
    },
    "grid": {
        "include_layers": [
            r"(?:^|\|)(?:A-GRID|S-GRID|GRID|AXIS|A-AXIS|S-AXIS|S-ANNO-DOTE|S-ANNO-AXIS)$",
        ],
        "exclude_layers": [
            r"(?:TEXT|TXT|DIM|DIMS|NUM|SYMBOL|HATC|TITLE|TTLB)",
        ],
        # AutoCAD PDF exports commonly draw an authoritative grid as aligned
        # dash segments rather than one long vector. The geometry engine
        # clusters these fragments into full axes after this per-dash gate.
        "min_length_m": 1.0,
        "required": True,
    },
    "opening": {
        # PyMuPDF exposes CAD text as ordinary text without its OCG/layer
        # name.  Match that empty source layer for PDFs with a native text
        # map; outlined labels remain recoverable through the exact-hash,
        # deployment-controlled engineering profile below.
        "label_layers": [
            r"^OCR$|^$|(?:^|\|)(?:C-墙洞|S-WALL-HO(?:LE)?(?:-TEXT)?|A-WALL-HO(?:LE)?(?:-TEXT)?|OPENING|WALL[-_]?HOLE)$",
        ],
        "boundary_layers": [
            r"(?:^|\|)(?:C-墙洞|S-WALL-HO(?:LE)?(?:-TEXT)?|A-WALL-HO(?:LE)?(?:-TEXT)?|OPENING|WALL[-_]?HOLE)$",
        ],
        "exclude_layers": [
            # ``S-WALL-HO-TEXT`` is the consultant's authoritative opening
            # marker layer, so a generic TEXT exclusion would discard the
            # exact vector evidence this scope was introduced to preserve.
            r"(?:ANNO|DIM|NUM|SYMBOL|HATC|TITLE|TTLB)",
        ],
    },
    "beam": {
        # Coupling-beam faces are kept separate from wall faces.  The geometry
        # engine pairs these lines into beam centerlines and OCR only supplies
        # the LL mark.
        "line_layers": [
            r"(?:^|\|)(?:S-WALL-BEAM|S-BEAM|BEAM|LL-BEAM)$",
        ],
        "label_layers": [r"^OCR$|^$|(?:^|\|)(?:S-WALL-BEAM|S-BEAM|BEAM)(?:-TEXT)?$"],
        "exclude_layers": [r"(?:ANNO|DIM|NUM|SYMBOL|HATC|TITLE|TTLB)"],
    },
}
# Drawing2BIM's legacy path is intentionally limited to the formats its
# perception adapter can actually consume.  Unknown/missing extensions must
# fail at intake instead of silently routing an engineering drawing through a
# weaker text/vision fallback.
_LEGACY_DRAWING_SUFFIXES = frozenset({".ifc", ".png", ".jpg", ".jpeg"})
_LEGACY_REVIEW_PENDING = "legacy_review_pending"
_LEGACY_AUTO_CANDIDATE = "legacy_auto_candidate"
_LEGACY_REVIEW_APPROVED = "legacy_review_approved"
_LEGACY_AUTO_PASSED = "legacy_auto_passed"
_LEGACY_COMPLETE = "legacy_complete"
logger = logging.getLogger(__name__)
_ARTIFACT_KINDS = {
    "source_manifest.json": "wall_source_manifest",
    "source_entities.json": "wall_source_entities",
    "wall_evidence.json": "wall_evidence",
    "wall_model.json": "wall_model",
    "revit_result.json": "revit_result",
    "audit_report.json": "wall_audit_report",
    "source_render.png": "wall_source_render",
    "revit_output.rvt": "revit_working_copy",
    "revit_actual_view.png": "revit_actual_view",
    "audit_overlay.png": "wall_audit_overlay",
}
_LINEAGE_ARTIFACT_KINDS = {
    "source_manifest.json": "wall_source_manifest",
    "source_entities.json": "wall_source_entities",
    "wall_evidence.json": "wall_evidence",
}


def _approved_profile_settings(source_path: Path) -> dict[str, Any]:
    """Resolve an exact-hash, deployment-controlled training supplement."""

    if not _APPROVED_PROFILE_REGISTRY.is_file():
        return {}
    try:
        payload = json.loads(_APPROVED_PROFILE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValidationFailure(
            "approved wall-pipeline training registry is invalid"
        ) from exc
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, dict):
        raise ValidationFailure(
            "approved wall-pipeline training registry has no profiles object"
        )
    input_hash = file_sha256(source_path)
    entry = profiles.get(input_hash)
    if entry is None:
        return {}
    if not isinstance(entry, dict):
        raise ValidationFailure("approved column profile registry entry is invalid")
    root = Path(__file__).resolve().parents[2]
    raw_model_path = str(entry.get("model_path") or "").strip()
    if not raw_model_path:
        raise ValidationFailure("approved column profile model path is missing")
    model_path = (root / raw_model_path).resolve()
    try:
        model_path.relative_to(root)
    except ValueError as exc:
        raise PolicyFailure(
            "approved column profile model must remain inside the project"
        ) from exc
    if not model_path.is_file():
        raise ValidationFailure(
            f"approved column profile model does not exist: {model_path}"
        )
    model_hash = str(entry.get("model_sha256") or "").lower()
    source_hash = str(entry.get("source_sha256") or "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", model_hash) or not re.fullmatch(
        r"[a-f0-9]{64}", source_hash
    ):
        raise ValidationFailure(
            "approved column profile registry contains an invalid SHA-256"
        )
    try:
        match_ratio = float(entry.get("minimum_axis_match_ratio", 0.75))
    except (TypeError, ValueError) as exc:
        raise ValidationFailure(
            "approved column profile minimum axis match ratio is invalid"
        ) from exc
    if not 0.5 <= match_ratio <= 1.0:
        raise ValidationFailure(
            "approved column profile minimum axis match ratio must be 0.5..1.0"
        )
    return {
        "approved_profile_model_path": str(model_path),
        "approved_profile_model_sha256": model_hash,
        "approved_profile_input_sha256": input_hash,
        "approved_profile_source_sha256": source_hash,
        "approved_profile_min_axis_match_ratio": match_ratio,
    }


def _project_id(context: RequestContext) -> str:
    if not context.project_id:
        raise ValidationFailure("wall_pipeline requires a project_id")
    return context.project_id


def _approval_operator(context: RequestContext, envelope: TaskEnvelope) -> str:
    """Return the authenticated HITL actor and reject forged resume claims."""

    payload = envelope.resume_payload or {}
    claimed_operator = str(payload.get("operator") or "").strip()
    if claimed_operator and claimed_operator != context.user_id:
        raise PolicyFailure("人工审批人身份与恢复消息不一致")
    claimed_role = str(payload.get("operator_role") or context.role).strip()
    if claimed_role not in {"admin", "project", "reviewer"}:
        raise PolicyFailure("当前恢复消息没有有效的人工审批角色")
    return context.user_id


def _legacy_hitl_approval(
    context: RequestContext,
    envelope: TaskEnvelope,
    *,
    action: str,
) -> tuple[str, str]:
    """Validate a legacy Drawing2BIM resume before recording an approval.

    The legacy Agent is retained for IFC/image compatibility, but its v2
    wrapper must still treat a resume message as an authenticated HITL action.
    Keeping this check at the workflow boundary prevents a forged queue
    envelope from turning an arbitrary payload into a successful review.
    """

    payload = envelope.resume_payload or {}
    if payload.get("decision") != "approved":
        raise ValidationFailure(
            f"legacy Drawing2BIM {action} requires an approved decision"
        )
    # Legacy resumes may arrive from older clients, but an approval-bearing
    # message must still carry an explicit actor and role.  The repository/API
    # normally injects these fields from the authenticated context; requiring
    # them here also protects direct worker invocations and stale queue
    # messages from being treated as an implicit approval.
    if not str(payload.get("operator") or "").strip():
        raise ValidationFailure(
            f"legacy Drawing2BIM {action} requires an operator"
        )
    if not str(payload.get("operator_role") or "").strip():
        raise ValidationFailure(
            f"legacy Drawing2BIM {action} requires an operator role"
        )
    operator = _approval_operator(context, envelope)
    reason = str(payload.get("reason") or action).strip()
    if not reason:
        raise ValidationFailure(
            f"legacy Drawing2BIM {action} requires an approval reason"
        )
    return operator, reason


def _runtime_dir(task_id: str) -> Path:
    root = Path(__file__).resolve().parents[2] / "data" / "runtime" / "wall_pipeline" / "tasks"
    raw_task_id = str(task_id or "")
    safe_task_id = "".join(ch for ch in raw_task_id if ch.isalnum() or ch in "-_")
    if not safe_task_id:
        raise ValidationFailure("invalid wall pipeline task id")
    # Stripping separators alone is not sufficient: ``task/a`` and ``taska``
    # would otherwise share one workspace and could mix artifacts between
    # retries.  Keep the readable prefix for diagnostics but add a digest
    # whenever normalization changed the identifier (or it is unusually
    # long), making task workspaces collision-resistant and bounded.
    if safe_task_id != raw_task_id or len(safe_task_id) > 96:
        digest = hashlib.sha256(raw_task_id.encode("utf-8")).hexdigest()[:16]
        safe_task_id = f"{safe_task_id[:96].rstrip('-_')}-{digest}" if safe_task_id else digest
    return root / safe_task_id


def _build_config(
    context: RequestContext,
    source_path: Path,
    output_dir: Path,
    options: dict[str, Any],
    *,
    source_files: list[dict[str, Any]] | None = None,
    source_type: str | None = None,
):
    """Build a validated config while forcing source and output paths.

    Callers may tune coordinate, level, grid, wall and Revit settings, but a
    task can never replace the uploaded source with an arbitrary filesystem
    path or write outside its task workspace.
    """
    from backend.engines.wall_pipeline.contracts import WallPipelineConfig

    resolved_source_type = source_type or _SOURCE_EXTENSIONS.get(source_path.suffix.lower())
    if resolved_source_type is None:
        raise ValidationFailure(
            "wall_pipeline accepts PDF, DWG or DXF artifacts; IFC/image compatibility is retired"
        )
    entries = source_files or [{"path": source_path, "role": "plan"}]
    if not entries:
        raise ValidationFailure("wall_pipeline requires at least one source file")
    try:
        workspace = output_dir.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValidationFailure("wall_pipeline task workspace is invalid") from exc
    normalized_sources: list[dict[str, Any]] = []
    expected_extensions = (
        {".pdf"} if resolved_source_type == "pdf" else {".dwg", ".dxf"}
    )
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationFailure("wall_pipeline source entries must be objects")
        candidate = Path(str(entry.get("path") or "")).expanduser()
        if candidate.suffix.lower() not in expected_extensions:
            raise ValidationFailure(
                f"source.type={resolved_source_type} cannot read {candidate.suffix}: {candidate}"
            )
        try:
            candidate = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            raise ValidationFailure(
                f"wall_pipeline source is not a readable staged artifact: {candidate}"
            ) from exc
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            # ``source_files`` is an internal representation of the already
            # authenticated uploads.  Requiring task-local paths prevents a
            # client-supplied pipeline_config from turning this helper into an
            # arbitrary local-file reader.
            raise ValidationFailure(
                "wall_pipeline source must be inside the task workspace"
            ) from exc
        if not candidate.is_file():
            raise ValidationFailure(f"wall_pipeline source is not a file: {candidate}")
        role = str(entry.get("role") or "").strip()
        if not role:
            raise ValidationFailure("wall_pipeline source role cannot be empty")
        normalized: dict[str, Any] = {
            "path": str(candidate),
            "role": role,
        }
        if entry.get("page_no") is not None:
            try:
                page_no = int(entry["page_no"])
            except (TypeError, ValueError) as exc:
                raise ValidationFailure("wall_pipeline source page_no must be an integer") from exc
            if page_no < 1:
                raise ValidationFailure("wall_pipeline source page_no must be >= 1")
            normalized["page_no"] = page_no
        normalized_sources.append(normalized)

    config_override = options.get("pipeline_config") or options.get("config") or {}
    if not isinstance(config_override, dict):
        raise ValidationFailure("wall_pipeline option 'config' must be an object")
    supplied_level = config_override.get("level", options.get("level"))
    level_override = None
    elevation_range_values: tuple[float, float] | None = None
    if supplied_level is not None:
        if not isinstance(supplied_level, dict):
            raise ValidationFailure("wall_pipeline option 'level' must be an object")
        level_override = dict(supplied_level)
        raw_range = level_override.get("elevation_range")
        if raw_range is not None:
            try:
                elevation_range_values = parse_level_elevation_range(str(raw_range))
            except ValueError as exc:
                raise ValidationFailure(str(exc)) from exc
            bottom_m, top_m = elevation_range_values
            # The range is the operator's authoritative envelope.  Derive
            # the legacy bottom/height fields here so all existing geometry
            # builders consume the same values without a second code path.
            level_override.update({
                "elevation_m": bottom_m,
                "top_elevation_m": top_m,
                "wall_height_m": round(top_m - bottom_m, 6),
                "elevation_source": level_override.get("elevation_source") or "input",
            })
    explicit_level_elevation = (
        isinstance(supplied_level, dict)
        and bool({"elevation_m", "top_elevation_m", "elevation_range"}
                 .intersection(supplied_level))
    )

    value: dict[str, Any] = {
        "tenant_id": context.tenant_id,
        "project_id": _project_id(context),
        "source": {
            "type": resolved_source_type,
            "files": normalized_sources,
        },
        "coordinate": {
            "origin": "revit_project_base_point",
            "source_unit": "auto",
        },
        "level": {
            "id": "level-main",
            "name": "Main Level",
            "elevation_m": 0.0,
            "elevation_source": "unresolved",
            "wall_height_m": 3.0,
        },
        "output_dir": str(output_dir.resolve()),
    }

    for section in (
        "coordinate", "grid", "wall", "column", "opening",
        "review_gate", "modeling_standard", "level", "revit", "beam",
    ):
        section_value = config_override.get(section, options.get(section))
        if section == "level" and level_override is not None:
            section_value = level_override
        if section_value is not None:
            if not isinstance(section_value, dict):
                raise ValidationFailure(f"wall_pipeline option '{section}' must be an object")
            if section == "revit":
                # Execution is a workflow control, not part of the strict
                # WallPipelineConfig contract sent to the geometry engine.
                section_value = {
                    key: item for key, item in section_value.items()
                    if key in {"target_model_path", "floor_code", "wall_type_name"}
                }
            elif section == "column":
                forbidden = _APPROVED_PROFILE_OPTION_KEYS.intersection(section_value)
                if forbidden:
                    raise PolicyFailure(
                        "approved column profile settings are deployment-controlled"
                    )
            value[section] = {**value.get(section, {}), **section_value}

    revit_options = value.setdefault("revit", {})
    floor_code = str(revit_options.get("floor_code") or "").strip().upper()
    if not floor_code and elevation_range_values is not None:
        inferred_floor_code = infer_floor_code_from_elevation_range(
            *elevation_range_values,
        )
        if inferred_floor_code:
            floor_code = inferred_floor_code
            revit_options["floor_code"] = inferred_floor_code

    # The frontend normally supplies only ``revit.floor_code`` (for example
    # ``B1``) and does not know the native Level name in the target RVT.  The
    # old ``Main Level`` default made Revit 2020 fail before any element was
    # created when the model used a native level named ``B1``/``地下1层``.
    # Derive a deterministic lookup name from the floor code only when the
    # caller did not provide an explicit level section; an explicit level
    # remains authoritative for projects with custom naming.
    if supplied_level is None:
        if floor_code:
            normalized_level_id = re.sub(r"[^A-Za-z0-9_-]+", "-", floor_code).strip("-_")
            value["level"] = {
                **value.get("level", {}),
                "id": "level-" + (normalized_level_id.lower() or "main"),
                "name": floor_code,
            }
    else:
        # An explicit level section is an operator assertion.  Preserve a
        # caller-supplied provenance value, otherwise mark the elevation as
        # input-provided so the risk report does not flag a known value.
        value["level"] = {
            **value.get("level", {}),
            "elevation_source": str(
                value.get("level", {}).get("elevation_source")
                or ("input" if explicit_level_elevation else "unresolved")
            ),
        }
        # A range-only submission is enough to identify the conventional
        # first basement.  Keep custom names authoritative when supplied.
        if (
            elevation_range_values is not None
            and floor_code
            and str(value["level"].get("name") or "").strip().casefold()
            in {"main level", "level main"}
        ):
            normalized_level_id = re.sub(r"[^A-Za-z0-9_-]+", "-", floor_code).strip("-_")
            value["level"].update({
                "id": "level-" + (normalized_level_id.lower() or "main"),
                "name": floor_code,
            })

    pdf_layer_mode = options.get("pdf_layer_mode")
    if pdf_layer_mode is not None:
        if resolved_source_type != "pdf":
            raise ValidationFailure("pdf_layer_mode is only valid for PDF sources")
        if pdf_layer_mode != "structural_auto":
            raise ValidationFailure(
                "wall_pipeline option 'pdf_layer_mode' must be 'structural_auto'"
            )
        for section, defaults in _PDF_STRUCTURAL_AUTO_SCOPES.items():
            target_section = value.setdefault(section, {})
            for key, default_value in defaults.items():
                # Explicit pipeline settings win, including intentionally empty
                # lists. Auto mode only supplies missing structural scopes.
                target_section.setdefault(
                    key,
                    list(default_value) if isinstance(default_value, list) else default_value,
                )

    # Common short options make the API convenient without creating a second
    # configuration language.  Explicit section values take precedence.
    coordinate = value["coordinate"]
    for key in (
        "origin", "source_unit", "scale_to_m", "calibration",
        "apply_pdf_page_rotation", "rotation_deg",
        "translation_m", "source_frame", "apply_base_point_rotation",
        "frame_offsets_m",
    ):
        if key in options and key not in coordinate:
            coordinate[key] = options[key]
    # ODA is a host-level executable, not a user-controlled workflow input.
    # A client may tune bounded converter metadata, but it cannot replace the
    # executable or inject an arbitrary argument template into the worker.
    if resolved_source_type == "dwg":
        supplied_converters: list[Any] = []
        for candidate in (
            options.get("converter"),
            config_override.get("converter"),
            (config_override.get("source") or {}).get("converter")
            if isinstance(config_override.get("source"), dict) else None,
        ):
            if candidate is not None:
                supplied_converters.append(candidate)
        converter_options: dict[str, Any] = {}
        for supplied in supplied_converters:
            if not isinstance(supplied, dict):
                raise ValidationFailure("wall_pipeline converter must be an object")
            forbidden = {"executable", "command"}.intersection(supplied)
            if forbidden:
                raise PolicyFailure(
                    "ODA executable and command are deployment-controlled; "
                    "configure ODA_FILE_CONVERTER on the worker"
                )
            converter_options.update(supplied)
        settings = get_settings()
        configured_executable = str(
            getattr(settings, "oda_file_converter", "")
            or os.environ.get("ODA_FILE_CONVERTER", "")
        ).strip()
        if configured_executable:
            converter_options["executable"] = configured_executable
        if converter_options:
            value["source"]["converter"] = converter_options

    if resolved_source_type == "pdf":
        approved_profile = _approved_profile_settings(source_path)
        if approved_profile:
            value.setdefault("column", {}).update(approved_profile)

    # The task owns these fields; values in a client-supplied config cannot
    # cross tenants/projects or escape the per-task workspace.
    value["tenant_id"] = context.tenant_id
    value["project_id"] = _project_id(context)
    value["source"] = {
        **{key: item for key, item in value["source"].items() if key == "converter"},
        "type": resolved_source_type,
        "files": normalized_sources,
    }
    value["output_dir"] = str(output_dir.resolve())
    # The standards profile records the same source frame used by the
    # deterministic coordinate transform.  When the client only selects a
    # frame, inherit it instead of creating contradictory metadata.
    standard_value = value.setdefault("modeling_standard", {})
    standard_value.setdefault(
        "coordinate_frame",
        value.get("coordinate", {}).get("source_frame", "project_north"),
    )
    target_model_path = value.get("revit", {}).get("target_model_path")
    if target_model_path:
        target = Path(str(target_model_path)).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        value["revit"]["target_model_path"] = str(
            _validate_revit_target_path(target)
        )
    try:
        config = WallPipelineConfig.model_validate(value)
    except Exception as exc:
        raise ValidationFailure(f"invalid wall_pipeline configuration: {exc}") from exc
    if not config.revit.target_model_path or not config.revit.floor_code:
        raise ValidationFailure(
            "wall_pipeline requires options.revit.target_model_path and options.revit.floor_code; "
            "floor_code may be omitted only when elevation_range unambiguously identifies the level"
        )
    return config


async def _persist_outputs(
    context: RequestContext,
    output_paths: dict[str, Path],
) -> dict[str, dict]:
    service = get_artifact_service()
    persisted: dict[str, dict] = {}
    for name, path in output_paths.items():
        if path.is_file():
            persisted[name] = await service.store_file(
                context, path, kind=_ARTIFACT_KINDS.get(name, "wall_pipeline"),
            )
    source_render = next(iter(output_paths.values())).parent / "source_render.png" if output_paths else None
    if source_render and source_render.is_file() and "source_render.png" not in persisted:
        persisted["source_render.png"] = await service.store_file(
            context, source_render, kind=_ARTIFACT_KINDS["source_render.png"],
        )
    required = {
        "source_manifest.json", "source_entities.json", "wall_evidence.json",
        "wall_model.json", "revit_result.json", "audit_report.json",
        "source_render.png",
    }
    missing = sorted(required.difference(persisted))
    if missing:
        raise ValidationFailure(
            "wall_pipeline did not produce required artifacts: " + ", ".join(missing)
        )
    return persisted


def _artifact_ids(persisted: dict[str, dict]) -> dict[str, str]:
    return {name: row["id"] for name, row in persisted.items()}


def _artifact_result(
    *,
    status: str,
    answer: str,
    structured_output: dict,
    artifact_ids: list[str],
    evidence_artifact_ids: list[str] | None = None,
    confidence: float = 1.0,
    next_action: str | None = None,
) -> AgentResult:
    """Build a result with one canonical artifact-to-evidence mapping."""

    artifacts = list(dict.fromkeys(str(item) for item in artifact_ids if item))
    evidence = list(dict.fromkeys(
        str(item) for item in (evidence_artifact_ids or artifacts) if item
    ))
    return AgentResult(
        status=status,
        answer=answer,
        structured_output=structured_output,
        artifact_ids=artifacts,
        evidence_refs=[
            EvidenceRef(kind="artifact", artifact_id=item) for item in evidence
        ],
        confidence=max(0.0, min(1.0, confidence)),
        next_action=next_action,
    )


def _atomic_verified_copy(
    source: Path,
    target: Path,
    validate_staged: Callable[[Path], None],
    *,
    prefix: str | None = None,
    suffix: str = ".tmp",
    copy_error: str = "artifact copy failed",
) -> Path:
    """Copy beside the destination, validate the snapshot, then publish it."""

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=prefix or f".{target.name}.",
        suffix=suffix,
        dir=str(target.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        try:
            shutil.copy2(source, temporary)
        except OSError as exc:
            raise ValidationFailure(copy_error) from exc
        validate_staged(temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def _safe_staged_source_name(index: int, filename: str, suffix: str) -> str:
    """Create a deterministic, task-local source name without trusting uploads."""

    base = Path(filename or "source").name
    base = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", base)
    base = base.strip("._") or "source"
    if Path(base).suffix.lower() != suffix:
        base += suffix
    # Keep the complete name comfortably below Windows MAX_PATH when the
    # runtime directory is nested under a long project path.
    return f"input_{index:04d}_{base[:220]}"


async def _stage_source_artifact(
    context: RequestContext,
    artifact_id: str,
    output_dir: Path,
    *,
    index: int,
) -> tuple[dict, Path, str]:
    """Copy one immutable upload into the task workspace and verify its hash.

    Artifact storage may be remote or content-addressed, and its resolved path
    deliberately has no extension.  Always stage a fresh, extension-bearing
    copy so the parser sees one stable snapshot and never the storage backend's
    mutable cache path.
    """

    artifact, source_path = await _resolve_verified_artifact(
        context, artifact_id, label="source artifact"
    )
    filename = str(artifact.get("filename") or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in _WALL_SOURCE_SUFFIXES:
        raise ValidationFailure(
            f"wall_pipeline requires a PDF, DWG or DXF artifact: {filename or artifact_id}"
        )
    try:
        from backend.engines.wall_pipeline.adapters import validate_source_file

        validate_source_file(source_path, source_kind=suffix.lstrip(".").upper())
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        raise ValidationFailure(str(exc)[:500] or f"source artifact cannot be read: {filename or artifact_id}") from exc

    expected_hash = str(artifact.get("sha256") or "").lower()
    if expected_hash and not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        raise ValidationFailure(f"source artifact has an invalid SHA-256: {artifact_id}")

    # Some lightweight/local adapters do not persist a database hash.  Do
    # not let that optional metadata turn a mutable cache path into an
    # unbounded source of engineering evidence: capture a content snapshot
    # before copying and compare it again after the copy.  When a trusted
    # artifact hash is present, ``_resolve_verified_artifact`` plus the staged
    # hash check below provide the same protection without hashing the source
    # a second time.
    source_snapshot_hash: str | None = None
    source_snapshot_size: int | None = None
    if not expected_hash:
        try:
            source_snapshot_size = source_path.stat().st_size
            source_snapshot_hash = file_sha256(source_path)
        except (FileNotFoundError, OSError) as exc:
            raise ValidationFailure(
                f"source artifact cannot be snapshotted: {filename or artifact_id}"
            ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / _safe_staged_source_name(index, filename, suffix)

    def _validate_staged(temporary: Path) -> None:
        try:
            staged_size = validate_source_file(
                temporary, source_kind=suffix.lstrip(".").upper()
            )
            staged_hash = file_sha256(temporary)
        except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
            raise ValidationFailure(
                str(exc)[:500] or f"staged source artifact is unreadable: {filename or artifact_id}"
            ) from exc
        if expected_hash and staged_hash != expected_hash:
            raise ValidationFailure(
                f"source artifact content hash mismatch: {filename or artifact_id}"
            )
        if not expected_hash and source_snapshot_hash is not None:
            try:
                source_after_size = source_path.stat().st_size
                source_after_hash = file_sha256(source_path)
            except (FileNotFoundError, OSError) as exc:
                raise ValidationFailure(
                    f"source artifact changed while staging: {filename or artifact_id}"
                ) from exc
            if (
                source_after_size != source_snapshot_size
                or source_after_hash != source_snapshot_hash
                or staged_hash != source_snapshot_hash
            ):
                raise ValidationFailure(
                    f"source artifact changed while staging: {filename or artifact_id}"
                )
        # A missing database hash is tolerated for test/fallback storage, but
        # the staged bytes are still content-addressed in the manifest.
        if staged_size <= 0:
            raise ValidationFailure(f"source artifact is empty: {filename or artifact_id}")

    _atomic_verified_copy(
        source_path,
        target,
        _validate_staged,
        prefix=f".input_{index:04d}-",
        suffix=suffix,
        copy_error=f"source artifact staging failed: {filename or artifact_id}",
    )
    return artifact, target, suffix


def _source_roles(options: dict[str, Any], count: int) -> list[str]:
    value = options.get("source_roles")
    if value is None:
        return ["plan", *(["supplementary"] * (count - 1))]
    if not isinstance(value, list) or len(value) != count:
        raise ValidationFailure(
            "wall_pipeline option 'source_roles' must contain one role per source artifact"
        )
    roles = [str(item).strip() for item in value]
    if any(not item or len(item) > 64 for item in roles):
        raise ValidationFailure("wall_pipeline source roles must be non-empty and <= 64 characters")
    return roles


def _wall_result(
    source_artifact_id: str,
    wall_model: WallModel,
    persisted: dict[str, dict],
    source_type: str,
    *,
    source_artifact_ids: list[str] | None = None,
) -> AgentResult:
    source_ids = list(dict.fromkeys(source_artifact_ids or [source_artifact_id]))
    gate = wall_model.gate
    confidence = min((wall.confidence for wall in wall_model.walls), default=0.0)
    structured = {
        "pipeline": "wall_pipeline",
        "stage": "wall_model_review",
        "source_artifact_id": source_artifact_id,
        "source_artifact_ids": source_ids,
        "artifact_ids": _artifact_ids(persisted),
        "gate": gate.model_dump(mode="json"),
        "wall_count": len(wall_model.walls),
        "shear_wall_count": sum(
            1 for wall in wall_model.walls
            if wall.construction is not None
            and wall.construction.structural_role == "shear_wall"
        ),
        "architectural_wall_count": sum(
            1 for wall in wall_model.walls
            if wall.construction is not None
            and wall.construction.structural_role == "architectural_wall"
        ),
        "unresolved_wall_count": sum(
            1 for wall in wall_model.walls
            if wall.construction is None
            or wall.construction.structural_role == "unresolved"
        ),
        "column_count": len(wall_model.columns),
        "irregular_column_count": sum(
            1 for column in wall_model.columns
            if column.profile_kind == "irregular"
        ),
        "beam_count": len(wall_model.beams),
        "beam_elevation_resolved_count": sum(
            1 for beam in wall_model.beams if beam.elevation_status == "resolved"
        ),
        "beam_elevation_level_default_count": sum(
            1 for beam in wall_model.beams if beam.elevation_status == "level_default"
        ),
        "beam_elevation_unresolved_count": sum(
            1 for beam in wall_model.beams if beam.elevation_status == "unresolved"
        ),
        "beam_elevation_conflict_count": sum(
            1 for beam in wall_model.beams if beam.elevation_status == "conflict"
        ),
        "junction_count": len(wall_model.junctions),
        "opening_count": len(wall_model.openings),
        "opening_cut_ready_count": sum(item.cut_status == "ready" for item in wall_model.openings),
        "opening_cut_pending_count": sum(item.cut_status != "ready" for item in wall_model.openings),
        "opening_drawing_datum_count": sum(
            item.elevation_source in {"drawing", "drawing_inferred_basement"}
            for item in wall_model.openings
        ),
        "opening_input_override_count": sum(
            item.elevation_source == "input" for item in wall_model.openings
        ),
        "opening_geometry_match_count": sum(
            1 for opening in wall_model.openings if opening.status == "matched"
        ),
        "opening_review_required_count": sum(
            1 for opening in wall_model.openings
            if opening.status == "review_required"
        ),
        "review_status": wall_model.review_status,
        "source_type": source_type,
        "modeling_standard": wall_model.modeling_standard.model_dump(mode="json"),
        "revit": wall_model.revit.model_dump(mode="json"),
    }
    risk_diagnostics = [
        item for item in gate.diagnostics
        if item.get("severity") == "warning"
        and item.get("code") in {
            "COLUMN_DETAIL_SOURCE_MISSING",
            "COLUMN_DETAIL_ASSOCIATION_UNRESOLVED",
            "LEVEL_ELEVATION_UNRESOLVED",
            "BEAM_SPECIFICATION_UNRESOLVED",
            "BEAM_ELEVATION_UNRESOLVED",
            "BEAM_ELEVATION_LEVEL_DEFAULT",
            "BEAM_ELEVATION_CONFLICT",
            "OPENING_CUT_UNRESOLVED",
            "WALL_STRUCTURAL_ROLE_UNRESOLVED",
            "WALL_STRUCTURAL_ROLE_CONFLICT",
        }
    ]
    structured["risk_diagnostics"] = risk_diagnostics
    if gate.status == "pass":
        answer = (
            f"墙体/柱/连梁几何识别完成：{len(wall_model.walls)} 面墙、{len(wall_model.columns)} 根柱、"
            f"{len(wall_model.beams)} 根连梁、{len(wall_model.junctions)} 个连接点；"
            f"其中 {structured['beam_elevation_resolved_count']} 根连梁已解析顶/底标高；"
            f"识别到 {len(wall_model.openings)} 个洞口语义标注，"
            f"其中 {sum(1 for opening in wall_model.openings if opening.status == 'matched')} 个有矢量边界和墙体宿主。"
            f"{structured['opening_cut_ready_count']} 个洞口尺寸和标高齐备，审批后实际开洞；"
            f"{structured['opening_cut_pending_count']} 个待核定，不会以标注代替实际开洞。"
            f"其中 {structured['opening_drawing_datum_count']} 个洞底标高来自图纸文字/表格说明。"
            "请审核 WallModel 后继续静态 Dry-run。"
        )
        if risk_diagnostics:
            answer += " 风险提示：" + "；".join(
                str(item.get("detail") or item.get("code"))
                for item in risk_diagnostics[:4]
            ) + "。"
        unresolved_beam_count = structured["beam_elevation_unresolved_count"]
        level_default_beam_count = structured["beam_elevation_level_default_count"]
        if level_default_beam_count:
            answer += (
                f" {level_default_beam_count} 根连梁依据图纸说明按本层顶标高布置，"
                "已保留说明证据，仍建议审核梁表/大样。"
            )
        if unresolved_beam_count:
            answer += (
                f" 另有 {unresolved_beam_count} 根连梁未找到可追溯顶/底标高，"
                "当前仅保留目标楼层作为待审核基准。"
            )
        unresolved_wall_count = structured["unresolved_wall_count"]
        if unresolved_wall_count:
            answer += (
                f" 另有 {unresolved_wall_count} 面墙尚未找到明确的剪力墙/建筑墙类型证据，"
                "需按图例或墙体标注核定；不会仅凭墙厚或图纸名称判定类型。"
            )
        next_action = "approve_wall_model"
    else:
        answer = (
            "本次未生成可交付模型，确定性审核门禁未通过："
            + "、".join(gate.errors)
            + "。请更换建筑/结构平面图，或调整输入配置后重新提交；当前没有可审批的 WallModel。"
        )
        next_action = "fix_source_or_rules"
    output_ids = list(_artifact_ids(persisted).values())
    return _artifact_result(
        status="waiting_human",
        answer=answer,
        structured_output=structured,
        artifact_ids=output_ids,
        evidence_artifact_ids=[*source_ids, *output_ids],
        confidence=confidence,
        next_action=next_action,
    )


async def wall_pipeline_prepare_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    if not envelope.input_artifact_ids:
        raise ValidationFailure("wall_pipeline requires at least one PDF, DWG or DXF artifact")
    if len(set(envelope.input_artifact_ids)) != len(envelope.input_artifact_ids):
        raise ValidationFailure("wall_pipeline source artifact IDs must be unique")
    budget.consume_tool_call("wall_pipeline.extract")
    output_dir = _runtime_dir(envelope.task_id)
    continuation_task_id = str(
        envelope.options.get("continue_from_task_id") or ""
    ).strip()
    continuation_target: Path | None = None
    if continuation_task_id:
        # Model continuation is deliberately artifact-backed: only a
        # successful, same-tenant/project wall delivery can become the next
        # run's Revit base.  Never accept a client filesystem path as memory.
        previous_run = await TaskRepository().get(context, continuation_task_id)
        if previous_run.get("workflow") != "wall_pipeline":
            raise ValidationFailure("continue_from_task_id must reference a wall_pipeline task")
        if previous_run.get("status") != "succeeded":
            raise ValidationFailure("only a succeeded wall_pipeline task can be continued")
        previous_result = previous_run.get("result") or {}
        previous_structured = (
            previous_result.get("structured_output")
            if isinstance(previous_result, dict) else {}
        ) or {}
        previous_artifacts = previous_structured.get("artifact_ids") or {}
        previous_rvt_id = previous_artifacts.get("revit_output.rvt")
        if not previous_rvt_id:
            raise ValidationFailure(
                "the selected wall_pipeline task has no persisted Revit delivery artifact"
            )
        continuation_target = await _copy_artifact_to_workspace(
            context,
            str(previous_rvt_id),
            output_dir,
            "continuation_source.rvt",
            expected_kind="revit_working_copy",
        )
    staged: list[dict[str, Any]] = []
    suffixes: list[str] = []
    for index, artifact_id in enumerate(envelope.input_artifact_ids, start=1):
        artifact, staged_path, suffix = await _stage_source_artifact(
            context, artifact_id, output_dir, index=index
        )
        staged.append({"path": staged_path, "role": ""})
        suffixes.append(suffix)
    if ".pdf" in suffixes and any(item != ".pdf" for item in suffixes):
        raise ValidationFailure("wall_pipeline cannot mix PDF and DWG/DXF source artifacts")
    source_kind = "pdf" if all(item == ".pdf" for item in suffixes) else (
        "dwg" if ".dwg" in suffixes else "dxf"
    )
    roles = _source_roles(envelope.options, len(staged))
    for entry, role in zip(staged, roles):
        entry["role"] = role
    effective_options = dict(envelope.options)
    if continuation_target is not None:
        effective_options["revit"] = {
            **(effective_options.get("revit") or {}),
            "target_model_path": str(continuation_target),
        }
    config = _build_config(
        context, Path(staged[0]["path"]), output_dir, effective_options,
        source_files=staged, source_type=source_kind,
    )
    try:
        output_paths = await _run_wall_pipeline_isolated(config)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValidationFailure(f"wall_pipeline failed: {str(exc)[:500]}") from exc
    persisted = await _persist_outputs(context, output_paths)
    wall_model = read_artifact(output_paths["wall_model.json"], WallModel)
    result = _wall_result(
        envelope.input_artifact_ids[0], wall_model, persisted, source_kind,
        source_artifact_ids=envelope.input_artifact_ids,
    )
    if continuation_task_id:
        result = result.model_copy(update={
            "structured_output": {
                **(result.structured_output or {}),
                "continued_from_task_id": continuation_task_id,
            },
        })
    return result


async def _run_wall_pipeline_isolated(config) -> dict[str, Path]:
    """Run the CPU-heavy parser in a cancellable child process.

    ``asyncio.to_thread`` cannot stop its worker after ``wait_for`` expires;
    the old implementation marked a task failed at five minutes while OCR
    kept writing artifacts in the background.  A dedicated process makes the
    workflow timeout authoritative: cancellation terminates the parser and a
    successful process must publish the complete artifact bundle atomically.
    """

    output_dir = Path(config.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    descriptor, raw_config_path = tempfile.mkstemp(
        prefix=".wall-pipeline-config-", suffix=".json", dir=str(output_dir)
    )
    os.close(descriptor)
    config_path = Path(raw_config_path)
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "run_wall_pipeline.py"
    try:
        config_path.write_text(
            config.model_dump_json(indent=2), encoding="utf-8"
        )
        # BuildMate uses a Windows Selector event loop for async database
        # compatibility; that loop cannot create asyncio subprocesses.  Own a
        # normal Popen process and wait for it in a thread so cancellation can
        # still kill the actual parser process rather than abandoning it.
        process = subprocess.Popen(
            [
                sys.executable,
                str(script_path),
                "run",
                str(config_path),
            ],
            cwd=str(script_path.parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        communication = asyncio.create_task(asyncio.to_thread(process.communicate))
        try:
            stdout, stderr = await asyncio.shield(communication)
        except asyncio.CancelledError:
            # Windows TerminateProcess is immediate for the Python parser;
            # waiting here prevents a timed-out task from publishing later.
            if process.poll() is None:
                process.kill()
            try:
                await asyncio.wait_for(
                    asyncio.shield(communication), timeout=10.0
                )
            except (asyncio.TimeoutError, subprocess.SubprocessError):
                pass
            for candidate in output_dir.glob(".wall-pipeline-*"):
                if candidate.is_dir():
                    shutil.rmtree(candidate, ignore_errors=True)
            raise
        if process.returncode != 0:
            detail = (stderr or stdout or b"").decode(
                "utf-8", errors="replace"
            ).strip()
            raise RuntimeError(
                "wall pipeline worker exited with code "
                f"{process.returncode}: {detail[-2000:]}"
            )
        paths = {
            name: output_dir / name
            for name in (*ARTIFACT_FILENAMES, "source_render.png")
        }
        missing = [name for name, path in paths.items() if not path.is_file()]
        if missing:
            raise RuntimeError(
                "wall pipeline worker did not publish: " + ", ".join(missing)
            )
        return paths
    finally:
        config_path.unlink(missing_ok=True)


async def _latest_wall_payload(context: RequestContext, task_id: str) -> dict:
    payload = await _latest_task_payload(context, task_id)
    if payload.get("pipeline") != "wall_pipeline":
        raise ValidationFailure("wall_pipeline state from the previous step is missing")
    return payload


async def _latest_task_payload(context: RequestContext, task_id: str) -> dict:
    steps = await TaskRepository().list_steps(context, task_id)
    for step in reversed(steps):
        payload = step.get("output_payload") or {}
        structured = payload.get("structured_output") or {}
        if structured:
            return structured
    raise ValidationFailure("workflow state from the previous step is missing")


async def _copy_artifact_to_workspace(
    context: RequestContext,
    artifact_id: str,
    directory: Path,
    filename: str,
    expected_kind: str | None = None,
) -> Path:
    artifact, source = await _resolve_verified_artifact(
        context, artifact_id, label=filename, expected_kind=expected_kind
    )
    target = directory / filename

    def _validate_staged(temporary: Path) -> None:
        expected_hash = str(artifact.get("sha256") or "").lower()
        copied_hash = file_sha256(temporary)
        if expected_hash and copied_hash != expected_hash:
            raise ValidationFailure(
                f"{filename} changed while being copied from artifact storage"
            )
        expected_size = artifact.get("size_bytes")
        if expected_size is not None:
            try:
                if int(expected_size) != temporary.stat().st_size:
                    raise ValidationFailure(
                        f"{filename} size does not match artifact metadata"
                    )
            except (TypeError, ValueError) as exc:
                raise ValidationFailure(
                    f"{filename} has invalid artifact size metadata"
                ) from exc

    return _atomic_verified_copy(
        source,
        target,
        _validate_staged,
        copy_error=f"{filename} could not be copied from artifact storage",
    )


def _copy_local_snapshot(
    source: Path,
    target: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> Path:
    """Atomically copy a previously verified source into a durable snapshot.

    The workflow normally copies through the Artifact service.  The fallback
    is intentionally limited to an already recorded manifest path and still
    verifies bytes before and after the copy; it is used only for old task
    payloads that predate ``source_artifact_ids``.
    """

    source = Path(source)
    target = Path(target)
    if not source.is_file():
        raise ValidationFailure(f"source snapshot is not readable: {source}")
    if source.stat().st_size != expected_size or file_sha256(source) != expected_sha256:
        raise ValidationFailure(
            "source snapshot no longer matches the recorded manifest: "
            + source.name
        )
    def _validate_staged(temporary: Path) -> None:
        if temporary.stat().st_size != expected_size or file_sha256(temporary) != expected_sha256:
            raise ValidationFailure(
                "source snapshot changed while being copied: " + source.name
            )

    return _atomic_verified_copy(
        source,
        target,
        _validate_staged,
        copy_error="source snapshot could not be copied: " + source.name,
    ).resolve()


async def _materialize_task_lineage_snapshot(
    context: RequestContext,
    task_dir: Path,
    artifact_ids: dict[str, str],
    *,
    source_artifact_ids: list[str] | None,
    strict: bool,
) -> tuple[dict[str, Path], dict[str, str], list[str]]:
    """Keep a self-contained source lineage beside a delivery result.

    Approval workspaces are temporary by design.  Without this snapshot a
    successful or failed task would retain a ``source_manifest`` whose paths
    point into a deleted temp directory, making later audit/replay impossible.
    Canonical contract hashes are path-independent, so rewriting only the
    operational source paths does not invalidate ``SourceEntities`` or
    ``WallEvidence``.

    ``strict`` is used for a successful delivery (the snapshot is part of the
    audit contract) and disabled for failure journaling so an original Bridge
    error is never hidden by a secondary persistence problem.
    """

    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    updated_ids = dict(artifact_ids)
    errors: list[str] = []
    expected_kinds = {
        "source_manifest.json": "wall_source_manifest",
        "source_entities.json": "wall_source_entities",
        "wall_evidence.json": "wall_evidence",
    }

    for filename, kind in _LINEAGE_ARTIFACT_KINDS.items():
        artifact_id = artifact_ids.get(filename)
        if not artifact_id:
            message = f"missing lineage artifact: {filename}"
            if strict:
                raise ValidationFailure(message)
            errors.append(message)
            continue
        try:
            paths[filename] = await _copy_artifact_to_workspace(
                context,
                artifact_id,
                task_dir,
                filename,
                expected_kind=expected_kinds[filename],
            )
        except Exception as exc:
            if strict:
                raise
            errors.append(f"could not persist lineage artifact {filename}: {str(exc)[:200]}")

    manifest_path = paths.get("source_manifest.json")
    if manifest_path is None:
        return paths, updated_ids, errors
    try:
        manifest = read_artifact(manifest_path, SourceManifest)
    except Exception as exc:
        if strict:
            raise ValidationFailure("durable source manifest is invalid") from exc
        errors.append(f"durable source manifest is invalid: {str(exc)[:200]}")
        return paths, updated_ids, errors

    source_dir = task_dir / "sources"
    updated_sources: list[ManifestSource] = []
    if source_artifact_ids is not None:
        if (
            not isinstance(source_artifact_ids, list)
            or len(source_artifact_ids) != len(manifest.sources)
            or any(not isinstance(item, str) or not item.strip() for item in source_artifact_ids)
            or len({item for item in source_artifact_ids if isinstance(item, str)})
            != len(source_artifact_ids)
        ):
            message = "source artifact IDs do not match the source manifest"
            if strict:
                raise ValidationFailure(message)
            errors.append(message)
            source_artifact_ids = None

    for index, source in enumerate(manifest.sources, start=1):
        try:
            if source_artifact_ids is not None:
                artifact, _ = await _resolve_verified_artifact(
                    context,
                    source_artifact_ids[index - 1],
                    label=f"source snapshot {source.source_file_id}",
                )
                filename = str(artifact.get("filename") or Path(source.path).name)
                suffix = Path(source.path).suffix.lower()
                if suffix not in _WALL_SOURCE_SUFFIXES:
                    raise ValidationFailure(
                        f"source snapshot has an unsupported extension: {source.path}"
                    )
                if Path(filename).suffix.lower() != suffix:
                    filename = f"{source.source_file_id}{suffix}"
                staged = await _copy_artifact_to_workspace(
                    context,
                    source_artifact_ids[index - 1],
                    source_dir,
                    _safe_staged_source_name(index, filename, suffix),
                )
                if staged.stat().st_size != source.size_bytes or file_sha256(staged) != source.sha256:
                    raise ValidationFailure(
                        f"source snapshot {source.source_file_id} does not match its manifest"
                    )
            else:
                suffix = Path(source.path).suffix.lower()
                staged = _copy_local_snapshot(
                    Path(source.path),
                    source_dir / _safe_staged_source_name(index, Path(source.path).name, suffix),
                    expected_sha256=source.sha256,
                    expected_size=source.size_bytes,
                )
            updated_sources.append(source.model_copy(update={"path": str(staged)}))
        except Exception as exc:
            if strict:
                raise
            errors.append(
                f"could not persist source snapshot {source.source_file_id}: {str(exc)[:200]}"
            )

    if len(updated_sources) == len(manifest.sources):
        durable_manifest = manifest.model_copy(update={"sources": updated_sources})
        write_artifact(manifest_path, durable_manifest)
        try:
            stored_manifest = await get_artifact_service().store_file(
                context,
                manifest_path,
                kind="wall_source_manifest",
                filename="source_manifest.json",
            )
            if stored_manifest.get("id"):
                updated_ids["source_manifest.json"] = stored_manifest["id"]
        except Exception as exc:
            if strict:
                raise
            errors.append(f"could not persist durable source manifest: {str(exc)[:200]}")

    return paths, updated_ids, errors


async def _materialize_source_render_snapshot(
    context: RequestContext,
    task_dir: Path,
    artifact_ids: dict[str, str],
    *,
    strict: bool,
) -> tuple[Path | None, list[str]]:
    """Copy the source-side render into the durable delivery workspace."""

    artifact_id = artifact_ids.get("source_render.png")
    if not artifact_id:
        message = "source render artifact is missing"
        if strict:
            raise ValidationFailure(message)
        return None, [message]
    try:
        path = await _copy_artifact_to_workspace(
            context,
            artifact_id,
            Path(task_dir),
            "source_render.png",
            expected_kind="wall_source_render",
        )
        return path, []
    except Exception as exc:
        if strict:
            raise
        return None, [f"could not persist source render: {str(exc)[:200]}"]


async def _resolve_verified_artifact(
    context: RequestContext,
    artifact_id: str,
    *,
    label: str,
    expected_kind: str | None = None,
) -> tuple[dict, Path]:
    """Resolve an artifact and verify the immutable metadata before parsing.

    The database row and the storage object are separate failure domains.  A
    stale cache or a replaced local file must not silently become a new source
    of engineering evidence, so metadata hashes/sizes are checked whenever
    the storage adapter provides them.  Lightweight test/fallback adapters may
    omit optional metadata; the subsequent pipeline manifest still records and
    verifies the actual bytes.
    """

    try:
        artifact, raw_path = await get_artifact_service().resolve(context, artifact_id)
    except Exception:
        # Preserve ResourceNotFound/DependencyFailure semantics from the
        # storage boundary; callers add the stage-specific context.
        raise
    if not isinstance(artifact, dict):
        raise ValidationFailure(f"{label} metadata is invalid")
    path = Path(raw_path)
    if not path.is_file():
        raise ValidationFailure(f"{label} is not a readable file")

    row_tenant = str(artifact.get("tenant_id") or "").strip()
    row_project = str(artifact.get("project_id") or "").strip()
    if row_tenant and row_tenant != context.tenant_id:
        raise PolicyFailure(f"{label} belongs to another tenant")
    if row_project and row_project != (context.project_id or ""):
        raise PolicyFailure(f"{label} belongs to another project")
    if expected_kind and artifact.get("kind") and artifact.get("kind") != expected_kind:
        raise ValidationFailure(
            f"{label} has unexpected artifact kind: {artifact.get('kind')}"
        )

    expected_hash = str(artifact.get("sha256") or "").lower()
    if expected_hash:
        if re.fullmatch(r"[a-f0-9]{64}", expected_hash) is None:
            raise ValidationFailure(f"{label} has invalid SHA-256 metadata")
        actual_hash = file_sha256(path)
        if actual_hash != expected_hash:
            raise ValidationFailure(f"{label} content hash does not match metadata")
    expected_size = artifact.get("size_bytes")
    if expected_size is not None:
        try:
            if int(expected_size) != path.stat().st_size:
                raise ValidationFailure(f"{label} size does not match metadata")
        except (TypeError, ValueError) as exc:
            raise ValidationFailure(f"{label} has invalid size metadata") from exc
    return artifact, path.resolve()


async def _verify_wall_model_lineage(
    context: RequestContext,
    model: WallModel,
    artifact_ids: dict[str, str],
    source_artifact_ids: list[str] | None = None,
) -> None:
    """Verify the immutable source → evidence → model artifact chain.

    A WallModel contains hashes and source references, but checking only the
    model file would allow a stale or substituted evidence artifact to travel
    through the approval gate.  Re-read the upstream contracts at each human
    gate so the Revit side never receives an orphaned engineering decision.
    """

    required = (
        "source_manifest.json",
        "source_entities.json",
        "wall_evidence.json",
    )
    missing = [name for name in required if not artifact_ids.get(name)]
    if missing:
        raise ValidationFailure(
            "wall model lineage artifacts are missing: " + ", ".join(missing)
        )
    service = get_artifact_service()
    paths: dict[str, Path] = {}
    expected_kinds = {
        "source_manifest.json": "wall_source_manifest",
        "source_entities.json": "wall_source_entities",
        "wall_evidence.json": "wall_evidence",
    }
    for name in required:
        try:
            _, path = await _resolve_verified_artifact(
                context,
                artifact_ids[name],
                label=f"wall model lineage artifact {name}",
                expected_kind=expected_kinds[name],
            )
        except (ValidationFailure, PolicyFailure):
            raise
        except Exception as exc:
            raise ValidationFailure(
                f"cannot resolve wall model lineage artifact: {name}"
            ) from exc
        paths[name] = path
    try:
        manifest = read_artifact(paths["source_manifest.json"], SourceManifest)
        entities = read_artifact(paths["source_entities.json"], SourceEntities)
        evidence = read_artifact(paths["wall_evidence.json"], WallEvidence)
    except Exception as exc:
        raise ValidationFailure(
            f"wall model lineage artifact validation failed: {str(exc)[:400]}"
        ) from exc

    if (context.tenant_id, _project_id(context)) != (
        model.tenant_id, model.project_id
    ):
        raise ValidationFailure("wall model tenant/project does not match request context")
    if (manifest.tenant_id, manifest.project_id) != (
        model.tenant_id, model.project_id
    ):
        raise ValidationFailure("wall model lineage tenant/project does not match")
    if (entities.tenant_id, entities.project_id) != (
        model.tenant_id, model.project_id
    ) or (evidence.tenant_id, evidence.project_id) != (
        model.tenant_id, model.project_id
    ):
        raise ValidationFailure("wall model lineage contains a cross-project artifact")
    if entities.manifest_sha256 != canonical_sha256(manifest):
        raise ValidationFailure("source_entities does not reference the source manifest")
    if evidence.source_entities_sha256 != canonical_sha256(entities):
        raise ValidationFailure("wall_evidence does not reference source_entities")
    if model.wall_evidence_sha256 != canonical_sha256(evidence):
        raise ValidationFailure("wall_model does not reference wall_evidence")

    # Validate source identities before validating references.  A duplicate
    # entity ID or source-file ID would make an otherwise valid-looking
    # locator ambiguous at the Revit approval gate.
    manifest_by_source = {item.source_file_id: item for item in manifest.sources}
    if len(manifest_by_source) != len(manifest.sources):
        raise ValidationFailure("source_manifest contains duplicate source_file_id values")
    source_ids = set(manifest_by_source)
    if not source_ids or set(entities.source_units) != source_ids:
        raise ValidationFailure("source_entities source_units do not match the manifest")

    # Bind manifest entries back to the authenticated upload artifacts when
    # the workflow has retained that mapping.  This closes the gap where a
    # persisted manifest path could be replaced between the preparation and
    # human-approval pauses.  The path check remains as a fallback for older
    # payloads that predate ``source_artifact_ids``.
    if source_artifact_ids is not None:
        if not isinstance(source_artifact_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in source_artifact_ids
        ):
            raise ValidationFailure("wall model lineage source artifact IDs are invalid")
        if len(source_artifact_ids) != len(manifest.sources):
            raise ValidationFailure(
                "wall model lineage source artifact count does not match manifest"
            )
        if len(set(source_artifact_ids)) != len(source_artifact_ids):
            raise ValidationFailure("wall model lineage source artifact IDs must be unique")
        for source, source_artifact_id in zip(manifest.sources, source_artifact_ids):
            artifact, source_path = await _resolve_verified_artifact(
                context,
                source_artifact_id,
                label=f"source file {source.source_file_id}",
            )
            if source_path.is_file():
                actual_size = source_path.stat().st_size
                actual_hash = file_sha256(source_path)
                if actual_size != source.size_bytes or actual_hash != source.sha256:
                    raise ValidationFailure(
                        f"source file {source.source_file_id} no longer matches its manifest"
                    )
            filename = str(artifact.get("filename") or "")
            if filename and Path(filename).suffix.lower() != Path(source.path).suffix.lower():
                raise ValidationFailure(
                    f"source artifact extension does not match manifest: {source.source_file_id}"
                )
    else:
        from backend.engines.wall_pipeline.adapters import verify_source_file_against_manifest

        for source in manifest.sources:
            try:
                verify_source_file_against_manifest(source)
            except Exception as exc:
                raise ValidationFailure(
                    f"source file {source.source_file_id} no longer matches its manifest"
                ) from exc

    entity_by_key: dict[tuple[str, str, str], Any] = {}
    for entity in entities.entities:
        key = (entity.source_file_id, entity.entity_id, entity.locator)
        if key in entity_by_key:
            raise ValidationFailure("source_entities contains duplicate entity identities")
        source = manifest_by_source.get(entity.source_file_id)
        if source is None:
            raise ValidationFailure(
                "source_entities contains an unknown source_file_id: "
                + entity.source_file_id
            )
        if source.page_no is not None and entity.page_no != source.page_no:
            raise ValidationFailure(
                f"source entity page does not match manifest source: {entity.locator}"
            )
        if manifest.source_type == "pdf":
            if entity.page_no is None:
                raise ValidationFailure(
                    f"PDF source entity has no page_no: {entity.locator}"
                )
            expected_frame = (
                f"{entity.source_file_id}:page:{entity.page_no:04d}"
            )
        else:
            if entity.page_no is not None:
                raise ValidationFailure(
                    f"CAD source entity unexpectedly has page_no: {entity.locator}"
                )
            expected_frame = entity.source_file_id
        if entity.frame_id and entity.frame_id != expected_frame:
            raise ValidationFailure(
                f"source entity frame does not match its source/page: {entity.locator}"
            )
        entity_by_key[key] = entity

    evidence_by_id = {item.evidence_id: item for item in evidence.items}
    if len(evidence_by_id) != len(evidence.items):
        raise ValidationFailure("wall_evidence contains duplicate evidence IDs")

    def _reference_key(ref: Any, entity: Any | None = None) -> tuple[str, str, str, str]:
        frame_id = ref.frame_id or ""
        if not frame_id and entity is not None:
            if entity.frame_id:
                frame_id = entity.frame_id
            elif entity.page_no is not None:
                frame_id = f"{entity.source_file_id}:page:{entity.page_no:04d}"
            else:
                frame_id = entity.source_file_id
        return (ref.source_file_id, ref.entity_id, ref.locator, frame_id)

    def _parent_locator(locator: str) -> str | None:
        parent, marker, segment = locator.rpartition("/segment:")
        if not marker or not parent or not segment.isdigit():
            return None
        return parent

    evidence_refs_by_id: dict[str, set[tuple[str, str, str, str]]] = {}
    for item in evidence.items:
        refs: set[tuple[str, str, str, str]] = set()
        for ref in item.source_refs:
            parent_locator = _parent_locator(ref.locator)
            if parent_locator is None:
                raise ValidationFailure(
                    f"wall_evidence has an invalid source locator: {ref.locator}"
                )
            entity = entity_by_key.get((ref.source_file_id, ref.entity_id, parent_locator))
            if entity is None:
                raise ValidationFailure(
                    "wall_evidence contains an unknown source reference: "
                    + ref.entity_id
                )
            expected_frame = entity.frame_id or (
                f"{entity.source_file_id}:page:{entity.page_no:04d}"
                if entity.page_no is not None else entity.source_file_id
            )
            if ref.page_no != entity.page_no:
                raise ValidationFailure(
                    f"wall_evidence reference page mismatch: {ref.locator}"
                )
            if ref.frame_id and ref.frame_id != expected_frame:
                raise ValidationFailure(
                    f"wall_evidence reference frame mismatch: {ref.locator}"
                )
            if ref.layer != entity.layer:
                raise ValidationFailure(
                    f"wall_evidence reference layer mismatch: {ref.locator}"
                )
            refs.add(_reference_key(ref, entity))
        if len(refs) != len(item.source_refs):
            raise ValidationFailure(
                f"wall_evidence contains duplicate source references: {item.evidence_id}"
            )
        evidence_refs_by_id[item.evidence_id] = refs

    wall_ids: set[str] = set()
    for wall in model.walls:
        if wall.wall_id in wall_ids:
            raise ValidationFailure("wall_model contains duplicate wall IDs")
        wall_ids.add(wall.wall_id)
        if len(set(wall.evidence_ids)) != len(wall.evidence_ids):
            raise ValidationFailure(f"wall {wall.wall_id} contains duplicate evidence IDs")
        if any(evidence_id not in evidence_by_id for evidence_id in wall.evidence_ids):
            raise ValidationFailure(
                f"wall {wall.wall_id} references missing wall evidence"
            )
        actual_refs: set[tuple[str, str, str, str]] = set()
        for ref in wall.source_refs:
            parent_locator = _parent_locator(ref.locator)
            entity = (
                entity_by_key.get((ref.source_file_id, ref.entity_id, parent_locator))
                if parent_locator is not None else None
            )
            if entity is None:
                raise ValidationFailure(
                    f"wall {wall.wall_id} contains an untraceable source reference"
                )
            expected_frame = entity.frame_id or (
                f"{entity.source_file_id}:page:{entity.page_no:04d}"
                if entity.page_no is not None else entity.source_file_id
            )
            if ref.page_no != entity.page_no or (ref.frame_id and ref.frame_id != expected_frame):
                raise ValidationFailure(
                    f"wall {wall.wall_id} contains a source frame mismatch"
                )
            actual_refs.add(_reference_key(ref, entity))
        if len(actual_refs) != len(wall.source_refs):
            raise ValidationFailure(f"wall {wall.wall_id} contains duplicate source references")
        expected_refs = set().union(
            *(evidence_refs_by_id[evidence_id] for evidence_id in wall.evidence_ids)
        ) if wall.evidence_ids else set()
        if actual_refs != expected_refs:
            raise ValidationFailure(
                f"wall {wall.wall_id} source references do not match its evidence IDs"
            )

    approved_source_hash = manifest.column.approved_profile_source_sha256
    approved_model_path = manifest.column.approved_profile_model_path
    approved_prefix = (
        "approved_training_" + approved_source_hash[:16]
        if approved_source_hash else ""
    )
    approved_locator_prefix = (
        "approved-training:" + approved_source_hash + ":"
        if approved_source_hash else ""
    )
    approved_entities_by_key: dict[tuple[str, str], Any] = {}
    if approved_model_path:
        profile_model_path = Path(approved_model_path).expanduser().resolve()
        if not profile_model_path.is_file() or file_sha256(profile_model_path) != (
            manifest.column.approved_profile_model_sha256
        ):
            raise ValidationFailure(
                "approved column profile model no longer matches its hash"
            )
        profile_manifest_path = profile_model_path.with_name("source_manifest.json")
        profile_entities_path = profile_model_path.with_name("source_entities.json")
        if not profile_manifest_path.is_file() or not profile_entities_path.is_file():
            raise ValidationFailure(
                "approved column profile lineage artifacts are missing"
            )
        try:
            profile_manifest = read_artifact(
                profile_manifest_path, SourceManifest
            )
            profile_entities = read_artifact(
                profile_entities_path, SourceEntities
            )
            raw_profile_manifest = json.loads(
                profile_manifest_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise ValidationFailure(
                "approved column profile lineage cannot be read"
            ) from exc
        if profile_entities.manifest_sha256 != canonical_sha256(
            raw_profile_manifest
        ):
            raise ValidationFailure(
                "approved column profile source lineage is invalid"
            )
        if approved_source_hash not in {
            source.sha256 for source in profile_manifest.sources
        }:
            raise ValidationFailure(
                "approved column profile source hash is invalid"
            )
        from backend.engines.wall_pipeline.adapters import (
            verify_source_file_against_manifest,
        )
        for source in profile_manifest.sources:
            try:
                verify_source_file_against_manifest(source)
            except Exception as exc:
                raise ValidationFailure(
                    "approved column profile source no longer matches its manifest"
                ) from exc
        approved_entities_by_key = {
            (entity.entity_id, entity.locator): entity
            for entity in profile_entities.entities
        }

    opening_ids: set[str] = set()
    for opening in model.openings:
        if opening.opening_id in opening_ids:
            raise ValidationFailure("wall_model contains duplicate opening IDs")
        opening_ids.add(opening.opening_id)
        if any(wall_id not in wall_ids for wall_id in opening.host_wall_ids):
            raise ValidationFailure(
                f"opening {opening.opening_id} references an invalid wall host"
            )
        if opening.status == "matched" and not opening.host_wall_ids:
            raise ValidationFailure(
                f"matched opening {opening.opening_id} has no wall host"
            )
        opening_refs: set[tuple[str, str, str, str]] = set()
        for ref in opening.source_refs:
            entity = entity_by_key.get(
                (ref.source_file_id, ref.entity_id, ref.locator)
            )
            is_approved_reference = (
                entity is None
                and approved_entities_by_key
                and ref.source_file_id == approved_prefix
                and ref.frame_id == approved_prefix
                and ref.locator.startswith(approved_locator_prefix)
            )
            if is_approved_reference:
                original_locator = ref.locator[len(approved_locator_prefix):]
                entity = approved_entities_by_key.get(
                    (ref.entity_id, original_locator)
                )
                if entity is None:
                    parent_locator = _parent_locator(original_locator)
                    if parent_locator is not None:
                        entity = approved_entities_by_key.get(
                            (ref.entity_id, parent_locator)
                        )
            if entity is None:
                raise ValidationFailure(
                    f"opening {opening.opening_id} contains an untraceable source reference"
                )
            expected_frame = (
                approved_prefix if is_approved_reference else entity.frame_id or (
                    f"{entity.source_file_id}:page:{entity.page_no:04d}"
                    if entity.page_no is not None else entity.source_file_id
                )
            )
            if (
                ref.page_no != entity.page_no
                or (ref.frame_id and ref.frame_id != expected_frame)
                or ref.layer != entity.layer
            ):
                raise ValidationFailure(
                    f"opening {opening.opening_id} contains a source frame/layer mismatch"
                )
            key = _reference_key(ref, entity)
            if key in opening_refs:
                raise ValidationFailure(
                    f"opening {opening.opening_id} contains duplicate source references"
                )
            opening_refs.add(key)

    column_ids: set[str] = set()
    for column in model.columns:
        if column.column_id in wall_ids or column.column_id in column_ids:
            raise ValidationFailure("wall_model contains duplicate column IDs")
        column_ids.add(column.column_id)
        for ref in column.source_refs:
            # Column references point directly at the closed source outline;
            # unlike wall evidence they intentionally have no /segment:N
            # suffix.  Resolve the exact immutable entity identity here so a
            # column cannot be moved into the model by editing coordinates.
            entity = entity_by_key.get((ref.source_file_id, ref.entity_id, ref.locator))
            if (
                entity is None
                and approved_entities_by_key
                and ref.source_file_id == approved_prefix
                and ref.frame_id == approved_prefix
                and ref.locator.startswith(approved_locator_prefix)
            ):
                original_locator = ref.locator[len(approved_locator_prefix):]
                entity = approved_entities_by_key.get(
                    (ref.entity_id, original_locator)
                )
                if entity is None:
                    parent_locator = _parent_locator(original_locator)
                    if parent_locator is not None:
                        entity = approved_entities_by_key.get(
                            (ref.entity_id, parent_locator)
                        )
            if entity is None:
                raise ValidationFailure(
                    f"column {column.column_id} contains an untraceable source reference"
                )
            is_approved_reference = ref.source_file_id == approved_prefix
            expected_frame = (
                approved_prefix if is_approved_reference else entity.frame_id or (
                    f"{entity.source_file_id}:page:{entity.page_no:04d}"
                    if entity.page_no is not None else entity.source_file_id
                )
            )
            if (
                ref.page_no != entity.page_no
                or (ref.frame_id and ref.frame_id != expected_frame)
                or ref.layer != entity.layer
            ):
                raise ValidationFailure(
                    f"column {column.column_id} contains a source frame/layer mismatch"
                )

    beam_ids: set[str] = set()
    for beam in model.beams:
        if beam.beam_id in wall_ids or beam.beam_id in column_ids or beam.beam_id in beam_ids:
            raise ValidationFailure("wall_model contains duplicate beam IDs")
        beam_ids.add(beam.beam_id)
        for ref in beam.source_refs:
            # Beam evidence is built from paired line segments.  Resolve both
            # the exact segment locator and its immutable parent entity.
            entity = entity_by_key.get((ref.source_file_id, ref.entity_id, ref.locator))
            if entity is None:
                parent_locator = _parent_locator(ref.locator)
                if parent_locator is not None:
                    entity = entity_by_key.get(
                        (ref.source_file_id, ref.entity_id, parent_locator)
                    )
            if entity is None:
                raise ValidationFailure(
                    f"beam {beam.beam_id} contains an untraceable source reference"
                )
            expected_frame = entity.frame_id or (
                f"{entity.source_file_id}:page:{entity.page_no:04d}"
                if entity.page_no is not None else entity.source_file_id
            )
            if (
                ref.page_no != entity.page_no
                or (ref.frame_id and ref.frame_id != expected_frame)
                or ref.layer != entity.layer
            ):
                raise ValidationFailure(
                    f"beam {beam.beam_id} contains a source frame/layer mismatch"
                )
        if beam.elevation_status == "resolved" and not beam.elevation_refs:
            raise ValidationFailure(
                f"beam {beam.beam_id} has resolved elevation without evidence references"
            )
        for ref in beam.elevation_refs:
            # Elevation references point to the heading/value/mark text rows
            # used by the resolver.  They are checked independently from the
            # geometric beam references so a valid-looking Z value cannot be
            # injected into an approved model without its source evidence.
            entity = entity_by_key.get((ref.source_file_id, ref.entity_id, ref.locator))
            if entity is None:
                parent_locator = _parent_locator(ref.locator)
                if parent_locator is not None:
                    entity = entity_by_key.get(
                        (ref.source_file_id, ref.entity_id, parent_locator)
                    )
            if entity is None:
                raise ValidationFailure(
                    f"beam {beam.beam_id} contains an untraceable elevation reference"
                )
            expected_frame = entity.frame_id or (
                f"{entity.source_file_id}:page:{entity.page_no:04d}"
                if entity.page_no is not None else entity.source_file_id
            )
            if (
                ref.page_no != entity.page_no
                or (ref.frame_id and ref.frame_id != expected_frame)
                or ref.layer != entity.layer
            ):
                raise ValidationFailure(
                    f"beam {beam.beam_id} elevation reference frame/layer mismatch"
                )

    if model.transform_chain != evidence.transform_chain:
        raise ValidationFailure("wall_model transform_chain does not match wall_evidence")
    if model.coordinate_origin != manifest.coordinate.origin:
        raise ValidationFailure("wall_model coordinate origin does not match manifest")
    if model.level != manifest.level or model.revit != manifest.revit:
        raise ValidationFailure("wall_model level/Revit settings do not match manifest")

    # Keep an exact frame registry for grid provenance.  Checking only the
    # source prefix (for example, accepting every ``source_0001:page:*``)
    # would let a stale/tampered model refer to a page that was never parsed.
    # Entity-derived frames cover frames emitted by the adapters; an explicit
    # coordinate offset is also a valid registration for an otherwise empty
    # supplementary page, but only when its prefix maps unambiguously to a
    # manifest source.
    known_frames: set[str] = set(source_ids)
    frame_owner: dict[str, str] = {source_id: source_id for source_id in source_ids}
    for entity in entities.entities:
        expected_frame = entity.frame_id or (
            f"{entity.source_file_id}:page:{entity.page_no:04d}"
            if entity.page_no is not None else entity.source_file_id
        )
        known_frames.add(expected_frame)
        frame_owner[expected_frame] = entity.source_file_id
    for frame_id in manifest.coordinate.frame_offsets_m:
        if frame_id in source_ids:
            known_frames.add(frame_id)
            frame_owner[frame_id] = frame_id
            continue
        matches = [
            source_id for source_id in source_ids
            if frame_id.startswith(source_id + ":")
        ]
        if len(matches) == 1:
            known_frames.add(frame_id)
            frame_owner.setdefault(frame_id, matches[0])

    axis_ids = set()
    for axis in model.grid:
        if axis.axis_id in axis_ids:
            raise ValidationFailure("wall_model contains duplicate grid axis IDs")
        axis_ids.add(axis.axis_id)
        if axis.source_file_id and axis.source_file_id not in source_ids:
            raise ValidationFailure("wall_model grid references an unknown source_file_id")
        if axis.frame_id:
            if axis.frame_id not in known_frames:
                raise ValidationFailure("wall_model grid references an unknown frame_id")
            owner = frame_owner.get(axis.frame_id)
            if (
                axis.source_file_id
                and owner is not None
                and owner != axis.source_file_id
            ):
                raise ValidationFailure(
                    "wall_model grid frame and source_file_id disagree"
                )

    junction_ids = set()
    for junction in model.junctions:
        if junction.junction_id in junction_ids:
            raise ValidationFailure("wall_model contains duplicate junction IDs")
        junction_ids.add(junction.junction_id)
        if len(set(junction.wall_ids)) != len(junction.wall_ids) or any(
            wall_id not in wall_ids for wall_id in junction.wall_ids
        ):
            raise ValidationFailure(
                f"junction {junction.junction_id} references invalid wall IDs"
            )


async def wall_pipeline_approve_model_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    budget.consume_tool_call("wall_pipeline.approve_model")
    if envelope.resume_payload.get("decision") != "approved":
        raise ValidationFailure("wall_pipeline model step requires an approved decision")
    previous = await _latest_wall_payload(context, envelope.task_id)
    artifact_ids = previous.get("artifact_ids") or {}
    model_artifact_id = artifact_ids.get("wall_model.json")
    if not model_artifact_id:
        raise ValidationFailure("wall_model artifact is missing from the previous step")
    operator = _approval_operator(context, envelope)
    reason = str(envelope.resume_payload.get("reason") or "wall model approved")
    with tempfile.TemporaryDirectory(prefix="buildmate-wall-approval-") as temp:
        directory = Path(temp)
        model_path = await _copy_artifact_to_workspace(
            context,
            model_artifact_id,
            directory,
            "wall_model.json",
            expected_kind="wall_model",
        )
        pending_result_artifact_id = artifact_ids.get("revit_result.json")
        if not pending_result_artifact_id:
            raise ValidationFailure(
                "initial Revit result artifact is missing from the wall pipeline state"
            )
        pending_result_path = await _copy_artifact_to_workspace(
            context,
            pending_result_artifact_id,
            directory,
            "revit_result.json",
            expected_kind="revit_result",
        )
        lineage_paths: dict[str, Path] = {}
        for filename, kind in _LINEAGE_ARTIFACT_KINDS.items():
            artifact_id = artifact_ids.get(filename)
            if not artifact_id:
                raise ValidationFailure(
                    "wall model lineage artifact is missing from the previous step: "
                    + filename
                )
            lineage_paths[filename] = await _copy_artifact_to_workspace(
                context,
                artifact_id,
                directory,
                filename,
                expected_kind=kind,
            )
        try:
            model = read_artifact(model_path, WallModel)
            try:
                pending_result = read_artifact(pending_result_path, RevitResult)
            except Exception as exc:
                raise ValidationFailure(
                    "initial Revit result artifact validation failed"
                ) from exc
            if (
                pending_result.tenant_id != model.tenant_id
                or pending_result.project_id != model.project_id
            ):
                raise ValidationFailure(
                    "initial Revit result tenant/project does not match WallModel"
                )
            if pending_result.status != "pending_approval" or pending_result.approval is not None:
                raise ValidationFailure(
                    "initial Revit result is not pending model approval"
                )
            if pending_result.wall_model_sha256 != canonical_sha256(model):
                raise ValidationFailure(
                    "initial Revit result does not reference the pending WallModel"
                )
            await _verify_wall_model_lineage(
                context,
                model,
                artifact_ids,
                source_artifact_ids=previous.get("source_artifact_ids"),
            )
            model = approve_wall_model(
                model_path,
                actor_id=operator,
                reason=reason,
                lineage_paths=lineage_paths,
            )
            dry_run = dry_run_wall_model(model_path, lineage_paths=lineage_paths)
        except (ValueError, OSError) as exc:
            raise ValidationFailure(f"wall model approval/dry-run failed: {str(exc)[:500]}") from exc
        if _bridge_preflight_requested(envelope.options):
            # Optional capability preflight runs after the deterministic
            # compiler check and before the second human gate.  It is explicit
            # because local/offline mode may not have a Windows/Revit host.
            budget.consume_tool_call("revit_bridge.preflight_wall_model")
            from workers.revit_bridge.contracts import RunWallModelRequest

            source_model_path = str(model.revit.target_model_path or "")
            source_model_sha256 = str(
                (dry_run.readback or {}).get("source_model_sha256") or ""
            )
            bridge_preflight = await RevitBridgeClient().run_wall_model(
                RunWallModelRequest(
                    build_id=_bridge_build_id(
                        envelope.task_id,
                        tenant_id=context.tenant_id,
                        project_id=_project_id(context),
                        wall_model_sha256=canonical_sha256(model),
                    ),
                    source_model_path=source_model_path,
                    wall_model=model.model_dump(mode="json"),
                    revit_result=dry_run.model_dump(mode="json"),
                    source_model_sha256=source_model_sha256 or None,
                    dry_run=True,
                )
            )
            preflight_build_id = _bridge_build_id(
                envelope.task_id,
                tenant_id=context.tenant_id,
                project_id=_project_id(context),
                wall_model_sha256=canonical_sha256(model),
            )
            if (
                bridge_preflight.build_id != preflight_build_id
                or bridge_preflight.status != "validated"
                or bridge_preflight.dry_run is not True
            ):
                raise ValidationFailure("Revit Bridge preflight did not validate the WallModel")
            details = bridge_preflight.details or {}
            compiler, plan_hash = _wall_compile_identity(model)
            expected_target = _validate_revit_target_path(
                Path(model.revit.target_model_path or ""), get_settings()
            )
            expected_target = normalize_target_path(str(expected_target))
            if (
                details.get("tenant_id") != model.tenant_id
                or details.get("project_id") != model.project_id
                or details.get("wall_model_sha256") != canonical_sha256(model)
                or not _wall_compiler_matches(details.get("compiler"), compiler, model)
                or details.get("compiled_plan_sha256") != plan_hash
                or details.get("target_model_path") != expected_target
                or details.get("wall_count") != len(model.walls)
                or details.get("column_count", 0) != len(model.columns)
                or details.get("beam_count", 0) != len(model.beams)
                or details.get("grid_count") != sum(
                    1 for _ in model.grid
                )
                or details.get("opening_count") != len(model.openings)
                or details.get("matched_opening_count") != sum(
                    opening.status == "matched" for opening in model.openings
                )
                or details.get("opening_host_count") != len({
                    host_id
                    for opening in model.openings
                    if opening.status == "matched"
                    for host_id in opening.host_wall_ids
                })
                or details.get("source_model_sha256") != source_model_sha256
            ):
                raise ValidationFailure("Revit Bridge preflight read-back count mismatch")
            dry_run = dry_run.model_copy(update={
                "readback": {
                    **(dry_run.readback or {}),
                    "bridge_preflight": details,
                },
            })
            dry_run = RevitResult.model_validate(dry_run.model_dump(mode="json"))
        service = get_artifact_service()
        # ``dry_run_wall_model`` writes the first snapshot itself; rewrite it
        # when an optional Bridge preflight added capability/read-back facts.
        write_artifact(directory / "revit_result.json", dry_run)
        wall_model_artifact = await service.store_file(context, model_path, kind="wall_model")
        revit_artifact = await service.store_file(context, directory / "revit_result.json", kind="revit_result")
    updated_ids = {**artifact_ids, "wall_model.json": wall_model_artifact["id"], "revit_result.json": revit_artifact["id"]}
    structured = {
        **previous,
        "stage": "revit_dry_run",
        "artifact_ids": updated_ids,
        "review_status": model.review_status,
        "revit_result": dry_run.model_dump(mode="json"),
    }
    return _artifact_result(
        status="waiting_human",
        answer=(
            f"WallModel 已批准，Revit 静态 Dry-run 通过（{len(model.walls)} 面墙、"
            f"{len(model.columns)} 根柱）。请批准 Revit 写入。"
        ),
        structured_output=structured,
        artifact_ids=list(updated_ids.values()),
        confidence=1.0,
        next_action="approve_revit_write",
    )


def _execute_revit_requested(options: dict[str, Any]) -> bool:
    """Return whether this task explicitly authorizes live Bridge execution."""
    value = options.get("execute_revit")
    nested = options.get("revit")
    if isinstance(nested, dict) and "execute" in nested:
        if value is not None and value != nested["execute"]:
            raise ValidationFailure("execute_revit and revit.execute disagree")
        value = nested["execute"]
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValidationFailure("execute_revit must be a boolean")
    return value


def _bridge_preflight_requested(options: dict[str, Any]) -> bool:
    """Return whether the approval gate must query the Windows Bridge first."""

    nested = options.get("revit")
    value = options.get("bridge_preflight")
    if isinstance(nested, dict) and "bridge_preflight" in nested:
        if value is not None and value != nested["bridge_preflight"]:
            raise ValidationFailure("bridge_preflight and revit.bridge_preflight disagree")
        value = nested["bridge_preflight"]
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValidationFailure("bridge_preflight must be a boolean")
    return value


def _audit_thresholds(options: dict[str, Any]) -> tuple[float, float, float]:
    """Read independent-overlay thresholds with a non-bypassable 95% floor."""

    nested = options.get("audit")
    if nested is None:
        nested = {}
    if not isinstance(nested, dict):
        raise ValidationFailure("audit options must be an object")

    def value(name: str, default: float) -> float:
        raw = nested.get(name, options.get(name, default))
        if isinstance(raw, bool):
            raise ValidationFailure(f"audit.{name} must be a number")
        try:
            result = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationFailure(f"audit.{name} must be a number") from exc
        if not math.isfinite(result) or not 0.0 <= result <= 1.0:
            raise ValidationFailure(f"audit.{name} must be between 0 and 1")
        if result < REQUIRED_SIMILARITY:
            raise ValidationFailure(
                f"audit.{name} cannot be lower than the required "
                f"{REQUIRED_SIMILARITY:.2f} independent similarity floor"
            )
        return result

    return (
        value("minimum_edge_iou", REQUIRED_SIMILARITY),
        value("minimum_source_coverage", REQUIRED_SIMILARITY),
        value("minimum_revit_precision", REQUIRED_SIMILARITY),
    )


def _bridge_build_id(
    task_id: str,
    *,
    tenant_id: str = "",
    project_id: str = "",
    wall_model_sha256: str = "",
) -> str:
    safe = "".join(ch for ch in task_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValidationFailure("invalid task id for Revit Bridge")
    identity = "|".join((tenant_id, project_id, safe, wall_model_sha256))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return ("wall-" + safe[:45] + "-" + digest)[:64]


def _configured_revit_target_root(settings: Any) -> Path | None:
    """Return the normalized RVT allow-list root when one is configured."""

    raw = str(getattr(settings, "revit_target_root", "") or "").strip()
    if not raw:
        return None
    try:
        root = Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValidationFailure("revit_target_root is invalid") from exc
    if not root.is_dir():
        raise ValidationFailure("revit_target_root must be an existing directory")
    return root


def _validate_revit_target_path(path: Path, settings: Any | None = None) -> Path:
    """Validate an RVT path and, when configured, keep it inside the allow-list."""

    try:
        resolved = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValidationFailure("Revit target path is invalid") from exc
    if resolved.suffix.lower() != ".rvt":
        raise ValidationFailure("Revit target path must be an RVT file")
    root = _configured_revit_target_root(settings or get_settings())
    if root is not None and resolved != root and root not in resolved.parents:
        raise PolicyFailure("Revit target path is outside the configured target root")
    return resolved


def _wall_compile_identity(model: WallModel) -> tuple[str, str]:
    """Return the compiler version and path-independent plan fingerprint."""

    from workers.pyrevit.wall_model_contract import normalize_wall_model_payload
    from workers.revit_bridge.wall_compiler import (
        WALL_COMPILER_VERSION,
        compiled_plan_sha256,
    )

    compiled = normalize_wall_model_payload(model.model_dump(mode="json"))
    return WALL_COMPILER_VERSION, compiled_plan_sha256(compiled)


def _wall_compiler_matches(actual: object, expected: str, model: WallModel) -> bool:
    """Accept the pre-column compiler only for legacy wall-only snapshots.

    ``typed_wall_model_v3`` added columns, v4 added connected wall delivery,
    v5 added the Revit 2020 grid-marker fallback, v6 separated the exact
    geometry transaction from the final wall-join transaction, and v7 makes
    optional near-pair joins fail closed without aborting unrelated walls.
    v9 treats Revit 2020 solid JoinGeometry as optional while retaining
    endpoint join permissions as the durable wall-connection contract. v15
    requires a native LocationCurve or solid-join read-back for every accepted
    topology junction and direction-preserving endpoint convergence. v16
    keeps evidence in the approved plan hash while omitting those unused
    locators from the size-bounded Revit Routes execution body.
    v23 adds drawing-specification evidence/status to the instance metadata;
    v24 adds source-evidence-backed shear/architectural wall semantics and
    the Revit structural-wall switch.  v24 intentionally does not accept a
    v23 approval snapshot because replaying it could create a shear wall as
    non-structural. v29 keeps exact-profile/irregular columns on the
    DirectShape path. v30 also binds native rectangular columns to the source
    level at both ends and falls back to exact geometry when a target family
    cannot represent the approved height.
    Existing wall-only test fixtures and approvals may still carry the v2
    label; accepting that label is safe only when the approved model contains
    no columns.  Any model with columns must use the current compiler.
    """

    if actual == expected:
        return True
    return (
        expected in {
            "typed_wall_model_v12",
            "typed_wall_model_v13",
            "typed_wall_model_v14",
            "typed_wall_model_v15",
            "typed_wall_model_v16",
            "typed_wall_model_v17",
            "typed_wall_model_v18",
            "typed_wall_model_v19",
            "typed_wall_model_v20",
            "typed_wall_model_v21",
            "typed_wall_model_v22",
            "typed_wall_model_v23",
        }
        and actual in {
            "typed_wall_model_v2", "typed_wall_model_v3", "typed_wall_model_v4",
            "typed_wall_model_v5", "typed_wall_model_v6", "typed_wall_model_v7",
        }
        and not model.columns
    )


def _validate_bridge_wall_delivery(
    bridge_result: Any,
    *,
    expected_build_id: str,
    model: WallModel,
    approved_result: RevitResult,
    target_path: Path,
    source_hash: str,
    convergence_round: int,
) -> dict[str, Any]:
    """Fail closed on a typed Bridge response before storing any artifact.

    A successful HTTP response is not proof that the approved model was
    applied.  Every identity, count, topology junction and coordinate
    read-back must bind to the exact approval snapshot sent by this task.
    """

    if getattr(bridge_result, "build_id", None) != expected_build_id:
        raise DependencyFailure("Revit Bridge response build_id does not match the request")
    if getattr(bridge_result, "status", None) != "executed":
        raise ValidationFailure(
            "Revit Bridge did not execute the approved WallModel: "
            + str(getattr(bridge_result, "message", ""))[:300]
        )
    details = getattr(bridge_result, "details", None)
    if not isinstance(details, dict):
        raise DependencyFailure("Revit Bridge returned no typed delivery details")

    wall_model_hash = canonical_sha256(model)
    compiler, plan_hash = _wall_compile_identity(model)
    expected_target = normalize_target_path(str(target_path))
    identity_checks = {
        "tenant_id": model.tenant_id,
        "project_id": model.project_id,
        "wall_model_sha256": wall_model_hash,
        "compiler": compiler,
        "compiled_plan_sha256": plan_hash,
        "source_model_sha256": source_hash,
        "target_model_path": expected_target,
        "convergence_round": convergence_round,
    }
    for field, expected in identity_checks.items():
        actual = details.get(field)
        if field == "target_model_path":
            try:
                actual = normalize_target_path(str(actual or ""))
            except ValueError as exc:
                raise DependencyFailure(
                    "Revit Bridge returned an invalid target_model_path"
                ) from exc
        if field == "compiler":
            if not _wall_compiler_matches(actual, expected, model):
                raise DependencyFailure(
                    f"Revit Bridge {field} does not match the approved delivery"
                )
            continue
        if actual != expected:
            raise DependencyFailure(
                f"Revit Bridge {field} does not match the approved delivery"
            )
    output_value = str(getattr(bridge_result, "output_path", "") or "").strip()
    detail_output_value = str(details.get("output_path") or "").strip()
    if not output_value or not detail_output_value:
        raise DependencyFailure("Revit Bridge output path is missing from typed details")
    try:
        if (
            not Path(output_value).is_absolute()
            or Path(detail_output_value).resolve(strict=False)
            != Path(output_value).resolve(strict=False)
        ):
            raise DependencyFailure("Revit Bridge output path is not consistently bound")
    except (OSError, RuntimeError) as exc:
        raise DependencyFailure("Revit Bridge output path is invalid") from exc

    approved_readback = approved_result.readback or {}
    if approved_result.tenant_id != model.tenant_id or approved_result.project_id != model.project_id:
        raise ValidationFailure("Revit approval result tenant/project does not match WallModel")
    if approved_result.wall_model_sha256 != wall_model_hash:
        raise ValidationFailure("Revit approval result does not reference the approved WallModel")
    expected_compiler = approved_readback.get("compiler")
    expected_plan = approved_readback.get("compiled_plan_sha256")
    if not _wall_compiler_matches(expected_compiler, compiler, model) or expected_plan != plan_hash:
        raise ValidationFailure(
            "Revit approval snapshot was produced by a different WallModel compiler"
        )

    readback = details.get("readback")
    if not isinstance(readback, dict):
        raise DependencyFailure("Revit Bridge did not return typed model read-back")
    readback_checks = {
        "tenant_id": model.tenant_id,
        "project_id": model.project_id,
        "wall_model_sha256": wall_model_hash,
        "compiler": compiler,
        "compiled_plan_sha256": plan_hash,
        "source_model_sha256": source_hash,
        "target_model_path": expected_target,
    }
    for field, expected in readback_checks.items():
        actual = readback.get(field)
        if field == "target_model_path":
            try:
                actual = normalize_target_path(str(actual or ""))
            except ValueError as exc:
                raise DependencyFailure("Revit read-back target path is invalid") from exc
        if field == "compiler":
            if not _wall_compiler_matches(actual, expected, model):
                raise DependencyFailure(
                    f"Revit read-back {field} does not match the approved delivery"
                )
            continue
        if actual != expected:
            raise DependencyFailure(
                f"Revit read-back {field} does not match the approved delivery"
            )
    detail_output_path = str(details.get("output_path") or "")
    detail_view_path = str(details.get("actual_view_path") or "")
    if (
        readback.get("output_path") != detail_output_path
        or readback.get("actual_view_path") != detail_view_path
        or readback.get("actual_view_sha256") != details.get("actual_view_sha256")
    ):
        raise DependencyFailure("Revit read-back artifact identity is inconsistent")

    wall_count = details.get("wall_count")
    column_count = details.get("column_count", 0)
    beam_count = details.get("beam_count", 0)
    grid_count = details.get("grid_count")
    if isinstance(wall_count, bool) or not isinstance(wall_count, int) or wall_count != len(model.walls):
        raise DependencyFailure("Revit Bridge wall read-back count is missing or mismatched")
    if isinstance(grid_count, bool) or not isinstance(grid_count, int) or grid_count != len(model.grid):
        raise DependencyFailure("Revit Bridge grid read-back count is missing or mismatched")
    if (
        isinstance(column_count, bool)
        or not isinstance(column_count, int)
        or column_count != len(model.columns)
    ):
        raise DependencyFailure("Revit Bridge column read-back count is missing or mismatched")
    if (
        isinstance(beam_count, bool)
        or not isinstance(beam_count, int)
        or beam_count != len(model.beams)
    ):
        raise DependencyFailure("Revit Bridge coupling-beam read-back count is missing or mismatched")
    if (
        readback.get("wall_count") != wall_count
        or readback.get("column_count", 0) != column_count
        or readback.get("beam_count", 0) != beam_count
        or readback.get("grid_count") != grid_count
    ):
        raise DependencyFailure("Revit Bridge read-back counts are internally inconsistent")
    opening_count = details.get("opening_count")
    matched_opening_count = details.get("matched_opening_count")
    opening_host_count = details.get("opening_host_count")
    expected_matched_openings = [
        opening for opening in model.openings if opening.status == "matched"
    ]
    expected_opening_hosts = {
        host_id
        for opening in expected_matched_openings
        for host_id in opening.host_wall_ids
    }
    if (
        isinstance(opening_count, bool)
        or not isinstance(opening_count, int)
        or opening_count != len(model.openings)
        or isinstance(matched_opening_count, bool)
        or not isinstance(matched_opening_count, int)
        or matched_opening_count != len(expected_matched_openings)
        or isinstance(opening_host_count, bool)
        or not isinstance(opening_host_count, int)
        or opening_host_count != len(expected_opening_hosts)
    ):
        raise DependencyFailure("Revit Bridge opening read-back count is missing or mismatched")
    if (
        readback.get("opening_count") != opening_count
        or readback.get("matched_opening_count") != matched_opening_count
        or readback.get("opening_host_count") != opening_host_count
        or readback.get("opening_semantics") != details.get("opening_semantics")
    ):
        raise DependencyFailure("Revit opening read-back is internally inconsistent")

    expected_cuts = sum(item.cut_status == "ready" for item in model.openings)
    cut_receipt = readback.get("opening_semantics") or {}
    if expected_cuts and (
        cut_receipt.get("cut_count") != expected_cuts
        or cut_receipt.get("vertical_cut_status") != "verified"
        or len(set(cut_receipt.get("created_opening_ids") or [])) != expected_cuts
    ):
        raise DependencyFailure("Revit did not verify the expected physical opening cuts")

    created_element_ids = details.get("created_element_ids")
    if (
        not isinstance(created_element_ids, list)
        or len(created_element_ids) != wall_count
        or any(not isinstance(item, str) or not item.strip() for item in created_element_ids)
        or len(set(created_element_ids)) != len(created_element_ids)
    ):
        raise DependencyFailure("Revit Bridge element-id read-back is missing or non-unique")
    created_column_ids = details.get("created_column_ids", [])
    if (
        not isinstance(created_column_ids, list)
        or len(created_column_ids) != column_count
        or any(not isinstance(item, str) or not item.strip() for item in created_column_ids)
        or len(set(created_column_ids)) != len(created_column_ids)
    ):
        raise DependencyFailure("Revit Bridge column element-id read-back is missing or non-unique")
    created_beam_ids = details.get("created_beam_ids", [])
    if (
        not isinstance(created_beam_ids, list)
        or len(created_beam_ids) != beam_count
        or any(not isinstance(item, str) or not item.strip() for item in created_beam_ids)
        or len(set(created_beam_ids)) != len(created_beam_ids)
    ):
        raise DependencyFailure("Revit Bridge coupling-beam element-id read-back is missing or non-unique")

    expected_junctions = {
        junction.junction_id: junction.kind for junction in model.junctions
    }
    topology = details.get("topology")
    if not isinstance(topology, dict):
        raise DependencyFailure("Revit Bridge did not return topology read-back")
    if topology.get("junction_count") != len(expected_junctions):
        raise DependencyFailure("Revit topology junction count does not match the model")
    joined_pairs = topology.get("joined_pairs")
    if isinstance(joined_pairs, bool) or not isinstance(joined_pairs, int) or joined_pairs < 0:
        raise DependencyFailure("Revit topology joined_pairs is invalid")
    junction_rows = topology.get("junctions")
    if not isinstance(junction_rows, list):
        raise DependencyFailure("Revit topology junction details are missing")
    actual_junctions: dict[str, str] = {}
    per_junction_counts: list[int] = []
    for row in junction_rows:
        if not isinstance(row, dict):
            raise DependencyFailure("Revit topology junction detail is invalid")
        junction_id = str(row.get("id") or "").strip()
        kind = str(row.get("kind") or "").strip()
        count = row.get("joined_pairs")
        if not junction_id or junction_id in actual_junctions or not kind:
            raise DependencyFailure("Revit topology returned duplicate or empty junction IDs")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise DependencyFailure("Revit topology junction has no joined wall pair")
        actual_junctions[junction_id] = kind
        per_junction_counts.append(count)
    if actual_junctions != expected_junctions:
        raise DependencyFailure("Revit topology junction read-back does not match the model")
    if expected_junctions:
        if joined_pairs <= 0 or joined_pairs < max(per_junction_counts) or joined_pairs > sum(per_junction_counts):
            raise DependencyFailure("Revit topology joined_pairs is inconsistent with junction details")
    elif joined_pairs != 0:
        raise DependencyFailure("Revit reported topology joins for a model without junctions")
    expected_kinds = sorted(set(expected_junctions.values()))
    actual_kinds = topology.get("kinds")
    if not isinstance(actual_kinds, list) or sorted(str(item) for item in actual_kinds) != expected_kinds:
        raise DependencyFailure("Revit topology kind read-back does not match the model")

    coordinate = details.get("coordinate_transform")
    if not isinstance(coordinate, dict):
        raise DependencyFailure("Revit coordinate-transform read-back is missing")
    for field in ("offset_x_mm", "offset_y_mm", "angle_rad"):
        value = coordinate.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise DependencyFailure("Revit coordinate-transform read-back is not finite")
    nested_topology = readback.get("topology")
    if nested_topology is not None and nested_topology != topology:
        raise DependencyFailure("Revit nested topology read-back is inconsistent")

    return {
        "details": details,
        "wall_count": wall_count,
        "column_count": column_count,
        "beam_count": beam_count,
        "grid_count": grid_count,
        "opening_count": opening_count,
        "matched_opening_count": matched_opening_count,
        "opening_host_count": opening_host_count,
        "created_element_ids": created_element_ids,
        "created_column_ids": created_column_ids,
        "created_beam_ids": created_beam_ids,
        "topology": topology,
        "coordinate_transform": coordinate,
        "compiler": compiler,
        "compiled_plan_sha256": plan_hash,
    }


def _require_local_bridge_artifact(
    value: str | None,
    *,
    suffix: str,
    artifact_name: str,
    allowed_root: str | Path | None = None,
) -> Path:
    """Resolve a Bridge artifact only when it is locally readable.

    The first Bridge deployment uses a shared filesystem.  A separate
    Windows host can still return a perfectly valid *host-local* path that is
    meaningless to the backend process; treating that path as an ordinary
    missing file obscures the deployment boundary and makes the task look
    nondeterministically broken.  Until an artifact-transfer/URI contract is
    enabled, fail with an actionable dependency error instead.
    """

    raw = str(value or "").strip()
    if not raw:
        raise DependencyFailure(
            f"Revit Bridge did not return a local {artifact_name} artifact; "
            "the Bridge must provide a shared path or an artifact-transfer URI"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise DependencyFailure(
            f"Revit Bridge returned a relative {artifact_name} path; "
            "return an absolute shared path or an artifact-transfer URI"
        )
    if path.suffix.lower() != suffix.lower():
        raise DependencyFailure(
            f"Revit Bridge returned an invalid {artifact_name} artifact suffix"
        )
    if allowed_root:
        try:
            root = Path(allowed_root).expanduser().resolve(strict=False)
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise DependencyFailure(
                f"Revit Bridge returned an invalid {artifact_name} path"
            ) from exc
        if resolved != root and root not in resolved.parents:
            raise DependencyFailure(
                f"Revit Bridge {artifact_name} is outside the configured Bridge work root"
            )
    if not path.is_file():
        raise DependencyFailure(
            f"Revit Bridge returned a host-local {artifact_name} path that is "
            "not readable by the backend; configure shared artifact storage "
            "or an artifact-transfer endpoint"
        )
    return path.resolve()


async def _persist_revit_delivery_failure(
    context: RequestContext,
    envelope: TaskEnvelope,
    *,
    model_path: Path,
    revit_result_path: Path,
    artifact_ids: dict[str, str],
    message: str,
    source_artifact_ids: list[str] | None = None,
    side_effect_possible: bool = False,
    rollback_state: str | None = None,
    rollback_hash: str | None = None,
) -> dict[str, str]:
    """Persist failed Revit and blocked-audit artifacts for later replay."""

    task_dir = _runtime_dir(envelope.task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    delivery_model = task_dir / "wall_model.json"
    delivery_result = task_dir / "revit_result.json"
    if model_path.is_file():
        shutil.copy2(model_path, delivery_model)
    if not delivery_model.is_file():
        return {}
    model = read_artifact(delivery_model, WallModel)

    # Preserve the complete source lineage beside the failure snapshot.  A
    # failed Bridge attempt is often replayed after the temporary approval
    # workspace has been removed; without a self-contained source manifest
    # and input snapshot the CLI/audit lineage gate would have no evidence to
    # inspect and an operator would be forced to rebuild the whole drawing
    # pipeline just to diagnose the failed delivery.
    _, durable_ids, lineage_errors = await _materialize_task_lineage_snapshot(
        context,
        task_dir,
        artifact_ids,
        source_artifact_ids=source_artifact_ids,
        strict=False,
    )
    artifact_ids.update(durable_ids)
    durable_source_render, render_errors = await _materialize_source_render_snapshot(
        context,
        task_dir,
        artifact_ids,
        strict=False,
    )
    lineage_errors.extend(render_errors)

    if revit_result_path.is_file():
        previous_result = read_artifact(revit_result_path, RevitResult)
    else:
        previous_result = RevitResult(
            tenant_id=model.tenant_id,
            project_id=model.project_id,
            wall_model_sha256=canonical_sha256(model),
            status="failed",
        )
    failed_result = previous_result.model_copy(update={
        "status": "failed",
        "errors": [message],
        "readback": {
            **(previous_result.readback or {}),
            "delivery_failure": message,
            "side_effect_possible": bool(
                side_effect_possible
                or (previous_result.readback or {}).get("after_sha256")
                or artifact_ids.get("revit_output.rvt")
                or artifact_ids.get("revit_actual_view.png")
            ),
            "rollback_state": str(rollback_state or "unknown"),
            "rollback_hash": rollback_hash,
        },
    })
    failed_result = RevitResult.model_validate(failed_result.model_dump(mode="json"))
    write_artifact(delivery_result, failed_result)
    service = get_artifact_service()
    result_artifact = await service.store_file(
        context, delivery_result, kind="revit_result",
    )

    source_path = durable_source_render
    source_hash = None
    source_id = None if source_path is not None else artifact_ids.get("source_render.png")
    if source_path is not None:
        try:
            source_hash = file_sha256(source_path)
        except Exception:
            source_path = None
    if source_path is None and source_id:
        try:
            # Use the same tenant/project, size and content-hash gate as the
            # success path.  A failure report must not accidentally point at a
            # replaced or cross-project render artifact.
            _, source_path = await _resolve_verified_artifact(
                context,
                source_id,
                label="source render",
                expected_kind="wall_source_render",
            )
            source_hash = file_sha256(source_path)
        except Exception:
            source_path = None
    report_path = task_dir / "audit_report.json"
    existing_report: AuditReport | None = None
    if report_path.is_file():
        try:
            candidate = read_artifact(report_path, AuditReport)
            # A completed independent comparison is the strongest available
            # failure evidence.  Do not replace its directional metrics and
            # overlay lineage with a generic blocked report merely because
            # the 95% gate raised after the report was persisted.
            if (
                candidate.independent_sources
                and candidate.metrics
                and candidate.status == "fail"
            ):
                existing_report = candidate
        except Exception:
            existing_report = None
    if existing_report is None:
        report = AuditReport(
            tenant_id=model.tenant_id,
            project_id=model.project_id,
            wall_model_sha256=canonical_sha256(model),
            revit_result_sha256=canonical_sha256(failed_result),
            status="blocked",
            independent_sources=False,
            source_render_path=str(source_path.resolve()) if source_path else None,
            source_render_sha256=source_hash,
            errors=["Revit delivery failed: " + message, *lineage_errors],
        )
        write_artifact(report_path, report)
    report_artifact = await service.store_file(
        context, report_path, kind="wall_audit_report",
    )
    return {
        **artifact_ids,
        "revit_result.json": result_artifact["id"],
        "audit_report.json": report_artifact["id"],
    }


async def _execute_revit_and_audit(
    context: RequestContext,
    envelope: TaskEnvelope,
    *,
    model_path: Path,
    revit_result_path: Path,
    previous: dict[str, Any],
    artifact_ids: dict[str, str],
    budget: ExecutionBudget,
) -> AgentResult:
    """Execute delivery and persist a failure snapshot on every failed gate."""

    # A Bridge call can mutate its isolated RVT and then fail before returning
    # a response.  Keep this state outside the implementation so the failure
    # journal can distinguish a preflight/validation failure from an attempted
    # external write.
    execution_state: dict[str, bool] = {"attempted": False}
    try:
        return await _execute_revit_and_audit_impl(
            context,
            envelope,
            model_path=model_path,
            revit_result_path=revit_result_path,
            previous=previous,
            artifact_ids=artifact_ids,
            budget=budget,
            execution_state=execution_state,
        )
    except Exception as exc:
        # The task runtime records the failed step, but a failed external
        # delivery must also leave durable engineering artifacts for replay and
        # audit.  Never hide the original error if persistence itself fails.
        try:
            failure_ids = await _persist_revit_delivery_failure(
                context,
                envelope,
                model_path=model_path,
                revit_result_path=revit_result_path,
                artifact_ids=artifact_ids,
                message=str(exc)[:1000] or "Revit delivery failed",
                source_artifact_ids=previous.get("source_artifact_ids"),
                side_effect_possible=execution_state["attempted"],
                rollback_state=getattr(exc, "rollback_state", None),
                rollback_hash=getattr(exc, "rollback_hash", None),
            )
            if failure_ids:
                # WorkflowRuntime can surface these durable artifacts in the
                # task result/event even though the original exception remains
                # the source of the failure classification.
                setattr(exc, "failure_artifact_ids", failure_ids)
        except Exception as persistence_error:
            logger.exception(
                "failed to persist Revit delivery failure snapshot: %s",
                persistence_error,
            )
        raise


async def _execute_revit_and_audit_impl(
    context: RequestContext,
    envelope: TaskEnvelope,
    *,
    model_path: Path,
    revit_result_path: Path,
    previous: dict[str, Any],
    artifact_ids: dict[str, str],
    budget: ExecutionBudget,
    execution_state: dict[str, bool] | None = None,
) -> AgentResult:
    from workers.revit_bridge.contracts import PresentWallModelRequest, RunWallModelRequest

    model = read_artifact(model_path, WallModel)
    # Re-check the complete immutable source lineage immediately before any
    # external Revit call.  The model approval step may have happened much
    # earlier, and artifact references must not be replaceable between gates.
    await _verify_wall_model_lineage(
        context,
        model,
        artifact_ids,
        source_artifact_ids=previous.get("source_artifact_ids"),
    )
    approved_result = read_artifact(revit_result_path, RevitResult)
    if approved_result.approval is None or approved_result.approval.status != "approved":
        raise ValidationFailure("Revit write approval record is missing")
    settings = get_settings()
    target_path = _validate_revit_target_path(
        Path(model.revit.target_model_path or ""), settings
    )
    if not target_path.is_file():
        raise ValidationFailure("approved Revit target RVT is missing")
    source_model_hash = file_sha256(target_path)
    approved_snapshot = (approved_result.readback or {}).get("source_model_sha256")
    if approved_snapshot != source_model_hash:
        raise ValidationFailure(
            "target RVT changed after dry-run; repeat the approval flow"
        )
    wall_model_hash = canonical_sha256(model)
    if (
        approved_result.tenant_id != model.tenant_id
        or approved_result.project_id != model.project_id
        or approved_result.wall_model_sha256 != wall_model_hash
    ):
        raise ValidationFailure(
            "Revit approval result is not bound to the approved WallModel context"
        )
    expected_compiler, expected_plan_hash = _wall_compile_identity(model)
    approved_readback = approved_result.readback or {}
    if (
        not _wall_compiler_matches(approved_readback.get("compiler"), expected_compiler, model)
        or approved_readback.get("compiled_plan_sha256") != expected_plan_hash
    ):
        raise ValidationFailure(
            "Revit approval result was produced by a different WallModel compiler"
        )
    try:
        expected_convergence_round = int(envelope.options.get("convergence_round", 1))
    except (TypeError, ValueError) as exc:
        raise ValidationFailure("convergence_round must be an integer") from exc
    if not 1 <= expected_convergence_round <= 5:
        raise ValidationFailure("convergence_round must be between 1 and 5")
    approval_binding = approval_digest(approved_result.approval)
    bridge_build_id = _bridge_build_id(
        envelope.task_id,
        tenant_id=context.tenant_id,
        project_id=_project_id(context),
        wall_model_sha256=wall_model_hash,
    )
    token = issue_approval_token(
        settings.revit_bridge_approval_secret,
        build_id=bridge_build_id,
        action="run_wall_model",
        wall_model_sha256=wall_model_hash,
        target_model_path=normalize_target_path(str(target_path)),
        source_model_sha256=source_model_hash,
        approval_digest=approval_binding,
    )
    budget.consume_tool_call("revit_bridge.run_wall_model")
    if execution_state is not None:
        execution_state["attempted"] = True
    bridge_result = await RevitBridgeClient().run_wall_model(RunWallModelRequest(
        build_id=bridge_build_id,
        source_model_path=str(target_path),
        wall_model=model.model_dump(mode="json"),
        revit_result=approved_result.model_dump(mode="json"),
        source_model_sha256=source_model_hash,
        dry_run=False,
        approval_token=token,
        convergence_round=expected_convergence_round,
    ))
    validated_bridge = _validate_bridge_wall_delivery(
        bridge_result,
        expected_build_id=bridge_build_id,
        model=model,
        approved_result=approved_result,
        target_path=target_path,
        source_hash=source_model_hash,
        convergence_round=expected_convergence_round,
    )
    details = validated_bridge["details"]
    output_path = _require_local_bridge_artifact(
        bridge_result.output_path,
        suffix=".rvt",
        artifact_name="RVT working copy",
        allowed_root=getattr(settings, "revit_bridge_work_root", None),
    )
    actual_view_path = _require_local_bridge_artifact(
        details.get("actual_view_path"),
        suffix=".png",
        artifact_name="actual plan-view PNG",
        allowed_root=getattr(settings, "revit_bridge_work_root", None),
    )
    if output_path == target_path:
        raise DependencyFailure("Revit Bridge returned the approved source RVT as its output")
    actual_view_hash = file_sha256(actual_view_path)
    if str(details.get("actual_view_sha256") or "") != actual_view_hash:
        raise ValidationFailure("Revit Bridge actual-view hash does not match the returned file")
    output_hash = file_sha256(output_path)
    expected_source_hash = str(
        (approved_result.readback or {}).get("source_model_sha256") or ""
    )
    if str(details.get("before_sha256") or "") != expected_source_hash:
        raise ValidationFailure("Revit Bridge before-hash does not match the approved snapshot")
    if str(details.get("after_sha256") or "") != output_hash:
        raise ValidationFailure("Revit Bridge after-hash does not match the returned RVT")
    if output_hash == expected_source_hash:
        raise ValidationFailure("Revit Bridge returned an unchanged RVT")
    wall_count = validated_bridge["wall_count"]
    column_count = validated_bridge.get("column_count", 0)
    beam_count = validated_bridge.get("beam_count", 0)
    grid_count = validated_bridge["grid_count"]
    created_element_ids = validated_bridge["created_element_ids"]
    created_column_ids = validated_bridge.get("created_column_ids", [])
    created_beam_ids = validated_bridge.get("created_beam_ids", [])
    source_render_artifact_id = artifact_ids.get("source_render.png")
    if not source_render_artifact_id:
        raise ValidationFailure("source render artifact is required for independent audit")

    service = get_artifact_service()
    output_artifact = await service.store_file(
        context, output_path, kind="revit_working_copy", filename="revit_output.rvt",
    )
    view_artifact = await service.store_file(
        context, actual_view_path, kind="revit_actual_view", filename="revit_actual_view.png",
    )
    _, stored_output_path = await _resolve_verified_artifact(
        context,
        output_artifact["id"],
        label="stored Revit output",
        expected_kind="revit_working_copy",
    )
    _, stored_view_path = await _resolve_verified_artifact(
        context,
        view_artifact["id"],
        label="stored Revit actual view",
        expected_kind="revit_actual_view",
    )
    _, stored_source_path = await _resolve_verified_artifact(
        context,
        source_render_artifact_id,
        label="source render",
        expected_kind="wall_source_render",
    )
    # Make the successful Bridge read-back visible to the failure journal too;
    # an overlay failure must not erase evidence that Revit already changed the
    # isolated copy.
    artifact_ids.update({
        "revit_output.rvt": output_artifact["id"],
        "revit_actual_view.png": view_artifact["id"],
    })
    updated_result = approved_result.model_copy(update={
        "status": "succeeded",
        "transaction_id": bridge_result.build_id,
        "created_element_ids": [str(item) for item in created_element_ids],
        "created_column_ids": [str(item) for item in created_column_ids],
        "created_beam_ids": [str(item) for item in created_beam_ids],
        "readback": {
            **(approved_result.readback or {}),
            **(details.get("readback") or {}),
            "output_rvt_path": str(stored_output_path.resolve()),
            "source_model_sha256": expected_source_hash,
            "before_sha256": str(details["before_sha256"]),
            "after_sha256": str(details["after_sha256"]),
            "wall_count": wall_count,
            "column_count": column_count,
            "beam_count": beam_count,
            "grid_count": grid_count,
            "topology": details.get("topology") or {},
        },
        "actual_view_path": str(stored_view_path.resolve()),
        "actual_view_sha256": actual_view_hash,
        "errors": [],
    })
    updated_result = RevitResult.model_validate(updated_result.model_dump(mode="json"))
    write_artifact(revit_result_path, updated_result)

    # Keep a persistent task workspace for the final audit and later replay.
    # Approval workspaces are temporary; materialize both the contracts and
    # the original source bytes before writing the audit report so its paths
    # remain usable after this worker invocation ends.
    task_dir = _runtime_dir(envelope.task_id)
    audit_lineage_paths, durable_ids, lineage_errors = await _materialize_task_lineage_snapshot(
        context,
        task_dir,
        artifact_ids,
        source_artifact_ids=previous.get("source_artifact_ids"),
        strict=True,
    )
    if lineage_errors:
        raise ValidationFailure("durable source lineage snapshot failed: " + "; ".join(lineage_errors))
    artifact_ids.update(durable_ids)
    durable_source_path, render_errors = await _materialize_source_render_snapshot(
        context,
        task_dir,
        artifact_ids,
        strict=True,
    )
    if render_errors or durable_source_path is None:
        raise ValidationFailure(
            "durable source render snapshot failed: " + "; ".join(render_errors)
        )
    delivery_model = task_dir / "wall_model.json"
    delivery_result = task_dir / "revit_result.json"
    shutil.copy2(model_path, delivery_model)
    write_artifact(delivery_result, updated_result)
    updated_result_artifact = await service.store_file(
        context, delivery_result, kind="revit_result",
    )
    artifact_ids["revit_result.json"] = updated_result_artifact["id"]
    minimum_edge_iou, minimum_source_coverage, minimum_revit_precision = _audit_thresholds(
        envelope.options
    )
    audit = finalize_audit(
        delivery_model,
        delivery_result,
        source_render_path=durable_source_path,
        overlay_path=task_dir / "audit_overlay.png",
        minimum_edge_iou=minimum_edge_iou,
        minimum_source_coverage=minimum_source_coverage,
        minimum_revit_precision=minimum_revit_precision,
        lineage_paths=audit_lineage_paths,
    )
    audit_artifact = await service.store_file(
        context, task_dir / "audit_report.json",
        kind="wall_audit_report",
    )
    overlay_artifact = await service.store_file(
        context, task_dir / "audit_overlay.png",
        kind="wall_audit_overlay",
    )
    artifact_ids.update({
        "audit_report.json": audit_artifact["id"],
        "audit_overlay.png": overlay_artifact["id"],
    })
    if audit.status != "pass":
        raise ValidationFailure(
            "independent Revit overlay audit did not pass: " + "; ".join(audit.errors)
        )

    budget.consume_tool_call("revit_bridge.present_wall_model")
    presentation = await RevitBridgeClient().present_wall_model(
        PresentWallModelRequest(
            build_id=bridge_build_id,
            output_sha256=output_hash,
        )
    )
    if (
        presentation.status != "presented"
        or presentation.build_id != bridge_build_id
        or not presentation.output_path
        or Path(presentation.output_path).resolve() != output_path.resolve()
        or presentation.details.get("output_sha256") != output_hash
    ):
        raise DependencyFailure(
            "Revit Bridge did not confirm opening the audited delivery in Revit"
        )

    updated_ids = {
        **artifact_ids,
        "revit_result.json": updated_result_artifact["id"],
        "revit_output.rvt": output_artifact["id"],
        "revit_actual_view.png": view_artifact["id"],
        "audit_report.json": audit_artifact["id"],
        "audit_overlay.png": overlay_artifact["id"],
    }
    structured = {
        **previous,
        "stage": "delivery_complete",
        "artifact_ids": updated_ids,
        "revit_result": updated_result.model_dump(mode="json"),
        "audit_report": audit.model_dump(mode="json"),
        "external_side_effect": True,
        "revit_bridge_required": False,
        "independent_audit": True,
        "revit_presentation": presentation.details,
    }
    return _artifact_result(
        status="succeeded",
        answer="图纸已完成清洗、墙体证据审核、Revit Dry-run、人工批准、Bridge 交付和独立叠图验收。",
        structured_output=structured,
        artifact_ids=list(updated_ids.values()),
        confidence=1.0,
        next_action="delivery_complete",
    )

async def wall_pipeline_approve_write_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    budget.consume_tool_call("wall_pipeline.approve_revit_write")
    if envelope.resume_payload.get("decision") != "approved":
        raise ValidationFailure("wall_pipeline Revit step requires an approved decision")
    previous = await _latest_wall_payload(context, envelope.task_id)
    artifact_ids = previous.get("artifact_ids") or {}
    model_artifact_id = artifact_ids.get("wall_model.json")
    revit_artifact_id = artifact_ids.get("revit_result.json")
    if not model_artifact_id or not revit_artifact_id:
        raise ValidationFailure("approved wall model and dry-run artifacts are required")
    operator = _approval_operator(context, envelope)
    reason = str(envelope.resume_payload.get("reason") or "Revit write approved")
    with tempfile.TemporaryDirectory(prefix="buildmate-wall-write-") as temp:
        directory = Path(temp)
        model_path = await _copy_artifact_to_workspace(
            context,
            model_artifact_id,
            directory,
            "wall_model.json",
            expected_kind="wall_model",
        )
        await _copy_artifact_to_workspace(
            context,
            revit_artifact_id,
            directory,
            "revit_result.json",
            expected_kind="revit_result",
        )
        lineage_paths: dict[str, Path] = {}
        for filename, kind in _LINEAGE_ARTIFACT_KINDS.items():
            artifact_id = artifact_ids.get(filename)
            if not artifact_id:
                raise ValidationFailure(
                    "wall model lineage artifact is missing from the previous step: "
                    + filename
                )
            lineage_paths[filename] = await _copy_artifact_to_workspace(
                context,
                artifact_id,
                directory,
                filename,
                expected_kind=kind,
            )
        try:
            # The second approval is a fresh engineering gate.  Re-read the
            # immutable source/evidence chain after the potentially long
            # human-review pause so a replaced upload or stale artifact can
            # never be promoted to a Revit write capability.
            model_for_approval = read_artifact(model_path, WallModel)
            await _verify_wall_model_lineage(
                context,
                model_for_approval,
                artifact_ids,
                source_artifact_ids=previous.get("source_artifact_ids"),
            )
            result = approve_revit_write(
                model_path,
                actor_id=operator,
                reason=reason,
                lineage_paths=lineage_paths,
            )
        except (ValueError, OSError) as exc:
            raise ValidationFailure(f"Revit write approval failed: {str(exc)[:500]}") from exc
        revit_artifact = await get_artifact_service().store_file(context, directory / "revit_result.json", kind="revit_result")
        updated_ids = {
            **artifact_ids,
            "revit_result.json": revit_artifact["id"],
        }
        if _execute_revit_requested(envelope.options):
            return await _execute_revit_and_audit(
                context,
                envelope,
                model_path=model_path,
                revit_result_path=directory / "revit_result.json",
                previous=previous,
                artifact_ids=updated_ids,
                budget=budget,
            )

    # The Windows Bridge remains a separately deployed process.  The task is
    # complete only for the approved hand-off; no Revit side effect is claimed.
    structured = {
        **previous,
        "stage": "revit_bridge_handoff",
        "artifact_ids": updated_ids,
        "revit_result": result.model_dump(mode="json"),
        "external_side_effect": False,
        "revit_bridge_required": True,
    }
    return _artifact_result(
        status="succeeded",
        answer="Revit 写入审批已记录。当前任务已生成 write_approved 交接物，等待 Windows Revit Bridge 执行并回传独立视图。",
        structured_output=structured,
        artifact_ids=list(structured["artifact_ids"].values()),
        confidence=1.0,
        next_action="revit_bridge_execution",
    )


async def drawing_review_prepare_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    """Select the deterministic wall path for PDF/DWG/DXF, legacy Agent otherwise."""
    if not envelope.input_artifact_ids:
        raise ValidationFailure("drawing_review requires at least one drawing artifact")
    if len(set(envelope.input_artifact_ids)) != len(envelope.input_artifact_ids):
        raise ValidationFailure("drawing_review source artifact IDs must be unique")

    # Resolve through the same immutable artifact gate used by the wall
    # pipeline staging step.  The extension is only a routing hint; a
    # stale/replaced object must not decide whether a task is sent to the
    # deterministic wall chain or the legacy agent.  This check deliberately
    # runs even when ``engine=legacy``: that option is kept for IFC/image
    # compatibility, but must not become a client-controlled escape hatch for
    # PDF/DWG wall work.
    suffixes: list[str] = []
    for artifact_id in envelope.input_artifact_ids:
        artifact, _ = await _resolve_verified_artifact(
            context, artifact_id, label="drawing review source"
        )
        suffixes.append(Path(str(artifact.get("filename") or "")).suffix.lower())

    wall_sources = [suffix in _WALL_SOURCE_SUFFIXES for suffix in suffixes]
    if any(wall_sources) and not all(wall_sources):
        raise ValidationFailure(
            "drawing_review cannot mix PDF/DWG/DXF with IFC/image/other artifacts"
        )
    if all(wall_sources):
        if envelope.options.get("engine") == "legacy":
            raise ValidationFailure(
                "PDF/DWG/DXF drawing_review must use the deterministic wall_pipeline; "
                "engine=legacy cannot bypass WallEvidence/WallModel gates"
            )
        result = await wall_pipeline_prepare_step(context, envelope, budget)
        return result
    if not all(suffix in _LEGACY_DRAWING_SUFFIXES for suffix in suffixes):
        raise ValidationFailure(
            "drawing_review source artifact has an unsupported or missing extension"
        )
    if len(suffixes) > 1:
        raise ValidationFailure(
            "legacy Drawing2BIM review currently accepts one IFC or image artifact; "
            "use wall_pipeline for multiple PDF/DWG/DXF sources"
        )

    from backend.workers.workflows import legacy_agent_step

    result = await legacy_agent_step(context, envelope, budget)
    structured = result.structured_output if isinstance(result.structured_output, dict) else {}
    # The legacy Agent's payload is advisory.  Derive the workflow stage from
    # its bounded runtime status instead of accepting a stage supplied by an
    # LLM or an old client; this gives the approval/write handlers a finite
    # state machine to enforce.
    waiting_for_human = (
        result.status == "waiting_human"
        or bool(structured.get("requires_human_review"))
        or bool(structured.get("hitl_required"))
    )
    result.structured_output = {
        **structured,
        "pipeline": "legacy_drawing2bim",
        "stage": _LEGACY_REVIEW_PENDING if waiting_for_human else _LEGACY_AUTO_CANDIDATE,
        "requires_human_review": waiting_for_human,
    }
    return result


async def drawing_review_approval_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    previous = await _latest_task_payload(context, envelope.task_id)
    if previous.get("pipeline") == "wall_pipeline":
        return await wall_pipeline_approve_model_step(context, envelope, budget)
    if previous.get("pipeline") != "legacy_drawing2bim":
        raise ValidationFailure(
            "drawing_review approval state is missing or belongs to another pipeline"
        )
    stage = str(previous.get("stage") or "")
    if stage not in {_LEGACY_REVIEW_PENDING, _LEGACY_AUTO_CANDIDATE}:
        raise ValidationFailure(
            "legacy Drawing2BIM approval requires a pending or auto-candidate stage"
        )
    # Low-risk legacy runs can still complete automatically.  Whenever the
    # Agent (or a caller) indicates that this is a human-gated resume, require
    # the same authenticated decision contract as the deterministic path.
    approval: dict[str, str] | None = None
    if stage == _LEGACY_REVIEW_PENDING or envelope.resume_payload:
        operator, reason = _legacy_hitl_approval(
            context, envelope, action="review approval"
        )
        approval = {"status": "approved", "actor_id": operator, "reason": reason}
    structured = {
        **previous,
        "stage": _LEGACY_REVIEW_APPROVED if approval else _LEGACY_AUTO_PASSED,
        "requires_human_review": False,
        **({"approval": approval} if approval else {}),
    }
    artifact_ids = list(dict.fromkeys(
        str(item) for item in (previous.get("artifact_ids") or envelope.input_artifact_ids)
        if str(item).strip()
    ))
    # A human review approval must not implicitly authorize the following
    # write step in the same queue delivery.  Returning ``waiting_human``
    # causes WorkflowRuntime to persist this step and stop; the write handler
    # can only run after a second, independently authenticated resume.  The
    # automatic legacy candidate remains a one-pass compatibility path.
    if approval is not None:
        return _artifact_result(
            status="waiting_human",
            answer="Drawing2BIM 审查已批准。请单独确认后续模型写入。",
            structured_output=structured,
            artifact_ids=artifact_ids,
            confidence=1.0,
            next_action="write_decision",
        )
    return _artifact_result(
        status="succeeded",
        answer="Drawing2BIM 审查结果已记录。",
        structured_output=structured,
        artifact_ids=artifact_ids,
        confidence=1.0,
    )


async def drawing_review_write_step(
    context: RequestContext,
    envelope: TaskEnvelope,
    budget: ExecutionBudget,
) -> AgentResult:
    previous = await _latest_task_payload(context, envelope.task_id)
    if previous.get("pipeline") == "wall_pipeline":
        return await wall_pipeline_approve_write_step(context, envelope, budget)
    if previous.get("pipeline") != "legacy_drawing2bim":
        raise ValidationFailure(
            "drawing_review write state is missing or belongs to another pipeline"
        )
    stage = str(previous.get("stage") or "")
    if stage == _LEGACY_REVIEW_APPROVED:
        operator, reason = _legacy_hitl_approval(
            context, envelope, action="write approval"
        )
        approval = {
            "status": "approved", "actor_id": operator, "reason": reason,
        }
        structured = {
            **previous,
            "stage": _LEGACY_COMPLETE,
            "write_approval": approval,
            "requires_human_review": False,
        }
    elif stage == _LEGACY_AUTO_PASSED:
        if envelope.resume_payload:
            raise ValidationFailure(
                "legacy Drawing2BIM write has no pending approval"
            )
        structured = {
            **previous,
            "stage": _LEGACY_COMPLETE,
            "requires_human_review": False,
        }
    else:
        raise ValidationFailure(
            "legacy Drawing2BIM write requires an approved or auto-passed stage"
        )
    artifact_ids = list(dict.fromkeys(
        str(item) for item in (previous.get("artifact_ids") or envelope.input_artifact_ids)
        if str(item).strip()
    ))
    return _artifact_result(
        status="succeeded",
        answer="Drawing2BIM 审查工作流已完成。",
        structured_output=structured,
        artifact_ids=artifact_ids,
        confidence=1.0,
    )


__all__ = [
    "drawing_review_approval_step",
    "drawing_review_prepare_step",
    "drawing_review_write_step",
    "wall_pipeline_approve_model_step",
    "wall_pipeline_approve_write_step",
    "wall_pipeline_prepare_step",
]
