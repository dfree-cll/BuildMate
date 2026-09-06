import asyncio
import shutil
import threading
import uuid
import hashlib
import json
import re
from types import SimpleNamespace
from pathlib import Path

import cv2
import ezdxf
import pytest

from backend.application.wall_pipeline_workflow import (
    _build_config,
    drawing_review_approval_step,
    drawing_review_prepare_step,
    drawing_review_write_step,
    _materialize_task_lineage_snapshot,
    _run_wall_pipeline_isolated,
    wall_pipeline_approve_model_step,
    wall_pipeline_approve_write_step,
    wall_pipeline_prepare_step,
)
from backend.adapters.task_repository import TaskRepository
from backend.application.task_service import TaskService
from backend.application.workflow_runtime import ExecutionBudget
from backend.domain.contracts import AgentResult, RequestContext, TaskEnvelope
from backend.domain.errors import PolicyFailure, ValidationFailure
from backend.engines.wall_pipeline.contracts import SourceManifest
from backend.engines.wall_pipeline.io import canonical_sha256, read_artifact
from backend.workers.workflows import legacy_agent_step
from workers.revit_bridge.wall_compiler import WALL_COMPILER_VERSION


def _write_wall_dxf(path: Path) -> None:
    document = ezdxf.new()
    document.header["$INSUNITS"] = 4
    document.layers.add("GEOMETRY-WALL")
    modelspace = document.modelspace()
    modelspace.add_lwpolyline(
        [(0, 0), (2000, 0), (2000, 200), (0, 200)],
        close=True,
        dxfattribs={"layer": "GEOMETRY-WALL"},
    )
    document.saveas(path)


class _FakeArtifactService:
    def __init__(self, source: Path, root: Path):
        self.source = source
        self.root = root
        self.files: dict[str, Path] = {"source-artifact": source}
        self.counter = 0

    async def resolve(self, _context, artifact_id: str):
        return {"id": artifact_id, "filename": self.files[artifact_id].name}, self.files[artifact_id]

    async def store_file(self, _context, source: Path, *, kind: str, filename: str | None = None):
        self.counter += 1
        artifact_id = f"generated-{self.counter}"
        target = self.root / f"{artifact_id}-{filename or source.name}"
        shutil.copy2(source, target)
        self.files[artifact_id] = target
        return {"id": artifact_id, "kind": kind, "filename": filename or source.name}


class _FakeTaskRepository:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads

    async def list_steps(self, _context, _task_id):
        return [{"output_payload": {"structured_output": payload}} for payload in self.payloads]


async def test_isolated_wall_pipeline_kills_child_when_step_is_cancelled(
    tmp_path, monkeypatch
):
    class _BlockingProcess:
        def __init__(self):
            self.returncode = None
            self.killed = False
            self.done = threading.Event()

        def communicate(self):
            self.done.wait(timeout=5.0)
            return b"", b""

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9
            self.done.set()

    process = _BlockingProcess()
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    config = SimpleNamespace(
        output_dir=str(tmp_path),
        model_dump_json=lambda **_kwargs: "{}",
    )
    task = asyncio.create_task(_run_wall_pipeline_isolated(config))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True


def _context(*, user_id: str = "reviewer", role: str = "project") -> RequestContext:
    trace = uuid.uuid4().hex
    return RequestContext(
        tenant_id="tenant-wall-workflow",
        project_id="project-wall-workflow",
        user_id=user_id,
        role=role,
        trace_id=trace,
        correlation_id=trace,
    )


def _envelope(
    *,
    task_id: str,
    source_id: str,
    resume_payload: dict | None = None,
    target_path: str | Path | None = None,
) -> TaskEnvelope:
    target = str(target_path or "C:/models/project.rvt")
    return TaskEnvelope(
        task_id=task_id,
        tenant_id="tenant-wall-workflow",
        project_id="project-wall-workflow",
        actor_id="submitter",
        workflow="wall_pipeline",
        input_artifact_ids=[source_id],
        idempotency_key="wall-workflow-" + task_id,
        correlation_id=task_id,
        options={
            "wall": {"include_layers": ["GEOMETRY-WALL"]},
            "level": {"id": "level-1", "name": "Main", "wall_height_m": 3.0},
            "revit": {"target_model_path": target, "floor_code": "MAIN"},
        },
        resume_payload=resume_payload or {},
    )


