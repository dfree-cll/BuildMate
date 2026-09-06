from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.domain.approval_token import approval_digest, issue_approval_token
from backend.domain.errors import PolicyFailure
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
from backend.engines.wall_pipeline.io import canonical_sha256, file_sha256
from workers.revit_bridge.contracts import RunWallModelRequest
from workers.revit_bridge.service import RevitBridgeService
from workers.revit_bridge.service import (
    _read_delivery_receipt,
    _receipt_artifact_path,
    _receipt_detail_paths_are_scoped,
    _write_delivery_receipt,
)
from backend.domain.approval_token import normalize_target_path


def _hash(seed: str) -> str:
    return (seed * 64)[:64]


def test_delivery_receipt_round_trip_binds_target_and_scopes_details(
    tmp_path: Path,
):
    work_dir = tmp_path / "build"
    work_dir.mkdir()
    output = work_dir / "working_copy.rvt"
    view = work_dir / "actual.png"
    output.write_bytes(b"rvt")
    view.write_bytes(b"png")
    receipt_path = work_dir / "delivery_receipt.json"

    _write_delivery_receipt(
        receipt_path,
        wall_model_sha256=_hash("a"),
        source_model_sha256=_hash("b"),
        approval_digest=_hash("c"),
        target_model_path=tmp_path / "target.rvt",
        output_path=output,
        actual_view_path=view,
        after_sha256=_hash("d"),
        actual_view_sha256=_hash("e"),
        details={"readback": {"output_rvt_path": str(output.resolve())}},
    )

    receipt = _read_delivery_receipt(receipt_path)
    assert receipt is not None
    assert receipt["target_model_path"] == normalize_target_path(
        str(tmp_path / "target.rvt")
    )
    assert _receipt_artifact_path(
        receipt["output_path"], work_dir, suffix=".rvt"
    ) == output.resolve()
    assert _receipt_artifact_path(
        receipt["actual_view_path"], work_dir, suffix=".png"
    ) == view.resolve()
    assert _receipt_detail_paths_are_scoped(receipt["details"], work_dir)


def test_legacy_or_malformed_receipts_are_treated_as_stale(tmp_path: Path):
    receipt_path = tmp_path / "delivery_receipt.json"
    # Legacy receipts have no target binding and must never be replayed.
    receipt_path.write_text(
        json.dumps({
            "wall_model_sha256": _hash("a"),
            "source_model_sha256": _hash("b"),
            "approval_digest": _hash("c"),
            "output_path": str(tmp_path / "working_copy.rvt"),
            "actual_view_path": str(tmp_path / "actual.png"),
            "after_sha256": _hash("d"),
            "actual_view_sha256": _hash("e"),
            "details": {},
        }),
        encoding="utf-8",
    )
    assert _read_delivery_receipt(receipt_path) is None

    receipt_path.write_text("{\"target_model_path\": 123}", encoding="utf-8")
    assert _read_delivery_receipt(receipt_path) is None


def test_modern_receipt_with_tampered_details_is_rejected(tmp_path: Path):
    """A receipt with an integrity field must not silently trigger a rerun."""

    work_dir = tmp_path / "build"
    work_dir.mkdir()
    receipt_path = work_dir / "delivery_receipt.json"
    _write_delivery_receipt(
        receipt_path,
        wall_model_sha256=_hash("a"),
        source_model_sha256=_hash("b"),
        approval_digest=_hash("c"),
        target_model_path=tmp_path / "target.rvt",
        output_path=work_dir / "working_copy.rvt",
        actual_view_path=work_dir / "actual.png",
        after_sha256=_hash("d"),
        actual_view_sha256=_hash("e"),
        details={"output_path": str(work_dir / "working_copy.rvt")},
    )
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["details"]["output_path"] = str(work_dir / "tampered.rvt")
    # Keep the original digest to model an on-disk mutation/corruption.
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyFailure, match="detail integrity"):
        _read_delivery_receipt(receipt_path)


def test_receipt_rejects_paths_outside_build_workspace(tmp_path: Path):
    work_dir = tmp_path / "build"
    work_dir.mkdir()
    outside = tmp_path / "outside.rvt"
    assert not _receipt_detail_paths_are_scoped(
        {"readback": {"output_rvt_path": str(outside)}}, work_dir
    )
    assert _receipt_artifact_path(
        str(outside), work_dir, suffix=".rvt"
    ) is None


def _model(target: Path) -> WallModel:
    return WallModel(
        tenant_id="tenant-receipt",
        project_id="project-receipt",
        wall_evidence_sha256="a" * 64,
        coordinate_origin="source_origin",
        level=LevelConfig(id="level-1", name="Main"),
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
        revit=RevitConfig(target_model_path=str(target), floor_code="MAIN"),
    )


@pytest.mark.asyncio
async def test_receipt_cannot_replay_same_bytes_for_a_different_target_path(
    tmp_path: Path,
):
    target_a = tmp_path / "target-a.rvt"
    target_b = tmp_path / "target-b.rvt"
    target_a.write_bytes(b"same-target-snapshot")
    target_b.write_bytes(target_a.read_bytes())
    model = _model(target_b)
    approval = model.approval
    assert approval is not None
    model_hash = canonical_sha256(model)
    source_hash = file_sha256(target_b)
    result = RevitResult(
        tenant_id=model.tenant_id,
        project_id=model.project_id,
        wall_model_sha256=model_hash,
        status="write_approved",
        approval=approval,
        readback={"source_model_sha256": source_hash},
    )
    service = RevitBridgeService()
    secret = "r" * 32
    service.settings.revit_bridge_execution_enabled = True
    service.settings.revit_bridge_approval_secret = secret
    service.settings.revit_bridge_work_root = str(tmp_path / "bridge")
    build_id = "receipt-target-binding"
    work_dir = service._work_dir(build_id)
    output = work_dir / "working_copy.rvt"
    view = work_dir / "actual.png"
    output.write_bytes(b"output")
    view.write_bytes(b"view")
    from workers.revit_bridge.service import _write_delivery_receipt

    _write_delivery_receipt(
        work_dir / "delivery_receipt.json",
        wall_model_sha256=model_hash,
        source_model_sha256=source_hash,
        approval_digest=approval_digest(approval),
        target_model_path=target_a,
        output_path=output,
        actual_view_path=view,
        after_sha256=file_sha256(output),
        actual_view_sha256=file_sha256(view),
        details={},
    )
    token = issue_approval_token(
        secret,
        build_id=build_id,
        action="run_wall_model",
        wall_model_sha256=model_hash,
        target_model_path=str(target_b),
        source_model_sha256=source_hash,
        approval_digest=approval_digest(approval),
    )

    with pytest.raises(PolicyFailure, match="different target RVT"):
        await service.run_wall_model(
            RunWallModelRequest(
                build_id=build_id,
                source_model_path=str(target_b),
                wall_model=model.model_dump(mode="json"),
                revit_result=result.model_dump(mode="json"),
                source_model_sha256=source_hash,
                dry_run=False,
                approval_token=token,
            )
        )
