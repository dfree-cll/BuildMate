# -*- coding: utf-8 -*-
"""Pure-Python wall_model.json validator used before any Revit transaction.

Keep this module IronPython-compatible: it is copied next to ``script.py`` by
``scripts/sync_revit_ext.py``.
"""
import hashlib
import json
import math


def _finite_point(value, field):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError("%s must contain x and y" % field)
    point = [float(value[0]), float(value[1])]
    if any(math.isnan(item) or math.isinf(item) for item in point):
        raise ValueError("%s contains non-finite coordinate" % field)
    return point


def _normalize_opening_cut(opening, walls, level):
    status = opening.get("cut_status") or "review_required"
    if status not in ("ready", "review_required"):
        raise ValueError("unsupported opening cut status")
    if status != "ready":
        return {"cut_status": status}
    hosts = opening.get("host_wall_ids") or []
    refs = opening.get("specification_refs") or []
    if (opening.get("status") != "matched" or len(hosts) != 1
            or len(opening.get("boundary_m") or []) < 3
            or not refs or any(ref not in (opening.get("source_refs") or []) for ref in refs)):
        raise ValueError("opening cut requires matched geometry, one host and specification evidence")
    start = _finite_point(opening.get("cut_start_m"), "opening.cut_start_m")
    end = _finite_point(opening.get("cut_end_m"), "opening.cut_end_m")
    bottom = float(opening["base_elevation_m"])
    top = float(opening["top_elevation_m"])
    spec = opening.get("specification_mm") or {}
    width, height = float(spec.get("width_mm") or 0), float(spec.get("height_mm") or 0)
    if any(math.isnan(v) or math.isinf(v) for v in (bottom, top, width, height)):
        raise ValueError("opening cut contains non-finite dimensions/elevations")
    if (width <= 0 or height <= 0 or top <= bottom
            or abs(math.hypot(end[0] - start[0], end[1] - start[1]) * 1000 - width) > 0.01
            or abs((top - bottom) * 1000 - height) > 0.01):
        raise ValueError("opening cut dimensions disagree with specification")
    host = next((wall for wall in walls if wall.get("wall_id") == hosts[0]), None)
    if host is None:
        raise ValueError("opening cut references an unknown host")
    host_start = _finite_point(host.get("start_m"), "host.start_m")
    host_end = _finite_point(host.get("end_m"), "host.end_m")
    length = math.hypot(host_end[0] - host_start[0], host_end[1] - host_start[1])
    if length <= 0:
        raise ValueError("opening host is degenerate")
    dx, dy = (host_end[0] - host_start[0]) / length, (host_end[1] - host_start[1]) / length
    for point in (start, end):
        px, py = point[0] - host_start[0], point[1] - host_start[1]
        if abs(px * dy - py * dx) > 0.001 or not 0.005 <= px * dx + py * dy <= length - 0.005:
            raise ValueError("opening cut is outside its host wall")
    floor = float(level.get("elevation_m") or 0)
    if bottom < floor - 0.00001 or top > floor + float(host["height_m"]) + 0.00001:
        raise ValueError("opening cut is outside host vertical envelope")
    return {
        "cut_status": "ready", "cut_start": [v * 1000 for v in start],
        "cut_end": [v * 1000 for v in end], "base_elevation": bottom * 1000,
        "top_elevation": top * 1000, "cut_width": width, "cut_height": height,
        "elevation_source": str(opening.get("elevation_source") or "unresolved"),
    }


