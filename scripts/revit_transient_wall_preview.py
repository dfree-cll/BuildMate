"""IronPython payload for a no-save Revit wall preview.

This file is read by the local Revit MCP bridge and executed inside Revit 2020.
It intentionally creates only temporary native walls and never calls Document.Save.
"""
import io
import json
import math
import os

MM_PER_FOOT = 304.8
HEIGHT_MM = 6400.0
ENDPOINT_SNAP_MM = 10.0
COLLINEAR_TOL_MM = 10.0
ANGLE_TOL_DEG = 1.0
HOST_WALL_GAP_MM = 3000.0
JUNCTION_EXTRA_MM = 25.0
MIN_WALL_FRAGMENT_MM = 100.0
SOLID_OVERLAP_TOL_MM = 0.5
MARKER_PREFIX = "BM_PREVIEW_WALL:"
GRID_MARKER_PREFIX = "BM_PREVIEW_GRID:"
ARCH_PATH = os.environ.get("BUILDMATE_PREVIEW_ARCH_MODEL", "")
ARCH_GRID_PATH = os.environ.get("BUILDMATE_PREVIEW_ARCH_GRID", "")
STRUCT_PATH = os.environ.get("BUILDMATE_PREVIEW_STRUCT_MODEL", "")
STRUCT_GRID_PATH = os.environ.get("BUILDMATE_PREVIEW_STRUCT_GRID", "")
PDF_WALL_PATH = os.environ.get("BUILDMATE_PREVIEW_PDF_WALL", "")


def read_json(path):
    if not path:
        raise RuntimeError(
            "Revit preview input path is missing; set the BUILDMATE_PREVIEW_* environment variables"
        )
    with io.open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _pdf_primary_wall_inputs(reference):
    """Split the PDF wall reference while retaining a safe fallback contract."""
    items = [
        item for item in reference.get("model_elements", [])
        if item.get("type") == "Wall" and item.get("start") and item.get("end")
    ]
    if not items:
        return None
    return (
        [item for item in items if str(item.get("wall_group", "A")).upper() != "S"],
        [item for item in items if str(item.get("wall_group", "A")).upper() == "S"],
        reference.get("grid") or {},
    )


def _rotate(point, angle_deg, center):
    radians = math.radians(float(angle_deg))
    cosine, sine = math.cos(radians), math.sin(radians)
    x = float(point[0]) - float(center[0])
    y = float(point[1]) - float(center[1])
    return (
        x * cosine - y * sine + float(center[0]),
        x * sine + y * cosine + float(center[1]),
    )


def _grid_contract(grid):
    """Return the source drawing frame needed for a deterministic transform."""
    meta = grid.get("meta") or {}
    origin = grid.get("origin_mm")
    if not (isinstance(origin, list) and len(origin) >= 2):
        raise RuntimeError("图纸轴网缺少 origin_mm，禁止直接混用坐标")
    if not (meta.get("gcx") is not None and meta.get("gcy") is not None and
            meta.get("rot_deg") is not None):
        raise RuntimeError("图纸轴网缺少旋转/中心元数据，禁止直接混用坐标")
    return {
        "origin": (float(origin[0]), float(origin[1])),
        "center": (float(meta["gcx"]), float(meta["gcy"])),
        "rotation": float(meta["rot_deg"]),
    }


def _transform_point_between_grids(point_mm, source_grid, target_grid):
    """Transform source-local mm into target-local mm using declared grid metadata."""
    source = _grid_contract(source_grid)
    target = _grid_contract(target_grid)
    source_world = (
        float(point_mm[0]) + source["origin"][0],
        float(point_mm[1]) + source["origin"][1],
    )
    world = _rotate(source_world, -source["rotation"], source["center"])
    target_world = _rotate(world, target["rotation"], target["center"])
    return (
        target_world[0] - target["origin"][0],
        target_world[1] - target["origin"][1],
    )


def _transform_wall_to_target(item, source_grid, target_grid):
    """Copy a wall and normalize both endpoints into the architecture grid frame."""
    normalized = dict(item)
    normalized["start"] = list(_transform_point_between_grids(
        item["start"], source_grid, target_grid)) + list(item["start"][2:3])
    normalized["end"] = list(_transform_point_between_grids(
        item["end"], source_grid, target_grid)) + list(item["end"][2:3])
    return normalized


def wall_length(item):
    start, end = item["start"], item["end"]
    return math.hypot(float(end[0]) - float(start[0]), float(end[1]) - float(start[1]))


def in_scope(item):
    # The architecture 201/202 comparison scope in grid-local metres.
    x_min, x_max, y_min, y_max = -22.0, 210.0, -3.5, 58.0
    start, end = item["start"], item["end"]
    return not (
        max(float(start[0]), float(end[0])) / 1000.0 < x_min
        or min(float(start[0]), float(end[0])) / 1000.0 > x_max
        or max(float(start[1]), float(end[1])) / 1000.0 < y_min
        or min(float(start[1]), float(end[1])) / 1000.0 > y_max
    )


