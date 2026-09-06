"""SQL repository for durable workflow state and ordered task events."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.db.session import engine
from backend.adapters.approval_repository import insert_approval
from backend.domain.contracts import (
    TERMINAL_TASK_STATUSES,
    RequestContext,
    TaskEnvelope,
    TaskEvent,
    TaskFailureType,
    TaskStatus,
)
from backend.domain.errors import (
    InvalidTransition,
    PolicyFailure,
    ResourceNotFound,
    ValidationFailure,
)


_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELED, TaskStatus.FAILED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.WAITING_HUMAN,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELED,
    }),
    TaskStatus.WAITING_HUMAN: frozenset({TaskStatus.RESUMED, TaskStatus.CANCELED}),
    TaskStatus.RESUMED: frozenset({
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELED,
    }),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELED: frozenset(),
}

_WORKFLOW_RUN_COLUMNS = """
    id, tenant_id, project_id, actor_id, workflow, status,
    idempotency_key, correlation_id, schema_version,
    input_artifact_ids, options, result, error_type, error_message,
    created_at, updated_at, version
"""

_PROJECT_SCOPE_SQL = """
    AND (
        project_id IS NULL
        OR (:project_id IS NOT NULL AND project_id=:project_id)
    )
"""


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class TaskRepository:
    async def create_or_get(
        self,
        context: RequestContext,
        envelope: TaskEnvelope,
    ) -> tuple[dict, bool]:
        """Create a run atomically or return the tenant-scoped idempotent run."""

        existing = await self.get_by_idempotency(
            context.tenant_id,
            envelope.idempotency_key,
            project_id=context.project_id,
        )
        if existing:
            return existing, False

        event_id = "evt_" + uuid.uuid4().hex
        outbox_id = "out_" + uuid.uuid4().hex
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO workflow_runs (
                        id, tenant_id, project_id, actor_id, workflow, status,
                        idempotency_key, correlation_id, schema_version,
                        input_artifact_ids, options, version
                    ) VALUES (
                        :id, :tenant_id, :project_id, :actor_id, :workflow, 'queued',
                        :idempotency_key, :correlation_id, :schema_version,
                        :input_artifact_ids, :options, 1
                    )
                """), {
                    **envelope.model_dump(exclude={"input_artifact_ids"}),
                    "id": envelope.task_id,
                    "input_artifact_ids": json.dumps(envelope.input_artifact_ids),
                    "options": json.dumps(envelope.options, ensure_ascii=False),
                })
                event_payload = {"workflow": envelope.workflow}
                await conn.execute(text("""
                    INSERT INTO task_events (
                        id, run_id, tenant_id, project_id, seq, type, stage,
                        status, payload, trace_id, created_by, version
                    ) VALUES (
                        :id, :run_id, :tenant_id, :project_id, 1, 'task.created', '',
                        'queued', :payload, :trace_id, :created_by, 1
                    )
                """), {
                    "id": event_id,
                    "run_id": envelope.task_id,
                    "tenant_id": context.tenant_id,
                    "project_id": context.project_id,
                    "payload": json.dumps(event_payload, ensure_ascii=False),
                    "trace_id": context.trace_id,
                    "created_by": context.user_id,
                })
                await conn.execute(text("""
                    INSERT INTO outbox_events (
                        id, aggregate_type, aggregate_id, tenant_id, project_id,
                        event_type, routing_key, payload, status, attempts, version
                    ) VALUES (
                        :id, 'workflow_run', :aggregate_id, :tenant_id, :project_id,
                        'task.created', :routing_key, :payload, 'pending', 0, 1
                    )
                """), {
                    "id": outbox_id,
                    "aggregate_id": envelope.task_id,
                    "tenant_id": context.tenant_id,
                    "project_id": context.project_id,
                    "routing_key": envelope.workflow + ".run",
                    "payload": envelope.model_dump_json(),
                })
        except IntegrityError:
            existing = await self.get_by_idempotency(
                context.tenant_id,
                envelope.idempotency_key,
                project_id=context.project_id,
            )
            if existing:
                return existing, False
            raise
        return await self.get(context, envelope.task_id), True

    async def get_by_idempotency(
        self, tenant_id: str, idempotency_key: str, *, project_id: str | None = None
    ) -> dict | None:
        async with engine.connect() as conn:
            row = (await conn.execute(text(f"""
                SELECT {_WORKFLOW_RUN_COLUMNS}
                FROM workflow_runs
                WHERE tenant_id=:tenant_id AND idempotency_key=:idempotency_key
                  {_PROJECT_SCOPE_SQL}
            """), {
                "tenant_id": tenant_id,
                "idempotency_key": idempotency_key,
                "project_id": project_id,
            })).mappings().first()
        return self._row(row) if row else None

    async def get_workflow_scope(self, context: RequestContext, task_id: str) -> dict:
        """Return the tenant-scoped workflow/project binding for a task.

        Resume authorization needs to distinguish a missing project context
        from a genuinely global task before the normal project filter is
        applied.  This helper returns only the scope metadata and never
        bypasses the tenant boundary.
        """

        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, workflow
                FROM workflow_runs
                WHERE id=:id AND tenant_id=:tenant_id
            """), {
                "id": task_id,
                "tenant_id": context.tenant_id,
            })).mappings().first()
        if not row:
            raise ResourceNotFound("workflow run not found")
        return dict(row)

    async def list_by_actor(
        self, context: RequestContext, *, limit: int = 20
    ) -> list[dict]:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(f"""
                SELECT {_WORKFLOW_RUN_COLUMNS}
                FROM workflow_runs
                WHERE tenant_id=:tenant_id AND actor_id=:actor_id
                  {_PROJECT_SCOPE_SQL}
                ORDER BY created_at DESC LIMIT :limit
            """), {
                "tenant_id": context.tenant_id,
                "actor_id": context.user_id,
                "project_id": context.project_id,
                "limit": max(1, min(limit, 100)),
            })).mappings().all()
        return [self._row(row) for row in rows]

    async def get(self, context: RequestContext, task_id: str) -> dict:
        async with engine.connect() as conn:
            row = (await conn.execute(text(f"""
                SELECT {_WORKFLOW_RUN_COLUMNS}
                FROM workflow_runs
                WHERE id=:id AND tenant_id=:tenant_id
                  {_PROJECT_SCOPE_SQL}
            """), {
                "id": task_id,
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
            })).mappings().first()
        if not row:
            raise ResourceNotFound("workflow run not found")
        return self._row(row)

    async def transition(
        self,
        context: RequestContext,
        task_id: str,
        target: TaskStatus,
        *,
        event_type: str,
        stage: str = "",
        payload: dict | None = None,
        result: dict | None = None,
        failure_type: TaskFailureType | None = None,
        error_message: str | None = None,
        approval_action: str | None = None,
        approval_decision: str | None = None,
        approval_reason: str | None = None,
        approval_actor_id: str | None = None,
    ) -> dict:
        current = await self.get(context, task_id)
        current_status = TaskStatus(current["status"])
        if target not in _ALLOWED_TRANSITIONS[current_status]:
            raise InvalidTransition(
                f"cannot transition {current_status.value} to {target.value}"
            )
        if approval_action is not None:
            if approval_decision not in {"approved", "rejected"}:
                raise ValidationFailure("approval decision is required")
            if approval_decision == "rejected" and target != TaskStatus.CANCELED:
                raise ValidationFailure(
                    "a rejected approval must cancel the workflow"
                )
            if approval_decision == "approved" and target != TaskStatus.RESUMED:
                raise ValidationFailure(
                    "an approved decision must resume the workflow"
                )
            if approval_actor_id is None:
                approval_actor_id = context.user_id
            elif str(approval_actor_id).strip() != str(context.user_id).strip():
                raise PolicyFailure(
                    "approval actor does not match the authenticated context"
                )

        event_id = "evt_" + uuid.uuid4().hex
        event_payload = payload or {}
        event_project_id = current["project_id"] or context.project_id
        async with engine.begin() as conn:
            seq = int((await conn.execute(text("""
                SELECT COALESCE(MAX(seq), 0) + 1
                FROM task_events WHERE run_id=:run_id AND tenant_id=:tenant_id
            """), {
                "run_id": task_id,
                "tenant_id": context.tenant_id,
            })).scalar_one())
            updated = await conn.execute(text("""
                UPDATE workflow_runs
                SET status=:status,
                    result=COALESCE(:result, result),
                    error_type=:error_type,
                    error_message=:error_message,
                    updated_at=CURRENT_TIMESTAMP,
                    version=version + 1
                WHERE id=:id AND tenant_id=:tenant_id AND version=:version
            """), {
                "status": target.value,
                "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
                "error_type": failure_type.value if failure_type else None,
                "error_message": error_message,
                "id": task_id,
                "tenant_id": context.tenant_id,
                "version": current["version"],
            })
            if updated.rowcount != 1:
                raise InvalidTransition("workflow was updated concurrently")
            if approval_action is not None:
                await insert_approval(
                    conn,
                    run_id=current["id"],
                    tenant_id=current["tenant_id"],
                    project_id=current.get("project_id"),
                    context=context,
                    action=approval_action,
                    decision=approval_decision,
                    reason=approval_reason,
                    actor_id=approval_actor_id or "",
                )
            await conn.execute(text("""
                INSERT INTO task_events (
                    id, run_id, tenant_id, project_id, seq, type, stage,
                    status, payload, trace_id, created_by, version
                ) VALUES (
                    :id, :run_id, :tenant_id, :project_id, :seq, :type, :stage,
                    :status, :payload, :trace_id, :created_by, 1
                )
            """), {
                "id": event_id,
                "run_id": task_id,
                "tenant_id": context.tenant_id,
                "project_id": event_project_id,
                "seq": seq,
                "type": event_type,
                "stage": stage,
                "status": target.value,
                "payload": json.dumps(event_payload, ensure_ascii=False),
                "trace_id": context.trace_id,
                "created_by": context.user_id,
            })
        return await self.get(context, task_id)

    async def resume(
        self,
        context: RequestContext,
        task_id: str,
        *,
        payload: dict[str, Any],
        approver_id: str | None = None,
        approver_role: str | None = None,
        approval_action: str | None = None,
        approval_reason: str | None = None,
    ) -> dict:
        """Transition waiting_human -> resumed and enqueue a durable resume."""
        current = await self.get(context, task_id)
        if TaskStatus(current["status"]) != TaskStatus.WAITING_HUMAN:
            raise InvalidTransition("only waiting_human tasks can be resumed")
        decision = str((payload or {}).get("decision") or "").strip().lower()
        if decision != "approved":
            raise ValidationFailure(
                "repository resume accepts only an approved decision; "
                "use transition(..., canceled) for rejection"
            )
        event_id = "evt_" + uuid.uuid4().hex
        outbox_id = "out_" + uuid.uuid4().hex
        # The original task actor owns submission; the authenticated approver
        # owns this resume attempt.  Never derive the worker identity from a
        # client-controlled ``payload["operator"]`` field.
        authenticated_actor = str(context.user_id or "").strip()
        if not authenticated_actor:
            raise PolicyFailure("resume approver identity is missing")
        if approver_id is not None and str(approver_id).strip() != authenticated_actor:
            raise PolicyFailure(
                "resume approver does not match the authenticated context"
            )
        if approver_role is not None and str(approver_role).strip() != str(context.role).strip():
            raise PolicyFailure(
                "resume approver role does not match the authenticated context"
            )
        trusted_approver = authenticated_actor
        payload = {
            **payload,
            # Repository is the final persistence boundary; do not trust a
            # caller that bypassed TaskService to forge the approval actor.
            "operator": trusted_approver,
        }
        if approver_role:
            payload["operator_role"] = str(approver_role)
        envelope = TaskEnvelope(
            task_id=current["id"], tenant_id=current["tenant_id"],
            project_id=current["project_id"], actor_id=trusted_approver,
            workflow=current["workflow"], input_artifact_ids=current["input_artifact_ids"],
            options=current.get("options") or {},
            idempotency_key=current["idempotency_key"],
            correlation_id=current["correlation_id"],
            schema_version=current["schema_version"], resume_payload=payload,
        )
        event_project_id = current["project_id"] or context.project_id
        async with engine.begin() as conn:
            seq = int((await conn.execute(text("""
                SELECT COALESCE(MAX(seq), 0) + 1
                FROM task_events WHERE run_id=:run_id AND tenant_id=:tenant_id
            """), {
                "run_id": task_id, "tenant_id": context.tenant_id,
            })).scalar_one())
            updated = await conn.execute(text("""
                UPDATE workflow_runs
                SET status='resumed', updated_at=CURRENT_TIMESTAMP, version=version + 1
                WHERE id=:id AND tenant_id=:tenant_id AND version=:version
            """), {
                "id": task_id, "tenant_id": context.tenant_id,
                "version": current["version"],
            })
            if updated.rowcount != 1:
                raise InvalidTransition("workflow was updated concurrently")
            if approval_action is not None:
                await insert_approval(
                    conn,
                    run_id=current["id"],
                    tenant_id=current["tenant_id"],
                    project_id=current.get("project_id"),
                    context=context,
                    action=approval_action,
                    decision="approved",
                    reason=approval_reason,
                    actor_id=trusted_approver,
                )
            await conn.execute(text("""
                INSERT INTO task_events (
                    id, run_id, tenant_id, project_id, seq, type, stage,
                    status, payload, trace_id, created_by, version
                ) VALUES (
                    :id, :run_id, :tenant_id, :project_id, :seq, 'task.resumed',
                    'hitl', 'resumed', :payload, :trace_id, :created_by, 1
                )
            """), {
                "id": event_id, "run_id": task_id,
                "tenant_id": context.tenant_id, "project_id": event_project_id,
                "seq": seq, "payload": json.dumps(payload, ensure_ascii=False),
                "trace_id": context.trace_id, "created_by": context.user_id,
            })
            await conn.execute(text("""
                INSERT INTO outbox_events (
                    id, aggregate_type, aggregate_id, tenant_id, project_id,
                    event_type, routing_key, payload, status, attempts, version
                ) VALUES (
                    :id, 'workflow_run', :aggregate_id, :tenant_id, :project_id,
                    'task.resumed', 'hitl.resume', :payload, 'pending', 0, 1
                )
            """), {
                "id": outbox_id, "aggregate_id": task_id,
                "tenant_id": context.tenant_id, "project_id": event_project_id,
                "payload": envelope.model_dump_json(),
            })
        return await self.get(context, task_id)

    async def begin_step(
        self,
        context: RequestContext,
        task_id: str,
        *,
        name: str,
        position: int,
        input_payload: dict,
        attempt: int,
    ) -> str:
        """Persist a step attempt before invoking any external dependency."""
        step_id = f"step_{task_id}_{position}"
        async with engine.begin() as conn:
            existing = (await conn.execute(text("""
                SELECT id, status FROM workflow_steps
                WHERE run_id=:run_id AND tenant_id=:tenant_id AND position=:position
            """), {
                "run_id": task_id,
                "tenant_id": context.tenant_id,
                "position": position,
            })).mappings().first()
            if existing:
                await conn.execute(text("""
                    UPDATE workflow_steps
                    SET status='running', attempts=:attempt, input_payload=:input_payload,
                        output_payload=NULL, error_message=NULL,
                        started_at=CURRENT_TIMESTAMP, finished_at=NULL,
                        updated_at=CURRENT_TIMESTAMP, version=version + 1
                    WHERE id=:id AND tenant_id=:tenant_id
                """), {
                    "id": existing["id"], "tenant_id": context.tenant_id,
                    "attempt": attempt,
                    "input_payload": json.dumps(input_payload, ensure_ascii=False),
                })
                return str(existing["id"])
            await conn.execute(text("""
                INSERT INTO workflow_steps (
                    id, run_id, tenant_id, project_id, name, position, status,
                    attempts, input_payload, started_at, created_by, version
                ) VALUES (
                    :id, :run_id, :tenant_id, :project_id, :name, :position, 'running',
                    :attempt, :input_payload, CURRENT_TIMESTAMP, :created_by, 1
                )
            """), {
                "id": step_id,
                "run_id": task_id,
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
                "name": name,
                "position": position,
                "attempt": attempt,
                "input_payload": json.dumps(input_payload, ensure_ascii=False),
                "created_by": context.user_id,
            })
        return step_id

    async def finish_step(
        self,
        context: RequestContext,
        step_id: str,
        *,
        status: str,
        output_payload: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "waiting_human", "canceled"}:
            raise InvalidTransition(f"invalid workflow step status: {status}")
        async with engine.begin() as conn:
            updated = await conn.execute(text("""
                UPDATE workflow_steps
                SET status=:status, output_payload=:output_payload,
                    error_message=:error_message, finished_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP, version=version + 1
                WHERE id=:id AND tenant_id=:tenant_id
            """), {
                "status": status,
                "output_payload": (
                    json.dumps(output_payload, ensure_ascii=False)
                    if output_payload is not None else None
                ),
                "error_message": error_message,
                "id": step_id,
                "tenant_id": context.tenant_id,
            })
            if updated.rowcount != 1:
                raise ResourceNotFound("workflow step not found")

    async def list_steps(self, context: RequestContext, task_id: str) -> list[dict]:
        await self.get(context, task_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT id, run_id, name, position, status, attempts,
                       input_payload, output_payload, error_message,
                       started_at, finished_at, version
                FROM workflow_steps
                WHERE run_id=:run_id AND tenant_id=:tenant_id
                ORDER BY position ASC
            """), {
                "run_id": task_id,
                "tenant_id": context.tenant_id,
            })).mappings().all()
        result = []
        for row in rows:
            item = dict(row)
            item["input_payload"] = _loads(item.get("input_payload"), {})
            item["output_payload"] = _loads(item.get("output_payload"), None)
            result.append(item)
        return result

    async def list_events(
        self, context: RequestContext, task_id: str, after_seq: int = 0
    ) -> list[TaskEvent]:
        await self.get(context, task_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT id, run_id, seq, type, stage, status, payload,
                       trace_id, occurred_at
                FROM task_events
                WHERE run_id=:run_id AND tenant_id=:tenant_id AND seq>:after_seq
                ORDER BY seq ASC
            """), {
                "run_id": task_id,
                "tenant_id": context.tenant_id,
                "after_seq": max(0, after_seq),
            })).mappings().all()
        return [TaskEvent(
            event_id=row["id"],
            task_id=row["run_id"],
            seq=row["seq"],
            type=row["type"],
            stage=row["stage"] or "",
            status=TaskStatus(row["status"]),
            payload=_loads(row["payload"], {}),
            trace_id=row["trace_id"],
            occurred_at=row["occurred_at"],
        ) for row in rows]

    @staticmethod
    def _row(row) -> dict:
        data = dict(row)
        data["input_artifact_ids"] = _loads(data.get("input_artifact_ids"), [])
        data["options"] = _loads(data.get("options"), {})
        data["result"] = _loads(data.get("result"), None)
        data["terminal"] = TaskStatus(data["status"]) in TERMINAL_TASK_STATUSES
        return data
