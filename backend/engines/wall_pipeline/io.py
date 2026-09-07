"""Canonical hashing and atomic JSON I/O for pipeline artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def _path_independent_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove host/workspace paths from deterministic engineering identity.

    A v2 worker stages the same upload below a task-specific directory, while
    the CLI commonly resolves a source relative to a config file.  Those
    operational paths must remain in the artifact for replay/audit, but they
    cannot change the hash that links ``SourceManifest`` to its downstream
    contracts.  Only the two input/config contracts are normalized here;
    target RVT paths and generated artifact paths intentionally remain part of
    their respective delivery identities.  Source/evidence diagnostic paths
    are normalized as well because they describe temporary adapter workspaces,
    not engineering content.
    """

    schema_version = payload.get("schema_version")

    # Adapter diagnostics may contain paths to temporary conversion/output
    # files (most notably ODA's task-local DXF).  Those paths are useful when
    # inspecting the raw artifact, but they are operational metadata rather
    # than engineering evidence.  If they enter the canonical hash, the
    # same DWG parsed in two worker directories receives a different
    # ``SourceEntities``/``WallEvidence`` lineage and cannot be replayed.
    # Keep the diagnostic shape while replacing only path-like values with a
    # stable source-scoped URI.  Do this only for the source/evidence
    # contracts; delivery paths in RevitResult/AuditReport remain explicitly
    # bound to their host and are checked separately at their gates.
    if schema_version in {
        "buildmate.source-entities/1.0",
        "buildmate.wall-evidence/1.0",
    }:
        diagnostics = payload.get("diagnostics")
        if isinstance(diagnostics, list):
            path_keys = {
                "path", "source_path", "converted_path", "output_path",
                "temporary_path", "working_path",
            }
            for index, diagnostic in enumerate(diagnostics, start=1):
                if not isinstance(diagnostic, dict):
                    continue
                source_id = str(
                    diagnostic.get("source_file_id") or f"diagnostic_{index:04d}"
                )
                for key in path_keys:
                    if isinstance(diagnostic.get(key), str):
                        diagnostic[key] = (
                            f"diagnostic://{source_id}/{key}"
                        )
    if schema_version == "buildmate.wall-pipeline-config/1.0":
        source = payload.get("source")
        if isinstance(source, dict):
            files = source.get("files")
            if isinstance(files, list):
                for index, item in enumerate(files, start=1):
                    if isinstance(item, dict):
                        # Preserve the extension/role/ordering, but not the
                        # machine-specific absolute path.
                        item["path"] = f"source://{index:04d}"
        # Output location is never an engineering input.
        payload["output_dir"] = "output://"
        revit = payload.get("revit")
        if isinstance(revit, dict) and revit.get("target_model_path"):
            # The target is bound separately by the approval capability; it is
            # not a geometric input and must not make an otherwise identical
            # model differ merely because a worker uses another mount point.
            revit["target_model_path"] = "target://rvt"
        column = payload.get("column")
        if isinstance(column, dict) and column.get("approved_profile_model_path"):
            column["approved_profile_model_path"] = "training://approved-column-model"
    elif schema_version == "buildmate.source-manifest/1.0":
        sources = payload.get("sources")
        if isinstance(sources, list):
            for item in sources:
                if isinstance(item, dict):
                    source_id = str(item.get("source_file_id") or "")
                    item["path"] = f"source://{source_id}"
        revit = payload.get("revit")
        if isinstance(revit, dict) and revit.get("target_model_path"):
            revit["target_model_path"] = "target://rvt"
        column = payload.get("column")
        if isinstance(column, dict) and column.get("approved_profile_model_path"):
            column["approved_profile_model_path"] = "training://approved-column-model"
    elif schema_version == "buildmate.wall-model/1.0":
        revit = payload.get("revit")
        if isinstance(revit, dict) and revit.get("target_model_path"):
            # Approval tokens and the Bridge request carry the normalized
            # target path explicitly.  Keep the content hash focused on the
            # approved geometry/configuration rather than host mount syntax.
            revit["target_model_path"] = "target://rvt"
    return payload


def canonical_payload(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    else:
        payload = deepcopy(value)
    # Creation time is operational metadata, not engineering identity.  The
    # same source/config/pipeline must retain the same content hash on rerun.
    payload.pop("created_at", None)
    return _path_independent_identity(payload)


def canonical_sha256(value: BaseModel | dict[str, Any]) -> str:
    encoded = json.dumps(
        canonical_payload(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_artifact(path: Path, value: BaseModel) -> None:
    """Write a validated artifact without exposing a partially written file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(
                value.model_dump(mode="json"), ensure_ascii=False, indent=2
            ))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_artifact(path: Path, model: type[T]) -> T:
    return model.model_validate_json(path.read_text(encoding="utf-8"))