def _wall_info(item):
    start, end = item["start"], item["end"]
    dx = float(end[0]) - float(start[0])
    dy = float(end[1]) - float(start[1])
    length = math.hypot(dx, dy)
    if length < 1.0:
        return None
    return (start, end, length, dx / length, dy / length)


def _collinear_mergeable(left, right):
    first = _wall_info(left)
    second = _wall_info(right)
    if first is None or second is None:
        return False
    p, q, length, ux, uy = first
    r, s, other_length, vx, vy = second
    if abs(float(left.get("thickness") or 0.0) -
           float(right.get("thickness") or 0.0)) > 1.0:
        return False
    if abs(ux * vx + uy * vy) < math.cos(math.radians(ANGLE_TOL_DEG)):
        return False
    if (abs(ux * (r[1] - p[1]) - uy * (r[0] - p[0])) > COLLINEAR_TOL_MM or
            abs(ux * (s[1] - p[1]) - uy * (s[0] - p[0])) > COLLINEAR_TOL_MM):
        return False
    t0 = (float(r[0]) - float(p[0])) * ux + (float(r[1]) - float(p[1])) * uy
    t1 = (float(s[0]) - float(p[0])) * ux + (float(s[1]) - float(p[1])) * uy
    low, high = min(t0, t1), max(t0, t1)
    gap = max(0.0, max(0.0 - high, low - length))
    # Door/window symbols are not modeled in this preview.  Revit hosted
    # families cut their own openings later, so a measured same-line wall must
    # remain a continuous host across ordinary opening-sized interruptions.
    return gap <= HOST_WALL_GAP_MM


def _merge_collinear_walls(items):
    """Merge only same-thickness, same-line overlaps/near-touching segments."""
    merged = []
    for original in items:
        current = dict(original)
        while True:
            match_index = None
            for index, existing in enumerate(merged):
                if _collinear_mergeable(current, existing):
                    match_index = index
                    break
            if match_index is None:
                break
            existing = merged.pop(match_index)
            p, q, length, ux, uy = _wall_info(current)
            r, s, other_length, vx, vy = _wall_info(existing)
            projections = [0.0, length,
                           (float(r[0]) - float(p[0])) * ux +
                           (float(r[1]) - float(p[1])) * uy,
                           (float(s[0]) - float(p[0])) * ux +
                           (float(s[1]) - float(p[1])) * uy]
            low, high = min(projections), max(projections)
            current["start"] = [p[0] + ux * low, p[1] + uy * low, 0.0]
            current["end"] = [p[0] + ux * high, p[1] + uy * high, 0.0]
            current["source_ids"] = list(existing.get("source_ids") or
                                          [existing.get("id")]) + list(
                                              current.get("source_ids") or
                                              [current.get("id")])
        merged.append(current)
    return merged


def _deduplicate_exact_walls(items):
    """Drop exact duplicate centerlines; keep the thicker representation."""
    kept = []
    for item in items:
        duplicate = None
        info = _wall_info(item)
        if info is None:
            continue
        p, q, length, ux, uy = info
        for index, existing in enumerate(kept):
            other = _wall_info(existing)
            if other is None:
                continue
            r, s, other_length, vx, vy = other
            if abs(length - other_length) > ENDPOINT_SNAP_MM:
                continue
            if abs(ux * vx + uy * vy) < math.cos(math.radians(ANGLE_TOL_DEG)):
                continue
            direct = max(math.hypot(p[0] - r[0], p[1] - r[1]),
                         math.hypot(q[0] - s[0], q[1] - s[1]))
            reverse = max(math.hypot(p[0] - s[0], p[1] - s[1]),
                          math.hypot(q[0] - r[0], q[1] - r[1]))
            if min(direct, reverse) <= ENDPOINT_SNAP_MM:
                duplicate = index
                break
        if duplicate is None:
            kept.append(item)
        elif float(item.get("thickness") or 0.0) > float(
                kept[duplicate].get("thickness") or 0.0):
            kept[duplicate] = item
    return kept


def _snap_wall_endpoints(items):
    """Snap tiny endpoint noise to shared junctions before Revit creates walls."""
    points = []
    for item_index, item in enumerate(items):
        for key in ("start", "end"):
            point = item[key]
            points.append([item_index, key, float(point[0]), float(point[1])])
    clusters = []
    membership = []
    for point in points:
        assigned = None
        for index, cluster in enumerate(clusters):
            if math.hypot(point[2] - cluster[0], point[3] - cluster[1]) <= ENDPOINT_SNAP_MM:
                assigned = index
                break
        if assigned is None:
            clusters.append([point[2], point[3], 1])
            assigned = len(clusters) - 1
        else:
            cluster = clusters[assigned]
            count = cluster[2] + 1
            cluster[0] = (cluster[0] * cluster[2] + point[2]) / count
            cluster[1] = (cluster[1] * cluster[2] + point[3]) / count
            cluster[2] = count
        membership.append(assigned)
    for point, cluster_index in zip(points, membership):
        item = items[point[0]]
        item[point[1]][0] = clusters[cluster_index][0]
        item[point[1]][1] = clusters[cluster_index][1]
    return items


