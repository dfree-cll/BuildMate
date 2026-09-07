"""Construction of trusted request context at the API boundary."""

from __future__ import annotations

import uuid

from backend.domain.contracts import RequestContext


def build_request_context(
    current_user: dict,
    *,
    project_id: str | None = None,
    trace_id: str | None = None,
    correlation_id: str | None = None,
) -> RequestContext:
    resolved_trace = trace_id or uuid.uuid4().hex
    context = RequestContext(
        tenant_id=str(current_user["tenant_id"]),
        project_id=project_id,
        user_id=str(current_user["user_id"]),
        role=str(current_user.get("role", "user")),
        trace_id=resolved_trace,
        correlation_id=correlation_id or resolved_trace,
    )
    from backend.db.rls import activate_rls_context

    activate_rls_context(context)
    return context