def normalize_wall_model_payload(data):
    """Validate an approved wall model and compile the existing Revit DTO."""
    if not isinstance(data, dict):
        raise ValueError("wall model root must be an object")
    if data.get("artifact_type") != "wall_model":
        return data  # Legacy model.json remains on its existing guarded path.
    if data.get("schema_version") != "buildmate.wall-model/1.0":
        raise ValueError("unsupported wall model schema_version")
    gate = data.get("gate") or {}
    if gate.get("status") != "pass":
        raise ValueError("wall model review gate is not passed")
    if data.get("review_status") != "approved":
        raise ValueError("wall model has not been approved by a human")
    approval = data.get("approval") or {}
    if approval.get("status") != "approved" or not approval.get("actor_id"):
        raise ValueError("wall model approval record is incomplete")
    if data.get("units") != "m":
        raise ValueError("wall model units must be m")
    project_id = str(data.get("project_id") or "")
    tenant_id = str(data.get("tenant_id") or "")
    if not project_id or not tenant_id:
        raise ValueError("tenant_id and project_id are required")
    level = data.get("level") or {}
    level_id = str(level.get("id") or "")
    level_name = str(level.get("name") or "")
    if not level_id or not level_name:
        raise ValueError("wall model level is incomplete")
    revit = data.get("revit") or {}
    target_model_path = str(revit.get("target_model_path") or "")
    floor_code = str(revit.get("floor_code") or "")
    if not target_model_path or not floor_code:
        raise ValueError("Revit target_model_path and floor_code are required")
    origin = str(data.get("coordinate_origin") or "")
    if origin not in (
        "source_origin", "revit_project_base_point", "revit_survey_point"
    ):
        raise ValueError("Revit worker does not support coordinate origin: " + origin)

    source_bounds = data.get("source_bounds_m")
    if source_bounds is not None:
        if not isinstance(source_bounds, (list, tuple)) or len(source_bounds) != 4:
            raise ValueError("wall model source_bounds_m must contain min x/y and max x/y")
        source_bounds = [float(value) for value in source_bounds]
        if any(math.isnan(value) or math.isinf(value) for value in source_bounds):
            raise ValueError("wall model source_bounds_m contains non-finite values")
        if source_bounds[2] <= source_bounds[0] or source_bounds[3] <= source_bounds[1]:
            raise ValueError("wall model source_bounds_m must have positive extents")

    elements = []
    wall_ids = set()
    for index, wall in enumerate(data.get("walls") or []):
        evidence_ids = wall.get("evidence_ids") or []
        source_refs = wall.get("source_refs") or []
        if not evidence_ids or not source_refs:
            raise ValueError("wall %d has no geometric evidence" % index)
        start = _finite_point(wall.get("start_m"), "wall.start_m")
        end = _finite_point(wall.get("end_m"), "wall.end_m")
        if math.hypot(start[0] - end[0], start[1] - end[1]) <= 1.0e-6:
            raise ValueError("wall %d is degenerate" % index)
        thickness_m = float(wall.get("thickness_m") or 0.0)
        height_m = float(wall.get("height_m") or 0.0)
        if thickness_m <= 0.0 or height_m <= 0.0:
            raise ValueError("wall %d thickness/height must be positive" % index)
        if str(wall.get("level_id") or "") != level_id:
            raise ValueError("wall %d is assigned to a different level" % index)
        wall_id = str(wall.get("wall_id") or "wall_%04d" % (index + 1))
        if wall_id in wall_ids:
            raise ValueError("duplicate wall id: " + wall_id)
        wall_ids.add(wall_id)
        length_m = math.hypot(start[0] - end[0], start[1] - end[1])
        construction = dict(wall.get("construction") or {})
        quantities = dict(wall.get("quantities") or {})
        if not construction:
            construction = {
                "category": "Wall",
                "type_name": "Wall-GEOMETRY",
                "family_name": "Basic Wall",
                "family_type": "Wall-GEOMETRY",
                "representation": "system_family",
                "specification_status": "unresolved",
                "specification_source": "none",
                "specification_mm": {},
                "specification_refs": [],
                "classification_code": "IfcWall",
                "classification_system": "GB/T 51269-2017",
                "material_name": None,
                "material_status": "unspecified",
                "quantity_basis": "deterministic_geometry",
            }
        if not quantities:
            quantities = {
                "length_m": length_m,
                "footprint_area_m2": length_m * thickness_m,
                "side_area_m2": length_m * height_m,
                "gross_volume_m3": length_m * thickness_m * height_m,
            }
        item = {
            "type": "Wall",
            "id": wall_id,
            "start": [start[0] * 1000.0, start[1] * 1000.0, 0.0],
            "end": [end[0] * 1000.0, end[1] * 1000.0, 0.0],
            "thickness": thickness_m * 1000.0,
            "height": height_m * 1000.0,
            "level": level_name,
            "source_evidence_ids": list(evidence_ids),
            "source_refs": list(source_refs),
            "confidence": float(wall.get("confidence") or 0.0),
            "topology": list(dict.fromkeys(wall.get("topology") or [])),
            "construction": construction,
            "quantities": quantities,
        }
        # The drawing mark is the authoritative type identity.  A configured
        # fallback wall type may only be used when no mark was recovered;
        # otherwise a template default would rename Q1/W2/GBZ1 during the
        # final Revit handoff.
        type_name = str(construction.get("type_name") or "")
        type_mark = str(construction.get("type_mark") or "")
        specification_status = str(construction.get("specification_status") or "")
        has_drawing_name = bool(
            type_name
            and (
                specification_status.startswith("resolved")
                or (
                    type_mark
                    and not type_mark.startswith(("BM-", "BM_"))
                    and type_mark.upper() != "UNRESOLVED"
                )
            )
        )
        if has_drawing_name or "规格待核定" in type_name:
            item["wall_type_name"] = type_name
        elif revit.get("wall_type_name"):
            item["wall_type_name"] = str(revit["wall_type_name"])
        elements.append(item)
    column_ids = set()
    for index, column in enumerate(data.get("columns") or []):
        source_refs = column.get("source_refs") or []
        if not source_refs:
            raise ValueError("column %d has no geometric source reference" % index)
        center = _finite_point(column.get("center_m"), "column.center_m")
        raw_profile = column.get("profile_m") or []
        if not isinstance(raw_profile, (list, tuple)) or len(raw_profile) < 4:
            raise ValueError("column %d profile must contain at least four points" % index)
        profile = [_finite_point(point, "column.profile_m") for point in raw_profile]
        for profile_index in range(len(profile)):
            current = profile[profile_index]
            following = profile[(profile_index + 1) % len(profile)]
            # Revit 2020's Application.ShortCurveTolerance is approximately
            # 0.8 mm.  Reject at 1 mm during the deterministic preflight so a
            # malformed source loop cannot reach a Revit transaction.
            if math.hypot(
                current[0] - following[0], current[1] - following[1]
            ) < 0.001:
                raise ValueError(
                    "column %d profile contains a Revit-short edge" % index
                )
        width_m = float(column.get("width_m") or 0.0)
        depth_m = float(column.get("depth_m") or 0.0)
        height_m = float(column.get("height_m") or 0.0)
        if width_m <= 0.0 or depth_m <= 0.0 or height_m <= 0.0:
            raise ValueError("column %d dimensions must be positive" % index)
        if str(column.get("level_id") or "") != level_id:
            raise ValueError("column %d is assigned to a different level" % index)
        column_id = str(column.get("column_id") or "column_%04d" % (index + 1))
        if column_id in column_ids or column_id in wall_ids:
            raise ValueError("duplicate model element id: " + column_id)
        column_ids.add(column_id)
        construction = dict(column.get("construction") or {})
        quantities = dict(column.get("quantities") or {})
        if not construction:
            type_name = str(column.get("type_mark") or column_id)
            profile_kind = str(column.get("profile_kind") or "rectangular")
            construction = {
                "category": "Column",
                "type_name": type_name,
                "type_mark": column.get("type_mark"),
                "family_name": (
                    "混凝土 - 矩形 - 柱"
                    if profile_kind == "rectangular"
                    else "BuildMate - 异形结构柱"
                ),
                "family_type": type_name,
                "representation": (
                    "loadable_family"
                    if profile_kind == "rectangular"
                    else "profile_directshape"
                ),
                "specification_status": "unresolved",
                "specification_source": "none",
                "specification_mm": {},
                "specification_refs": [],
                "classification_code": "IfcColumn",
                "classification_system": "GB/T 51269-2017",
                "material_name": None,
                "material_status": "unspecified",
                "quantity_basis": "deterministic_geometry",
            }
        elements.append({
            "type": "Column",
            "id": column_id,
            "center": [center[0] * 1000.0, center[1] * 1000.0, 0.0],
            "profile": [[point[0] * 1000.0, point[1] * 1000.0] for point in profile],
            "width": width_m * 1000.0,
            "depth": depth_m * 1000.0,
            "height": height_m * 1000.0,
            "base_z": float(level.get("elevation_m") or 0.0) * 1000.0,
            "level": level_name,
            "source_refs": list(source_refs),
            "confidence": float(column.get("confidence") or 0.0),
            "profile_kind": str(column.get("profile_kind") or "rectangular"),
            "type_mark": column.get("type_mark"),
            "construction": construction,
            "quantities": quantities,
        })
    beam_ids = set()
    for index, beam in enumerate(data.get("beams") or []):
        source_refs = beam.get("source_refs") or []
        if not source_refs:
            raise ValueError("beam %d has no geometric source reference" % index)
        start = _finite_point(beam.get("start_m"), "beam.start_m")
        end = _finite_point(beam.get("end_m"), "beam.end_m")
        if math.hypot(start[0] - end[0], start[1] - end[1]) <= 1.0e-6:
            raise ValueError("beam %d is degenerate" % index)
        width_m = float(beam.get("width_m") or 0.0)
        depth_m = float(beam.get("depth_m") or 0.0)
        if width_m <= 0.0 or depth_m <= 0.0:
            raise ValueError("beam %d dimensions must be positive" % index)
        if str(beam.get("level_id") or "") != level_id:
            raise ValueError("beam %d is assigned to a different level" % index)
        beam_id = str(beam.get("beam_id") or "beam_%04d" % (index + 1))
        if beam_id in beam_ids or beam_id in wall_ids or beam_id in column_ids:
            raise ValueError("duplicate model element id: " + beam_id)
        beam_ids.add(beam_id)
        construction = dict(beam.get("construction") or {})
        quantities = dict(beam.get("quantities") or {})
        if not construction:
            type_name = str(beam.get("type_mark") or beam_id)
            construction = {
                "category": "Beam",
                "type_name": type_name,
                "type_mark": beam.get("type_mark"),
                "family_name": "混凝土 - 矩形 - 梁",
                "family_type": type_name,
                "representation": "loadable_family",
                "specification_status": "unresolved",
                "specification_source": "none",
                "specification_mm": {},
                "specification_refs": [],
                "classification_code": "IfcBeam",
                "classification_system": "GB/T 51269-2017",
                "material_name": None,
                "material_status": "unspecified",
                "quantity_basis": "deterministic_geometry",
            }
        length_m = math.hypot(start[0] - end[0], start[1] - end[1])
        if not quantities:
            quantities = {
                "length_m": length_m,
                "footprint_area_m2": length_m * width_m,
                "side_area_m2": length_m * depth_m,
                "gross_volume_m3": length_m * width_m * depth_m,
            }
        raw_top_elevation = beam.get("top_elevation_m")
        raw_base_elevation = beam.get("base_elevation_m")
        top_elevation_m = (
            None if raw_top_elevation is None else float(raw_top_elevation)
        )
        base_elevation_m = (
            None if raw_base_elevation is None else float(raw_base_elevation)
        )
        for value, field in (
            (top_elevation_m, "beam.top_elevation_m"),
            (base_elevation_m, "beam.base_elevation_m"),
        ):
            if value is not None and (math.isnan(value) or math.isinf(value)):
                raise ValueError(field + " must be finite")
        elevation_status = str(beam.get("elevation_status") or "unresolved")
        if elevation_status not in ("resolved", "level_default", "unresolved", "conflict"):
            raise ValueError("beam elevation_status is unsupported")
        if elevation_status == "resolved" and (
            top_elevation_m is None or base_elevation_m is None
        ):
            raise ValueError("resolved beam elevation requires top and base values")
        if (
            top_elevation_m is not None
            and base_elevation_m is not None
            and top_elevation_m < base_elevation_m
        ):
            raise ValueError("beam top elevation cannot be below base elevation")
        base_z_mm = (
            base_elevation_m * 1000.0
            if base_elevation_m is not None
            else float(level.get("elevation_m") or 0.0) * 1000.0
        )
        top_z_mm = (
            top_elevation_m * 1000.0
            if top_elevation_m is not None
            # When the drawing does not provide a beam elevation, the floor
            # range still defines a deterministic physical envelope: place
            # the beam bottom at the requested level datum and derive its top
            # from the resolved section depth.  The unresolved status remains
            # visible so this fallback is never mistaken for drawing evidence.
            else base_z_mm + depth_m * 1000.0
        )
        elements.append({
            "type": "Beam",
            "id": beam_id,
            "start": [start[0] * 1000.0, start[1] * 1000.0, 0.0],
            "end": [end[0] * 1000.0, end[1] * 1000.0, 0.0],
            "width": width_m * 1000.0,
            "depth": depth_m * 1000.0,
            "height": depth_m * 1000.0,
            "base_z": base_z_mm,
            "top_z": top_z_mm,
            "elevation_status": elevation_status,
            "elevation_source": str(beam.get("elevation_source") or "none"),
            "elevation_text": beam.get("elevation_text"),
            # Keep the exact heading/value/mark references in the approved
            # compiled plan for audit/replay.  The Bridge receives only the
            # bounded count and readable text in its runtime DTO.
            "elevation_refs": list(beam.get("elevation_refs") or []),
            "level": level_name,
            "source_refs": list(source_refs),
            "confidence": float(beam.get("confidence") or 0.0),
            "type_mark": beam.get("type_mark"),
            "construction": construction,
            "quantities": quantities,
        })
    openings = []
    opening_ids = set()
    for index, opening in enumerate(data.get("openings") or []):
        opening_id = str(opening.get("opening_id") or "opening_%04d" % (index + 1))
        if opening_id in opening_ids:
            raise ValueError("duplicate opening id: " + opening_id)
        opening_ids.add(opening_id)
        mark = str(opening.get("mark") or "").strip()
        if not mark:
            raise ValueError("opening %d mark is required" % index)
        status = str(opening.get("status") or "")
        if status not in ("matched", "review_required"):
            raise ValueError("opening %d status is unsupported" % index)
        center = _finite_point(opening.get("center_m"), "opening.center_m")
        raw_boundary = opening.get("boundary_m") or []
        if not isinstance(raw_boundary, (list, tuple)):
            raise ValueError("opening %d boundary must be a list" % index)
        boundary = [_finite_point(point, "opening.boundary_m") for point in raw_boundary]
        width_m = float(opening.get("width_m") or 0.0)
        depth_m = float(opening.get("depth_m") or 0.0)
        if width_m < 0.0 or depth_m < 0.0:
            raise ValueError("opening %d dimensions must be non-negative" % index)
        hosts = [str(item) for item in (opening.get("host_wall_ids") or [])]
        if any(item not in wall_ids for item in hosts):
            raise ValueError("opening %d references an unknown wall host" % index)
        if status == "matched" and not hosts:
            raise ValueError("matched opening %d has no wall host" % index)
        source_refs = opening.get("source_refs") or []
        if not source_refs:
            raise ValueError("opening %d has no source reference" % index)
        openings.append({
            "id": opening_id,
            "mark": mark,
            "type_name": str(opening.get("type_name") or mark),
            "semantic_role": str(opening.get("semantic_role") or "shear_wall_opening"),
            "center": [center[0] * 1000.0, center[1] * 1000.0],
            "boundary": [[point[0] * 1000.0, point[1] * 1000.0] for point in boundary],
            "width": width_m * 1000.0,
            "depth": depth_m * 1000.0,
            "host_wall_ids": hosts,
            "source_refs": list(source_refs),
            "confidence": float(opening.get("confidence") or 0.0),
            "status": status,
            "limitations": list(opening.get("limitations") or []),
        })
        openings[-1].update(_normalize_opening_cut(opening, data.get("walls") or [], level))
    if not elements:
        raise ValueError("approved wall model contains no walls")

    x_axes, y_axes, x_labels, y_labels, axis_lines = [], [], [], [], []
    for axis in data.get("grid") or []:
        start = _finite_point(axis.get("start_m"), "grid.start_m")
        end = _finite_point(axis.get("end_m"), "grid.end_m")
        label = str(axis.get("label") or "")
        if abs(start[0] - end[0]) <= 0.001:
            x_axes.append((start[0] + end[0]) * 500.0)
            x_labels.append(label)
            axis_lines.append({
                "axis": "X", "label": label,
                "start": [start[0] * 1000.0, start[1] * 1000.0, 0.0],
                "end": [end[0] * 1000.0, end[1] * 1000.0, 0.0],
            })
        elif abs(start[1] - end[1]) <= 0.001:
            y_axes.append((start[1] + end[1]) * 500.0)
            y_labels.append(label)
            axis_lines.append({
                "axis": "Y", "label": label,
                "start": [start[0] * 1000.0, start[1] * 1000.0, 0.0],
                "end": [end[0] * 1000.0, end[1] * 1000.0, 0.0],
            })
        else:
            raise ValueError("Revit grid must be horizontal or vertical")

    identity_payload = dict(data)
    identity_payload.pop("created_at", None)
    # The target RVT is an operational deployment binding (and is signed
    # separately by the Bridge approval token), not a geometric input.  Use
    # the same path-independent identity convention as the backend canonical
    # hash so a worker mount change cannot alter the compiler plan fingerprint.
    identity_revit = identity_payload.get("revit")
    if isinstance(identity_revit, dict) and identity_revit.get("target_model_path"):
        identity_revit = dict(identity_revit)
        identity_revit["target_model_path"] = "target://rvt"
        identity_payload["revit"] = identity_revit
    canonical = json.dumps(
        identity_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"))
    wall_model_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    build_id = "wall-model-" + wall_model_sha256[:16]
    junctions = []
    junction_ids = set()
    for index, junction in enumerate(data.get("junctions") or []):
        kind = str(junction.get("kind") or "")
        if kind not in ("L", "T", "X", "Z", "straight"):
            raise ValueError("unsupported wall junction kind: " + kind)
        point = _finite_point(junction.get("point_m"), "junction.point_m")
        refs = [str(item) for item in (junction.get("wall_ids") or [])]
        if len(refs) < 2 or any(item not in wall_ids for item in refs):
            raise ValueError("junction %d references an unknown wall" % index)
        junction_id = str(
            junction.get("junction_id") or "junction_%04d" % (index + 1)
        )
        if junction_id in junction_ids:
            raise ValueError("duplicate junction id: " + junction_id)
        junction_ids.add(junction_id)
        junctions.append({
            "id": junction_id,
            "kind": kind,
            "point": [point[0] * 1000.0, point[1] * 1000.0],
            "wall_ids": list(dict.fromkeys(refs)),
        })
    transform_chain = data.get("transform_chain") or []
    apply_base_point_rotation = False
    for operation in transform_chain:
        if isinstance(operation, dict) and operation.get("operation") == "rotate":
            apply_base_point_rotation = bool(
                operation.get("apply_base_point_rotation")
            )
    modeling_standard = dict(data.get("modeling_standard") or {
        "profile": "cn_gb_bim_delivery_v1",
        "classification_system": "GB/T 51269-2017",
        "application_standard": "GB/T 51212-2016",
        "delivery_standard": "GB/T 51301-2018",
        "construction_standard": "GB/T 51235-2017",
        "units": "m",
        "coordinate_frame": "project_north",
    })
    # Older approved WallModels do not carry the delivery-unit annotation;
    # preserve their canonical metre geometry while making the Revit-facing
    # unit contract explicit and stable.
    modeling_standard.setdefault("delivery_units", "mm")
    level_bottom_m = float(level.get("elevation_m") or 0.0)
    level_top_m = level.get("top_elevation_m")
    if level_top_m is None:
        level_top_m = level_bottom_m + float(level.get("wall_height_m") or 0.0)
    return {
        "schema_version": "2.0",
        "project": {
            "name": project_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "units": "mm",
            "levels": [{
                "id": level_id,
                "name": level_name,
                "elevation": level_bottom_m * 1000.0,
                "top_elevation": float(level_top_m) * 1000.0,
                "elevation_range": "%g~%g" % (level_bottom_m, float(level_top_m)),
            }],
        },
        "build": {
            "build_id": build_id,
            "project_id": project_id,
            "floor_code": floor_code,
            "target_model_path": target_model_path,
            "approval": approval,
            "source_artifact_type": "wall_model",
            "wall_model_sha256": wall_model_sha256,
        },
        "coordinate_system": {
            "offset_policy": origin if origin != "source_origin" else "local_origin",
            "project_offset_mm": None,
            # Coordinates in WallModel have already passed the source
            # calibration/rotation stage.  When the model is expressed in
            # Project Base Point coordinates, the Bridge applies the active
            # base-point translation and angle at delivery time.
            "apply_base_point_rotation": apply_base_point_rotation,
            "source_frame": next(
                (
                    str(operation.get("source_frame"))
                    for operation in transform_chain
                    if isinstance(operation, dict)
                    and operation.get("operation") == "rotate"
                    and operation.get("source_frame")
                ),
                "project_north",
            ),
            "transform_chain": transform_chain,
            "source_bounds_m": source_bounds,
        },
        "modeling_standard": modeling_standard,
        "grids": [{
            "level": level_name,
            "x_axes": x_axes,
            "y_axes": y_axes,
            "x_axis_labels": x_labels,
            "y_axis_labels": y_labels,
            "axis_lines": axis_lines,
        }],
        "model_elements": elements,
        "junctions": junctions,
        "openings": openings,
    }