def _snap_near_axis_walls(items):
    """Remove millimetre-scale raster skew from walls intended to be orthogonal."""
    for item in items:
        info = _wall_info(item)
        if info is None:
            continue
        start, end, length, ux, uy = info
        if abs(float(end[1]) - float(start[1])) <= ENDPOINT_SNAP_MM and abs(ux) >= abs(uy):
            coordinate = (float(start[1]) + float(end[1])) / 2.0
            start[1] = coordinate
            end[1] = coordinate
        elif abs(float(end[0]) - float(start[0])) <= ENDPOINT_SNAP_MM and abs(uy) > abs(ux):
            coordinate = (float(start[0]) + float(end[0])) / 2.0
            start[0] = coordinate
            end[0] = coordinate
    return items


def _line_intersection(first, second):
    """Return the infinite-line intersection and parameters, or None."""
    p, q = first
    r, s = second
    ax, ay = float(q[0]) - float(p[0]), float(q[1]) - float(p[1])
    bx, by = float(s[0]) - float(r[0]), float(s[1]) - float(r[1])
    denominator = ax * by - ay * bx
    if abs(denominator) <= 1.0e-9:
        return None
    rx, ry = float(r[0]) - float(p[0]), float(r[1]) - float(p[1])
    first_ratio = (rx * by - ry * bx) / denominator
    second_ratio = (rx * ay - ry * ax) / denominator
    return ([float(p[0]) + first_ratio * ax,
             float(p[1]) + first_ratio * ay, 0.0],
            first_ratio, second_ratio)


def _nearest_endpoint(item, point, tolerance_mm):
    choices = []
    for key in ("start", "end"):
        endpoint = item[key]
        distance = math.hypot(
            float(endpoint[0]) - float(point[0]),
            float(endpoint[1]) - float(point[1]))
        if distance <= tolerance_mm:
            choices.append((distance, key))
    return min(choices)[1] if choices else None


def _snap_wall_junctions(items):
    """Extend nearby endpoints to exact T/L/Z centerline intersections."""
    for index in range(len(items)):
        left = items[index]
        left_info = _wall_info(left)
        if left_info is None:
            continue
        for other_index in range(index + 1, len(items)):
            right = items[other_index]
            right_info = _wall_info(right)
            if right_info is None:
                continue
            left_start, left_end, left_length, left_ux, left_uy = left_info
            right_start, right_end, right_length, right_ux, right_uy = right_info
            if abs(left_ux * right_uy - left_uy * right_ux) < 0.10:
                continue
            intersection = _line_intersection(
                (left_start, left_end), (right_start, right_end))
            if intersection is None:
                continue
            point, left_ratio, right_ratio = intersection
            left_tolerance = float(left.get("thickness") or
                                   left.get("thickness_mm") or 200.0) / 2.0 + JUNCTION_EXTRA_MM
            right_tolerance = float(right.get("thickness") or
                                    right.get("thickness_mm") or 200.0) / 2.0 + JUNCTION_EXTRA_MM
            left_key = _nearest_endpoint(left, point, left_tolerance)
            right_key = _nearest_endpoint(right, point, right_tolerance)
            left_contains = -1.0e-6 <= left_ratio <= 1.0 + 1.0e-6
            right_contains = -1.0e-6 <= right_ratio <= 1.0 + 1.0e-6
            if left_key is not None and right_contains:
                left[left_key] = list(point)
            if right_key is not None and left_contains:
                right[right_key] = list(point)
    return items


def _subtract_structural_overlaps(architecture_walls, structural_walls):
    """Remove architecture runs already occupied by a structural wall solid."""
    result = []
    for item in architecture_walls:
        info = _wall_info(item)
        if info is None:
            continue
        start, end, length, ux, uy = info
        architecture_thickness = float(item.get("thickness") or
                                       item.get("thickness_mm") or 200.0)
        occupied = []
        for structural in structural_walls:
            other = _wall_info(structural)
            if other is None:
                continue
            other_start, other_end, other_length, vx, vy = other
            if abs(ux * vx + uy * vy) < math.cos(math.radians(ANGLE_TOL_DEG)):
                continue
            structural_thickness = float(structural.get("thickness") or
                                         structural.get("thickness_mm") or 400.0)
            allowed_offset = ((architecture_thickness + structural_thickness) /
                              2.0 - SOLID_OVERLAP_TOL_MM)
            distances = [
                abs(ux * (float(point[1]) - float(start[1])) -
                    uy * (float(point[0]) - float(start[0])))
                for point in (other_start, other_end)
            ]
            if max(distances) > allowed_offset:
                continue
            projections = [
                (float(point[0]) - float(start[0])) * ux +
                (float(point[1]) - float(start[1])) * uy
                for point in (other_start, other_end)
            ]
            low = max(0.0, min(projections))
            high = min(length, max(projections))
            if high - low > SOLID_OVERLAP_TOL_MM:
                occupied.append((low, high))
        if not occupied:
            result.append(item)
            continue
        occupied.sort()
        merged = []
        for low, high in occupied:
            if merged and low <= merged[-1][1] + COLLINEAR_TOL_MM:
                merged[-1] = (merged[-1][0], max(merged[-1][1], high))
            else:
                merged.append((low, high))
        cursor = 0.0
        residuals = []
        for low, high in merged:
            if low - cursor >= MIN_WALL_FRAGMENT_MM:
                residuals.append((cursor, low))
            cursor = max(cursor, high)
        if length - cursor >= MIN_WALL_FRAGMENT_MM:
            residuals.append((cursor, length))
        for fragment_index, (low, high) in enumerate(residuals):
            fragment = dict(item)
            fragment["start"] = [float(start[0]) + ux * low,
                                 float(start[1]) + uy * low, 0.0]
            fragment["end"] = [float(start[0]) + ux * high,
                               float(start[1]) + uy * high, 0.0]
            fragment["id"] = "%s:structural-trim:%d" % (
                item.get("id", "wall"), fragment_index)
            result.append(fragment)
    return result


