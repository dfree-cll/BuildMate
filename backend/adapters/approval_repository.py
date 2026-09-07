"""Shared persistence helper for human approvals.

Approvals are part of the audit trail, not an implementation detail of one
repository.  Keeping the insert in one helper prevents task/build/review
repositories from drifting on validation, tenant scope or timestamp precision.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from backend.domain.contracts import RequestContext
from backend.domain.errors import PolicyFailure, ValidationFailure


async def insert_approval(
    conn: Any,
    *,
    run_id: str,
    tenant_id: str,
    project_id: str | None,
    context: RequestContext,
    action: str,
    decision: str,
    reason: str | None,
    actor_id: str,
) -> str:
    """Insert one validated approval and return its durable id."""

    normalized_action = str(action or "").strip()
    normalized_decision = str(decision or "").strip().lower()
    normalized_actor = str(actor_id or "").strip()
    if str(tenant_id or "").strip() != str(context.tenant_id or "").strip():
        raise PolicyFailure("approval tenant does not match the authenticated context")
    if project_id is not None and context.project_id is not None \
            and str(project_id).strip() != str(context.project_id).strip():
        raise PolicyFailure("approval project does not match the authenticated context")
    if not normalized_action or len(normalized_action) > 64:
        raise ValidationFailure("approval action is invalid")
    if normalized_decision not in {"approved", "rejected"}:
        raise ValidationFailure("approval decision is invalid")
    authenticated_actor = str(context.user_id or "").strip()
    if not normalized_actor or normalized_actor != authenticated_actor:
        raise PolicyFailure("approval actor does not match the authenticated context")
    approval_id = "approval_" + uuid.uuid4().hex
    await conn.execute(text("""
        INSERT INTO approvals (
            id, run_id, tenant_id, project_id, action, decision,
            reason, created_by, created_at, version
        ) VALUES (
            :id, :run_id, :tenant_id, :project_id, :action, :decision,
            :reason, :created_by, :created_at, 1
        )
    """), {
        "id": approval_id,
        "run_id": run_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "action": normalized_action,
        "decision": normalized_decision,
        "reason": reason,
        "created_by": normalized_actor,
        # SQLite CURRENT_TIMESTAMP only has second precision.  Gate order is
        # auditable, so persist microseconds as UTC without tzinfo to match
        # the shared DateTime column and PostgreSQL's asyncpg binding.
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
    })
    return approval_id
