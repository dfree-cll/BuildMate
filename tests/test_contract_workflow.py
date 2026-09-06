from pathlib import Path

import pytest

from backend.application.workflow_runtime import ExecutionBudget
from backend.application.task_service import TaskService
from backend.domain.contracts import RequestContext, TaskEnvelope
from backend.domain.task_plan import TaskPlan
from backend.adapters.task_repository import TaskRepository
from backend.workers.workflows import contract_review_step, get_default_runtime


@pytest.mark.asyncio
async def test_contract_review_returns_evidence_linked_findings(monkeypatch, tmp_path: Path):
    source = tmp_path / "contract.txt"
    source.write_text("项目合同\n双方应按约定交付。", encoding="utf-8")

    class FakeArtifacts:
        async def resolve(self, context, artifact_id):
            return {"filename": source.name}, source

    monkeypatch.setattr("backend.workers.workflows.get_artifact_service", lambda: FakeArtifacts())
    context = RequestContext(
        tenant_id="tenant", project_id="project", user_id="reviewer",
        role="reviewer", trace_id="trace", correlation_id="corr",
    )
    envelope = TaskEnvelope(
        task_id="task_contract", tenant_id="tenant", project_id="project",
        actor_id="reviewer", workflow="contract_review",
        input_artifact_ids=["artifact_1"], idempotency_key="contract-key",
        correlation_id="corr",
    )

    result = await contract_review_step(context, envelope, ExecutionBudget())

    assert result.status == "waiting_human"
    assert result.next_action == "contract_review_decision"
    assert result.findings
    assert all(finding["evidence_refs"] for finding in result.findings)


def test_default_runtime_registers_contract_review():
    runtime = get_default_runtime()
    assert "contract_review" in runtime._workflows  # noqa: SLF001 - registration contract
    assert "composite" in runtime._workflows  # noqa: SLF001 - parent coordination contract


@pytest.mark.asyncio
async def test_submit_composite_creates_durable_parent_and_child_rows():
    context = RequestContext(
        tenant_id="tenant", project_id="project", user_id="user",
        role="project", trace_id="trace", correlation_id="corr",
    )
    plan = TaskPlan.model_validate({
        "mode": "composite",
        "original_query": "审查投标并做合同审核",
        "steps": [{
            "step": 1, "intent": "contract_review", "domain": "contract",
            "route": "workflow.contract_review", "parameters": {"query": "合同审核"},
            "requires_confirmation": True, "missing_parameters": [],
        }],
        "needs_confirmation": True, "needs_clarification": False,
    })
    parent, children, created = await TaskService(TaskRepository()).submit_composite(
        context, plan=plan, input_artifact_ids=["artifact_contract"],
        idempotency_key="composite-contract-test",
    )
    assert created is True
    assert parent["workflow"] == "composite"
    assert children[0]["workflow"] == "contract_review"
