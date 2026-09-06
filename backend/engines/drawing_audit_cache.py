"""Strict, evidence-only cache helpers for drawing audits.

The cache deliberately excludes modelling authorization and execution state.
It can reuse expensive CV/YOLO evidence, but a caller must always recompute
Quality Gates and decide separately whether Revit may be changed.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import tempfile
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
_ERROR_STATUSES = frozenset({
    "CANCELLED", "CRASHED", "ERROR", "EXCEPTION", "FAILED", "TIMEOUT",
})
_FORBIDDEN_KEYS = frozenset({
    "allowmodeling",
    "approvedformodeling",
    "buildauthorized",
    "formalmodel",
    "gateapproval",
    "gateapproved",
    "gateauthorization",
    "gateauthorized",
    "gatedecision",
    "modeljson",
    "modelpath",
    "revitauthorized",
    "revitexecution",
    "revitresult",
    "revittriggered",
    "triggerrevit",
})


def file_sha256(path: str | Path) -> str:
    """Return the SHA-256 of a regular file."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_environment(
    names: Sequence[str], environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Capture only explicitly named settings that affect audit decisions."""
    source = os.environ if environ is None else environ
    return {name: source.get(name) for name in sorted(set(names))}


def collect_dependency_versions(names: Sequence[str]) -> dict[str, str]:
    """Collect installed versions, recording absence as part of the identity."""
    versions = {}
    for name in sorted(set(names)):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "MISSING"
    return versions


def _normalized(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cache identity and evidence require finite numbers")
        return value
    if isinstance(value, Path):
        return str(value.expanduser().resolve(strict=False))
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("cache mappings require string keys")
            normalized[key] = _normalized(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_normalized(item) for item in value]
        return sorted(items, key=_canonical_json)
    raise TypeError(f"unsupported cache value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _normalized(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _fingerprinted_files(paths: Sequence[str | Path]) -> list[dict[str, Any]]:
    entries = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise FileNotFoundError(path)
        entries.append({
            "path": str(path),
            "sha256": file_sha256(path),
            "size": path.stat().st_size,
        })
    return sorted(entries, key=lambda item: item["path"])


def build_audit_cache_identity(
    source_path: str | Path,
    *,
    code_paths: Sequence[str | Path],
    yolo_model_path: str | Path | None,
    cv_params: Mapping[str, Any],
    yolo_params: Mapping[str, Any],
    dependency_versions: Mapping[str, str],
    environment_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a complete, deterministic identity for reusable audit evidence."""
    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise FileNotFoundError(source)
    if not code_paths:
        raise ValueError("at least one relevant code path is required")
    if not dependency_versions:
        raise ValueError("relevant dependency versions are required")

    code_files = _fingerprinted_files(code_paths)
    code_fingerprint = hashlib.sha256(
        _canonical_json(code_files).encode("utf-8")
    ).hexdigest()

    yolo_model = None
    if yolo_model_path is not None:
        model = Path(yolo_model_path).expanduser().resolve(strict=True)
        if not model.is_file():
            raise FileNotFoundError(model)
        yolo_model = {
            "path": str(model),
            "sha256": file_sha256(model),
            "size": model.stat().st_size,
        }

    # Environment values are hashed so cache records cannot disclose secrets.
    environment = {
        key: hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
        for key, value in sorted(environment_config.items())
    }
    identity = {
        "schema": "drawing-audit-evidence-v1",
        "source": {
            "path": str(source),
            "sha256": file_sha256(source),
            "size": source.stat().st_size,
        },
        "code": {"fingerprint": code_fingerprint, "files": code_files},
        "yolo_model": yolo_model,
        "parameters": {
            "cv": _normalized(cv_params),
            "yolo": _normalized(yolo_params),
        },
        "dependencies": _normalized(dependency_versions),
        "environment": environment,
        "runtime": {"python": platform.python_version()},
    }
    return _normalized(identity)


