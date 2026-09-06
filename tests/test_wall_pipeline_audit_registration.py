from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from backend.engines.wall_pipeline.audit import (
    _register_edges,
    _tolerant_edge_iou,
    create_independent_audit,
)
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


def _approved_model() -> WallModel:
    return WallModel(
        tenant_id="tenant-audit",
        project_id="project-audit",
        wall_evidence_sha256="a" * 64,
        coordinate_origin="source_origin",
        level=LevelConfig(id="level-1", name="Main", wall_height_m=3.0),
        walls=[
            WallRecord(
                wall_id="wall-1",
                start_m=(0.0, 0.0),
                end_m=(4.0, 0.0),
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


def _write_source(path: Path, *, width: int = 300, height: int = 240) -> np.ndarray:
    image = np.full((height, width), 255, dtype=np.uint8)
    # The rectangle plus a short tee gives registration more than one axis
    # and avoids a degenerate single-line correlation problem.
    cv2.rectangle(image, (80, 70), (220, 180), 0, 2)
    cv2.line(image, (150, 70), (150, 35), 0, 2)
    assert cv2.imwrite(str(path), image)
    return image


def _write_result(model: WallModel, path: Path) -> RevitResult:
    return RevitResult(
        tenant_id=model.tenant_id,
        project_id=model.project_id,
        wall_model_sha256=canonical_sha256(model),
        status="succeeded",
        transaction_id="tx-audit",
        created_element_ids=["revit-wall-1"],
        readback={"wall_count": 1},
        actual_view_path=str(path),
        actual_view_sha256=file_sha256(path),
    )


def test_audit_registers_independent_renders_with_canvas_crop_and_translation(
    tmp_path: Path,
):
    source_path = tmp_path / "source.png"
    source = _write_source(source_path)

    # Revit exported a larger sheet and placed the same plan 30/20 pixels
    # away.  This is a canvas placement difference, not a WallModel redraw.
    revit = np.full((300, 360), 255, dtype=np.uint8)
    revit[20:, 30:] = 255
    cv2.rectangle(revit, (110, 90), (250, 200), 0, 2)
    cv2.line(revit, (180, 90), (180, 55), 0, 2)
    revit_path = tmp_path / "revit-larger.png"
    assert cv2.imwrite(str(revit_path), revit)

    model = _approved_model()
    report = create_independent_audit(
        model,
        _write_result(model, revit_path),
        source_path,
        tmp_path / "overlay.png",
        minimum_edge_iou=0.98,
    )

    assert report.status == "pass"
    assert report.independent_sources is True
    assert report.metrics["registration_mode"] == "content_bbox"
    assert report.metrics["registration_translation_x_px"] == pytest.approx(-30.0)
    assert report.metrics["registration_translation_y_px"] == pytest.approx(-20.0)
    assert report.metrics["edge_iou"] >= 0.98
    assert Path(report.overlay_path).is_file()


def test_audit_registers_a_cropped_revit_canvas(tmp_path: Path):
    source_path = tmp_path / "source.png"
    source = _write_source(source_path)
    # Crop the independent Revit raster around the plan.  The crop removes
    # white margins, so its dimensions and origin are different.
    # Keep every drawn edge inside the crop; only the white canvas margins are
    # removed so a genuine geometry mismatch is still observable.
    revit = source[20:220, 40:280].copy()
    revit_path = tmp_path / "revit-cropped.png"
    assert cv2.imwrite(str(revit_path), revit)

    model = _approved_model()
    report = create_independent_audit(
        model,
        _write_result(model, revit_path),
        source_path,
        tmp_path / "overlay-cropped.png",
        minimum_edge_iou=0.98,
    )

    assert report.status == "pass"
    assert report.metrics["registration_mode"] == "content_bbox"
    assert report.metrics["edge_iou"] >= 0.98


def test_audit_registers_a_right_angle_page_orientation(tmp_path: Path):
    source_path = tmp_path / "source.png"
    source = _write_source(source_path, width=300, height=240)
    # An independently exported portrait canvas can carry the same plan at a
    # right angle.  The audit may normalize only the four page orientations,
    # never an unrestricted affine rotation.
    revit = cv2.rotate(source, cv2.ROTATE_90_COUNTERCLOCKWISE)
    revit[0, 0] = 254  # keep the two artifacts independently hashed
    revit_path = tmp_path / "revit-portrait.png"
    assert cv2.imwrite(str(revit_path), revit)

    model = _approved_model()
    report = create_independent_audit(
        model,
        _write_result(model, revit_path),
        source_path,
        tmp_path / "overlay-portrait.png",
        minimum_edge_iou=0.98,
    )

    assert report.status == "pass"
    assert report.metrics["registration_rotation_deg"] == 90
    assert report.metrics["edge_iou"] >= 0.98


def test_audit_does_not_translate_equal_canvas_and_rejects_shifted_model(
    tmp_path: Path,
):
    source_path = tmp_path / "source.png"
    _write_source(source_path, width=300, height=240)
    shifted = np.full((240, 300), 255, dtype=np.uint8)
    cv2.rectangle(shifted, (100, 70), (240, 180), 0, 2)
    cv2.line(shifted, (170, 70), (170, 35), 0, 2)
    revit_path = tmp_path / "revit-shifted.png"
    assert cv2.imwrite(str(revit_path), shifted)

    model = _approved_model()
    report = create_independent_audit(
        model,
        _write_result(model, revit_path),
        source_path,
        tmp_path / "overlay-shifted.png",
        minimum_edge_iou=0.90,
    )

    assert report.status == "fail"
    assert report.metrics["registration_mode"] == "identity"
    assert report.metrics["registration_translation_x_px"] == 0.0
    assert report.metrics["registration_translation_y_px"] == 0.0
    assert report.metrics["edge_iou"] < 0.90


def test_audit_has_non_bypassable_95_percent_similarity_floor(tmp_path: Path):
    source_path = tmp_path / "source-floor.png"
    _write_source(source_path)
    shifted = np.full((240, 300), 255, dtype=np.uint8)
    cv2.rectangle(shifted, (100, 70), (240, 180), 0, 2)
    cv2.line(shifted, (170, 70), (170, 35), 0, 2)
    revit_path = tmp_path / "revit-floor.png"
    assert cv2.imwrite(str(revit_path), shifted)

    model = _approved_model()
    # A caller may ask for a weaker diagnostic threshold, but it must not
    # turn a failed delivery into a passing 95% acceptance decision.
    report = create_independent_audit(
        model,
        _write_result(model, revit_path),
        source_path,
        tmp_path / "overlay-floor.png",
        minimum_edge_iou=0.10,
        minimum_source_coverage=0.10,
        minimum_revit_precision=0.10,
    )

    assert report.status == "fail"
    assert report.metrics["required_similarity"] == pytest.approx(0.95)
    assert report.metrics["similarity_score"] == pytest.approx(
        min(
            report.metrics["edge_iou"],
            report.metrics["source_edge_coverage"],
            report.metrics["revit_edge_precision"],
        )
    )
    assert report.metrics["similarity_score"] < 0.95


def test_audit_blocks_context_or_readback_identity_mismatch(tmp_path: Path):
    source_path = tmp_path / "source.png"
    _write_source(source_path)
    revit_path = tmp_path / "revit.png"
    revit = cv2.imread(str(source_path), cv2.IMREAD_GRAYSCALE)
    assert revit is not None
    assert cv2.imwrite(str(revit_path), revit)
    model = _approved_model()

    mismatched_context = RevitResult(
        tenant_id="other-tenant",
        project_id=model.project_id,
        wall_model_sha256=canonical_sha256(model),
        status="succeeded",
        transaction_id="tx-1",
        created_element_ids=["revit-wall-1"],
        readback={"wall_count": 1},
        actual_view_path=str(revit_path),
        actual_view_sha256=file_sha256(revit_path),
    )
    report = create_independent_audit(
        model, mismatched_context, source_path, tmp_path / "overlay-context.png"
    )
    assert report.status == "blocked"
    assert any("tenant/project" in item for item in report.errors)

    mismatched_count = mismatched_context.model_copy(update={
        "tenant_id": model.tenant_id,
        "readback": {"wall_count": 0},
    })
    report = create_independent_audit(
        model, mismatched_count, source_path, tmp_path / "overlay-count.png"
    )
    assert report.status == "blocked"
    assert any("read-back count" in item for item in report.errors)


def test_registration_scales_search_tolerance_on_delivery_raster():
    """A 2400px delivery search must not reuse a four-pixel 800px radius."""

    source = np.zeros((1600, 2400), dtype=np.uint8)
    for x in range(40, 704, 45):
        cv2.line(source, (x, 40), (x, 1560), 255, 1)
    for y in range(40, 1561, 47):
        cv2.line(source, (40, y), (703, y), 255, 1)
    cv2.rectangle(source, (88, 65), (220, 180), 255, 2)
    cv2.rectangle(source, (420, 1080), (650, 1450), 255, 2)

    revit = np.zeros((1599, 2400), dtype=np.uint8)
    scale_x, scale_y = 0.987133, 0.999742
    translate_x, translate_y = 6.5, -0.8

    def revit_point(point: tuple[int, int]) -> tuple[int, int]:
        return (
            round((point[0] - translate_x) / scale_x),
            round((point[1] - translate_y) / scale_y),
        )

    for x in range(40, 704, 45):
        cv2.line(revit, revit_point((x, 40)), revit_point((x, 1560)), 255, 2)
    for y in range(40, 1561, 47):
        cv2.line(revit, revit_point((40, y)), revit_point((703, y)), 255, 2)
    cv2.rectangle(revit, revit_point((88, 65)), revit_point((220, 180)), 255, 3)
    cv2.rectangle(
        revit, revit_point((420, 1080)), revit_point((650, 1450)), 255, 3
    )

    registered, metrics = _register_edges(source, revit)

    assert metrics["registration_search_tolerance_px"] == 1
    assert _tolerant_edge_iou(source, registered, tolerance_px=4) >= 0.95
