"""Model IR v2: immutable, evidence-bearing BIM intermediate representation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.domain.contracts import EvidenceRef


class CoordinateSystem(BaseModel):
    name: str = "local"
    epsg: int | None = Field(default=None, ge=1)
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_deg: float = 0.0


class TransformStep(BaseModel):
    operation: Literal["translate", "rotate", "scale", "unit_convert", "align"]
    parameters: dict[str, float | str]
    source: str


class ModelLevel(BaseModel):
    id: str
    name: str
    elevation_m: float


class GridAxis(BaseModel):
    id: str
    label: str
    direction: Literal["x", "y", "other"]
    start: tuple[float, float]
    end: tuple[float, float]


class ModelElement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element_id: str = Field(..., min_length=1)
    type: Literal["Wall", "Column", "Beam", "Floor", "Slab", "Opening", "Grid"]
    geometry: dict[str, Any]
    properties: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[EvidenceRef] = Field(min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    review_status: Literal["pending", "approved", "rejected", "auto_passed"] = "pending"

    @field_validator("geometry")
    @classmethod
    def _geometry_not_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("geometry cannot be empty")
        return value


class ModelIRProvenance(BaseModel):
    source_artifact_ids: list[str] = Field(min_length=1)
    source_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    parser_version: str
    pipeline_version: str


class ModelIRV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    project: dict[str, Any]
    units: Literal["m"] = "m"
    coordinate_system: CoordinateSystem = Field(default_factory=CoordinateSystem)
    transform_chain: list[TransformStep] = Field(default_factory=list)
    levels: list[ModelLevel] = Field(min_length=1)
    grid: list[GridAxis] = Field(default_factory=list)
    model_elements: list[ModelElement] = Field(min_length=1)
    provenance: ModelIRProvenance

    @model_validator(mode="after")
    def _unique_ids_and_valid_levels(self) -> "ModelIRV2":
        element_ids = [element.element_id for element in self.model_elements]
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("model element ids must be unique")
        level_ids = [level.id for level in self.levels]
        if len(level_ids) != len(set(level_ids)):
            raise ValueError("level ids must be unique")
        return self

    def canonical_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