def audit_cache_key(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _normalized_key(key: str) -> str:
    return "".join(character for character in key.lower() if character.isalnum())


def _is_forbidden_path(value: str) -> bool:
    path_text = value.replace("\\", "/").lower()
    parts = [part for part in path_text.split("/") if part]
    return (
        path_text == "model.json"
        or path_text.endswith("/model.json")
        or any("revit" in part for part in parts)
    )


def _validate_evidence(value: Any, parent_key: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("audit evidence mappings require string keys")
            normalized_key = _normalized_key(key)
            if normalized_key in _FORBIDDEN_KEYS or normalized_key.startswith("revit"):
                raise ValueError(f"execution or authorization field is not cacheable: {key}")
            if (
                normalized_key.endswith("status")
                and isinstance(item, str)
                and item.upper() in _ERROR_STATUSES
            ):
                raise RuntimeError("ERROR audit evidence is not cacheable")
            _validate_evidence(item, key)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _validate_evidence(item, parent_key)
        return
    if isinstance(value, str) and _is_forbidden_path(value):
        raise ValueError("model.json and Revit execution artifacts are not cacheable")
    _normalized(value)


def _artifact_entries(artifacts: Mapping[str, str | Path]) -> list[dict[str, Any]]:
    if not artifacts:
        raise ValueError("at least one audit artifact is required")
    entries = []
    for name, raw_path in artifacts.items():
        if not isinstance(name, str) or not name:
            raise ValueError("artifact names must be non-empty strings")
        normalized_name = _normalized_key(name)
        if normalized_name == "modeljson" or normalized_name.startswith("revit"):
            raise ValueError("model.json and Revit execution artifacts are not cacheable")
        path = Path(raw_path).expanduser().resolve(strict=True)
        if _is_forbidden_path(str(path)):
            raise ValueError("model.json and Revit execution artifacts are not cacheable")
        if not path.is_file():
            raise FileNotFoundError(path)
        entries.append({
            "name": name,
            "path": str(path),
            "sha256": file_sha256(path),
            "size": path.stat().st_size,
        })
    return sorted(entries, key=lambda item: item["name"])


def _record_digest(record: Mapping[str, Any]) -> str:
    content = {key: value for key, value in record.items() if key != "record_sha256"}
    return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(
                value, stream, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _cache_path(cache_dir: str | Path, identity: Mapping[str, Any]) -> Path:
    return Path(cache_dir) / f"{audit_cache_key(identity)}.json"


def _write_invalid_marker(
    cache_dir: str | Path, identity: Mapping[str, Any], reason: str,
) -> None:
    record = {
        "schema_version": SCHEMA_VERSION,
        "cache_key": audit_cache_key(identity),
        "cache_state": "INVALID",
        "identity": _normalized(identity),
        "reason": reason,
    }
    record["record_sha256"] = _record_digest(record)
    _atomic_write_json(_cache_path(cache_dir, identity), record)


def write_audit_cache(
    cache_dir: str | Path,
    identity: Mapping[str, Any],
    evidence: Mapping[str, Any],
    artifacts: Mapping[str, str | Path],
) -> Path | None:
    """Atomically store evidence, or invalidate the key for an ERROR result."""
    try:
        _validate_evidence(evidence)
    except RuntimeError:
        _write_invalid_marker(cache_dir, identity, "ERROR_RESULT")
        return None

    record = {
        "schema_version": SCHEMA_VERSION,
        "cache_key": audit_cache_key(identity),
        "cache_state": "READY",
        "identity": _normalized(identity),
        "evidence": _normalized(evidence),
        "artifacts": _artifact_entries(artifacts),
    }
    record["record_sha256"] = _record_digest(record)
    path = _cache_path(cache_dir, identity)
    _atomic_write_json(path, record)
    return path


def _file_entry_matches(entry: Mapping[str, Any]) -> bool:
    try:
        path = Path(entry["path"])
        return (
            path.is_file()
            and path.stat().st_size == entry["size"]
            and file_sha256(path) == entry["sha256"]
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _identity_inputs_match(identity: Mapping[str, Any]) -> bool:
    try:
        if not _file_entry_matches(identity["source"]):
            return False
        code = identity["code"]
        files = code["files"]
        if not files or not all(_file_entry_matches(entry) for entry in files):
            return False
        expected_fingerprint = hashlib.sha256(
            _canonical_json(files).encode("utf-8")
        ).hexdigest()
        if expected_fingerprint != code["fingerprint"]:
            return False
        model = identity.get("yolo_model")
        return model is None or _file_entry_matches(model)
    except (KeyError, TypeError, ValueError):
        return False


def read_audit_cache(
    cache_dir: str | Path, identity: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return validated evidence and artifact paths; otherwise fail closed."""
    path = _cache_path(cache_dir, identity)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return None
        if record.get("schema_version") != SCHEMA_VERSION:
            return None
        if record.get("cache_state") != "READY":
            return None
        if record.get("cache_key") != audit_cache_key(identity):
            return None
        if _canonical_json(record.get("identity")) != _canonical_json(identity):
            return None
        if record.get("record_sha256") != _record_digest(record):
            return None
        _validate_evidence(record.get("evidence"))
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            return None
        names = [entry.get("name") for entry in artifacts if isinstance(entry, dict)]
        if len(names) != len(artifacts) or len(names) != len(set(names)):
            return None
        if any(
            not isinstance(name, str)
            or _normalized_key(name) == "modeljson"
            or _normalized_key(name).startswith("revit")
            or _is_forbidden_path(str(entry.get("path", "")))
            for name, entry in zip(names, artifacts)
        ):
            return None
        if not all(_file_entry_matches(entry) for entry in artifacts):
            return None
        if not _identity_inputs_match(identity):
            return None
        return {
            "cache_key": record["cache_key"],
            "evidence": record["evidence"],
            "artifacts": {entry["name"]: entry["path"] for entry in artifacts},
        }
    except (json.JSONDecodeError, OSError, RuntimeError, TypeError, ValueError):
        return None
