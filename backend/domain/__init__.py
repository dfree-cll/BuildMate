"""BuildMate v2 domain contracts plus the v3 intent-routing contract.

The domain package is intentionally framework-agnostic.  API, queue and storage
adapters may depend on it; domain code must not depend on those adapters.
"""

from backend.domain.contracts import (
    AgentResult,
    EvidenceRef,
    TaskEnvelope,
    TaskEvent,
    TaskFailureType,
    TaskStatus,
)
from backend.domain.model_ir import ModelIRV2
from backend.domain.intent import IntentRoute

__all__ = [
    "AgentResult",
    "EvidenceRef",
    "ModelIRV2",
    "IntentRoute",
    "TaskEnvelope",
    "TaskEvent",
    "TaskFailureType",
    "TaskStatus",
]