def test_pdf_structural_auto_scope_is_generic_and_keeps_explicit_options(tmp_path):
    workspace = tmp_path / "staged"
    workspace.mkdir()
    source = workspace / "plan.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    config = _build_config(
        _context(),
        source,
        workspace,
        {
            "pdf_layer_mode": "structural_auto",
            "coordinate": {"scale_to_m": 0.035277777777777776},
            "wall": {"min_length_m": 0.5},
            "revit": {
                "target_model_path": str(tmp_path / "model.rvt"),
                "floor_code": "B1",
            },
        },
    )

    assert config.wall.include_layers
    assert config.wall.min_length_m == 0.5
    assert config.column.include_layers
    assert config.grid.include_layers
    assert config.grid.required is True
    assert config.grid.min_length_m == 1.0
    assert config.opening.label_layers
    assert config.opening.boundary_layers
    assert any(re.search(pattern, "") for pattern in config.opening.label_layers)
    assert any(
        re.search(pattern, "S-WALL-HO-TEXT")
        for pattern in config.opening.boundary_layers
    )
    assert not any(
        re.search(pattern, "S-WALL-HO-TEXT")
        for pattern in config.opening.exclude_layers
    )
    assert any(
        re.search(pattern, "xref|S-WALL", re.IGNORECASE)
        for pattern in config.wall.include_layers
    )
    assert any(
        re.search(pattern, "S-AXIS", re.IGNORECASE)
        for pattern in config.grid.include_layers
    )
    assert not any(
        re.search(pattern, "xref|S-WALL-TEXT", re.IGNORECASE)
        for pattern in config.wall.include_layers
    )


def test_floor_code_drives_default_revit_level_lookup(tmp_path):
    workspace = tmp_path / "staged"
    workspace.mkdir()
    source = workspace / "plan.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    config = _build_config(
        _context(),
        source,
        workspace,
        {
            "revit": {
                "target_model_path": str(tmp_path / "model.rvt"),
                "floor_code": "B1",
            },
        },
    )

    assert config.level.id == "level-b1"
    assert config.level.name == "B1"


def test_explicit_level_name_is_not_overridden_by_floor_code(tmp_path):
    workspace = tmp_path / "staged"
    workspace.mkdir()
    source = workspace / "plan.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    config = _build_config(
        _context(),
        source,
        workspace,
        {
            "level": {"id": "level-custom", "name": "地下1层"},
            "revit": {
                "target_model_path": str(tmp_path / "model.rvt"),
                "floor_code": "B1",
            },
        },
    )

    assert config.level.id == "level-custom"
    assert config.level.name == "地下1层"


def test_elevation_range_derives_basement_one_and_wall_height(tmp_path):
    workspace = tmp_path / "staged"
    workspace.mkdir()
    source = workspace / "plan.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    config = _build_config(
        _context(),
        source,
        workspace,
        {
            "level": {"elevation_range": "-6.4~0"},
            "revit": {"target_model_path": str(tmp_path / "model.rvt")},
        },
    )

    assert config.revit.floor_code == "B1"
    assert config.level.id == "level-b1"
    assert config.level.name == "B1"
    assert config.level.elevation_m == -6.4
    assert config.level.top_elevation_m == 0.0
    assert config.level.wall_height_m == 6.4
    assert config.level.elevation_source == "input"


def test_non_first_basement_range_still_requires_explicit_floor_code(tmp_path):
    workspace = tmp_path / "staged"
    workspace.mkdir()
    source = workspace / "plan.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    with pytest.raises(ValidationFailure, match="floor_code"):
        _build_config(
            _context(),
            source,
            workspace,
            {
                "level": {"elevation_range": "-12.8~-6.4"},
                "revit": {"target_model_path": str(tmp_path / "model.rvt")},
            },
        )


