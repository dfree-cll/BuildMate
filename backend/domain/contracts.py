"""Stable, versioned contracts shared by API, agents and workers."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    RESUMED = "resumed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_TASK_STATUSES = frozenset({
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
})


class TaskFailureType(str, enum.Enum):
    VALIDATION = "validation"
    DEPENDENCY = "dependency"
    TIMEOUT = "timeout"
    POLICY = "policy"
    SYSTEM = "system"


class EvidenceRef(BaseModel):
    """A verifiable pointer to the fact supporting a conclusion."""

    kind: str = Field(..., min_length=1, max_length=32)
    artifact_id: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    element_id: str | None = None
    page_no: int | None = Field(default=None, ge=1)
    locator: str | None = Field(default=None, max_length=512)
    quote: str | None = Field(default=None, max_length=1000)

    @field_validator("artifact_id", "document_id", "chunk_id", "element_id")
    @classmethod
    def _non_blank_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("evidence identifiers cannot be blank")
        return value


class RequestContext(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    user_id: str = Field(..., min_length=1, max_length=128)
    role: str = Field(default="user", min_length=1, max_length=32)
    trace_id: str = Field(..., min_length=1, max_length=128)
    correlation_id: str = Field(..., min_length=1, max_length=128)


class TaskEnvelope(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=64)
    tenant_id: str = Field(..., min_length=1, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    actor_id: str = Field(..., min_length=1, max_length=128)
    workflow: str = Field(..., min_length=1, max_length=64)
    input_artifact_ids: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    correlation_id: str = Field(..., min_length=1, max_length=128)
    schema_version: str = Field(default="2.0", pattern=r"^2\.")
    # HITL decisions are carried only on a resume message.  Keeping this
    # optional preserves the original task contract and makes retries
    # deterministic without relying on worker memory.
    resume_payload: dict[str, Any] = Field(default_factory=dict)


class TaskEvent(BaseModel):
    event_id: str
    task_id: str
    seq: int = Field(..., ge=1)
    type: str = Field(..., min_length=1, max_length=64)
    stage: str = Field(default="", max_length=64)
    status: TaskStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentResult(BaseModel):
    status: str = Field(..., min_length=1, max_length=32)
    answer: str = ""
    structured_output: dict[str, Any] | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    next_action: str | None = None
    fallback_used: bool = False


class ReviewFindingInput(BaseModel):
    """Validated external/LLM review finding; evidence is mandatory."""

    rule_id: str | None = Field(default=None, max_length=64)
    track: str = Field(default="hard", pattern=r"^(hard|soft)$")
    severity: str = Field(default="warning", pattern=r"^(critical|warning|info)$")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    description: str = Field(..., min_length=1, max_length=4000)
    suggestion: str | None = Field(default=None, max_length=4000)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=20)
