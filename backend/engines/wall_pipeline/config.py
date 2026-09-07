"""Configuration loading with paths resolved relative to the config file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.engines.wall_pipeline.contracts import WallPipelineConfig


def _load_mapping(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        value = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("PyYAML is required to load YAML pipeline config") from exc
        value = yaml.safe_load(text)
    else:
        raise ValueError("pipeline config must be .json, .yaml or .yml")
    if not isinstance(value, dict):
        raise ValueError("pipeline config root must be an object")
    return value


def load_wall_pipeline_config(path: str | Path) -> WallPipelineConfig:
    config_path = Path(path).expanduser().resolve()
    value = _load_mapping(config_path)
    config = WallPipelineConfig.model_validate(value)

    resolved_files = []
    for source_file in config.source.files:
        source_path = Path(source_file.path).expanduser()
        if not source_path.is_absolute():
            source_path = config_path.parent / source_path
        resolved_files.append(source_file.model_copy(update={"path": str(source_path.resolve())}))
    source = config.source.model_copy(update={"files": resolved_files})

    output_dir = Path(config.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = config_path.parent / output_dir
    revit = config.revit
    if revit.target_model_path:
        target = Path(revit.target_model_path).expanduser()
        if not target.is_absolute():
            target = config_path.parent / target
        revit = revit.model_copy(update={"target_model_path": str(target.resolve())})
    return config.model_copy(update={
        "source": source,
        "output_dir": str(output_dir.resolve()),
        "revit": revit,
    })