def _legacy_envelope(
    *,
    task_id: str,
    source_id: str = "legacy-artifact",
    resume_payload: dict | None = None,
) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=task_id,
        tenant_id="tenant-wall-workflow",
        project_id="project-wall-workflow",
        actor_id="submitter",
        workflow="drawing_review",
        input_artifact_ids=[source_id],
        options={},
        idempotency_key="legacy-workflow-" + task_id,
        correlation_id=task_id,
        resume_payload=resume_payload or {},
    )


async def test_wall_workflow_persists_artifacts_and_requires_two_approvals(tmp_path, monkeypatch):
    source = tmp_path / "plan.dxf"
    _write_wall_dxf(source)
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_artifact_service",
        lambda: fake_artifacts,
    )

    context = _context()
    task_id = "task-wall-workflow"
    target_path = tmp_path / "project.rvt"
    target_path.write_bytes(b"deterministic-rvt-fixture")
    first = await wall_pipeline_prepare_step(
        context,
        _envelope(task_id=task_id, source_id="source-artifact", target_path=target_path),
        ExecutionBudget(),
    )
    assert first.status == "waiting_human"
    assert first.next_action == "approve_wall_model"
    assert first.structured_output["pipeline"] == "wall_pipeline"
    assert "wall_model.json" in first.structured_output["artifact_ids"]

    model_payload = first.structured_output
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.TaskRepository",
        lambda: _FakeTaskRepository([model_payload]),
    )
    second = await wall_pipeline_approve_model_step(
        context,
        _envelope(
            task_id=task_id,
            source_id="source-artifact",
            target_path=target_path,
            resume_payload={"decision": "approved", "operator": "reviewer", "reason": "verified"},
        ),
        ExecutionBudget(),
    )
    assert second.status == "waiting_human"
    assert second.next_action == "approve_revit_write"
    assert second.structured_output["revit_result"]["status"] == "dry_run_passed"

    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.TaskRepository",
        lambda: _FakeTaskRepository([model_payload, second.structured_output]),
    )
    third = await wall_pipeline_approve_write_step(
        _context(user_id="admin", role="admin"),
        _envelope(
            task_id=task_id,
            source_id="source-artifact",
            target_path=target_path,
            resume_payload={"decision": "approved", "operator": "admin", "reason": "release"},
        ),
        ExecutionBudget(),
    )
    assert third.status == "succeeded"
    assert third.next_action == "revit_bridge_execution"
    assert third.structured_output["revit_result"]["status"] == "write_approved"
    assert third.structured_output["external_side_effect"] is False


async def test_delivery_snapshot_keeps_source_replayable_after_temp_workspace_removal(
    tmp_path, monkeypatch
):
    """A delivery result must retain source bytes, not only temp manifest paths."""

    source = tmp_path / "plan.dxf"
    _write_wall_dxf(source)
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_artifact_service",
        lambda: fake_artifacts,
    )
    context = _context()
    prepared = await wall_pipeline_prepare_step(
        context,
        _envelope(task_id="task-durable-snapshot", source_id="source-artifact"),
        ExecutionBudget(),
    )
    artifact_ids = prepared.structured_output["artifact_ids"]
    task_dir = tmp_path / "durable" / "task-durable-snapshot"
    lineage_paths, updated_ids, errors = await _materialize_task_lineage_snapshot(
        context,
        task_dir,
        artifact_ids,
        source_artifact_ids=["source-artifact"],
        strict=True,
    )

    assert errors == []
    assert set(lineage_paths) == {
        "source_manifest.json", "source_entities.json", "wall_evidence.json",
    }
    manifest = read_artifact(lineage_paths["source_manifest.json"], SourceManifest)
    assert manifest.sources[0].path.startswith(str(task_dir / "sources"))
    assert Path(manifest.sources[0].path).is_file()
    # Rewriting an operational path must not break the contract hash used by
    # SourceEntities and WallEvidence.
    original_manifest = read_artifact(
        fake_artifacts.files[artifact_ids["source_manifest.json"]], SourceManifest
    )
    assert canonical_sha256(manifest) == canonical_sha256(original_manifest)
    assert updated_ids["source_manifest.json"] in fake_artifacts.files


