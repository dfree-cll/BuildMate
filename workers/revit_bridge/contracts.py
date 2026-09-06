"""Versioned HTTP contract for the Windows Revit Bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ValidateScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str = Field(..., min_length=1, max_length=50_000)
    modifying: bool = True


class RunScriptRequest(ValidateScriptRequest):
    build_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    source_model_path: str = Field(..., min_length=1, max_length=1024)
    # Live legacy script execution is capability-bound to both the exact
    # script bytes and the source RVT snapshot.  Dry-run requests may omit
    # these fields because no write capability is exercised.
    script_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_model_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    dry_run: bool = True
    # Binding the script, source snapshot and target path makes the signed
    # capability longer than the legacy build/action-only token.
    approval_token: str | None = Field(default=None, max_length=2048)
    convergence_round: int = Field(default=1, ge=1, le=5)

    @field_validator("source_model_path")
    @classmethod
    def require_rvt(cls, value: str) -> str:
        if Path(value).suffix.lower() != ".rvt":
            raise ValueError("source_model_path must be an RVT file")
        return value


class RunWallModelRequest(BaseModel):
    """Typed, non-script Revit write contract for the wall pipeline.

    The backend sends the already-approved WallModel and its matching
    write-approval result.  The Bridge compiles that DTO itself; callers never
    get to inject arbitrary Python into this endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    build_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    source_model_path: str = Field(..., min_length=1, max_length=1024)
    wall_model: dict = Field(...)
    revit_result: dict = Field(...)
    # Snapshot captured by the deterministic dry-run.  A live write is
    # rejected if the target RVT changed after the human approval.
    source_model_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    dry_run: bool = True
    # Typed wall delivery binds the model, target snapshot and approval record
    # in the signed claims; keep a bounded but sufficient envelope for those
    # claims instead of truncating a valid capability token.
    approval_token: str | None = Field(default=None, max_length=2048)
    convergence_round: int = Field(default=1, ge=1, le=5)

    @field_validator("source_model_path")
    @classmethod
    def require_rvt(cls, value: str) -> str:
        if Path(value).suffix.lower() != ".rvt":
            raise ValueError("source_model_path must be an RVT file")
        return value

    @model_validator(mode="after")
    def limit_payload(self) -> "RunWallModelRequest":
        # A wall model is normally small, but the Bridge must not become an
        # accidental unbounded JSON ingress endpoint.
        import json
        if len(json.dumps(self.wall_model, ensure_ascii=False)) > 2_000_000:
            raise ValueError("wall_model payload exceeds 2 MB")
        if len(json.dumps(self.revit_result, ensure_ascii=False)) > 256_000:
            raise ValueError("revit_result payload exceeds 256 KB")
        return self


class PresentWallModelRequest(BaseModel):
    """Open an audited Bridge delivery in Revit and show its delivery view."""

    model_config = ConfigDict(extra="forbid")

    build_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    output_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    view: Literal["plan", "3d"] = "plan"


class ImportIFCRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    ifc_path: str = Field(..., min_length=1, max_length=1024)
    dry_run: bool = True
    approval_token: str | None = Field(default=None, max_length=512)
    convergence_round: int = Field(default=1, ge=1, le=5)

    @field_validator("ifc_path")
    @classmethod
    def require_ifc(cls, value: str) -> str:
        if Path(value).suffix.lower() != ".ifc":
            raise ValueError("ifc_path must be an IFC file")
        return value


class DiffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before_path: str = Field(..., min_length=1, max_length=1024)
    after_path: str = Field(..., min_length=1, max_length=1024)


class BridgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    build_id: str | None = None
    dry_run: bool = False
    message: str
    output_path: str | None = None
    details: dict = Field(default_factory=dict)
