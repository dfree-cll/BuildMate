"""Extract GBZ/YBZ boundary-column profiles from structural DXF hatches."""
from __future__ import annotations

import math
import re
from pathlib import Path

import ezdxf
from ezdxf.disassemble import recursive_decompose
from ezdxf.path import from_hatch, from_hatch_boundary_path

from backend.agents.drawing2bim.nodes.dxf_adaptive import extract_columns_from_dxf

MARK_RE = re.compile(r"\b(?:GBZ|YBZ)\s*\d+[A-Z]?\b", re.IGNORECASE)
LABEL_LAYER = "S-W-INER-CO-NUMS"
PROFILE_LAYER = "S-WALL-HATC"


def register_profiles_to_walls(columns: list[dict], walls: list[dict],
                               resolution_mm: float = 50.0,
                               max_shift_mm: float = 25000.0,
                               min_overlap: float = 0.33,
                               group_distance_mm: float = 9000.0) -> list[dict]:
    """Translate hatch detail profiles onto the matching plan wall solids.

    In this drawing the GBZ/YBZ outlines are drawn beside the actual wall plan.
    Their shape and relative layout are authoritative, but their hatch group is
    offset from the plan.  Profiles in the same detail are therefore registered
    as one template.  Registering each small profile independently makes similar
    GBZ/YBZ shapes collapse onto the same attractive corner.
    """
    if not columns or not walls or resolution_mm <= 0 or max_shift_mm < 0:
        return []
    import cv2
    import numpy as np

    wall_points = [point for wall in walls
                   for point in (wall.get("start", [])[:2], wall.get("end", [])[:2])
                   if len(point) == 2]
    profile_points = [
        [float(column["x"]) + float(point[0]),
         float(column["y"]) + float(point[1])]
        for column in columns for point in (column.get("profile") or {}).get("points", [])
    ]
    if not wall_points or not profile_points:
        return []
    margin = max_shift_mm + 1000.0
    min_x = min(point[0] for point in wall_points + profile_points) - margin
    min_y = min(point[1] for point in wall_points + profile_points) - margin
    max_x = max(point[0] for point in wall_points + profile_points) + margin
    max_y = max(point[1] for point in wall_points + profile_points) + margin
    width = int(math.ceil((max_x - min_x) / resolution_mm)) + 1
    height = int(math.ceil((max_y - min_y) / resolution_mm)) + 1
    if width * height > 40_000_000:
        return []
    wall_mask = np.zeros((height, width), dtype=np.uint8)

    def pixel(point):
        return (int(round((float(point[0]) - min_x) / resolution_mm)),
                int(round((float(point[1]) - min_y) / resolution_mm)))

    for wall in walls:
        start, end = wall.get("start", [])[:2], wall.get("end", [])[:2]
        if len(start) != 2 or len(end) != 2:
            continue
        thickness = max(1, int(round(float(wall.get("thickness", 200.0)) /
                                     resolution_mm)))
        cv2.line(wall_mask, pixel(start), pixel(end), 255, thickness, cv2.LINE_8)

    shift_pixels = int(math.ceil(max_shift_mm / resolution_mm))
    remaining = set(range(len(columns)))
    groups = []
    while remaining:
        group = {remaining.pop()}
        changed = True
        while changed:
            changed = False
            for candidate in list(remaining):
                center = (float(columns[candidate]["x"]),
                          float(columns[candidate]["y"]))
                if any(math.dist(center,
                                 (float(columns[index]["x"]),
                                  float(columns[index]["y"]))) <= group_distance_mm
                       for index in group):
                    group.add(candidate)
                    remaining.remove(candidate)
                    changed = True
        groups.append(sorted(group))

    registered = []
    occupied = np.zeros_like(wall_mask)
    for group_number, group in enumerate(sorted(groups, key=len, reverse=True), 1):
        polygons = []
        valid_indices = []
        for index in group:
            source = columns[index]
            relative = (source.get("profile") or {}).get("points") or []
            if len(relative) < 3:
                continue
            polygons.append(np.asarray([
                pixel((float(source["x"]) + float(point[0]),
                       float(source["y"]) + float(point[1])))
                for point in relative], dtype=np.int32))
            valid_indices.append(index)
        if not polygons:
            continue
        absolute = np.vstack(polygons)
        x, y, w, h = cv2.boundingRect(absolute)
        template = np.zeros((h, w), dtype=np.uint8)
        for polygon in polygons:
            cv2.fillPoly(template, [polygon - np.asarray([x, y])], 255)
        filled = int(np.count_nonzero(template))
        if not filled:
            continue
        left, top = max(0, x - shift_pixels), max(0, y - shift_pixels)
        right = min(width - w, x + shift_pixels)
        bottom = min(height - h, y + shift_pixels)
        if right < left or bottom < top:
            continue
        available = cv2.bitwise_and(wall_mask, cv2.bitwise_not(occupied))
        search = available[top:bottom + h + 1, left:right + w + 1]
        scores = cv2.matchTemplate(search, template, cv2.TM_CCORR)
        scores /= float(255 * 255 * filled)
        rows, cols = np.indices(scores.shape)
        dx = cols + left - x
        dy = rows + top - y
        distance = np.hypot(dx, dy)
        utility = scores - 0.08 * distance / max(1, shift_pixels)
        best_row, best_col = np.unravel_index(int(np.argmax(utility)), utility.shape)
        overlap = float(scores[best_row, best_col])
        if overlap < min_overlap:
            continue
        offset_x = int(dx[best_row, best_col]) * resolution_mm
        offset_y = int(dy[best_row, best_col]) * resolution_mm
        pixel_offset = np.asarray([
            int(dx[best_row, best_col]), int(dy[best_row, best_col])])
        for index, polygon in zip(valid_indices, polygons):
            cv2.fillPoly(occupied, [polygon + pixel_offset], 255)
            source = columns[index]
            item = dict(source)
            item["x"] = round(float(source["x"]) + offset_x, 3)
            item["y"] = round(float(source["y"]) + offset_y, 3)
            item["registration"] = {
                "method": "wall-mask-group-cv", "overlap": round(overlap, 3),
                "offset_mm": [round(offset_x, 3), round(offset_y, 3)],
                "group": group_number, "group_size": len(valid_indices),
            }
            registered.append(item)
    return registered