async def test_drawing_review_selects_wall_pipeline_for_dxf(tmp_path, monkeypatch):
    source = tmp_path / "plan.dxf"
    _write_wall_dxf(source)
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_artifact_service",
        lambda: fake_artifacts,
    )
    result = await drawing_review_prepare_step(
        _context(), _envelope(task_id="task-drawing-review", source_id="source-artifact"), ExecutionBudget()
    )
    assert result.status == "waiting_human"
    assert result.structured_output["pipeline"] == "wall_pipeline"


async def test_drawing_review_legacy_option_cannot_bypass_wall_pipeline(tmp_path, monkeypatch):
    """A client cannot route a PDF/DWG/DXF through the weaker legacy Agent."""

    source = tmp_path / "plan.dxf"
    _write_wall_dxf(source)
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_artifact_service",
        lambda: fake_artifacts,
    )
    envelope = _legacy_envelope(task_id="task-legacy-bypass", source_id="source-artifact").model_copy(
        update={"options": {"engine": "legacy"}}
    )

    with pytest.raises(ValidationFailure, match="cannot bypass"):
        await drawing_review_prepare_step(_context(), envelope, ExecutionBudget())


async def test_legacy_agent_adapter_rejects_wall_sources_directly(tmp_path, monkeypatch):
    """The defense-in-depth adapter guard also rejects a direct PDF call."""

    source = tmp_path / "plan.pdf"
    source.write_bytes(b"%PDF-1.7")
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    monkeypatch.setattr(
        "backend.workers.workflows.get_artifact_service",
        lambda: fake_artifacts,
    )

    with pytest.raises(ValidationFailure, match="use wall_pipeline"):
        await legacy_agent_step(
            _context(),
            _legacy_envelope(task_id="task-direct-legacy-pdf", source_id="source-artifact"),
            ExecutionBudget(),
        )


async def test_drawing_review_keeps_ifc_image_legacy_compatibility(tmp_path, monkeypatch):
    """IFC/image remains on Drawing2BIM, with a deterministic pending stage."""

    source = tmp_path / "existing.ifc"
    source.write_text("ISO-10303-21;", encoding="utf-8")
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    fake_artifacts.files["legacy-artifact"] = source
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_artifact_service",
        lambda: fake_artifacts,
    )

    async def fake_legacy_agent(_context, envelope, _budget):
        return AgentResult(
            status="waiting_human",
            answer="",
            artifact_ids=envelope.input_artifact_ids,
        )

    monkeypatch.setattr(
        "backend.workers.workflows.legacy_agent_step", fake_legacy_agent
    )
    result = await drawing_review_prepare_step(
        _context(), _legacy_envelope(task_id="task-legacy-ifc"), ExecutionBudget()
    )

    assert result.status == "waiting_human"
    assert result.structured_output["pipeline"] == "legacy_drawing2bim"
    assert result.structured_output["stage"] == "legacy_review_pending"


