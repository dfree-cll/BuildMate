"""Contracts for safe multi-domain task understanding and handoff.

These models describe work; they do not create a task or invoke an agent.  A
worker may only execute a step after its owning workflow has confirmed it.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.domain.contracts import EvidenceRef, RequestContext
from backend.domain.intent import IntentName, IntentRouteName

TaskDomain = Literal["knowledge", "bim", "bid", "contract", "procurement", "negotiation"]
PlanMode = Literal["single", "composite"]
FactValue = str | int | float | bool | None
RiskSummary = Annotated[str, Field(min_length=1, max_length=1000)]


class TaskStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: int = Field(..., ge=1, le=20)
    intent: IntentName
    domain: TaskDomain
    route: IntentRouteName
    parameters: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = False
    missing_parameters: list[str] = Field(default_factory=list)


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    mode: PlanMode
    original_query: str = Field(..., min_length=1, max_length=4000)
    steps: list[TaskStep] = Field(..., min_length=1, max_length=20)
    needs_confirmation: bool = False
    needs_clarification: bool = False


class TaskPlanPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: TaskPlan
    trace_id: str


class TaskHandoffPackage(BaseModel):
    """Draft handoff, never evidence validation or permission to execute a task."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal["1.0"] = "1.0"
    source_task: str = Field(..., min_length=1, max_length=64)
    target_domain: TaskDomain
    tenant_id: str = Field(..., min_length=1, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    actor_id: str = Field(..., min_length=1, max_length=128)
    trace_id: str = Field(..., min_length=1, max_length=128)
    correlation_id: str = Field(..., min_length=1, max_length=128)
    status: Literal["draft"] = "draft"
    requires_evidence_revalidation: Literal[True] = True
    facts: dict[str, FactValue] = Field(default_factory=dict, max_length=50)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=50)
    risk_summary: list[RiskSummary] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _evidence_required_for_facts(self) -> "TaskHandoffPackage":
        if (self.facts or self.risk_summary) and not self.evidence_refs:
            raise ValueError("handoff facts and risks require evidence references")
        for ref in self.evidence_refs:
            if not any((ref.artifact_id, ref.document_id, ref.chunk_id, ref.element_id)):
                raise ValueError("handoff evidence requires a source identifier")
        # Workflow status, approval and conversation history must stay in their
        # owning repositories, not in an arbitrary cross-domain facts bag.
        forbidden = {"history", "messages", "conversation", "approval", "approvals", "token", "access_token"}
        if any(key.lower() in forbidden for key in self.facts):
            raise ValueError("handoff facts cannot carry history, approvals or credentials")
        if len(json.dumps(self.facts, ensure_ascii=False)) > 16000:
            raise ValueError("handoff facts exceed the 16000 character budget")
        return self

    @classmethod
    def for_context(
        cls,
        context: RequestContext,
        *,
        source_task: str,
        target_domain: TaskDomain,
        facts: dict[str, FactValue] | None = None,
        evidence_refs: list[EvidenceRef] | None = None,
        risk_summary: list[str] | None = None,
    ) -> "TaskHandoffPackage":
        return cls(
            source_task=source_task,
            target_domain=target_domain,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            actor_id=context.user_id,
            trace_id=context.trace_id,
            correlation_id=context.correlation_id,
            facts=facts or {},
            evidence_refs=evidence_refs or [],
            risk_summary=risk_summary or [],
        )

    def assert_context(self, context: RequestContext) -> None:
        if (self.tenant_id, self.project_id, self.actor_id) != (
            context.tenant_id, context.project_id, context.user_id
        ):
            raise ValueError("task handoff context does not match authenticated tenant/project/actor")
