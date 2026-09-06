"""Orchestration for the six deterministic wall-pipeline artifacts."""

from __future__ import annotations

import logging
import mimetypes
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend.domain.approval_token import normalize_target_path
from backend.engines.wall_pipeline.adapters import (
    extract_source_entities,
    validate_source_file,
    verify_source_file_against_manifest,
)
from backend.engines.wall_pipeline.audit import (
    REQUIRED_SIMILARITY,
    create_independent_audit,
    render_original_source,
    source_geometry_bounds,
)
from backend.engines.wall_pipeline.config import load_wall_pipeline_config
from backend.engines.wall_pipeline.contracts import (
    ApprovalRecord,
    AuditReport,
    ManifestSource,
    RevitResult,
    SourceEntities,
    SourceManifest,
    WallEvidence,
    WallModel,
    WallPipelineConfig,
)
from backend.engines.wall_pipeline.geometry import (
    build_wall_evidence,
    build_wall_model,
)
from backend.engines.wall_pipeline.io import (
    canonical_payload,
    canonical_sha256,
    file_sha256,
    read_artifact,
    write_artifact,
)


logger = logging.getLogger(__name__)


ARTIFACT_FILENAMES = (
    "source_manifest.json",
    "source_entities.json",
    "wall_evidence.json",
    "wall_model.json",
    "revit_result.json",
    "audit_report.json",
)
_PUBLISHED_FILENAMES = (*ARTIFACT_FILENAMES, "source_render.png")
_SOURCE_ENTITIES_CACHE_VERSION = 1