def _resolve_parallel_wall_overlaps(items):
    """Keep one physical wall where offset centerlines create overlapping solids."""
    def priority(item):
        thickness = float(item.get("thickness") or
                          item.get("thickness_mm") or 200.0)
        return (thickness, wall_length(item))

    accepted = []
    for original in sorted(items, key=priority, reverse=True):
        fragments = [original]
        for authority in accepted:
            authority_info = _wall_info(authority)
            if authority_info is None:
                continue
            authority_start, authority_end, authority_length, vx, vy = authority_info
            authority_thickness = float(authority.get("thickness") or
                                        authority.get("thickness_mm") or 200.0)
            next_fragments = []
            for fragment in fragments:
                info = _wall_info(fragment)
                if info is None:
                    continue
                start, end, length, ux, uy = info
                if abs(ux * vx + uy * vy) < math.cos(math.radians(ANGLE_TOL_DEG)):
                    next_fragments.append(fragment)
                    continue
                fragment_thickness = float(fragment.get("thickness") or
                                           fragment.get("thickness_mm") or 200.0)
                maximum_offset = ((fragment_thickness + authority_thickness) /
                                  2.0 - SOLID_OVERLAP_TOL_MM)
                offsets = [
                    abs(ux * (float(point[1]) - float(start[1])) -
                        uy * (float(point[0]) - float(start[0])))
                    for point in (authority_start, authority_end)
                ]
                if max(offsets) > maximum_offset:
                    next_fragments.append(fragment)
                    continue
                projections = [
                    (float(point[0]) - float(start[0])) * ux +
                    (float(point[1]) - float(start[1])) * uy
                    for point in (authority_start, authority_end)
                ]
                low = max(0.0, min(projections))
                high = min(length, max(projections))
                if high - low <= SOLID_OVERLAP_TOL_MM:
                    next_fragments.append(fragment)
                    continue
                residuals = []
                if low >= MIN_WALL_FRAGMENT_MM:
                    residuals.append((0.0, low))
                if length - high >= MIN_WALL_FRAGMENT_MM:
                    residuals.append((high, length))
                for fragment_index, (residual_low, residual_high) in enumerate(residuals):
                    residual = dict(fragment)
                    residual["start"] = [
                        float(start[0]) + ux * residual_low,
                        float(start[1]) + uy * residual_low, 0.0]
                    residual["end"] = [
                        float(start[0]) + ux * residual_high,
                        float(start[1]) + uy * residual_high, 0.0]
                    residual["id"] = "%s:solid-trim:%d" % (
                        fragment.get("id", "wall"), fragment_index)
                    next_fragments.append(residual)
            fragments = next_fragments
            if not fragments:
                break
        accepted.extend(fragments)
    return accepted


