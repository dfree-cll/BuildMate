"""Use cases for durable workflow creation and lifecycle transitions."""

from __future__ import annotations

import hashlib
import json
import uuid

from backend.adapters.task_repository import TaskRepository
from backend.domain.contracts import EvidenceRef, RequestContext, TaskEnvelope, TaskStatus
from backend.domain.errors import PolicyFailure, ResourceNotFound, ValidationFailure
from backend.domain.task_plan import TaskPlan


# These workflows produce project-bound drawing/model decisions.  A resume
# without the project context would make the approval ambiguous even when the
# underlying row was created by an older client without a project id.
_PROJECT_SCOPED_RESUME_WORKFLOWS = frozenset({"wall_pipeline", "drawing_review"})

_COMPOSITE_WORKFLOW_BY_ROUTE = {
    "workflow.bid_review": "bid_review",
    "workflow.procurement": "procurement",
    "workflow.negotiation": "negotiation",
    "workflow.contract_review": "contract_review",
    "workflow.wall_pipeline": "wall_pipeline",
}


class TaskService:
    def __init__(self, repository: TaskRepository):
        self._repository = repository

    async def _approval_action(
        self, context: RequestContext, task_id: str, workflow: str
    ) -> str:
        """Resolve a stable approval label from persisted workflow state.

        The client payload is never used to select the action.  The latest
        waiting-human event was written by the worker and distinguishes the
        two gates in the deterministic wall workflow; all other workflows
        receive a bounded workflow-specific HITL label.
        """

        stage = ""
        pipeline = ""
        current = await self._repository.get(context, task_id)
        result = current.get("result") or {}
        if isinstance(result, dict):
            structured = result.get("structured_output") or {}
            if isinstance(structured, dict):
                pipeline = str(structured.get("pipeline") or "").strip()
        events = await self._repository.list_events(context, task_id, 0)
        for event in reversed(events):
            if event.type == "task.waiting_human":
                stage = event.stage
                break
        # ``drawing_review`` is also the compatibility workflow name for
        # PDF/DWG/DXF.  Once its persisted payload identifies the
        # deterministic wall pipeline, retain the same action labels as the
        # explicit ``wall_pipeline`` entry point.
        is_wall_pipeline = workflow == "wall_pipeline" or pipeline == "wall_pipeline"
        if is_wall_pipeline and stage == "wall_model_approval_and_dry_run":
            return "revit_write"
        if is_wall_pipeline:
            return "wall_model_review"
        if workflow == "drawing_review":
            return "drawing_review"
        # Workflow names are API-validated, but TaskService is also used by
        # local workers/tests.  Keep the persisted action within the schema's
        # 64-character bound without trusting arbitrary punctuation.
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in workflow)
        return (safe[:50] + "_hitl") if safe else "workflow_hitl"

    async def submit(
        self,
        context: RequestContext,
        *,
        workflow: str,
        input_artifact_ids: list[str],
        options: dict | None = None,
        idempotency_key: str | None = None,
        task_id: str | None = None,
    ) -> tuple[dict, bool]:
        normalized_options = options or {}
        key = idempotency_key or hashlib.sha256(
            (context.tenant_id + ":" + (context.project_id or "") + ":"
             + workflow + ":" + ",".join(input_artifact_ids)
             + ":" + json.dumps(normalized_options, sort_keys=True, ensure_ascii=False)).encode()
        ).hexdigest()
        envelope = TaskEnvelope(
            task_id=task_id or "task_" + uuid.uuid4().hex,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            actor_id=context.user_id,
            workflow=workflow,
            input_artifact_ids=input_artifact_ids,
            options=normalized_options,
            idempotency_key=key,
            correlation_id=context.correlation_id,
        )
        run, created = await self._repository.create_or_get(context, envelope)
        return run, created

    async def submit_composite(
        self,
        context: RequestContext,
        *,
        plan: TaskPlan,
        input_artifact_ids: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict, list[dict], bool]:
        """Persist a confirmed plan as one parent run and durable child runs.

        Child runs retain the normal workflow contracts and outbox semantics;
        the parent is an auditable coordination record whose options contain
        the immutable plan and handoff packages.  Read-only/clarification
        steps stay in the parent plan and do not create a fake workflow.
        """
        if plan.mode != "composite":
            raise ValidationFailure("只有复合任务计划可以创建父子任务")
        if plan.needs_clarification:
            raise ValidationFailure("任务计划仍缺少必要参数，补齐后才能确认")
        artifacts = [str(item).strip() for item in (input_artifact_ids or []) if str(item).strip()]
        parent_key = idempotency_key or hashlib.sha256(
            (context.tenant_id + ":" + (context.project_id or "") + ":composite:" +
             plan.model_dump_json() + ":" + ",".join(artifacts)).encode()
        ).hexdigest()
        existing = await self._repository.get_by_idempotency(
            context.tenant_id, parent_key, project_id=context.project_id
        )
        if existing:
            children = (existing.get("options") or {}).get("children") or []
            return existing, list(children), False

        parent_id = "task_" + uuid.uuid4().hex
        child_specs: list[dict] = []
        for step in plan.steps:
            workflow = _COMPOSITE_WORKFLOW_BY_ROUTE.get(step.route)
            if workflow is None:
                # Registered read-only routes are represented by the plan;
                # unregistered side-effect routes must fail closed.
                continue
            step_artifacts = list(dict.fromkeys(
                artifacts + [str(item) for item in step.parameters.get("artifact_ids", []) if str(item).strip()]
            ))
            evidence = [EvidenceRef(
                kind="artifact", artifact_id=artifact
            ) for artifact in step_artifacts]
            facts = {"query": str(step.parameters.get("query") or plan.original_query)} if evidence else {}
            handoff = {
                "schema_version": "1.0", "source_task": parent_id,
                "target_domain": step.domain, "tenant_id": context.tenant_id,
                "project_id": context.project_id, "actor_id": context.user_id,
                "trace_id": context.trace_id, "correlation_id": context.correlation_id,
                "status": "draft", "requires_evidence_revalidation": True,
                "facts": facts, "evidence_refs": [item.model_dump(mode="json") for item in evidence],
                "risk_summary": [],
            }
            child_specs.append({"step": step.step, "workflow": workflow, "domain": step.domain,
                                "route": step.route, "handoff": handoff, "input_artifact_ids": step_artifacts})

        children: list[dict] = []
        for spec in child_specs:
            child_key = hashlib.sha256((parent_key + ":" + str(spec["step"])).encode()).hexdigest()
            child, _created = await self.submit(
                context, workflow=spec["workflow"], input_artifact_ids=spec["input_artifact_ids"],
                options={"parent_task_id": parent_id, "plan_step": spec["step"], "handoff": spec["handoff"]},
                idempotency_key=child_key,
            )
            children.append({"task_id": child["id"], "step": spec["step"], "workflow": spec["workflow"],
                             "domain": spec["domain"], "route": spec["route"], "handoff": spec["handoff"]})

        parent, created = await self.submit(
            context, workflow="composite", input_artifact_ids=artifacts,
            options={"plan": plan.model_dump(mode="json"), "children": children,
                     "child_task_ids": [item["task_id"] for item in children]},
            idempotency_key=parent_key, task_id=parent_id,
        )
        return parent, children, created

    async def cancel(self, context: RequestContext, task_id: str) -> dict:
        return await self._repository.transition(
            context,
            task_id,
            TaskStatus.CANCELED,
            event_type="task.canceled",
        )

    async def resume(
        self,
        context: RequestContext,
        task_id: str,
        *,
        payload: dict | None = None,
    ) -> dict:
        """Persist a HITL decision and enqueue exactly one resume envelope."""
        if context.role not in {"admin", "project", "reviewer"}:
            raise PolicyFailure("当前用户没有人工审批权限")

        scope = await self._repository.get_workflow_scope(context, task_id)
        workflow = str(scope.get("workflow") or "")
        if workflow in _PROJECT_SCOPED_RESUME_WORKFLOWS:
            request_project_id = str(context.project_id or "").strip()
            stored_project_id = str(scope.get("project_id") or "").strip()
            if not request_project_id:
                raise PolicyFailure(
                    f"{workflow} resume requires a project_id"
                )
            if not stored_project_id:
                raise PolicyFailure(
                    f"{workflow} task is missing its project scope"
                )
            if stored_project_id != request_project_id:
                # Keep the same not-found response used by the repository's
                # normal project filter; do not reveal another project's id.
                raise ResourceNotFound("workflow run not found")

        resume_payload = dict(payload or {})
        decision = str(resume_payload.get("decision") or "").strip().lower()
        if decision not in {"approved", "rejected"}:
            raise ValidationFailure("resume decision must be approved or rejected")
        resume_payload["decision"] = decision
        # The authenticated request context is the source of truth.  A client
        # supplied ``operator`` field must never be able to forge the actor
        # recorded on an approval-bearing artifact.
        resume_payload["operator"] = context.user_id
        resume_payload["operator_role"] = context.role
        approval_action = await self._approval_action(context, task_id, workflow)
        approval_reason = str(resume_payload.get("reason") or "").strip() or None
        if resume_payload.get("decision") == "rejected":
            return await self._repository.transition(
                context,
                task_id,
                TaskStatus.CANCELED,
                event_type="task.canceled",
                stage="hitl",
                payload=resume_payload,
                approval_action=approval_action,
                approval_decision="rejected",
                approval_reason=approval_reason,
                approval_actor_id=context.user_id,
            )
        return await self._repository.resume(
            context,
            task_id,
            payload=resume_payload,
            approver_id=context.user_id,
            approver_role=context.role,
            approval_action=approval_action,
            approval_reason=approval_reason,
        )
