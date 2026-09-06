"""Bounded, durable workflow runtime shared by local and RabbitMQ workers."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import ValidationError

from backend.adapters.task_repository import TaskRepository
from backend.domain.contracts import AgentResult, RequestContext, TaskEnvelope, TaskFailureType, TaskStatus
from backend.domain.errors import DependencyFailure, PolicyFailure, ValidationFailure
from backend.core.metrics import TASK_RESULTS, TASK_STEP_LATENCY


StepHandler = Callable[[RequestContext, TaskEnvelope, "ExecutionBudget"], Awaitable[AgentResult | dict]]


@dataclass(frozen=True)
class WorkflowStepDefinition:
    name: str
    handler: StepHandler
    timeout_seconds: float = 120.0
    max_attempts: int = 2


class ExecutionBudget:
    def __init__(self, max_tool_calls: int = 12) -> None:
        self.max_tool_calls = max_tool_calls
        self.tool_calls = 0

    def consume_tool_call(self, tool_name: str) -> None:
        if self.tool_calls >= self.max_tool_calls:
            raise PolicyFailure(
                f"tool-call budget exceeded before invoking {tool_name}"
            )
        self.tool_calls += 1


class WorkflowRuntime:
    def __init__(self, repository: TaskRepository, *, max_steps: int = 20, max_tool_calls: int = 12):
        self._repository = repository
        self._workflows: dict[str, tuple[WorkflowStepDefinition, ...]] = {}
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls

    def register(self, name: str, steps: list[WorkflowStepDefinition]) -> None:
        if not steps:
            raise ValueError("workflow must have at least one step")
        if len(steps) > self._max_steps:
            raise ValueError(f"workflow exceeds {self._max_steps} steps")
        if name in self._workflows:
            raise ValueError(f"workflow already registered: {name}")
        self._workflows[name] = tuple(steps)

    async def execute(self, envelope: TaskEnvelope) -> dict:
        resume_role = str((envelope.resume_payload or {}).get("operator_role") or "").strip()
        # Resume roles are persisted by TaskService after API authorization.
        # Keep ordinary queue execution under the worker role, while allowing
        # approval handlers to inspect the authenticated approver role.
        context = RequestContext(
            tenant_id=envelope.tenant_id,
            project_id=envelope.project_id,
            user_id=envelope.actor_id,
            role=resume_role or "worker",
            trace_id=envelope.correlation_id,
            correlation_id=envelope.correlation_id,
        )
        from backend.db.rls import activate_rls_context
        activate_rls_context(context)
        current = await self._repository.get(context, envelope.task_id)
        if current["terminal"] or current["status"] == "waiting_human":
            return current
        steps = self._workflows.get(envelope.workflow)
        if steps is None:
            return await self._fail(
                context, envelope.task_id, TaskFailureType.VALIDATION,
                f"workflow is not registered: {envelope.workflow}",
            )

        resuming = current["status"] == TaskStatus.RESUMED.value
        await self._repository.transition(
            context, envelope.task_id, TaskStatus.RUNNING,
            event_type="task.running", stage=("hitl.resume" if resuming else steps[0].name),
            payload={"resumed": resuming} if resuming else None,
        )
        budget = ExecutionBudget(self._max_tool_calls)
        last_result: AgentResult | None = None
        existing_steps = await self._repository.list_steps(context, envelope.task_id)
        completed_positions = {
            int(step["position"]): step for step in existing_steps
            if step["status"] == "succeeded"
        }
        resume_position = 1
        if resuming:
            waiting_positions = [
                int(step["position"]) for step in existing_steps
                if step["status"] == "waiting_human"
            ]
            resume_position = max(waiting_positions, default=0) + 1
        for position, definition in enumerate(steps, start=1):
            if position < resume_position or position in completed_positions:
                continue
            for attempt in range(1, max(1, definition.max_attempts) + 1):
                step_id = await self._repository.begin_step(
                    context, envelope.task_id, name=definition.name,
                    position=position,
                    input_payload={
                        "artifact_ids": envelope.input_artifact_ids,
                        "resume_payload": envelope.resume_payload,
                    },
                    attempt=attempt,
                )
                try:
                    step_started = time.perf_counter()
                    raw = await asyncio.wait_for(
                        definition.handler(context, envelope, budget),
                        timeout=max(0.1, definition.timeout_seconds),
                    )
                    last_result = raw if isinstance(raw, AgentResult) else AgentResult.model_validate(raw)
                    if last_result.status not in {"succeeded", "waiting_human"}:
                        raise ValidationFailure(
                            f"step returned unsupported status: {last_result.status}"
                        )
                    await self._repository.finish_step(
                        context, step_id,
                        status=("waiting_human" if last_result.status == "waiting_human" else "succeeded"),
                        output_payload=last_result.model_dump(mode="json"),
                    )
                    TASK_STEP_LATENCY.labels(
                        envelope.workflow, definition.name
                    ).observe(time.perf_counter() - step_started)
                    if last_result.status == "waiting_human":
                        return await self._repository.transition(
                            context, envelope.task_id, TaskStatus.WAITING_HUMAN,
                            event_type="task.waiting_human", stage=definition.name,
                            payload={"next_action": last_result.next_action},
                            result=last_result.model_dump(mode="json"),
                        )
                    break
                except Exception as exc:
                    failure_type, retryable, message = self._classify_step_failure(
                        exc, definition
                    )
                    await self._repository.finish_step(
                        context,
                        step_id,
                        status="failed",
                        error_message=message[:1000],
                    )
                    if not retryable or attempt >= definition.max_attempts:
                        return await self._fail(
                            context,
                            envelope.task_id,
                            failure_type,
                            message,
                            result=self._failure_result(exc),
                        )

        if last_result is None:
            # A final HITL gate may intentionally be the last step.  Resuming
            # it completes the workflow decision, but does not imply that an
            # external side effect (such as Revit write) happened.
            return await self._repository.transition(
                context, envelope.task_id, TaskStatus.SUCCEEDED,
                event_type="task.succeeded", stage="hitl.resume",
                result=current.get("result") or {"status": "succeeded", "answer": "HITL decision recorded"},
                payload={"resumed": True, "external_side_effect": False},
            )
        assert last_result is not None
        completed = await self._repository.transition(
            context, envelope.task_id, TaskStatus.SUCCEEDED,
            event_type="task.succeeded", stage=steps[-1].name,
            result=last_result.model_dump(mode="json"),
            payload={"tool_calls": budget.tool_calls},
        )
        TASK_RESULTS.labels(envelope.workflow, "succeeded", "").inc()
        return completed

    async def _fail(
        self,
        context: RequestContext,
        task_id: str,
        failure_type: TaskFailureType,
        message: str,
        *,
        result: dict | None = None,
    ) -> dict:
        failed = await self._repository.transition(
            context, task_id, TaskStatus.FAILED,
            event_type="task.failed", failure_type=failure_type,
            error_message=(message or failure_type.value)[:1000],
            result=result,
        )
        # Keep failure_type as a bounded label (the enum is closed).
        TASK_RESULTS.labels(failed["workflow"], "failed", failure_type.value).inc()
        return failed

    @staticmethod
    def _failure_result(exc: Exception) -> dict:
        """Expose durable recovery artifacts without replacing the exception."""

        result = {
            "status": "failed",
            "answer": str(exc)[:1000] or "workflow step failed",
        }
        artifact_ids = getattr(exc, "failure_artifact_ids", None)
        if isinstance(artifact_ids, dict) and artifact_ids:
            result["artifact_ids"] = list(dict.fromkeys(
                str(value) for value in artifact_ids.values() if value
            ))
            result["structured_output"] = {
                "failure_artifact_ids": artifact_ids,
                "recovery": "inspect artifacts before retrying external delivery",
            }
        return result

    @staticmethod
    def _classify_step_failure(
        exc: Exception,
        definition: WorkflowStepDefinition,
    ) -> tuple[TaskFailureType, bool, str]:
        """Map a step exception to its durable failure contract and retry policy."""

        if isinstance(exc, asyncio.TimeoutError):
            return (
                TaskFailureType.TIMEOUT,
                True,
                f"{definition.name} timed out after {definition.timeout_seconds:.0f}s",
            )
        if isinstance(exc, (ValidationError, ValidationFailure)):
            return TaskFailureType.VALIDATION, False, str(exc)
        if isinstance(exc, PolicyFailure):
            return TaskFailureType.POLICY, False, str(exc)
        if isinstance(exc, DependencyFailure):
            return TaskFailureType.DEPENDENCY, True, str(exc)
        return TaskFailureType.SYSTEM, True, str(exc)