def _polygon_area(points: list[tuple[float, float]]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in
                   zip(points, points[1:] + points[:1]))) / 2.0


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def _clean_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cleaned = []
    for point in points:
        if not cleaned or math.dist(point, cleaned[-1]) > 1.0:
            cleaned.append(point)
    # Some exported EdgePaths repeat the first one or two edges after closing.
    # Revit rejects that otherwise valid ring as a self-overlapping CurveLoop.
    if len(cleaned) >= 4:
        for index in range(3, len(cleaned)):
            if math.dist(cleaned[index], cleaned[0]) <= 1.0:
                cleaned = cleaned[:index]
                break
    return cleaned


def _path_points(boundary) -> list[tuple[float, float]]:
    if hasattr(boundary, "vertices"):
        points = [(float(p[0]), float(p[1])) for p in boundary.vertices]
    else:
        path = from_hatch_boundary_path(boundary)
        points = [(float(p.x), float(p.y)) for p in path.flattening(1.0)]
    return _clean_points(points)


def _transform(meta: dict, point: tuple[float, float]) -> list[float]:
    cx, cy = meta["rotation_center_world_mm"]
    ox, oy = meta["origin_world_mm"]
    angle = math.radians(meta["rot_deg"])
    x, y = point[0] - cx, point[1] - cy
    rx = x * math.cos(angle) - y * math.sin(angle) + cx
    ry = x * math.sin(angle) + y * math.cos(angle) + cy
    return [round(rx - ox, 3), round(ry - oy, 3)]


def extract_boundary_columns(dxf_path: str | Path,
                             max_match_mm: float = 5000.0) -> dict:
    """Return one-to-one label/profile matches from the main structural plan."""
    dxf_path = str(dxf_path)
    adaptive = extract_columns_from_dxf(dxf_path)
    meta = adaptive.get("meta") or {}
    required = ("rotation_center_world_mm", "origin_world_mm", "rot_deg")
    if any(key not in meta for key in required):
        return {"columns": [], "unmatched_labels": [],
                "meta": {"error": "drawing transform unavailable"}}

    doc = ezdxf.readfile(dxf_path)
    labels = []
    profiles = []
    for entity in recursive_decompose(doc.modelspace()):
        layer = getattr(entity.dxf, "layer", "")
        if entity.dxftype() in ("TEXT", "MTEXT") and layer == LABEL_LAYER:
            text = (entity.plain_text() if entity.dxftype() == "MTEXT"
                    else entity.dxf.text)
            match = MARK_RE.search(text or "")
            if match:
                insert = entity.dxf.insert
                labels.append({"mark": match.group(0).upper().replace(" ", ""),
                               "point": (float(insert.x), float(insert.y))})
        elif entity.dxftype() == "HATCH" and layer == PROFILE_LAYER:
            # from_hatch applies the INSERT transform. Boundary vertices alone
            # remain in block-local coordinates and would shift profiles.
            for path in from_hatch(entity):
                try:
                    points = _clean_points([(float(point.x), float(point.y))
                                            for point in path.flattening(1.0)])
                except (TypeError, ValueError):
                    continue
                if len(points) < 3:
                    continue
                xs, ys = zip(*points)
                width, depth = max(xs) - min(xs), max(ys) - min(ys)
                area = _polygon_area(points)
                # Exclude whole wall/core hatches; edge elements are compact.
                if area < 50000.0 or width > 5000.0 or depth > 5000.0:
                    continue
                profiles.append({"points": points, "center": _centroid(points),
                                 "area_mm2": area})

    pairs = []
    for li, label in enumerate(labels):
        for pi, profile in enumerate(profiles):
            distance = math.dist(label["point"], profile["center"])
            if distance <= max_match_mm:
                pairs.append((distance, li, pi))
    used_labels, used_profiles, columns = set(), set(), []
    for distance, li, pi in sorted(pairs):
        if li in used_labels or pi in used_profiles:
            continue
        used_labels.add(li)
        used_profiles.add(pi)
        label, profile = labels[li], profiles[pi]
        world_center = profile["center"]
        center = _transform(meta, world_center)
        absolute = [_transform(meta, point) for point in profile["points"]]
        relative = [[round(p[0] - center[0], 3), round(p[1] - center[1], 3)]
                    for p in absolute]
        stable_id = "boundary_%s_%d_%d" % (
            label["mark"], int(round(center[0])), int(round(center[1])))
        columns.append({
            "type": "Column", "subtype": "BoundaryColumn",
            "id": stable_id, "type_name": label["mark"],
            "x": center[0], "y": center[1], "base": -6500.0, "top": -100.0,
            "profile": {"kind": "poly", "points": relative},
            "source": ["drawing_text", "drawing_hatch"],
            "confidence": round(max(0.7, 1.0 - distance / max_match_mm * 0.3), 3),
            "label_distance_mm": round(distance, 1),
            "area_mm2": round(profile["area_mm2"], 1),
        })
    return {
        "columns": columns,
        "unmatched_labels": [labels[i]["mark"] for i in range(len(labels))
                             if i not in used_labels],
        "meta": {"label_count": len(labels), "profile_count": len(profiles),
                 "matched_count": len(columns), "transform": meta},
    }