def _source_entities_cache_dir(output_dir: Path) -> Path:
    """Return the shared cache for deterministic source extraction.

    Parsing a large PDF/DXF is the dominant cost before geometry can start.
    The cache is deliberately outside a task workspace so a retry or a new
    task for the same immutable upload can reuse it.  Only source entities are
    cached: evidence, model, approvals, Revit output and audits are always
    recalculated for the current task.
    """

    configured = os.environ.get("WALL_PIPELINE_SOURCE_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    resolved = output_dir.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name.lower() == "wall_pipeline":
            return candidate / ".source-cache"
    return resolved.parent / ".source-cache"


def _source_entities_cache_path(
    config: WallPipelineConfig, manifest: SourceManifest, output_dir: Path
) -> Path:
    """Build a content-addressed cache path for the extraction contract."""

    extraction_config = {
        name: getattr(config, name).model_dump(mode="json")
        for name in (
            "source", "coordinate", "grid", "wall", "column",
            "opening", "beam",
        )
    }
    # Normalize task-local paths and timestamps.  A retry stages the same
    # upload below another task directory; those operational paths must not
    # produce a different cache key.
    for index, source in enumerate(extraction_config["source"].get("files") or [], start=1):
        source["path"] = f"source://{manifest.sources[index - 1].sha256}"
    manifest_payload = canonical_payload(manifest)
    identity = {
        "schema": "buildmate.source-entities-cache/1.0",
        "version": _SOURCE_ENTITIES_CACHE_VERSION,
        "manifest": manifest_payload,
        "config": extraction_config,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()
    return _source_entities_cache_dir(output_dir) / f"{digest}.json"


def _load_cached_source_entities(
    config: WallPipelineConfig, manifest: SourceManifest, output_dir: Path
) -> SourceEntities | None:
    path = _source_entities_cache_path(config, manifest, output_dir)
    if not path.is_file():
        return None
    try:
        cached = read_artifact(path, SourceEntities)
        if cached.manifest_sha256 != canonical_sha256(manifest):
            return None
        if cached.tenant_id != manifest.tenant_id or cached.project_id != manifest.project_id:
            return None
        return cached
    except (OSError, ValueError, TypeError):
        # A truncated or old cache is a miss.  The source adapter remains the
        # authoritative path and will atomically replace it after extraction.
        return None


def _store_cached_source_entities(
    config: WallPipelineConfig,
    manifest: SourceManifest,
    output_dir: Path,
    entities: SourceEntities,
) -> None:
    path = _source_entities_cache_path(config, manifest, output_dir)
    try:
        write_artifact(path, entities)
    except OSError:
        # Caching improves latency but is never required for correctness.
        logger.warning("wall_pipeline.source_entities_cache_write_failed", extra={"path": str(path)})


def _publish_run_artifacts(staging_dir: Path, output_dir: Path) -> dict[str, Path]:
    """Publish one completed run without exposing partial stage artifacts.

    Every parser/geometry stage writes below a private directory.  A failed
    run therefore cannot leave a new manifest next to an old WallModel and
    make a later operator act on a mixed set of artifacts.  Individual files
    are moved with ``os.replace`` only after all stages have succeeded.
    """

    # Validate the complete set before touching an existing run.  This keeps
    # a malformed/private stage from deleting a previously published result.
    staged_paths: dict[str, Path] = {}
    for name in _PUBLISHED_FILENAMES:
        staged = staging_dir / name
        if not staged.is_file():
            if name == "source_render.png":
                raise RuntimeError("wall pipeline source render was not produced")
            raise RuntimeError(f"wall pipeline artifact was not produced: {name}")
        staged_paths[name] = staged

    # ``os.replace`` is atomic per file, not across the six-contract bundle.
    # Move an older bundle into a same-volume rollback directory first, then
    # publish the new files.  If any individual replacement fails (disk full,
    # antivirus lock, process interruption during a test), restore every old
    # file and remove the partial new bundle.  The backup is scoped to this
    # output directory and never touches unrelated user files.
    backup_dir = Path(tempfile.mkdtemp(prefix=".wall-publish-backup-", dir=str(output_dir)))
    moved_new: list[Path] = []
    moved_old: list[tuple[Path, Path]] = []
    cleanup_backup = True
    try:
        for name in _PUBLISHED_FILENAMES:
            destination = output_dir / name
            if destination.exists():
                backup = backup_dir / name
                os.replace(destination, backup)
                moved_old.append((destination, backup))
        final_paths: dict[str, Path] = {}
        for name, staged in staged_paths.items():
            destination = output_dir / name
            os.replace(staged, destination)
            moved_new.append(destination)
            final_paths[name] = destination
    except Exception as exc:
        rollback_errors: list[str] = []
        for destination in moved_new:
            try:
                if destination.exists():
                    destination.unlink()
            except OSError as rollback_error:
                rollback_errors.append(f"remove {destination}: {rollback_error}")
        for destination, backup in reversed(moved_old):
            try:
                if backup.exists():
                    os.replace(backup, destination)
            except OSError as rollback_error:
                rollback_errors.append(f"restore {destination}: {rollback_error}")
        detail = "; ".join(rollback_errors)
        if detail:
            # Keep the old bundle available for operator recovery.  Removing
            # this directory here would turn a recoverable publish failure
            # into permanent data loss when an antivirus lock or disk error
            # prevents restoring one of the previous files.
            cleanup_backup = False
            raise RuntimeError(
                "wall pipeline artifact publication failed and rollback was incomplete: "
                + detail
                + f"; recovery backup retained at {backup_dir}"
            ) from exc
        raise RuntimeError("wall pipeline artifact publication failed; previous bundle restored") from exc
    finally:
        if cleanup_backup:
            shutil.rmtree(backup_dir, ignore_errors=True)
    # The public API historically returns the six JSON contracts.  The source
    # render remains discoverable at output_dir/source_render.png and is also
    # registered by the v2 workflow, but is intentionally not added to that
    # return mapping to keep the CLI contract backward compatible.
    final_paths.pop("source_render.png", None)
    return final_paths


def _build_manifest(config: WallPipelineConfig) -> SourceManifest:
    sources = []
    for index, source in enumerate(config.source.files, start=1):
        path = Path(source.path)
        expected_extensions = (
            {".pdf"} if config.source.type == "pdf" else {".dwg", ".dxf"}
        )
        if path.suffix.lower() not in expected_extensions:
            raise ValueError(
                f"source.type={config.source.type} cannot read {path.suffix}: {path}"
            )
        # Apply the same size/regular-file gate as the source adapter before
        # hashing.  Hashing is deliberately expensive and must not become a
        # bypass for oversized, empty, or non-file inputs.
        source_kind = path.suffix.lstrip(".").upper() or config.source.type.upper()
        size_before = validate_source_file(path, source_kind=source_kind)
        try:
            stat_before = path.stat()
        except OSError as exc:
            raise RuntimeError(f"{source_kind}_SOURCE_STAT_FAILED: {path}") from exc
        snapshot_before = (
            stat_before.st_size,
            getattr(stat_before, "st_mtime_ns", 0),
            getattr(stat_before, "st_ctime_ns", 0),
            getattr(stat_before, "st_dev", 0),
            getattr(stat_before, "st_ino", 0),
        )
        try:
            source_sha256 = file_sha256(path)
        except Exception as exc:
            raise RuntimeError(
                f"{source_kind}_SOURCE_HASH_FAILED: {path}: {type(exc).__name__}"
            ) from exc
        try:
            stat_after = path.stat()
            snapshot_after = (
                stat_after.st_size,
                getattr(stat_after, "st_mtime_ns", 0),
                getattr(stat_after, "st_ctime_ns", 0),
                getattr(stat_after, "st_dev", 0),
                getattr(stat_after, "st_ino", 0),
            )
        except OSError as exc:
            raise RuntimeError(
                f"SOURCE_CHANGED_DURING_HASH: {path} (stat unavailable)"
            ) from exc
        if snapshot_after != snapshot_before:
            raise RuntimeError(
                f"SOURCE_CHANGED_DURING_HASH: {path}"
            )
        sources.append(ManifestSource(
            source_file_id=f"source_{index:04d}",
            path=str(path.resolve()), role=source.role,
            media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            sha256=source_sha256, size_bytes=size_before,
            page_no=source.page_no,
        ))
    return SourceManifest(
        tenant_id=config.tenant_id, project_id=config.project_id,
        source_type=config.source.type, sources=sources,
        coordinate=config.coordinate, grid=config.grid, wall=config.wall,
        column=config.column, opening=config.opening,
        beam=config.beam,
        modeling_standard=config.modeling_standard,
        level=config.level, revit=config.revit,
        config_sha256=canonical_sha256(config),
    )


def _pending_outputs(
    wall_model: WallModel, output_dir: Path, source_render: Path
) -> tuple[RevitResult, AuditReport]:
    gate_failed = wall_model.gate.status != "pass"
    revit_result = RevitResult(
        tenant_id=wall_model.tenant_id,
        project_id=wall_model.project_id,
        wall_model_sha256=canonical_sha256(wall_model),
        status="failed" if gate_failed else "pending_approval",
        errors=list(wall_model.gate.errors) if gate_failed else [],
    )
    audit_report = AuditReport(
        tenant_id=wall_model.tenant_id,
        project_id=wall_model.project_id,
        wall_model_sha256=canonical_sha256(wall_model),
        revit_result_sha256=canonical_sha256(revit_result),
        status="blocked", independent_sources=False,
        source_render_path=str(source_render.resolve()),
        source_render_sha256=file_sha256(source_render),
        errors=["Revit execution and actual-view readback have not completed"],
    )
    return revit_result, audit_report


def run_wall_pipeline(
    config_or_path: WallPipelineConfig | str | Path,
) -> dict[str, Path]:
    config = (
        config_or_path if isinstance(config_or_path, WallPipelineConfig)
        else load_wall_pipeline_config(config_or_path)
    )
    output_dir = Path(config.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Keep the staging directory inside the destination so publish is on the
    # same volume on Windows (and therefore os.replace remains atomic).
    staging_dir = Path(tempfile.mkdtemp(prefix=".wall-pipeline-", dir=str(output_dir)))
    try:
        manifest = _build_manifest(config)
        write_artifact(staging_dir / ARTIFACT_FILENAMES[0], manifest)
        source_entities = _load_cached_source_entities(config, manifest, output_dir)
        if source_entities is not None:
            logger.info(
                "wall_pipeline.source_entities_cache_hit",
                extra={"project_id": config.project_id, "entity_count": len(source_entities.entities)},
            )
        else:
            source_entities = extract_source_entities(config, manifest, staging_dir)
            _store_cached_source_entities(config, manifest, output_dir, source_entities)
        write_artifact(staging_dir / ARTIFACT_FILENAMES[1], source_entities)
        evidence = build_wall_evidence(config, manifest, source_entities)
        write_artifact(staging_dir / ARTIFACT_FILENAMES[2], evidence)
        wall_model = build_wall_model(config, manifest, source_entities, evidence)
        source_layers = [
            *config.grid.include_layers,
            *config.wall.include_layers,
            *config.column.include_layers,
            *config.column.profile_layers,
            *config.column.label_layers,
            *config.beam.line_layers,
            *config.beam.label_layers,
        ]
        # A required grid is the authoritative floor-plan frame.  Consultant
        # sheets frequently reuse wall/column layers in detached detail boxes;
        # including those layers as frame anchors would make the detail part of
        # both the Revit crop and the acceptance raster.  The fallback keeps
        # gridless drawings backwards compatible.
        source_anchor_layers = (
            list(config.grid.include_layers)
            if config.grid.required and config.grid.include_layers
            else [
                *config.wall.include_layers,
                *config.column.profile_layers,
            ]
        )
        source_anchor_min_length_m = (
            config.grid.min_length_m
            if config.grid.required and config.grid.include_layers
            else None
        )
        wall_model = wall_model.model_copy(update={
            "source_bounds_m": source_geometry_bounds(
                source_entities, evidence, include_layers=source_layers,
                anchor_layers=source_anchor_layers,
                anchor_min_length_m=source_anchor_min_length_m,
            ),
        })
        write_artifact(staging_dir / ARTIFACT_FILENAMES[3], wall_model)
        source_render = render_original_source(
            manifest, source_entities, staging_dir / "source_render.png", evidence,
            include_layers=source_layers,
            anchor_layers=source_anchor_layers,
            anchor_min_length_m=source_anchor_min_length_m,
        )
        revit_result, audit_report = _pending_outputs(
            wall_model, staging_dir, source_render
        )
        write_artifact(staging_dir / ARTIFACT_FILENAMES[4], revit_result)
        write_artifact(staging_dir / ARTIFACT_FILENAMES[5], audit_report)

        # AuditReport stores paths for later independent verification.  Point
        # it at the final published render before moving the files; otherwise
        # a successful run would retain a private staging path that no longer
        # exists after cleanup.
        audit_report = audit_report.model_copy(update={
            "source_render_path": str((output_dir / "source_render.png").resolve()),
        })
        write_artifact(staging_dir / ARTIFACT_FILENAMES[5], audit_report)
        return _publish_run_artifacts(staging_dir, output_dir)
    finally:
        # On failure this removes all private intermediate files.  On success
        # the publisher has moved the six JSON files and source render out.
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def verify_wall_model_lineage(
    wall_model_path: str | Path,
    *,
    lineage_paths: Mapping[str, str | Path] | None = None,
) -> tuple[SourceManifest, SourceEntities, WallEvidence]:
    """Validate the local source → evidence → wall-model contract chain.

    The API workflow performs the same checks against its artifact store.  The
    command-line pipeline has no request context or repository adapter, so it
    must perform the equivalent check from the immutable sibling contracts
    before an operator can approve, dry-run, or audit a model.  ``lineage_paths``
    is used by the workflow when those contracts have been copied into a
    private task directory; normal CLI calls resolve the conventional sibling
    filenames next to ``wall_model.json``.

    This function intentionally verifies references and source bytes instead of
    merely checking the three SHA-256 fields.  A malicious or stale upstream
    artifact could otherwise be replaced together with its downstream hash and
    still appear internally consistent at a human gate.
    """

    model_path = Path(wall_model_path)
    model = read_artifact(model_path, WallModel)
    base = model_path.parent
    paths: dict[str, Path] = {}
    for name in (
        "source_manifest.json",
        "source_entities.json",
        "wall_evidence.json",
    ):
        raw = lineage_paths.get(name) if lineage_paths is not None else None
        path = Path(raw) if raw is not None else base / name
        if not path.is_file():
            raise ValueError(
                f"wall model lineage artifact is missing: {path}"
            )
        paths[name] = path

    try:
        manifest = read_artifact(paths["source_manifest.json"], SourceManifest)
        entities = read_artifact(paths["source_entities.json"], SourceEntities)
        evidence = read_artifact(paths["wall_evidence.json"], WallEvidence)
    except Exception as exc:
        raise ValueError(
            f"wall model lineage artifact validation failed: {exc}"
        ) from exc

    if (model.tenant_id, model.project_id) != (
        manifest.tenant_id, manifest.project_id
    ):
        raise ValueError("wall model lineage tenant/project does not match")
    if (entities.tenant_id, entities.project_id) != (
        model.tenant_id, model.project_id
    ) or (evidence.tenant_id, evidence.project_id) != (
        model.tenant_id, model.project_id
    ):
        raise ValueError("wall model lineage contains a cross-project artifact")
    if entities.manifest_sha256 != canonical_sha256(manifest):
        raise ValueError("source_entities does not reference the source manifest")
    if evidence.source_entities_sha256 != canonical_sha256(entities):
        raise ValueError("wall_evidence does not reference source_entities")
    if model.wall_evidence_sha256 != canonical_sha256(evidence):
        raise ValueError("wall_model does not reference wall_evidence")
    if manifest.source_type not in {"pdf", "dwg", "dxf"}:
        raise ValueError("wall model lineage source type is unsupported")

    manifest_by_source = {item.source_file_id: item for item in manifest.sources}
    if len(manifest_by_source) != len(manifest.sources) or not manifest_by_source:
        raise ValueError("source_manifest contains duplicate or missing source IDs")
    source_ids = set(manifest_by_source)
    if set(entities.source_units) != source_ids:
        raise ValueError("source_entities source_units do not match the manifest")

    # Verify the actual source bytes at every human gate.  The manifest path is
    # operational metadata, while its hash is the engineering identity.
    expected_extensions = (
        {".pdf"} if manifest.source_type == "pdf" else {".dwg", ".dxf"}
    )
    for source in manifest.sources:
        source_path = Path(source.path)
        if source_path.suffix.lower() not in expected_extensions:
            raise ValueError(
                f"manifest source extension does not match source type: {source.path}"
            )
        if manifest.source_type != "pdf" and source.page_no is not None:
            raise ValueError("CAD manifest source cannot specify page_no")
        try:
            verify_source_file_against_manifest(source)
        except Exception as exc:
            raise ValueError(
                f"source file no longer matches its manifest: {source.source_file_id}"
            ) from exc

    def _entity_frame(entity: Any) -> str:
        if entity.page_no is not None:
            return f"{entity.source_file_id}:page:{entity.page_no:04d}"
        return entity.source_file_id

    entity_by_key: dict[tuple[str, str, str], Any] = {}
    for entity in entities.entities:
        if entity.source_file_id not in source_ids:
            raise ValueError(
                "source_entities contains an unknown source_file_id: "
                + entity.source_file_id
            )
        source = manifest_by_source[entity.source_file_id]
        if source.page_no is not None and entity.page_no != source.page_no:
            raise ValueError(
                f"source entity page does not match manifest: {entity.locator}"
            )
        if manifest.source_type == "pdf":
            if entity.page_no is None:
                raise ValueError(
                    f"PDF source entity has no page_no: {entity.locator}"
                )
        elif entity.page_no is not None:
            raise ValueError(
                f"CAD source entity unexpectedly has page_no: {entity.locator}"
            )
        expected_frame = _entity_frame(entity)
        if entity.frame_id and entity.frame_id != expected_frame:
            raise ValueError(
                f"source entity frame does not match its source/page: {entity.locator}"
            )
        key = (entity.source_file_id, entity.entity_id, entity.locator)
        if key in entity_by_key:
            raise ValueError("source_entities contains duplicate entity identities")
        entity_by_key[key] = entity

    evidence_by_id = {item.evidence_id: item for item in evidence.items}
    if len(evidence_by_id) != len(evidence.items):
        raise ValueError("wall_evidence contains duplicate evidence IDs")

    def _parent_locator(locator: str) -> str | None:
        parent, marker, segment = locator.rpartition("/segment:")
        if not marker or not parent or not segment.isdigit():
            return None
        return parent

    def _reference_key(ref: Any, entity: Any | None = None) -> tuple[str, str, str, str]:
        frame_id = ref.frame_id or ""
        if not frame_id and entity is not None:
            frame_id = entity.frame_id or _entity_frame(entity)
        return (ref.source_file_id, ref.entity_id, ref.locator, frame_id)

    def _validate_ref(ref: Any, *, owner: str) -> tuple[str, str, str, str]:
        if ref.source_file_id not in source_ids:
            raise ValueError(f"{owner} references an unknown source_file_id")
        parent = _parent_locator(ref.locator)
        # Most adapter references point directly to an emitted entity (for
        # example an opening label or closed marker).  Wall fragments may
        # instead point to a synthetic ``/segment:N`` locator whose parent is
        # the emitted polyline.  Resolve the exact locator first, then fall
        # back to that parent form; otherwise semantic opening evidence would
        # be rejected even though it is present in source_entities.
        entity = entity_by_key.get(
            (ref.source_file_id, ref.entity_id, ref.locator)
        )
        if entity is None and parent is not None:
            entity = entity_by_key.get(
                (ref.source_file_id, ref.entity_id, parent)
            )
        if entity is None:
            raise ValueError(f"{owner} contains an untraceable source reference")
        expected_frame = entity.frame_id or _entity_frame(entity)
        if ref.page_no != entity.page_no:
            raise ValueError(f"{owner} reference page mismatch: {ref.locator}")
        if ref.frame_id and ref.frame_id != expected_frame:
            raise ValueError(f"{owner} reference frame mismatch: {ref.locator}")
        if ref.layer != entity.layer:
            raise ValueError(f"{owner} reference layer mismatch: {ref.locator}")
        return _reference_key(ref, entity)

    evidence_refs_by_id: dict[str, set[tuple[str, str, str, str]]] = {}
    for item in evidence.items:
        refs = {
            _validate_ref(ref, owner=f"wall_evidence {item.evidence_id}")
            for ref in item.source_refs
        }
        if len(refs) != len(item.source_refs):
            raise ValueError(
                f"wall_evidence contains duplicate source references: {item.evidence_id}"
            )
        evidence_refs_by_id[item.evidence_id] = refs

    wall_ids: set[str] = set()
    for wall in model.walls:
        if wall.wall_id in wall_ids:
            raise ValueError("wall_model contains duplicate wall IDs")
        wall_ids.add(wall.wall_id)
        if len(set(wall.evidence_ids)) != len(wall.evidence_ids):
            raise ValueError(f"wall {wall.wall_id} contains duplicate evidence IDs")
        if any(evidence_id not in evidence_by_id for evidence_id in wall.evidence_ids):
            raise ValueError(f"wall {wall.wall_id} references missing wall evidence")
        refs = {
            _validate_ref(ref, owner=f"wall {wall.wall_id}")
            for ref in wall.source_refs
        }
        if len(refs) != len(wall.source_refs):
            raise ValueError(
                f"wall {wall.wall_id} contains duplicate source references"
            )
        expected_refs = set().union(
            *(evidence_refs_by_id[evidence_id] for evidence_id in wall.evidence_ids)
        ) if wall.evidence_ids else set()
        if refs != expected_refs:
            raise ValueError(
                f"wall {wall.wall_id} source references do not match its evidence IDs"
            )

    # Opening annotations are not wall-evidence items: a JD/HOLE label and
    # its vector marker are semantic evidence that must remain traceable even
    # when the host wall is split around the opening.  Validate their source
    # references directly instead of allowing an untraceable annotation to
    # enter the approval or Revit compilation gates.
    opening_ids: set[str] = set()
    for opening in model.openings:
        if opening.opening_id in opening_ids:
            raise ValueError("wall_model contains duplicate opening IDs")
        opening_ids.add(opening.opening_id)
        if any(wall_id not in wall_ids for wall_id in opening.host_wall_ids):
            raise ValueError(
                f"opening {opening.opening_id} references an invalid wall host"
            )
        if opening.status == "matched" and not opening.host_wall_ids:
            raise ValueError(
                f"matched opening {opening.opening_id} has no wall host"
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
            raise ValueError("approved column profile model no longer matches its hash")
        profile_manifest_path = profile_model_path.with_name("source_manifest.json")
        profile_entities_path = profile_model_path.with_name("source_entities.json")
        if not profile_manifest_path.is_file() or not profile_entities_path.is_file():
            raise ValueError("approved column profile lineage artifacts are missing")
        profile_manifest = read_artifact(profile_manifest_path, SourceManifest)
        profile_entities = read_artifact(profile_entities_path, SourceEntities)
        try:
            raw_profile_manifest = json.loads(
                profile_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(
                "approved column profile source manifest cannot be read"
            ) from exc
        # The approved reference predates optional training fields added to
        # SourceManifest.  Hash its immutable on-disk contract rather than a
        # parsed model with newly introduced defaults injected.
        if profile_entities.manifest_sha256 != canonical_sha256(
            raw_profile_manifest
        ):
            raise ValueError("approved column profile source lineage is invalid")
        if approved_source_hash not in {
            source.sha256 for source in profile_manifest.sources
        }:
            raise ValueError("approved column profile source hash is invalid")
        for source in profile_manifest.sources:
            verify_source_file_against_manifest(source)
        approved_entities_by_key = {
            (entity.entity_id, entity.locator): entity
            for entity in profile_entities.entities
        }

    for opening in model.openings:
        seen_opening_refs: set[tuple[str, str, str, str]] = set()
        for ref in opening.source_refs:
            if ref.source_file_id in source_ids:
                key = _validate_ref(
                    ref, owner=f"opening {opening.opening_id}"
                )
            elif (
                approved_entities_by_key
                and ref.source_file_id == approved_prefix
                and ref.frame_id == approved_prefix
                and ref.locator.startswith(approved_locator_prefix)
            ):
                original_locator = ref.locator[len(approved_locator_prefix):]
                entity = approved_entities_by_key.get(
                    (ref.entity_id, original_locator)
                )
                if entity is None:
                    parent = _parent_locator(original_locator)
                    if parent is not None:
                        entity = approved_entities_by_key.get(
                            (ref.entity_id, parent)
                        )
                if entity is None:
                    raise ValueError(
                        f"opening {opening.opening_id} has an untraceable approved profile"
                    )
                if ref.page_no != entity.page_no or ref.layer != entity.layer:
                    raise ValueError(
                        f"opening {opening.opening_id} approved profile reference mismatch"
                    )
                key = (
                    ref.source_file_id,
                    ref.entity_id,
                    ref.locator,
                    ref.frame_id or "",
                )
            else:
                raise ValueError(
                    f"opening {opening.opening_id} references an unknown source"
                )
            if key in seen_opening_refs:
                raise ValueError(
                    f"opening {opening.opening_id} contains duplicate source references"
                )
            seen_opening_refs.add(key)

    column_ids: set[str] = set()
    for column in model.columns:
        if column.column_id in column_ids:
            raise ValueError("wall_model contains duplicate column IDs")
        column_ids.add(column.column_id)
        if not column.source_refs:
            raise ValueError(f"column {column.column_id} has no source references")
        seen_column_refs: set[tuple[str, str, str, str]] = set()
        for ref in column.source_refs:
            if ref.source_file_id in source_ids:
                key = _validate_ref(ref, owner=f"column {column.column_id}")
            elif (
                approved_entities_by_key
                and ref.source_file_id == approved_prefix
                and ref.frame_id == approved_prefix
                and ref.locator.startswith(approved_locator_prefix)
            ):
                original_locator = ref.locator[len(approved_locator_prefix):]
                entity = approved_entities_by_key.get(
                    (ref.entity_id, original_locator)
                )
                if entity is None:
                    parent = _parent_locator(original_locator)
                    if parent is not None:
                        entity = approved_entities_by_key.get((ref.entity_id, parent))
                if entity is None:
                    raise ValueError(
                        f"column {column.column_id} has an untraceable approved profile"
                    )
                if ref.page_no != entity.page_no or ref.layer != entity.layer:
                    raise ValueError(
                        f"column {column.column_id} approved profile reference mismatch"
                    )
                key = (
                    ref.source_file_id, ref.entity_id, ref.locator,
                    ref.frame_id or "",
                )
            else:
                raise ValueError(
                    f"column {column.column_id} references an unknown source"
                )
            if key in seen_column_refs:
                raise ValueError(
                    f"column {column.column_id} contains duplicate source references"
                )
            seen_column_refs.add(key)

    if model.transform_chain != evidence.transform_chain:
        raise ValueError("wall_model transform_chain does not match wall_evidence")
    if model.coordinate_origin != manifest.coordinate.origin:
        raise ValueError("wall_model coordinate origin does not match manifest")
    if model.level != manifest.level or model.revit != manifest.revit:
        raise ValueError("wall_model level/Revit settings do not match manifest")
    if model.modeling_standard != manifest.modeling_standard:
        raise ValueError("wall_model modeling standard does not match manifest")

    # Reuse the geometry engine's authoritative frame registry.  It checks
    # actual PDF page numbers and rejects fabricated frame-offset keys, which
    # is stricter than prefix matching and keeps CLI/API decisions aligned.
    from backend.engines.wall_pipeline.geometry import _validate_frame_registry

    frame_config = WallPipelineConfig.model_validate({
        "tenant_id": manifest.tenant_id,
        "project_id": manifest.project_id,
        "source": {
            "type": manifest.source_type,
            "files": [
                {
                    "path": item.path,
                    "role": item.role,
                    **({"page_no": item.page_no} if item.page_no is not None else {}),
                }
                for item in manifest.sources
            ],
        },
        "coordinate": manifest.coordinate.model_dump(mode="json"),
        "grid": manifest.grid.model_dump(mode="json"),
        "wall": manifest.wall.model_dump(mode="json"),
        "column": manifest.column.model_dump(mode="json"),
        "opening": manifest.opening.model_dump(mode="json"),
        "modeling_standard": manifest.modeling_standard.model_dump(mode="json"),
        "level": manifest.level.model_dump(mode="json"),
        "revit": manifest.revit.model_dump(mode="json"),
    })
    known_frames, frame_owner = _validate_frame_registry(
        frame_config, manifest, entities
    )
    axis_ids: set[str] = set()
    for axis in model.grid:
        if axis.axis_id in axis_ids:
            raise ValueError("wall_model contains duplicate grid axis IDs")
        axis_ids.add(axis.axis_id)
        if axis.source_file_id and axis.source_file_id not in source_ids:
            raise ValueError("wall_model grid references an unknown source_file_id")
        if axis.frame_id:
            if axis.frame_id not in known_frames:
                raise ValueError("wall_model grid references an unknown frame_id")
            owner = frame_owner.get(axis.frame_id)
            if axis.source_file_id and owner is not None and owner != axis.source_file_id:
                raise ValueError("wall_model grid frame and source_file_id disagree")

    junction_ids: set[str] = set()
    for junction in model.junctions:
        if junction.junction_id in junction_ids:
            raise ValueError("wall_model contains duplicate junction IDs")
        junction_ids.add(junction.junction_id)
        if len(set(junction.wall_ids)) != len(junction.wall_ids) or any(
            wall_id not in wall_ids for wall_id in junction.wall_ids
        ):
            raise ValueError(
                f"junction {junction.junction_id} references invalid wall IDs"
            )
    return manifest, entities, evidence


def approve_wall_model(
    path: str | Path, *, actor_id: str, reason: str,
    approved: bool = True,
    lineage_paths: Mapping[str, str | Path] | None = None,
) -> WallModel:
    wall_model_path = Path(path)
    wall_model = read_artifact(wall_model_path, WallModel)
    if wall_model.review_status != "pending":
        raise ValueError(
            f"wall model is already {wall_model.review_status}; a pending model is required"
        )
    verify_wall_model_lineage(wall_model_path, lineage_paths=lineage_paths)
    pending_result_path = wall_model_path.parent / "revit_result.json"
    if not pending_result_path.is_file():
        raise ValueError("initial Revit result is missing beside WallModel")
    pending_result = read_artifact(pending_result_path, RevitResult)
    if (
        pending_result.tenant_id != wall_model.tenant_id
        or pending_result.project_id != wall_model.project_id
    ):
        raise ValueError("initial Revit result tenant/project does not match WallModel")
    if pending_result.status != "pending_approval" or pending_result.approval is not None:
        raise ValueError("initial Revit result is not pending model approval")
    if pending_result.wall_model_sha256 != canonical_sha256(wall_model):
        raise ValueError("initial Revit result does not reference the pending WallModel")
    if approved and wall_model.gate.status != "pass":
        raise ValueError("cannot approve wall model while review gate is failed")
    status = "approved" if approved else "rejected"
    updated = wall_model.model_copy(update={
        "review_status": status,
        "approval": ApprovalRecord(status=status, actor_id=actor_id, reason=reason),
    })
    updated = WallModel.model_validate(updated.model_dump(mode="json"))
    write_artifact(wall_model_path, updated)
    # Approval changes the engineering artifact identity.  Reset downstream
    # state instead of leaving a result bound to the pre-approval hash.
    revit_path = wall_model_path.parent / "revit_result.json"
    revit_result = RevitResult(
        tenant_id=updated.tenant_id, project_id=updated.project_id,
        wall_model_sha256=canonical_sha256(updated),
        status="ready_for_dry_run" if approved else "failed",
        errors=[] if approved else ["wall model was rejected"],
    )
    write_artifact(revit_path, revit_result)
    return updated


def dry_run_wall_model(
    path: str | Path,
    *,
    lineage_paths: Mapping[str, str | Path] | None = None,
) -> RevitResult:
    """Compile and validate Revit commands without writing Revit.

    A live write must be tied to the exact RVT bytes reviewed by a human.  The
    deterministic preflight therefore captures the target snapshot hash here;
    a missing target is an explicit failure rather than an approval of an
    unbound path.
    """

    from workers.pyrevit.wall_model_contract import normalize_wall_model_payload

    wall_model_path = Path(path)
    wall_model = read_artifact(wall_model_path, WallModel)
    verify_wall_model_lineage(wall_model_path, lineage_paths=lineage_paths)
    if wall_model.review_status != "approved":
        raise ValueError("wall model must be approved before Revit dry-run")
    result_path = wall_model_path.parent / "revit_result.json"
    if not result_path.is_file():
        raise ValueError("Revit result is missing beside WallModel")
    current_result = read_artifact(result_path, RevitResult)
    if (
        current_result.tenant_id != wall_model.tenant_id
        or current_result.project_id != wall_model.project_id
    ):
        raise ValueError("Revit result tenant/project does not match WallModel")
    if current_result.wall_model_sha256 != canonical_sha256(wall_model):
        raise ValueError("Revit result does not reference the approved WallModel")
    if current_result.status not in {"ready_for_dry_run", "dry_run_passed"}:
        raise ValueError(
            "Revit dry-run requires a ready_for_dry_run or dry_run_passed result"
        )
    if current_result.approval is not None:
        raise ValueError("Revit dry-run cannot overwrite an approved write result")
    compiled = normalize_wall_model_payload(wall_model.model_dump(mode="json"))
    walls = [item for item in compiled["model_elements"] if item.get("type") == "Wall"]
    columns = [item for item in compiled["model_elements"] if item.get("type") == "Column"]
    beams = [item for item in compiled["model_elements"] if item.get("type") == "Beam"]
    if len(walls) != len(wall_model.walls):
        raise ValueError("dry-run wall count differs from wall model")
    if len(columns) != len(wall_model.columns):
        raise ValueError("dry-run column count differs from wall model")
    if len(beams) != len(wall_model.beams):
        raise ValueError("dry-run beam count differs from wall model")
    # Compile the exact typed program before asking a human to approve a write.
    # This catches malformed IronPython templates and contract drift while the
    # failure is still deterministic and does not require a Revit process.
    from workers.revit_bridge.wall_compiler import (
        WALL_COMPILER_VERSION,
        compiled_plan_sha256,
        compile_wall_model_script,
    )

    script = compile_wall_model_script(
        compiled,
        actual_prefix=wall_model_path.parent / "dry_run_actual_view",
        expected_wall_count=len(walls),
        expected_column_count=len(columns),
        expected_beam_count=len(beams),
    )
    try:
        compile(script, "<buildmate-wall-model-dry-run>", "exec")
    except SyntaxError as exc:
        raise ValueError("compiled Revit program is invalid: %s" % exc.msg) from exc
    required_markers = (
        "BUILDMATE_COORDINATE_APPLIED",
        "BUILDMATE_DISPLAY_UNITS_APPLIED",
        "BUILDMATE_WALL_TOPOLOGY_APPLIED",
        "BUILDMATE_WALL_TOPOLOGY_VERIFIED",
        "BUILDMATE_WALL_TOPOLOGY_REFS",
        "BUILDMATE_ELEMENT_PROPERTIES_APPLIED",
        "BUILDMATE_SCHEDULE_DATA_APPLIED",
        "BUILDMATE_OPENING_SEMANTICS_APPLIED",
        "BUILDMATE_PERSISTED_BEFORE_RENDER",
        "BUILDMATE_AUDIT_GRID_BUBBLES_HIDDEN",
        "BUILDMATE_DELIVERY_GRID_BUBBLES_RESTORED",
        "BUILDMATE_WALL_MODEL_APPLIED",
    )
    if columns:
        required_markers = (*required_markers, "BUILDMATE_COLUMN_MODEL_APPLIED")
    if beams:
        required_markers = (*required_markers, "BUILDMATE_BEAM_MODEL_APPLIED")
    missing_markers = [marker for marker in required_markers if marker not in script]
    if missing_markers:
        raise ValueError(
            "compiled Revit program is missing delivery markers: "
            + ", ".join(missing_markers)
        )
    target_path = Path(wall_model.revit.target_model_path or "").expanduser()
    if not target_path.is_file():
        raise ValueError("Revit dry-run target RVT does not exist: %s" % target_path)
    target_hash = file_sha256(target_path)
    result = RevitResult(
        tenant_id=wall_model.tenant_id, project_id=wall_model.project_id,
        wall_model_sha256=canonical_sha256(wall_model),
        status="dry_run_passed",
        readback={
            "dry_run": True,
            "compiler": WALL_COMPILER_VERSION,
            "compiled_plan_sha256": compiled_plan_sha256(compiled),
            "compiled_script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
            "wall_count": len(walls),
            "column_count": len(columns),
            "beam_count": len(beams),
            "grid_count": len(wall_model.grid),
            "opening_count": len(wall_model.openings),
            "matched_opening_count": sum(
                opening.status == "matched" for opening in wall_model.openings
            ),
            "opening_host_count": len({
                host_id
                for opening in wall_model.openings
                if opening.status == "matched"
                for host_id in opening.host_wall_ids
            }),
            "target_model_path": str(target_path.resolve()),
            "source_model_sha256": target_hash,
            "source_model_size": target_path.stat().st_size,
            "floor_code": wall_model.revit.floor_code,
            "coordinate_origin": wall_model.coordinate_origin,
            "topology_junction_count": len(wall_model.junctions),
        },
    )
    write_artifact(wall_model_path.parent / "revit_result.json", result)
    return result


def approve_revit_write(
    wall_model_path: str | Path, *, actor_id: str, reason: str,
    lineage_paths: Mapping[str, str | Path] | None = None,
) -> RevitResult:
    model_path = Path(wall_model_path)
    wall_model = read_artifact(model_path, WallModel)
    verify_wall_model_lineage(model_path, lineage_paths=lineage_paths)
    result_path = model_path.parent / "revit_result.json"
    result = read_artifact(result_path, RevitResult)
    if wall_model.review_status != "approved" or wall_model.approval is None:
        raise ValueError("WallModel must have a human approval before Revit write approval")
    if (
        result.tenant_id != wall_model.tenant_id
        or result.project_id != wall_model.project_id
    ):
        raise ValueError("dry-run result tenant/project does not match WallModel")
    if result.wall_model_sha256 != canonical_sha256(wall_model):
        raise ValueError("dry-run result does not reference this wall model")
    if result.status != "dry_run_passed":
        raise ValueError("Revit write can only be approved after a passed dry-run")
    if result.approval is not None:
        raise ValueError("Revit write result is already approved")
    readback = result.readback or {}
    target = Path(wall_model.revit.target_model_path or "").expanduser()
    if not target.is_file():
        raise ValueError("Revit write approval target RVT does not exist")
    try:
        normalized_target = normalize_target_path(str(target))
    except ValueError as exc:
        raise ValueError("Revit write approval target RVT path is invalid") from exc
    try:
        readback_target = normalize_target_path(str(readback.get("target_model_path") or ""))
    except ValueError as exc:
        raise ValueError("dry-run result target RVT path is invalid") from exc
    if readback_target != normalized_target:
        raise ValueError("dry-run result target RVT does not match WallModel")
    snapshot_hash = str(readback.get("source_model_sha256") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot_hash):
        raise ValueError("dry-run result does not contain a valid target RVT snapshot")
    if file_sha256(target) != snapshot_hash:
        raise ValueError("target RVT changed after dry-run; repeat the dry-run")

    # Recompute the deterministic compiler identity at the approval boundary;
    # a stale or hand-authored readback must not be promoted to a write token.
    from workers.pyrevit.wall_model_contract import normalize_wall_model_payload
    from workers.revit_bridge.wall_compiler import (
        WALL_COMPILER_VERSION,
        compiled_plan_sha256,
    )

    compiled = normalize_wall_model_payload(wall_model.model_dump(mode="json"))
    if (
        readback.get("compiler") != WALL_COMPILER_VERSION
        or readback.get("compiled_plan_sha256") != compiled_plan_sha256(compiled)
    ):
        raise ValueError("dry-run result was produced by a different WallModel compiler")
    if readback.get("wall_count") != len(wall_model.walls):
        raise ValueError("dry-run wall count does not match WallModel")
    if readback.get("column_count", 0) != len(wall_model.columns):
        raise ValueError("dry-run column count does not match WallModel")
    if readback.get("beam_count", 0) != len(wall_model.beams):
        raise ValueError("dry-run beam count does not match WallModel")
    if readback.get("grid_count") != len(wall_model.grid):
        raise ValueError("dry-run grid count does not match WallModel")
    updated = result.model_copy(update={
        "status": "write_approved",
        "approval": ApprovalRecord(
            status="approved", actor_id=actor_id, reason=reason
        ),
    })
    updated = RevitResult.model_validate(updated.model_dump(mode="json"))
    write_artifact(result_path, updated)
    return updated


def finalize_audit(
    wall_model_path: str | Path,
    revit_result_path: str | Path,
    *,
    source_render_path: str | Path | None = None,
    overlay_path: str | Path | None = None,
    minimum_edge_iou: float = REQUIRED_SIMILARITY,
    minimum_source_coverage: float = REQUIRED_SIMILARITY,
    minimum_revit_precision: float = REQUIRED_SIMILARITY,
    lineage_paths: Mapping[str, str | Path] | None = None,
) -> AuditReport:
    model_path = Path(wall_model_path)
    wall_model = read_artifact(model_path, WallModel)
    verify_wall_model_lineage(model_path, lineage_paths=lineage_paths)
    result_path = Path(revit_result_path)
    revit_result = read_artifact(result_path, RevitResult)
    source_render = Path(source_render_path) if source_render_path else model_path.parent / "source_render.png"
    overlay = Path(overlay_path) if overlay_path else model_path.parent / "audit_overlay.png"
    report = create_independent_audit(
        wall_model, revit_result, source_render, overlay,
        minimum_edge_iou=minimum_edge_iou,
        minimum_source_coverage=minimum_source_coverage,
        minimum_revit_precision=minimum_revit_precision,
    )
    write_artifact(model_path.parent / "audit_report.json", report)
    return report


__all__ = [
    "approve_revit_write", "approve_wall_model", "dry_run_wall_model",
    "finalize_audit", "load_wall_pipeline_config", "run_wall_pipeline",
    "verify_wall_model_lineage",
]