async def test_legacy_drawing_review_approval_and_write_enforce_state_and_actor(
    monkeypatch,
):
    """Legacy resume messages need explicit decision, actor, and legal role."""

    pending = {
        "pipeline": "legacy_drawing2bim",
        "stage": "legacy_review_pending",
        "requires_human_review": True,
        "artifact_ids": ["legacy-artifact"],
    }
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.TaskRepository",
        lambda: _FakeTaskRepository([pending]),
    )
    base = _legacy_envelope(task_id="task-legacy-approval")

    with pytest.raises(ValidationFailure, match="requires an approved decision"):
        await drawing_review_approval_step(
            _context(), base, ExecutionBudget()
        )
    with pytest.raises(ValidationFailure, match="requires an operator"):
        await drawing_review_approval_step(
            _context(),
            base.model_copy(update={"resume_payload": {
                "decision": "approved", "operator_role": "project",
            }}),
            ExecutionBudget(),
        )
    with pytest.raises(ValidationFailure, match="requires an operator role"):
        await drawing_review_approval_step(
            _context(),
            base.model_copy(update={"resume_payload": {
                "decision": "approved", "operator": "reviewer",
            }}),
            ExecutionBudget(),
        )
    with pytest.raises(PolicyFailure, match="身份"):
        await drawing_review_approval_step(
            _context(),
            base.model_copy(update={"resume_payload": {
                "decision": "approved", "operator": "attacker",
                "operator_role": "project",
            }}),
            ExecutionBudget(),
        )
    with pytest.raises(PolicyFailure, match="有效的人工审批角色"):
        await drawing_review_approval_step(
            _context(),
            base.model_copy(update={"resume_payload": {
                "decision": "approved", "operator": "reviewer",
                "operator_role": "worker",
            }}),
            ExecutionBudget(),
        )

    approved = await drawing_review_approval_step(
        _context(),
        base.model_copy(update={"resume_payload": {
            "decision": "approved", "operator": "reviewer",
            "operator_role": "project", "reason": "reviewed",
        }}),
        ExecutionBudget(),
    )
    assert approved.structured_output["stage"] == "legacy_review_approved"
    assert approved.structured_output["approval"]["actor_id"] == "reviewer"

    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.TaskRepository",
        lambda: _FakeTaskRepository([approved.structured_output]),
    )
    completed = await drawing_review_write_step(
        _context(),
        base.model_copy(update={"resume_payload": {
            "decision": "approved", "operator": "reviewer",
            "operator_role": "project", "reason": "write reviewed",
        }}),
        ExecutionBudget(),
    )
    assert completed.structured_output["stage"] == "legacy_complete"
    assert completed.structured_output["write_approval"]["actor_id"] == "reviewer"

    # A tampered/early stage cannot be promoted by the write handler.
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.TaskRepository",
        lambda: _FakeTaskRepository([pending]),
    )
    with pytest.raises(ValidationFailure, match="approved or auto-passed"):
        await drawing_review_write_step(
            _context(),
            base.model_copy(update={"resume_payload": {
                "decision": "approved", "operator": "reviewer",
                "operator_role": "project",
            }}),
            ExecutionBudget(),
        )


async def test_wall_workflow_stages_multiple_source_artifacts_with_roles(tmp_path, monkeypatch):
    source = tmp_path / "plan.dxf"
    supplement = tmp_path / "structural-supplement.dxf"
    _write_wall_dxf(source)
    _write_wall_dxf(supplement)
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    fake_artifacts.files["supplement-artifact"] = supplement
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_artifact_service",
        lambda: fake_artifacts,
    )
    target_path = tmp_path / "project.rvt"
    target_path.write_bytes(b"deterministic-rvt-fixture")
    envelope = _envelope(
        task_id="task-multi-source",
        source_id="source-artifact",
        target_path=target_path,
    ).model_copy(update={
        "input_artifact_ids": ["source-artifact", "supplement-artifact"],
        "options": {
            **_envelope(
                task_id="task-multi-source",
                source_id="source-artifact",
                target_path=target_path,
            ).options,
            "source_roles": ["plan", "structural_supplement"],
        },
    })

    result = await wall_pipeline_prepare_step(
        _context(), envelope, ExecutionBudget()
    )

    assert result.status == "waiting_human"
    assert result.structured_output["source_artifact_ids"] == [
        "source-artifact", "supplement-artifact"
    ]
    manifest_id = result.structured_output["artifact_ids"]["source_manifest.json"]
    manifest = json.loads(
        fake_artifacts.files[manifest_id].read_text(encoding="utf-8")
    )
    assert len(manifest["sources"]) == 2
    assert [item["role"] for item in manifest["sources"]] == [
        "plan", "structural_supplement"
    ]