def _flatten_connected_axis_chains(items):
    """Give connected near-axis wall chains one exact shared axis coordinate."""
    axes = []
    for item in items:
        start, end = item["start"], item["end"]
        dx = abs(float(end[0]) - float(start[0]))
        dy = abs(float(end[1]) - float(start[1]))
        if dx <= ENDPOINT_SNAP_MM and dy > dx:
            axes.append(("V", (float(start[0]) + float(end[0])) / 2.0))
        elif dy <= ENDPOINT_SNAP_MM and dx >= dy:
            axes.append(("H", (float(start[1]) + float(end[1])) / 2.0))
        else:
            axes.append((None, 0.0))
    parents = list(range(len(items)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for index in range(len(items)):
        orientation, coordinate = axes[index]
        if orientation is None:
            continue
        left_points = (items[index]["start"], items[index]["end"])
        for other_index in range(index + 1, len(items)):
            other_orientation, other_coordinate = axes[other_index]
            if (other_orientation != orientation or
                    abs(coordinate - other_coordinate) > ENDPOINT_SNAP_MM):
                continue
            right_points = (items[other_index]["start"],
                            items[other_index]["end"])
            if min(math.hypot(float(left[0]) - float(right[0]),
                              float(left[1]) - float(right[1]))
                   for left in left_points for right in right_points) <= ENDPOINT_SNAP_MM:
                union(index, other_index)
    groups = {}
    for index, axis in enumerate(axes):
        if axis[0] is not None:
            groups.setdefault(find(index), []).append(index)
    for indexes in groups.values():
        coordinate = sum(axes[index][1] for index in indexes) / len(indexes)
        orientation = axes[indexes[0]][0]
        for index in indexes:
            if orientation == "V":
                items[index]["start"][0] = coordinate
                items[index]["end"][0] = coordinate
            else:
                items[index]["start"][1] = coordinate
                items[index]["end"][1] = coordinate
    return items


def _mark_parallel_transition_joins(items):
    """Prevent Revit from extending a butt end into an offset parallel wall."""
    for item in items:
        item["disallow_join_ends"] = []
    for index, item in enumerate(items):
        info = _wall_info(item)
        if info is None:
            continue
        start, end, length, ux, uy = info
        thickness = float(item.get("thickness") or
                          item.get("thickness_mm") or 200.0)
        for other_index, other in enumerate(items):
            if index == other_index:
                continue
            other_info = _wall_info(other)
            if other_info is None:
                continue
            other_start, other_end, other_length, vx, vy = other_info
            if abs(ux * vx + uy * vy) < math.cos(math.radians(ANGLE_TOL_DEG)):
                continue
            other_thickness = float(other.get("thickness") or
                                    other.get("thickness_mm") or 200.0)
            maximum_offset = ((thickness + other_thickness) / 2.0 -
                              SOLID_OVERLAP_TOL_MM)
            offsets = [
                abs(ux * (float(point[1]) - float(start[1])) -
                    uy * (float(point[0]) - float(start[0])))
                for point in (other_start, other_end)
            ]
            if max(offsets) > maximum_offset:
                continue
            projections = [
                (float(point[0]) - float(start[0])) * ux +
                (float(point[1]) - float(start[1])) * uy
                for point in (other_start, other_end)
            ]
            low, high = min(projections), max(projections)
            transition_tolerance = ((thickness + other_thickness) / 2.0 +
                                    JUNCTION_EXTRA_MM)
            if -transition_tolerance <= high <= transition_tolerance:
                item["disallow_join_ends"].append(0)
            if -transition_tolerance <= low - length <= transition_tolerance:
                item["disallow_join_ends"].append(1)
        item["disallow_join_ends"] = sorted(set(
            item["disallow_join_ends"]))
    return items


def clean_wall_segments(items):
    cleaned = _deduplicate_exact_walls(items)
    cleaned = _snap_wall_endpoints(cleaned)
    cleaned = _snap_near_axis_walls(cleaned)
    cleaned = _merge_collinear_walls(cleaned)
    cleaned = _snap_wall_junctions(cleaned)
    return _resolve_parallel_wall_overlaps(cleaned)


def _proper_segment_intersection(first, second):
    """True only when two centerlines cross in their interiors."""
    a, b = first
    c, d = second
    def cross(p, q, r):
        return ((q[0] - p[0]) * (r[1] - p[1]) -
                (q[1] - p[1]) * (r[0] - p[0]))
    o1, o2 = cross(a, b, c), cross(a, b, d)
    o3, o4 = cross(c, d, a), cross(c, d, b)
    return ((o1 > 1.0e-6 and o2 < -1.0e-6) or
            (o1 < -1.0e-6 and o2 > 1.0e-6)) and ((
                o3 > 1.0e-6 and o4 < -1.0e-6) or
                (o3 < -1.0e-6 and o4 > 1.0e-6))


def join_crossing_walls(DB, doc, wall_records):
    """Join genuine interior crossings without forcing unrelated near walls."""
    geometry = []
    for record in wall_records:
        wall, prefix = record[0], record[1]
        try:
            curve = wall.Location.Curve
            first, second = curve.GetEndPoint(0), curve.GetEndPoint(1)
            geometry.append((wall, prefix,
                             (first.X * MM_PER_FOOT, first.Y * MM_PER_FOOT),
                             (second.X * MM_PER_FOOT, second.Y * MM_PER_FOOT)))
        except Exception:
            pass
    joined = 0
    for index in range(len(geometry)):
        left, left_prefix, left_start, left_end = geometry[index]
        for other_index in range(index + 1, len(geometry)):
            right, right_prefix, right_start, right_end = geometry[other_index]
            if left_prefix != right_prefix:
                continue
            if not _proper_segment_intersection(
                    (left_start, left_end), (right_start, right_end)):
                continue
            try:
                if not DB.JoinGeometryUtils.AreElementsJoined(doc, left, right):
                    DB.JoinGeometryUtils.JoinGeometry(doc, left, right)
                    joined += 1
            except Exception:
                pass
    return joined


def _project_base_offset_mm(DB, doc):
    """Resolve the only allowed project translation: the active Revit base point."""
    try:
        base_points = list(DB.FilteredElementCollector(doc)
                           .OfCategory(DB.BuiltInCategory.OST_ProjectBasePoint)
                           .WhereElementIsNotElementType())
        if base_points:
            position = base_points[0].Position
            return [float(position.X) * MM_PER_FOOT,
                    float(position.Y) * MM_PER_FOOT]
    except Exception:
        pass
    return [0.0, 0.0]


def _set_grid_marker(DB, grid):
    try:
        parameter = grid.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if parameter is not None and not parameter.IsReadOnly:
            parameter.Set(GRID_MARKER_PREFIX + "1")
    except Exception:
        pass


def _grid_name(axis, index, item):
    label = item.get("label")
    if label is not None and str(label).strip():
        return "G-%s" % str(label).strip()
    return "G-%s%d" % (axis, index + 1)


def _existing_grid(DB, doc, axis, coordinate_ft):
    for grid in DB.FilteredElementCollector(doc).OfClass(DB.Grid):
        try:
            curve = grid.Curve
            first, second = curve.GetEndPoint(0), curve.GetEndPoint(1)
            if axis == "x":
                if abs(first.X - second.X) < 1.0e-7 and abs(first.X - coordinate_ft) < 1.0e-6:
                    return grid
            elif abs(first.Y - second.Y) < 1.0e-7 and abs(first.Y - coordinate_ft) < 1.0e-6:
                return grid
        except Exception:
            pass
    return None


def create_axis_grids(DB, doc, grid_data, level, view, project_offset_mm):
    """Create and label canonical axes before any wall transaction work."""
    x_axes = grid_data.get("x_axes") or []
    y_axes = grid_data.get("y_axes") or []
    if not x_axes or not y_axes:
        raise RuntimeError("建筑轴网为空，禁止建墙")
    dx, dy = float(project_offset_mm[0]), float(project_offset_mm[1])
    x_values = [float(item.get("coord")) * 1000.0 + dx for item in x_axes]
    y_values = [float(item.get("coord")) * 1000.0 + dy for item in y_axes]
    xmin, xmax = min(x_values) - 2000.0, max(x_values) + 2000.0
    ymin, ymax = min(y_values) - 2000.0, max(y_values) + 2000.0
    created = 0
    handles = []
    for index, item in enumerate(x_axes):
        coordinate = float(item.get("coord")) * 1000.0 + dx
        existing = _existing_grid(DB, doc, "x", coordinate / MM_PER_FOOT)
        if existing is not None:
            handles.append(existing)
            continue
        line = DB.Line.CreateBound(
            DB.XYZ(coordinate / MM_PER_FOOT, ymin / MM_PER_FOOT, level.Elevation),
            DB.XYZ(coordinate / MM_PER_FOOT, ymax / MM_PER_FOOT, level.Elevation))
        grid = DB.Grid.Create(doc, line)
        try:
            grid.Name = _grid_name("X", index, item)
        except Exception:
            pass
        _set_grid_marker(DB, grid)
        handles.append(grid)
        created += 1
    for index, item in enumerate(y_axes):
        coordinate = float(item.get("coord")) * 1000.0 + dy
        existing = _existing_grid(DB, doc, "y", coordinate / MM_PER_FOOT)
        if existing is not None:
            handles.append(existing)
            continue
        line = DB.Line.CreateBound(
            DB.XYZ(xmin / MM_PER_FOOT, coordinate / MM_PER_FOOT, level.Elevation),
            DB.XYZ(xmax / MM_PER_FOOT, coordinate / MM_PER_FOOT, level.Elevation))
        grid = DB.Grid.Create(doc, line)
        try:
            grid.Name = _grid_name("Y", index, item)
        except Exception:
            pass
        _set_grid_marker(DB, grid)
        handles.append(grid)
        created += 1
    grid_overrides = DB.OverrideGraphicSettings()
    grid_overrides.SetProjectionLineColor(DB.Color(90, 180, 90))
    try:
        grid_overrides.SetCutLineColor(DB.Color(90, 180, 90))
    except Exception:
        pass
    for grid in handles:
        try:
            view.SetElementOverrides(grid.Id, grid_overrides)
        except Exception:
            pass
    return created, handles


def basic_wall_types(DB, doc):
    return [
        item for item in DB.FilteredElementCollector(doc).OfClass(DB.WallType)
        if getattr(item, "FamilyName", "") in ("Basic Wall", u"\u57fa\u672c\u5899")
    ]


def type_label(DB, item):
    try:
        return getattr(item, "Name", "") or str(item.Id.IntegerValue)
    except Exception:
        return str(item.Id.IntegerValue)


def get_preview_type(DB, doc, cache, prefix, thickness_mm):
    key = (prefix, int(round(float(thickness_mm))))
    if key in cache:
        return cache[key]
    target_name = "%s_%dmm" % (prefix, key[1])
    for item in basic_wall_types(DB, doc):
        if type_label(DB, item) == target_name:
            cache[key] = item
            return item
    base_types = basic_wall_types(DB, doc)
    if not base_types:
        raise RuntimeError("当前项目没有可用的 Basic Wall 类型")
    try:
        wall_type = base_types[0].Duplicate(target_name)
    except Exception:
        # Duplicate names can survive an earlier unsaved preview transaction.
        # Reuse the closest basic type rather than aborting all wall creation.
        wall_type = min(base_types, key=lambda item: abs(item.Width * MM_PER_FOOT - key[1]))
    try:
        structure = wall_type.GetCompoundStructure()
        widths = [structure.GetLayerWidth(index) for index in range(structure.LayerCount)]
        target_ft = key[1] / MM_PER_FOOT
        thickest = max(range(len(widths)), key=lambda index: widths[index])
        structure.SetLayerWidth(thickest, widths[thickest] + target_ft - sum(widths))
        wall_type.SetCompoundStructure(structure)
    except Exception:
        # A preview still has value if a template has an unusual compound wall;
        # the instance is retained and the original type remains untouched.
        pass
    cache[key] = wall_type
    return wall_type


def delete_old_preview(DB, doc):
    deleted = 0
    for wall in list(DB.FilteredElementCollector(doc).OfClass(DB.Wall).WhereElementIsNotElementType()):
        try:
            parameter = wall.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            marker = parameter.AsString() if parameter is not None else ""
            if marker and marker.startswith(MARKER_PREFIX):
                doc.Delete(wall.Id)
                deleted += 1
        except Exception:
            pass
    return deleted


def create_preview_wall(DB, doc, level, item, prefix, structural, wall_type, view, colour,
                        project_offset_mm):
    start, end = item["start"], item["end"]
    dx, dy = float(project_offset_mm[0]), float(project_offset_mm[1])
    x0, y0 = (float(start[0]) + dx) / MM_PER_FOOT, (float(start[1]) + dy) / MM_PER_FOOT
    x1, y1 = (float(end[0]) + dx) / MM_PER_FOOT, (float(end[1]) + dy) / MM_PER_FOOT
    if math.hypot(x1 - x0, y1 - y0) < 0.02:
        return None
    line = DB.Line.CreateBound(DB.XYZ(x0, y0, level.Elevation), DB.XYZ(x1, y1, level.Elevation))
    wall = DB.Wall.Create(doc, line, level.Id, bool(structural))
    # Keep native joins enabled.  The input has already been endpoint-snapped
    # and collinear overlaps merged, so Revit can form real T/L junctions.
    blocked_joins = set(item.get("disallow_join_ends") or [])
    for end_index in (0, 1):
        try:
            if end_index in blocked_joins:
                DB.WallUtils.DisallowWallJoinAtEnd(wall, end_index)
            else:
                DB.WallUtils.AllowWallJoinAtEnd(wall, end_index)
        except Exception:
            pass
    if wall.GetTypeId() != wall_type.Id:
        wall.ChangeTypeId(wall_type.Id)
    height = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
    if height is not None and not height.IsReadOnly:
        height.Set(HEIGHT_MM / MM_PER_FOOT)
    comments = wall.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if comments is not None and not comments.IsReadOnly:
        comments.Set(MARKER_PREFIX + prefix)
    overrides = DB.OverrideGraphicSettings()
    overrides.SetProjectionLineColor(DB.Color(colour[0], colour[1], colour[2]))
    try:
        overrides.SetCutLineColor(DB.Color(colour[0], colour[1], colour[2]))
    except Exception:
        pass
    view.SetElementOverrides(wall.Id, overrides)
    return wall


def main():
    arch = read_json(ARCH_PATH)
    arch_grid = read_json(ARCH_GRID_PATH)
    structural = read_json(STRUCT_PATH)
    struct_grid = read_json(STRUCT_GRID_PATH)
    pdf_reference = None
    try:
        if os.path.isfile(PDF_WALL_PATH):
            pdf_reference = read_json(PDF_WALL_PATH)
    except Exception:
        pdf_reference = None
    primary_inputs = _pdf_primary_wall_inputs(pdf_reference or {})
    if primary_inputs is not None:
        # PDF wall layers are authoritative for this preview.  DXF-derived
        # inputs remain available only as a fail-safe if the PDF reference is
        # unavailable or empty.
        architecture_walls, structural_walls, pdf_grid = primary_inputs
        if pdf_grid.get("x_axes") and pdf_grid.get("y_axes"):
            arch_grid = pdf_grid
        input_source = "PDF_PRIMARY"
    else:
        architecture_walls = [
            item for item in arch.get("model_elements", [])
            if item.get("type") == "Wall" and item.get("start") and item.get("end")
        ]
        # The structure pipeline uses its own 1/A local origin.  Normalize it
        # to the architecture grid frame from declared grid metadata.
        structural_walls = []
        for item in structural.get("model_elements", []):
            if item.get("type") != "Wall" or not item.get("start") or not item.get("end"):
                continue
            normalized = _transform_wall_to_target(item, struct_grid, arch_grid)
            if in_scope(normalized):
                structural_walls.append(normalized)
        input_source = "DXF_FALLBACK"
    architecture_input_count = len(architecture_walls)
    structural_input_count = len(structural_walls)
    architecture_walls = clean_wall_segments(architecture_walls)
    structural_walls = clean_wall_segments(structural_walls)
    # The architectural and structural drawings can describe the same physical
    # wall with different thicknesses/location lines.  Keep the structural
    # solid and retain only genuinely uncovered architectural runs.
    architecture_walls = _subtract_structural_overlaps(
        architecture_walls, structural_walls)
    _snap_wall_junctions(architecture_walls + structural_walls)
    _snap_wall_endpoints(architecture_walls + structural_walls)
    # Junction extension can reintroduce a short parallel overlap at a corner.
    # Resolve once more after topology is final, then flatten connected axis
    # chains to remove sub-millimetre Revit off-axis warnings.
    architecture_walls = _resolve_parallel_wall_overlaps(architecture_walls)
    structural_walls = _resolve_parallel_wall_overlaps(structural_walls)
    architecture_walls = _subtract_structural_overlaps(
        architecture_walls, structural_walls)
    _flatten_connected_axis_chains(architecture_walls + structural_walls)
    _mark_parallel_transition_joins(architecture_walls + structural_walls)
    levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements())
    level = next((item for item in levels if str(item.Name) == "标高 1"), None)
    if level is None:
        level = next((item for item in levels if abs(item.Elevation) < 0.01), None)
    if level is None:
        raise RuntimeError("找不到标高 1 / 0.0ft 建模标高")
    view = uidoc.ActiveView
    cache = {}
    created_architecture = 0
    created_structural = 0
    created_walls = []
    failures = []
    transaction = DB.Transaction(doc, "BuildMate 临时墙体预览（不保存）")
    transaction.Start()
    try:
        deleted = delete_old_preview(DB, doc)
        # Axis-first contract: create/resolve the canonical G axes and only
        # then place walls in that same base-point coordinate frame.
        project_offset_mm = _project_base_offset_mm(DB, doc)
        created_grids, _ = create_axis_grids(
            DB, doc, arch_grid, level, view, project_offset_mm)
        doc.Regenerate()
        for item in architecture_walls:
            try:
                thickness = float(item.get("thickness") or item.get("thickness_mm") or 200.0)
                wall_type = get_preview_type(DB, doc, cache, "BM_PREVIEW_A_WALL", thickness)
                wall = create_preview_wall(DB, doc, level, item, "A", False, wall_type, view, (40, 130, 220), project_offset_mm)
                if wall:
                    created_walls.append((wall, "A", item))
                    created_architecture += 1
            except Exception as error:
                failures.append("A:%s:%s" % (item.get("id", "?"), str(error)[:100]))
        for item in structural_walls:
            try:
                thickness = float(item.get("thickness") or item.get("thickness_mm") or 400.0)
                wall_type = get_preview_type(DB, doc, cache, "BM_PREVIEW_S_WALL", thickness)
                wall = create_preview_wall(DB, doc, level, item, "S", True, wall_type, view, (220, 45, 45), project_offset_mm)
                if wall:
                    created_walls.append((wall, "S", item))
                    created_structural += 1
            except Exception as error:
                failures.append("S:%s:%s" % (item.get("id", "?"), str(error)[:100]))
        doc.Regenerate()
        # Re-apply native join permission after all adjacent walls exist.
        for wall, prefix, item in created_walls:
            blocked_joins = set(item.get("disallow_join_ends") or [])
            for end_index in (0, 1):
                try:
                    if end_index in blocked_joins:
                        DB.WallUtils.DisallowWallJoinAtEnd(wall, end_index)
                    else:
                        DB.WallUtils.AllowWallJoinAtEnd(wall, end_index)
                except Exception:
                    pass
        crossing_joins = join_crossing_walls(DB, doc, created_walls)
        doc.Regenerate()
        transaction.Commit()
    except Exception:
        try:
            transaction.RollBack()
        except Exception:
            pass
        raise
    try:
        for open_view in uidoc.GetOpenUIViews():
            if open_view.ViewId == view.Id:
                open_view.ZoomToFit()
                break
        uidoc.RefreshActiveView()
    except Exception:
        pass
    print("TEMP_PREVIEW source=%s deleted=%d grids=%d architecture=%d structural=%d total=%d failures=%d level_id=%d base_offset_mm=%.3f,%.3f cleaned=%d+%d->%d+%d crossing_joins=%d" % (
        input_source, deleted, created_grids, created_architecture, created_structural,
        created_architecture + created_structural, len(failures), level.Id.IntegerValue,
        project_offset_mm[0], project_offset_mm[1], architecture_input_count,
        structural_input_count, len(architecture_walls), len(structural_walls), crossing_joins))
    if failures:
        print("FAILURES " + " | ".join(failures[:10]))


main()
