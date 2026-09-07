"""Raster wall-line audit using OpenCV.

Detects long straight edges, reports their deviation from horizontal/vertical,
and writes a color overlay for human verification.  This is deliberately
independent from the DXF/JSON/Revit geometry pipeline.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np


def _axis_line(raw, tolerance_deg=6.0):
    x1, y1, x2, y2 = map(float, raw)
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    angle = abs(math.degrees(math.atan2(dy, dx))) % 180.0
    hdev = min(angle, 180.0 - angle)
    vdev = abs(angle - 90.0)
    if hdev <= tolerance_deg:
        y = (y1 + y2) / 2.0
        return {"axis": "H", "coord": y, "lo": min(x1, x2),
                "hi": max(x1, x2), "length": length, "deviation": hdev}
    if vdev <= tolerance_deg:
        x = (x1 + x2) / 2.0
        return {"axis": "V", "coord": x, "lo": min(y1, y2),
                "hi": max(y1, y2), "length": length, "deviation": vdev}
    return None


def _merge_axis_lines(lines, coord_tolerance=3.0, gap_tolerance=8.0):
    merged = []
    for axis in ("H", "V"):
        group = sorted((x for x in lines if x["axis"] == axis),
                       key=lambda x: (x["coord"], x["lo"]))
        for line in group:
            target = None
            for old in reversed(merged):
                if old["axis"] != axis:
                    continue
                if abs(old["coord"] - line["coord"]) > coord_tolerance:
                    break
                if line["lo"] <= old["hi"] + gap_tolerance:
                    target = old
                    break
            if target is None:
                merged.append(dict(line))
            else:
                total = target["length"] + line["length"]
                target["coord"] = ((target["coord"] * target["length"] +
                                    line["coord"] * line["length"]) / total)
                target["lo"] = min(target["lo"], line["lo"])
                target["hi"] = max(target["hi"], line["hi"])
                target["length"] = target["hi"] - target["lo"]
                target["deviation"] = max(target["deviation"], line["deviation"])
    return merged


def extract_wall_centerlines(lines, min_thickness_px=3.0, max_thickness_px=24.0,
                             min_overlap_px=15.0, reuse_edges=True):
    """Pair parallel raster edges and return one centerline per wall face pair."""
    axis_lines = [x for x in (_axis_line(raw) for raw in lines) if x]
    axis_lines = _merge_axis_lines(axis_lines)
    walls, used_pairs, used_lines = [], set(), set()
    for i, first in enumerate(axis_lines):
        if not reuse_edges and i in used_lines:
            continue
        candidates = []
        for j in range(i + 1, len(axis_lines)):
            second = axis_lines[j]
            if not reuse_edges and j in used_lines:
                continue
            if second["axis"] != first["axis"]:
                continue
            thickness = abs(second["coord"] - first["coord"])
            if not min_thickness_px <= thickness <= max_thickness_px:
                continue
            lo = max(first["lo"], second["lo"])
            hi = min(first["hi"], second["hi"])
            overlap = hi - lo
            if overlap < min_overlap_px:
                continue
            candidates.append((thickness, -overlap, j, lo, hi))
        if not candidates:
            continue
        thickness, _, j, lo, hi = min(candidates)
        if (i, j) in used_pairs:
            continue
        used_pairs.add((i, j))
        used_lines.update((i, j))
        center = (first["coord"] + axis_lines[j]["coord"]) / 2.0
        if first["axis"] == "H":
            start, end = [lo, center], [hi, center]
        else:
            start, end = [center, lo], [center, hi]
        walls.append({"start_px": [round(v, 2) for v in start],
                      "end_px": [round(v, 2) for v in end],
                      "thickness_px": round(thickness, 2),
                      "axis": first["axis"], "confidence": 0.85})
    return walls


def audit(image_path: Path, output_path: Path, pixels_per_mm=None,
          min_line_length_px=None, detect_binary_centers=False,
          reuse_edges=True, min_thickness_px=3.0,
          max_thickness_px=24.0, min_skew_drift_px=2.0) -> dict:
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("cannot read image: %s" % image_path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    foreground = cv2.threshold(gray, 35, 255, cv2.THRESH_BINARY)[1]
    edges = cv2.Canny(foreground, 50, 150, apertureSize=3)
    min_len = (max(8, int(min_line_length_px)) if min_line_length_px
               else max(18, int(min(image.shape[:2]) * 0.06)))
    line_image = foreground if detect_binary_centers else edges
    lines = cv2.HoughLinesP(
        line_image, 1, np.pi / 720.0, threshold=12 if detect_binary_centers else 18,
        minLineLength=min_len, maxLineGap=5,
    )
    records, raw_lines = [], []
    overlay = image.copy()
    if lines is not None:
        # OpenCV 4 commonly returns N x 1 x 4; OpenCV 5 may return N x 4.
        for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
            raw_lines.append([int(x1), int(y1), int(x2), int(y2)])
            length = math.hypot(x2 - x1, y2 - y1)
            angle = abs(math.degrees(math.atan2(y2 - y1, x2 - x1))) % 90.0
            deviation = min(angle, 90.0 - angle)
            # A one-pixel staircase is normal rasterisation even for a perfect
            # model-space axis line. Do not turn display aliasing into a BIM
            # geometry correction; require measurable cross-axis drift too.
            cross_axis_drift = int(min(abs(x2 - x1), abs(y2 - y1)))
            is_skewed = bool(deviation > 0.5 and
                             cross_axis_drift >= min_skew_drift_px)
            records.append({
                "start": [int(x1), int(y1)], "end": [int(x2), int(y2)],
                "length_px": round(length, 2),
                "axis_deviation_deg": round(deviation, 4),
                "cross_axis_drift_px": cross_axis_drift,
                "is_skewed": is_skewed,
            })
            color = (0, 0, 255) if is_skewed else (0, 255, 0)
            cv2.line(overlay, (x1, y1), (x2, y2), color, 1, cv2.LINE_AA)
    walls = extract_wall_centerlines(
        raw_lines, min_thickness_px=min_thickness_px,
        max_thickness_px=max_thickness_px, reuse_edges=reuse_edges)
    for wall in walls:
        p1 = tuple(int(round(v)) for v in wall["start_px"])
        p2 = tuple(int(round(v)) for v in wall["end_px"])
        cv2.line(overlay, p1, p2, (255, 255, 0), 2, cv2.LINE_AA)
        if pixels_per_mm:
            wall["start_mm"] = [round(v / pixels_per_mm, 2) for v in wall["start_px"]]
            wall["end_mm"] = [round(v / pixels_per_mm, 2) for v in wall["end_px"]]
            wall["thickness_mm"] = round(wall["thickness_px"] / pixels_per_mm, 2)
    cv2.imwrite(str(output_path), overlay)
    long_lines = [r for r in records if r["length_px"] >= min_len]
    skewed = [r for r in long_lines if r["is_skewed"]]
    return {
        "image": str(image_path), "overlay": str(output_path),
        "image_size": [int(image.shape[1]), int(image.shape[0])],
        "minimum_line_length_px": min_len,
        "minimum_skew_drift_px": min_skew_drift_px,
        "line_count": len(long_lines), "skewed_count": len(skewed),
        "max_axis_deviation_deg": max(
            (r["axis_deviation_deg"] for r in long_lines), default=0.0),
        "worst_lines": sorted(
            skewed, key=lambda r: r["axis_deviation_deg"], reverse=True)[:30],
        "wall_count": len(walls), "walls": walls,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--pixels-per-mm", type=float)
    args = parser.parse_args()
    result = audit(args.image, args.output, args.pixels_per_mm)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