async def test_wall_workflow_rejects_grid_frame_for_unparsed_page(tmp_path, monkeypatch):
    """The approval gate must not accept a fabricated PDF page frame."""

    source = tmp_path / "plan.dxf"
    _write_wall_dxf(source)
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_artifact_service",
        lambda: fake_artifacts,
    )
    target_path = tmp_path / "project.rvt"
    target_path.write_bytes(b"deterministic-rvt-fixture")
    envelope = _envelope(
        task_id="task-invalid-grid-frame",
        source_id="source-artifact",
        target_path=target_path,
    ).model_copy(update={
        "options": {
            **_envelope(
                task_id="task-invalid-grid-frame",
                source_id="source-artifact",
                target_path=target_path,
            ).options,
            "grid": {
                "axes": [{
                    "label": "A",
                    "start": [0.0, 0.0],
                    "end": [0.0, 2000.0],
                    "source_file_id": "source_0001",
                }],
            },
        },
    })
    first = await wall_pipeline_prepare_step(
        _context(), envelope, ExecutionBudget()
    )
    model_id = first.structured_output["artifact_ids"]["wall_model.json"]
    model_path = fake_artifacts.files[model_id]
    model_payload = json.loads(model_path.read_text(encoding="utf-8"))
    assert model_payload["grid"][0]["source_file_id"] == "source_0001"
    # Preserve the evidence hash so the test reaches the frame registry check,
    # rather than failing earlier on the expected model→evidence link.
    model_payload["grid"][0]["frame_id"] = "source_0001:page:9999"
    model_path.write_text(json.dumps(model_payload), encoding="utf-8")
    # Keep the pending result bound to this (otherwise unchanged) model so the
    # assertion exercises the exact frame registry rather than hash binding.
    from backend.engines.wall_pipeline.contracts import WallModel
    from backend.engines.wall_pipeline.io import canonical_sha256

    tampered_model = WallModel.model_validate(model_payload)
    result_id = first.structured_output["artifact_ids"]["revit_result.json"]
    result_path = fake_artifacts.files[result_id]
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    result_payload["wall_model_sha256"] = canonical_sha256(tampered_model)
    result_path.write_text(json.dumps(result_payload), encoding="utf-8")

    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.TaskRepository",
        lambda: _FakeTaskRepository([first.structured_output]),
    )
    with pytest.raises(ValidationFailure, match="unknown frame_id"):
        await wall_pipeline_approve_model_step(
            _context(),
            envelope.model_copy(update={
                "resume_payload": {
                    "decision": "approved",
                    "operator": "reviewer",
                    "reason": "verified",
                },
            }),
            ExecutionBudget(),
        )


async def test_wall_workflow_binds_model_to_pending_revit_result_hash(
    tmp_path, monkeypatch
):
    """A substituted WallModel cannot pass the first human approval gate."""

    source = tmp_path / "plan.dxf"
    _write_wall_dxf(source)
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_artifact_service",
        lambda: fake_artifacts,
    )
    target_path = tmp_path / "project.rvt"
    target_path.write_bytes(b"deterministic-rvt-fixture")
    envelope = _envelope(
        task_id="task-model-hash-binding",
        source_id="source-artifact",
        target_path=target_path,
    )
    first = await wall_pipeline_prepare_step(
        _context(), envelope, ExecutionBudget()
    )
    model_id = first.structured_output["artifact_ids"]["wall_model.json"]
    model_path = fake_artifacts.files[model_id]
    model_payload = json.loads(model_path.read_text(encoding="utf-8"))
    assert model_payload["walls"]
    # Keep all evidence/source references intact while changing engineering
    # geometry.  The pending RevitResult retains the original model hash.
    model_payload["walls"][0]["start_m"] = [0.123, 0.0]
    model_path.write_text(json.dumps(model_payload), encoding="utf-8")

    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.TaskRepository",
        lambda: _FakeTaskRepository([first.structured_output]),
    )
    with pytest.raises(ValidationFailure, match="initial Revit result does not reference"):
        await wall_pipeline_approve_model_step(
            _context(),
            envelope.model_copy(update={
                "resume_payload": {
                    "decision": "approved",
                    "operator": "reviewer",
                    "reason": "verified",
                },
            }),
            ExecutionBudget(),
        )


