from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.application import wall_pipeline_workflow as workflow
from backend.application.workflow_runtime import ExecutionBudget
from backend.domain.contracts import RequestContext, TaskEnvelope
from backend.domain.errors import DependencyFailure
from backend.engines.wall_pipeline.contracts import (
    ApprovalRecord,
    EvidenceSourceRef,
    LevelConfig,
    RevitConfig,
    RevitResult,
    ReviewGate,
    WallModel,
    WallRecord,
)
from backend.engines.wall_pipeline.io import canonical_sha256, read_artifact, write_artifact


class _FakeArtifactService:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.files: dict[str, Path] = {}
        self.counter = 0

    async def store_file(
        self,
        _context: RequestContext,
        source: Path,
        *,
        kind: str,
        filename: str | None = None,
    ) -> dict:
        self.counter += 1
        artifact_id = f"artifact-{self.counter}"
        target = self.root / f"{artifact_id}-{filename or source.name}"
        shutil.copy2(source, target)
        self.files[artifact_id] = target
        return {"id": artifact_id, "kind": kind, "filename": filename or source.name}

    async def resolve(self, _context: RequestContext, artifact_id: str):
        path = self.files[artifact_id]
        return {"id": artifact_id, "filename": path.name}, path


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="tenant-recovery",
        project_id="project-recovery",
        user_id="operator",
        role="worker",
        trace_id="trace-recovery",
        correlation_id="correlation-recovery",
    )


def _envelope() -> TaskEnvelope:
    return TaskEnvelope(
        task_id="task-recovery",
        tenant_id="tenant-recovery",
        project_id="project-recovery",
        actor_id="operator",
        workflow="wall_pipeline",
        idempotency_key="recovery-idempotency",
        correlation_id="correlation-recovery",
    )


def _approved_model() -> WallModel:
    return WallModel(
        tenant_id="tenant-recovery",
        project_id="project-recovery",
        wall_evidence_sha256="a" * 64,
        coordinate_origin="source_origin",
        level=LevelConfig(id="level-1", name="Main", wall_height_m=3.0),
        walls=[
            WallRecord(
                wall_id="wall-1",
                start_m=(0.0, 0.0),
                end_m=(1.0, 0.0),
                thickness_m=0.2,
                height_m=3.0,
                level_id="level-1",
                evidence_ids=["e-1"],
                source_refs=[
                    EvidenceSourceRef(
                        source_file_id="source-1",
                        entity_id="line-1",
                        locator="line:1",
                    )
                ],
                confidence=1.0,
            )
        ],
        gate=ReviewGate(status="pass", checks={"geometry": True}, metrics={}),
        review_status="approved",
        approval=ApprovalRecord(
            status="approved", actor_id="reviewer", reason="checked"
        ),
        revit=RevitConfig(target_model_path="target.rvt", floor_code="MAIN"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("side_effect_possible", [False, True])
async def test_failure_journal_records_whether_a_bridge_write_was_attempted(
    tmp_path: Path, monkeypatch, side_effect_possible: bool
):
    model = _approved_model()
    model_path = tmp_path / "wall_model.json"
    result_path = tmp_path / "revit_result.json"
    write_artifact(model_path, model)
    result = RevitResult(
        tenant_id=model.tenant_id,
        project_id=model.project_id,
        wall_model_sha256=canonical_sha256(model),
        status="write_approved",
        approval=model.approval,
        readback={"source_model_sha256": "c" * 64},
    )
    write_artifact(result_path, result)
    fake = _FakeArtifactService(tmp_path / "stored")
    monkeypatch.setattr(workflow, "get_artifact_service", lambda: fake)
    runtime_dir = tmp_path / "runtime" / "task-recovery"
    monkeypatch.setattr(workflow, "_runtime_dir", lambda _task_id: runtime_dir)

    ids = await workflow._persist_revit_delivery_failure(
        _context(),
        _envelope(),
        model_path=model_path,
        revit_result_path=result_path,
        artifact_ids={},
        message="Bridge disconnected after execution began",
        side_effect_possible=side_effect_possible,
    )

    failed = read_artifact(runtime_dir / "revit_result.json", RevitResult)
    report = read_artifact(runtime_dir / "audit_report.json", workflow.AuditReport)
    assert failed.status == "failed"
    assert failed.readback["side_effect_possible"] is side_effect_possible
    assert report.status == "blocked"
    assert report.independent_sources is False
    assert set(ids) == {"revit_result.json", "audit_report.json"}


@pytest.mark.asyncio
async def test_delivery_wrapper_marks_side_effect_possible_when_bridge_raises(
    monkeypatch,
):
    state_seen: dict[str, bool] = {}

    async def _failing_impl(*_args, execution_state=None, **_kwargs):
        assert execution_state is not None
        execution_state["attempted"] = True
        raise DependencyFailure("Bridge disconnected after changing working copy")

    async def _record_failure(*_args, side_effect_possible=False, **_kwargs):
        state_seen["side_effect_possible"] = side_effect_possible
        return {"revit_result.json": "failure-result"}

    monkeypatch.setattr(workflow, "_execute_revit_and_audit_impl", _failing_impl)
    monkeypatch.setattr(workflow, "_persist_revit_delivery_failure", _record_failure)

    with pytest.raises(DependencyFailure, match="changing working copy"):
        await workflow._execute_revit_and_audit(
            _context(),
            _envelope(),
            model_path=Path("wall_model.json"),
            revit_result_path=Path("revit_result.json"),
            previous={},
            artifact_ids={},
            budget=ExecutionBudget(),
        )

    assert state_seen == {"side_effect_possible": True}


@pytest.mark.asyncio
async def test_failure_journal_preserves_completed_independent_audit(
    tmp_path: Path, monkeypatch,
):
    model = _approved_model()
    model_path = tmp_path / "wall_model.json"
    result_path = tmp_path / "revit_result.json"
    write_artifact(model_path, model)
    result = RevitResult(
        tenant_id=model.tenant_id,
        project_id=model.project_id,
        wall_model_sha256=canonical_sha256(model),
        status="succeeded",
        transaction_id="tx-1",
        created_element_ids=["1"],
        readback={"wall_count": 1, "column_count": 0},
    )
    write_artifact(result_path, result)
    fake = _FakeArtifactService(tmp_path / "stored")
    monkeypatch.setattr(workflow, "get_artifact_service", lambda: fake)
    runtime_dir = tmp_path / "runtime" / "task-recovery"
    runtime_dir.mkdir(parents=True)
    monkeypatch.setattr(workflow, "_runtime_dir", lambda _task_id: runtime_dir)
    independent = workflow.AuditReport(
        tenant_id=model.tenant_id,
        project_id=model.project_id,
        wall_model_sha256=canonical_sha256(model),
        revit_result_sha256=canonical_sha256(result),
        status="fail",
        independent_sources=True,
        metrics={"source_edge_coverage": 1.0, "revit_edge_precision": 0.945145},
        errors=["Revit edge precision is below the required threshold"],
    )
    write_artifact(runtime_dir / "audit_report.json", independent)

    await workflow._persist_revit_delivery_failure(
        _context(),
        _envelope(),
        model_path=model_path,
        revit_result_path=result_path,
        artifact_ids={},
        message="independent audit failed",
        side_effect_possible=True,
    )

    preserved = read_artifact(
        runtime_dir / "audit_report.json", workflow.AuditReport
    )
    assert preserved.status == "fail"
    assert preserved.independent_sources is True
    assert preserved.metrics["source_edge_coverage"] == 1.0
    assert preserved.metrics["revit_edge_precision"] == 0.945145
