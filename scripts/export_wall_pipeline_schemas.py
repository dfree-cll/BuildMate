"""Export committed JSON Schemas from the wall pipeline Pydantic contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.engines.wall_pipeline.contracts import (  # noqa: E402
    AuditReport,
    RevitResult,
    SourceEntities,
    SourceManifest,
    WallEvidence,
    WallModel,
    WallPipelineConfig,
)


SCHEMAS = {
    "wall_pipeline_config.schema.json": WallPipelineConfig,
    "source_manifest.schema.json": SourceManifest,
    "source_entities.schema.json": SourceEntities,
    "wall_evidence.schema.json": WallEvidence,
    "wall_model.schema.json": WallModel,
    "revit_result.schema.json": RevitResult,
    "audit_report.schema.json": AuditReport,
}


def main() -> int:
    output = ROOT / "docs" / "contracts" / "wall_pipeline"
    output.mkdir(parents=True, exist_ok=True)
    for filename, model in SCHEMAS.items():
        (output / filename).write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(f"exported {len(SCHEMAS)} schemas to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