async def test_wall_workflow_can_execute_bridge_and_independent_audit(tmp_path, monkeypatch):
    source = tmp_path / "plan.dxf"
    _write_wall_dxf(source)
    target_path = tmp_path / "project.rvt"
    target_path.write_bytes(b"deterministic-rvt-fixture")
    fake_artifacts = _FakeArtifactService(source, tmp_path / "stored")
    fake_artifacts.root.mkdir()
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_artifact_service",
        lambda: fake_artifacts,
    )
    task_dir = tmp_path / "task-runtime"
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow._runtime_dir",
        lambda _task_id: task_dir,
    )

    first = await wall_pipeline_prepare_step(
        _context(),
        _envelope(
            task_id="task-bridge", source_id="source-artifact",
            target_path=target_path,
        ),
        ExecutionBudget(),
    )
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.TaskRepository",
        lambda: _FakeTaskRepository([first.structured_output]),
    )
    second = await wall_pipeline_approve_model_step(
        _context(),
        _envelope(
            task_id="task-bridge", source_id="source-artifact",
            target_path=target_path,
            resume_payload={"decision": "approved", "operator": "reviewer", "reason": "verified"},
        ),
        ExecutionBudget(),
    )
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.TaskRepository",
        lambda: _FakeTaskRepository([first.structured_output, second.structured_output]),
    )

    source_render_id = first.structured_output["artifact_ids"]["source_render.png"]
    source_render = fake_artifacts.files[source_render_id]
    actual_view = tmp_path / "bridge" / "actual.png"
    actual_view.parent.mkdir()
    image = cv2.imread(str(source_render), cv2.IMREAD_COLOR)
    assert image is not None
    image[0, 0] = (1, 2, 3)
    assert cv2.imwrite(str(actual_view), image)
    output_model = tmp_path / "bridge" / "output.rvt"
    output_model.write_bytes(b"fake-rvt-output")
    digest = hashlib.sha256(actual_view.read_bytes()).hexdigest()
    output_digest = hashlib.sha256(output_model.read_bytes()).hexdigest()

    class _FakeBridge:
        async def run_wall_model(self, request):
            assert request.dry_run is False
            assert request.revit_result["status"] == "write_approved"
            from backend.engines.wall_pipeline.contracts import WallModel
            from backend.engines.wall_pipeline.io import canonical_sha256
            from workers.pyrevit.wall_model_contract import normalize_wall_model_payload
            from workers.revit_bridge.wall_compiler import compiled_plan_sha256

            model = WallModel.model_validate(request.wall_model)
            compiled = normalize_wall_model_payload(request.wall_model)
            model_hash = canonical_sha256(model)
            plan_hash = compiled_plan_sha256(compiled)
            target = str(target_path.resolve()).replace("/", "\\")
            return SimpleNamespace(
                status="executed", build_id=request.build_id,
                message="ok", output_path=str(output_model),
                details={
                    "tenant_id": model.tenant_id,
                    "project_id": model.project_id,
                    "wall_model_sha256": model_hash,
                    "compiler": WALL_COMPILER_VERSION,
                    "compiled_plan_sha256": plan_hash,
                    "source_model_sha256": request.source_model_sha256,
                    "target_model_path": target,
                    "convergence_round": request.convergence_round,
                    "before_sha256": request.source_model_sha256,
                    "after_sha256": output_digest,
                    "output_path": str(output_model),
                    "actual_view_path": str(actual_view),
                    "actual_view_sha256": digest,
                        "wall_count": 1,
                        "column_count": 0,
                        "grid_count": 0,
                        "opening_count": 0,
                        "matched_opening_count": 0,
                        "opening_host_count": 0,
                        "opening_semantics": {
                            "opening_count": 0,
                            "matched_opening_count": 0,
                            "host_wall_count": 0,
                            "marks": [],
                            "vertical_cut_status": (
                                "not_requested_without_sill_and_head_evidence"
                            ),
                        },
                        "created_element_ids": ["revit-wall-1"],
                        "readback": {
                            "wall_count": 1,
                            "column_count": 0,
                            "grid_count": 0,
                            "opening_count": 0,
                            "matched_opening_count": 0,
                            "opening_host_count": 0,
                            "opening_semantics": {
                                "opening_count": 0,
                                "matched_opening_count": 0,
                                "host_wall_count": 0,
                                "marks": [],
                                "vertical_cut_status": (
                                    "not_requested_without_sill_and_head_evidence"
                                ),
                            },
                        "source_model_sha256": request.source_model_sha256,
                        "wall_model_sha256": model_hash,
                        "compiler": WALL_COMPILER_VERSION,
                        "compiled_plan_sha256": plan_hash,
                        "target_model_path": target,
                        "tenant_id": model.tenant_id,
                        "project_id": model.project_id,
                        "after_sha256": output_digest,
                        "output_path": str(output_model),
                        "actual_view_path": str(actual_view),
                        "actual_view_sha256": digest,
                        "topology": {
                            "junction_count": 0,
                            "joined_pairs": 0,
                            "kinds": [],
                            "junctions": [],
                        },
                    },
                    "topology": {
                        "junction_count": 0,
                        "joined_pairs": 0,
                        "kinds": [],
                        "junctions": [],
                    },
                    "coordinate_transform": {
                        "offset_x_mm": 0.0,
                        "offset_y_mm": 0.0,
                        "angle_rad": 0.0,
                    },
                },
            )

        async def present_wall_model(self, request):
            assert request.output_sha256 == output_digest
            return SimpleNamespace(
                status="presented",
                build_id=request.build_id,
                output_path=str(output_model),
                details={
                    "output_sha256": output_digest,
                    "view_name": "BM-ACTUAL-MAIN",
                    "visible_counts": {"walls": 1, "columns": 0, "grids": 0},
                },
            )

    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.RevitBridgeClient",
        lambda: _FakeBridge(),
    )
    monkeypatch.setattr(
        "backend.application.wall_pipeline_workflow.get_settings",
        lambda: SimpleNamespace(revit_bridge_approval_secret="s" * 32),
    )
    envelope = _envelope(
        task_id="task-bridge", source_id="source-artifact",
        target_path=target_path,
        resume_payload={"decision": "approved", "operator": "admin", "reason": "release"},
    ).model_copy(update={
        "options": {
            **_envelope(
                task_id="task-bridge", source_id="source-artifact",
                target_path=target_path,
            ).options,
            "execute_revit": True,
        },
    })
    third = await wall_pipeline_approve_write_step(
        _context(user_id="admin", role="admin"), envelope, ExecutionBudget()
    )
    assert third.status == "succeeded"
    assert third.next_action == "delivery_complete"
    assert third.structured_output["revit_result"]["status"] == "succeeded"
    assert third.structured_output["audit_report"]["status"] == "pass"
    assert third.structured_output["external_side_effect"] is True
    assert third.structured_output["revit_presentation"]["view_name"] == "BM-ACTUAL-MAIN"


async def test_wall_workflow_options_are_persisted_with_the_task():
    context = _context()
    key = "wall-options-" + uuid.uuid4().hex
    run, created = await TaskService(TaskRepository()).submit(
        context,
        workflow="wall_pipeline",
        input_artifact_ids=["source-artifact"],
        options={"revit": {"floor_code": "MAIN"}, "coordinate": {"scale_to_m": 0.001}},
        idempotency_key=key,
    )
    assert created is True
    assert run["options"]["revit"]["floor_code"] == "MAIN"
    repeated, repeated_created = await TaskService(TaskRepository()).submit(
        context,
        workflow="wall_pipeline",
        input_artifact_ids=["source-artifact"],
        options={"revit": {"floor_code": "MAIN"}, "coordinate": {"scale_to_m": 0.001}},
        idempotency_key=key,
    )
    assert repeated_created is False
    assert repeated["options"]["coordinate"]["scale_to_m"] == 0.001
