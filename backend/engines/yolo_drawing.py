"""Local YOLO evidence for drawing-cleaning audits.

YOLO detections are deliberately non-authoritative.  They can corroborate CV
line omissions, but boxes or masks are never converted directly into BIM
geometry.  Exact coordinates must still come from DXF vectors or a calibrated
CV-to-DXF transform.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import math
import os
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np


DEFAULT_WALL_LABELS = frozenset({"wall", "curtain wall", "partition"})

REFERENCE_PIXEL_RECALL_THRESHOLD = 0.50
DETECTION_BOX_PRECISION_THRESHOLD = 0.08
MAXIMUM_WALL_CONFIDENCE_THRESHOLD = 0.25
REFERENCE_COMPONENT_RECALL_THRESHOLD = 0.50
DOMINANT_REFERENCE_COMPONENT_THRESHOLD = 0.25
MAXIMUM_SINGLE_BOX_COVERAGE_SHARE = 0.80


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scalar(value: Any) -> float:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "item"):
        value = value.item()
    return float(value)


def _vector(value: Any) -> list[float]:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "tolist"):
        value = value.tolist()
    while isinstance(value, list) and len(value) == 1:
        value = value[0]
    return [float(item) for item in value]


def _tile_origins(width: int, height: int, tile_size: int,
                  overlap: int) -> list[tuple[int, int]]:
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    if overlap < 0 or overlap >= tile_size:
        raise ValueError("tile overlap must satisfy 0 <= overlap < tile_size")
    stride = tile_size - overlap
    origins = []
    seen = set()
    for top in range(0, height, stride):
        y = min(top, max(0, height - tile_size))
        for left in range(0, width, stride):
            x = min(left, max(0, width - tile_size))
            if (x, y) not in seen:
                seen.add((x, y))
                origins.append((x, y))
    return origins


def _intersection_over_union(first: list[float], second: list[float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


def _classwise_nms(detections: list[dict], threshold: float) -> list[dict]:
    kept = []
    by_class: dict[int, list[dict]] = {}
    for item in detections:
        by_class.setdefault(int(item["class_id"]), []).append(item)
    for items in by_class.values():
        pending = sorted(items, key=lambda item: item["confidence"], reverse=True)
        while pending:
            winner = pending.pop(0)
            kept.append(winner)
            pending = [item for item in pending
                       if _intersection_over_union(winner["xyxy"], item["xyxy"])
                       < threshold]
    return sorted(kept, key=lambda item: item["confidence"], reverse=True)


def _resolve_name(names: Any, class_id: int) -> str:
    if isinstance(names, dict):
        return str(names.get(class_id, class_id))
    if isinstance(names, (list, tuple)) and 0 <= class_id < len(names):
        return str(names[class_id])
    return str(class_id)


def _quality_against_reference(
    shape: tuple[int, int],
    wall_detections: list[dict],
    reference_mask_path: Path | None,
) -> dict:
    if reference_mask_path is None or not reference_mask_path.is_file():
        return {"status": "NOT_EVALUATED", "reason": "reference mask unavailable"}
    reference = cv2.imread(str(reference_mask_path), cv2.IMREAD_GRAYSCALE)
    if reference is None:
        return {"status": "ERROR", "reason": "reference mask unreadable"}
    height, width = shape
    if reference.shape != (height, width):
        reference = cv2.resize(reference, (width, height),
                               interpolation=cv2.INTER_NEAREST)
    reference = reference > 0
    component_count, component_labels = cv2.connectedComponents(
        reference.astype(np.uint8), connectivity=8)
    reference_component_count = component_count - 1
    component_sizes = np.bincount(
        component_labels.ravel(), minlength=component_count)[1:]
    dilated = cv2.dilate(
        reference.astype(np.uint8), np.ones((11, 11), np.uint8)) > 0
    detected = np.zeros((height, width), dtype=np.uint8)
    qualified_detections = []
    for item in wall_detections:
        x1, y1, x2, y2 = item["xyxy"]
        left = max(0, min(width, int(math.floor(x1))))
        top = max(0, min(height, int(math.floor(y1))))
        right = max(0, min(width, int(math.ceil(x2))))
        bottom = max(0, min(height, int(math.ceil(y2))))
        box_area = (right - left) * (bottom - top)
        if box_area <= 0:
            continue
        box_precision = float(np.count_nonzero(
            dilated[top:bottom, left:right])) / box_area
        labels_in_box = component_labels[top:bottom, left:right]
        label_counts = np.bincount(
            labels_in_box.ravel(), minlength=component_count)[1:]
        exact_reference_pixels = int(label_counts.sum())
        dominant_component_fraction = (
            float(label_counts.max()) / exact_reference_pixels
            if exact_reference_pixels and label_counts.size else 0.0
        )
        if (box_precision < DETECTION_BOX_PRECISION_THRESHOLD or
                dominant_component_fraction <
                DOMINANT_REFERENCE_COMPONENT_THRESHOLD):
            continue
        detected[top:bottom, left:right] = 1
        qualified_detections.append({
            "confidence": float(item["confidence"]),
            "exact_reference_pixels": exact_reference_pixels,
        })
    detected_pixels = int(np.count_nonzero(detected))
    reference_pixels = int(np.count_nonzero(reference))
    if not detected_pixels or not reference_pixels:
        return {
            "status": "REVIEW",
            "reference_pixel_recall": 0.0,
            "detection_box_precision": 0.0,
            "maximum_wall_confidence": 0.0,
            "qualified_wall_detection_count": len(qualified_detections),
            "reference_component_count": reference_component_count,
            "covered_reference_component_count": 0,
            "reference_component_recall": 0.0,
            "maximum_single_box_coverage_share": 0.0,
            "coverage_diversity_passed": False,
            "thresholds": {
                "reference_pixel_recall":
                    REFERENCE_PIXEL_RECALL_THRESHOLD,
                "detection_box_precision":
                    DETECTION_BOX_PRECISION_THRESHOLD,
                "maximum_wall_confidence":
                    MAXIMUM_WALL_CONFIDENCE_THRESHOLD,
                "reference_component_recall":
                    REFERENCE_COMPONENT_RECALL_THRESHOLD,
                "dominant_reference_component_fraction_per_box":
                    DOMINANT_REFERENCE_COMPONENT_THRESHOLD,
                "maximum_single_box_coverage_share":
                    MAXIMUM_SINGLE_BOX_COVERAGE_SHARE,
            },
            "reason": (
                "no vector-qualified wall detections or no vector wall "
                "reference"),
        }
    covered_reference_pixels = int(np.count_nonzero(
        reference & (detected > 0)))
    recall = float(covered_reference_pixels) / reference_pixels
    precision = float(np.count_nonzero(dilated & (detected > 0))) / detected_pixels
    max_confidence = max(
        (item["confidence"] for item in qualified_detections), default=0.0)
    covered_component_pixels = np.bincount(
        component_labels[detected > 0], minlength=component_count)[1:]
    component_coverages = np.divide(
        covered_component_pixels,
        component_sizes,
        out=np.zeros_like(covered_component_pixels, dtype=float),
        where=component_sizes > 0,
    )
    covered_reference_component_count = int(np.count_nonzero(
        component_coverages >= REFERENCE_COMPONENT_RECALL_THRESHOLD))
    component_recall = (
        float(covered_reference_component_count) / reference_component_count
        if reference_component_count else 0.0
    )
    maximum_single_box_coverage_share = max((
        item["exact_reference_pixels"] / covered_reference_pixels
        for item in qualified_detections
    ), default=0.0)
    coverage_diversity_passed = (
        reference_component_count <= 1 or
        (len(qualified_detections) >= 2 and
         maximum_single_box_coverage_share <=
         MAXIMUM_SINGLE_BOX_COVERAGE_SHARE)
    )
    passed = (
        recall >= REFERENCE_PIXEL_RECALL_THRESHOLD and
        precision >= DETECTION_BOX_PRECISION_THRESHOLD and
        max_confidence >= MAXIMUM_WALL_CONFIDENCE_THRESHOLD and
        component_recall >= REFERENCE_COMPONENT_RECALL_THRESHOLD and
        coverage_diversity_passed
    )
    return {
        "status": "PASS" if passed else "REVIEW",
        "reference_pixel_recall": round(recall, 4),
        "detection_box_precision": round(precision, 4),
        "maximum_wall_confidence": round(max_confidence, 4),
        "qualified_wall_detection_count": len(qualified_detections),
        "reference_component_count": reference_component_count,
        "covered_reference_component_count":
            covered_reference_component_count,
        "reference_component_recall": round(component_recall, 4),
        "maximum_single_box_coverage_share": round(
            maximum_single_box_coverage_share, 4),
        "coverage_diversity_passed": coverage_diversity_passed,
        "thresholds": {
            "reference_pixel_recall": REFERENCE_PIXEL_RECALL_THRESHOLD,
            "detection_box_precision": DETECTION_BOX_PRECISION_THRESHOLD,
            "maximum_wall_confidence":
                MAXIMUM_WALL_CONFIDENCE_THRESHOLD,
            "reference_component_recall":
                REFERENCE_COMPONENT_RECALL_THRESHOLD,
            "dominant_reference_component_fraction_per_box":
                DOMINANT_REFERENCE_COMPONENT_THRESHOLD,
            "maximum_single_box_coverage_share":
                MAXIMUM_SINGLE_BOX_COVERAGE_SHARE,
        },
        "reason": ("YOLO wall boxes agree with vector wall evidence" if passed else
                   "YOLO wall boxes do not sufficiently agree with vector wall evidence"),
    }


def correlate_cv_candidates(detections: list[dict], candidates: list[dict],
                            wall_labels: set[str] | frozenset[str] =
                            DEFAULT_WALL_LABELS) -> dict:
    """Return CV line omissions whose midpoint falls inside a YOLO wall box."""
    normalized = {name.casefold() for name in wall_labels}
    wall_boxes = [item["xyxy"] for item in detections
                  if str(item.get("class_name", "")).casefold() in normalized]
    supported = []
    for candidate in candidates:
        start = candidate.get("start_px") or []
        end = candidate.get("end_px") or []
        if len(start) < 2 or len(end) < 2:
            continue
        midpoint = ((float(start[0]) + float(end[0])) / 2.0,
                    (float(start[1]) + float(end[1])) / 2.0)
        if any(box[0] <= midpoint[0] <= box[2] and
               box[1] <= midpoint[1] <= box[3] for box in wall_boxes):
            supported.append(candidate)
    return {
        "cv_candidate_count": len(candidates),
        "yolo_supported_candidate_count": len(supported),
        "candidates": supported[:100],
        "geometry_authority": False,
        "note": "YOLO+CV agreement is review evidence; DXF geometry remains authoritative",
    }


def run_yolo_drawing_audit(
    image_path: Path,
    model_path: Path | None,
    output_overlay: Path | None = None,
    reference_mask_path: Path | None = None,
    confidence: float = 0.10,
    tile_size: int = 640,
    overlap: int = 320,
    nms_iou: float = 0.35,
    wall_labels: set[str] | frozenset[str] = DEFAULT_WALL_LABELS,
    model_factory: Callable[[str], Any] | None = None,
) -> dict:
    """Run local tiled YOLO inference and return JSON-serializable evidence."""
    if model_path is None or not str(model_path).strip():
        return {"inference_status": "DISABLED", "geometry_authority": False,
                "reason": "DRAWING_YOLO_MODEL_PATH is not configured"}
    model_path = Path(model_path)
    image_path = Path(image_path)
    if not model_path.is_file():
        return {"inference_status": "MISSING_MODEL", "geometry_authority": False,
                "model_path": str(model_path), "reason": "YOLO model file is missing"}
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        return {"inference_status": "ERROR", "geometry_authority": False,
                "model_path": str(model_path), "reason": "drawing image is unreadable"}
    try:
        if model_factory is None:
            os.environ.setdefault("YOLO_CONFIG_DIR", str(
                Path(__file__).resolve().parents[2] / "data" / "runtime"))
            from ultralytics import YOLO
            model_factory = YOLO
        model = model_factory(str(model_path))
        height, width = image.shape[:2]
        origins = _tile_origins(width, height, tile_size, overlap)
        tiles = [image[y:min(y + tile_size, height),
                       x:min(x + tile_size, width)] for x, y in origins]
        results = model.predict(
            tiles, conf=confidence, imgsz=tile_size, device="cpu",
            verbose=False, max_det=1000, batch=min(4, len(tiles)))
        detections = []
        for result, (offset_x, offset_y) in zip(results, origins):
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            names = getattr(result, "names", None) or getattr(model, "names", {})
            for box in boxes:
                coords = _vector(box.xyxy)
                if len(coords) != 4:
                    continue
                class_id = int(round(_scalar(box.cls)))
                detections.append({
                    "class_id": class_id,
                    "class_name": _resolve_name(names, class_id),
                    "confidence": round(_scalar(box.conf), 6),
                    "xyxy": [round(coords[0] + offset_x, 2),
                              round(coords[1] + offset_y, 2),
                              round(coords[2] + offset_x, 2),
                              round(coords[3] + offset_y, 2)],
                })
        detections = _classwise_nms(detections, nms_iou)
    except ImportError as exc:
        return {"inference_status": "DEPENDENCY_MISSING", "geometry_authority": False,
                "model_path": str(model_path), "reason": str(exc)[:240]}
    except Exception as exc:
        return {"inference_status": "ERROR", "geometry_authority": False,
                "model_path": str(model_path), "reason": str(exc)[:240]}

    normalized_labels = {name.casefold() for name in wall_labels}
    wall_detections = [item for item in detections
                       if item["class_name"].casefold() in normalized_labels]
    if output_overlay is not None:
        output_overlay = Path(output_overlay)
        output_overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay_image = image.copy()
        for item in detections:
            x1, y1, x2, y2 = [int(round(value)) for value in item["xyxy"]]
            is_wall = item in wall_detections
            color = (0, 240, 255) if is_wall else (255, 130, 30)
            cv2.rectangle(overlay_image, (x1, y1), (x2, y2), color, 2)
            cv2.putText(overlay_image,
                        f"{item['class_name']} {item['confidence']:.2f}",
                        (x1, max(16, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.45, color, 1, cv2.LINE_AA)
        cv2.imwrite(str(output_overlay), overlay_image)
    counts = Counter(item["class_name"] for item in detections)
    quality = _quality_against_reference(
        image.shape[:2], wall_detections, reference_mask_path)
    return {
        "inference_status": "OK",
        "geometry_authority": False,
        "model_path": str(model_path),
        "model_sha256": _sha256(model_path),
        "image_path": str(image_path),
        "image_size": [int(image.shape[1]), int(image.shape[0])],
        "tile_size": tile_size,
        "tile_overlap": overlap,
        "tile_count": len(origins),
        "confidence_threshold": confidence,
        "detection_count": len(detections),
        "class_counts": dict(counts),
        "wall_detection_count": len(wall_detections),
        "wall_labels": sorted(wall_labels),
        "quality": quality,
        "overlay": str(output_overlay) if output_overlay is not None else None,
        "detections": detections[:500],
    }
