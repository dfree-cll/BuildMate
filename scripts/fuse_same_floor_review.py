"""Create a review-only same-floor fusion artifact from two cleaned drawings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.engines.same_floor_fusion import fuse_same_floor_review_ir


def _load_object(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"cannot hash file {path}: {exc}") from exc
    return digest.hexdigest()


def _verify_declared_source(ir: dict, discipline: str) -> None:
    source = ir.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"{discipline} source identity is missing")
    path = Path(str(source.get("path") or ""))
    declared_sha256 = str(source.get("sha256") or "").lower()
    actual_sha256 = _file_sha256(path)
    if actual_sha256 != declared_sha256:
        raise ValueError(
            f"{discipline} source SHA256 mismatch: {path}")


def _fresh_root(path: Path) -> Path:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name.lower().startswith("fresh_"):
            return candidate
    raise ValueError(f"input is not inside a fresh run directory: {path}")


def _validate_output_scope(output: Path, input_paths: list[Path]) -> None:
    roots = {_fresh_root(path) for path in input_paths}
    if len(roots) != 1:
        raise ValueError("all inputs must belong to the same fresh run directory")
    fresh_root = next(iter(roots))
    output_resolved = output.resolve()
    try:
        relative = output_resolved.relative_to(fresh_root)
    except ValueError as exc:
        raise ValueError("output must stay inside the same fresh run") from exc
    if (len(relative.parts) < 2 or
            not relative.parts[0].lower().startswith("fusion")):
        raise ValueError("output must be inside a fusion review directory")
    lowered_name = output_resolved.name.lower()
    if output_resolved.suffix.lower() != ".json" or "review" not in lowered_name:
        raise ValueError("output must be an explicitly named review JSON")
    if lowered_name in {"model.json", "model_status.json"}:
        raise ValueError("fusion review cannot overwrite a modeling JSON")


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fuse architecture and structure review IRs for one floor")
    parser.add_argument("--architecture-ir", required=True, type=Path)
    parser.add_argument("--architecture-grid", required=True, type=Path)
    parser.add_argument("--structure-ir", required=True, type=Path)
    parser.add_argument("--structure-grid", required=True, type=Path)
    parser.add_argument("--floor-code", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()

    input_paths = [
        arguments.architecture_ir,
        arguments.architecture_grid,
        arguments.structure_ir,
        arguments.structure_grid,
    ]
    output_resolved = arguments.output.resolve()
    if any(output_resolved == path.resolve() for path in input_paths):
        raise ValueError("output path must be different from every input path")
    _validate_output_scope(arguments.output, input_paths)

    architecture_ir = _load_object(arguments.architecture_ir)
    architecture_grid = _load_object(arguments.architecture_grid)
    structure_ir = _load_object(arguments.structure_ir)
    structure_grid = _load_object(arguments.structure_grid)
    _verify_declared_source(architecture_ir, "architecture")
    _verify_declared_source(structure_ir, "structure")
    result = fuse_same_floor_review_ir(
        architecture_ir,
        architecture_grid,
        structure_ir,
        structure_grid,
        floor_code=arguments.floor_code,
    )
    result["input_artifacts"] = [
        {
            "role": role,
            "path": str(path.resolve()),
            "sha256": _file_sha256(path),
        }
        for role, path in zip((
            "architecture_review_ir",
            "architecture_grid",
            "structure_review_ir",
            "structure_grid",
        ), input_paths)
    ]
    _atomic_write_json(arguments.output, result)
    metrics = result["metrics"]
    print(
        f"{result['artifact_status']}: "
        f"{metrics['fused_columns']} columns, "
        f"{metrics['fused_structural_walls']} structural walls, "
        f"{metrics['architectural_walls']} architectural walls; "
        f"output={arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
