"""Deterministic PDF/DWG wall-to-Revit pipeline.

The package is intentionally independent from IFC ingestion.  Source adapters
produce evidence; only reviewed ``WallModel`` artifacts may reach Revit.
"""

from backend.engines.wall_pipeline.contracts import (
    AuditReport,
    BeamRecognitionConfig,
    BeamRecord,
    ColumnRecognitionConfig,
    ColumnRecord,
    ConstructionInfo,
    ModelingStandardConfig,
    QuantityTakeoff,
    RevitResult,
    SourceEntities,
    SourceManifest,
    WallEvidence,
    WallModel,
    WallPipelineConfig,
    infer_floor_code_from_elevation_range,
    parse_level_elevation_range,
)
from backend.engines.wall_pipeline.pipeline import (
    approve_revit_write,
    approve_wall_model,
    dry_run_wall_model,
    load_wall_pipeline_config,
    run_wall_pipeline,
    verify_wall_model_lineage,
)
from backend.engines.wall_pipeline.audit import REQUIRED_SIMILARITY

__all__ = [
    "AuditReport",
    "BeamRecognitionConfig",
    "BeamRecord",
    "ColumnRecognitionConfig",
    "ColumnRecord",
    "ConstructionInfo",
    "ModelingStandardConfig",
    "QuantityTakeoff",
    "RevitResult",
    "SourceEntities",
    "SourceManifest",
    "WallEvidence",
    "WallModel",
    "WallPipelineConfig",
    "infer_floor_code_from_elevation_range",
    "parse_level_elevation_range",
    "REQUIRED_SIMILARITY",
    "approve_revit_write",
    "approve_wall_model",
    "dry_run_wall_model",
    "load_wall_pipeline_config",
    "run_wall_pipeline",
    "verify_wall_model_lineage",
]
