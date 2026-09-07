"""Versioned intent-routing contract for the v3 question entry.

The router is deliberately small and deterministic at the boundary.  An LLM
may later provide a candidate classification, but the returned intent and
route still have to satisfy this contract and the server-side allow-list.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


IntentName = Literal[
    "building_standard_query",
    "bid_query",
    "contract_review",
    "procurement_query",
    "negotiation_query",
    "bim_command",
    "general_question",
]

IntentRouteName = Literal[
    "rag.answer",
    "bid.search",
    "workflow.bid_review",
    "workflow.contract_review",
    "workflow.procurement",
    "workflow.negotiation",
    "workflow.wall_pipeline",
    "clarify",
]

EvidenceSource = Literal["rag", "structured", "rules", "bim"]


class IntentRoute(BaseModel):
    """A safe, auditable result of question intent classification."""

    intent: IntentName
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    parameters: dict[str, Any] = Field(default_factory=dict)
    route: IntentRouteName
    evidence_sources: list[EvidenceSource] = Field(default_factory=list)
    requires_confirmation: bool = False
    missing_parameters: list[str] = Field(default_factory=list)
