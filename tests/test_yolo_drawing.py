from types import SimpleNamespace

import cv2
import numpy as np

from backend.engines.yolo_drawing import (
    _classwise_nms,
    _quality_against_reference,
    _tile_origins,
    correlate_cv_candidates,
    run_yolo_drawing_audit,
)


def _write_reference(path, shape, rectangles):
    reference = np.zeros(shape, dtype=np.uint8)
    for top, left, bottom, right in rectangles:
        reference[top:bottom, left:right] = 255
    cv2.imwrite(str(path), reference)
    return path


class _FakeModel:
    names = {0: "Wall"}

    def predict(self, tiles, **_kwargs):
        box = SimpleNamespace(
            xyxy=np.array([[10.0, 10.0, 90.0, 90.0]]),
            cls=np.array([0.0]),
            conf=np.array([0.9]),
        )
        return [SimpleNamespace(boxes=[box], names=self.names) for _ in tiles]


def test_tiles_cover_right_and_bottom_edges_without_duplicates():
    origins = _tile_origins(1000, 700, 448, 112)

    assert len(origins) == len(set(origins))
    assert max(x for x, _y in origins) == 1000 - 448
    assert max(y for _x, y in origins) == 700 - 448


def test_bundled_yolo_defaults_use_model_native_tiles(monkeypatch):
    from scripts.audit_drawing_cleaning import _resolved_yolo_configuration

    monkeypatch.delenv("DRAWING_YOLO_TILE_SIZE", raising=False)
    monkeypatch.delenv("DRAWING_YOLO_TILE_OVERLAP", raising=False)

    _model_path, parameters = _resolved_yolo_configuration()

    assert parameters["tile_size"] == 640
    assert parameters["overlap"] == 320


def test_classwise_nms_keeps_distinct_classes_and_removes_overlap():
    detections = [
        {"class_id": 0, "confidence": 0.9, "xyxy": [0, 0, 20, 20]},
        {"class_id": 0, "confidence": 0.7, "xyxy": [1, 1, 21, 21]},
        {"class_id": 1, "confidence": 0.8, "xyxy": [1, 1, 21, 21]},
    ]

    result = _classwise_nms(detections, 0.35)

    assert [(item["class_id"], item["confidence"]) for item in result] == [
        (0, 0.9), (1, 0.8),
    ]


def test_missing_model_is_reported_without_inference(tmp_path):
    result = run_yolo_drawing_audit(
        tmp_path / "drawing.png", tmp_path / "missing.pt")

    assert result["inference_status"] == "MISSING_MODEL"
    assert result["geometry_authority"] is False


def test_cv_candidates_require_yolo_wall_box_support():
    detections = [{
        "class_name": "Wall",
        "xyxy": [0, 0, 50, 50],
    }]
    candidates = [
        {"start_px": [10, 10], "end_px": [20, 20]},
        {"start_px": [70, 70], "end_px": [80, 80]},
    ]

    result = correlate_cv_candidates(detections, candidates)

    assert result["cv_candidate_count"] == 2
    assert result["yolo_supported_candidate_count"] == 1
    assert result["geometry_authority"] is False


def test_fake_inference_is_validated_against_vector_reference(tmp_path):
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    reference = np.zeros((100, 100), dtype=np.uint8)
    reference[10:90, 10:90] = 255
    image_path = tmp_path / "drawing.png"
    reference_path = tmp_path / "reference.png"
    overlay_path = tmp_path / "overlay.png"
    model_path = tmp_path / "model.pt"
    cv2.imwrite(str(image_path), image)
    cv2.imwrite(str(reference_path), reference)
    model_path.write_bytes(b"fake model")

    result = run_yolo_drawing_audit(
        image_path,
        model_path,
        output_overlay=overlay_path,
        reference_mask_path=reference_path,
        tile_size=128,
        overlap=16,
        model_factory=lambda _path: _FakeModel(),
    )

    assert result["inference_status"] == "OK"
    assert result["wall_detection_count"] == 1
    assert result["quality"]["status"] == "PASS"
    assert result["quality"]["qualified_wall_detection_count"] == 1
    assert result["geometry_authority"] is False
    assert overlay_path.is_file()


def test_unmatched_high_confidence_box_cannot_supply_quality_confidence(
        tmp_path):
    reference_path = _write_reference(
        tmp_path / "reference.png", (100, 100), [(10, 10, 40, 40)])

    quality = _quality_against_reference((100, 100), [
        {"confidence": 0.20, "xyxy": [10, 10, 40, 40]},
        {"confidence": 0.99, "xyxy": [60, 60, 90, 90]},
    ], reference_path)

    assert quality["status"] == "REVIEW"
    assert quality["reference_pixel_recall"] == 1.0
    assert quality["maximum_wall_confidence"] == 0.2
    assert quality["qualified_wall_detection_count"] == 1


def test_single_oversized_box_cannot_validate_multiple_wall_components(
        tmp_path):
    reference_path = _write_reference(
        tmp_path / "reference.png", (100, 100), [
            (10, 10, 30, 30),
            (70, 70, 90, 90),
        ])

    quality = _quality_against_reference((100, 100), [{
        "confidence": 0.90,
        "xyxy": [5, 5, 95, 95],
    }], reference_path)

    assert quality["reference_pixel_recall"] == 1.0
    assert quality["reference_component_recall"] == 1.0
    assert quality["coverage_diversity_passed"] is False
    assert quality["status"] == "REVIEW"


def test_distinct_vector_qualified_boxes_validate_distinct_components(
        tmp_path):
    reference_path = _write_reference(
        tmp_path / "reference.png", (100, 100), [
            (10, 10, 30, 30),
            (70, 70, 90, 90),
        ])

    quality = _quality_against_reference((100, 100), [
        {"confidence": 0.90, "xyxy": [10, 10, 30, 30]},
        {"confidence": 0.80, "xyxy": [70, 70, 90, 90]},
    ], reference_path)

    assert quality["status"] == "PASS"
    assert quality["qualified_wall_detection_count"] == 2
    assert quality["reference_component_recall"] == 1.0
    assert quality["coverage_diversity_passed"] is True
