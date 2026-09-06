"""Geometry-first CAD cleaning and component extraction trial."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

import cv2
import ezdxf
import numpy as np

from backend.engines.wall_geometry import wall_geometry_group


INFO_TYPES = {"TEXT", "MTEXT", "DIMENSION", "LEADER", "MLEADER", "ATTRIB"}
NOISE_KEYS = (
    "家具", "KITCHEN", "KE-KJ", "厨洗", "A-CAR", "车位", "A-DOOR",
    "A-STAIR", "A-FIRE", "EQUIP", "A-FURT", "F-FURN", "卫生间",
    "洁具", "集水坑", "设备", "ASHADE", "消火栓", "3T_BAR",
)
ANNOTATION_KEYS = ("ANNO", "DIM", "TEXT", "AXIS-TXT", "编号", "文字")
DOOR_VECTOR_TYPES = {"LINE", "LWPOLYLINE", "ARC"}


def effective_layer(entity, inherited: str) -> str:
    own = str(getattr(entity.dxf, "layer", "") or "")
    return inherited if own in ("", "0") else own


def contains(layer: str, keys) -> bool:
    upper = (layer or "").upper()
    return any(key.upper() in upper for key in keys)


def _ignore_virtual_entity(_entity, _reason):
    """Ignore unsupported proxy entities while retaining supported geometry."""
    return None


def explicit_door_layer(layer: str) -> bool:
    """Return whether the leaf layer explicitly carries door semantics."""
    leaf = (layer or "").upper().strip().rsplit("$0$", 1)[-1]
    return "DOOR" in leaf or "门" in leaf


def evidence_type(layer: str) -> str:
    leaf = (layer or "").upper().rsplit("$0$", 1)[-1]
    if "S-COLU" in leaf or "COLUMN" in leaf or "COLU" in leaf:
        return "column"
    if "S-BEAM" in leaf or "BEAM" in leaf:
        return "beam"
    if "S-WALL" in leaf or "A-WALL" in leaf or "A-PART" in leaf or leaf == "WALL":
        return "wall"
    if "S-SLAB" in leaf or "结构降板" in leaf or "底板" in leaf or "顶板" in leaf:
        return "slab_boundary"
    if "A-GRID" in leaf or "GRID" in leaf or "轴网" in leaf:
        return "grid"
    return "unresolved"


def walk(items, inherited="", depth=0, skip_noise=True):
    if depth > 16:
        return
    for entity in items:
        layer = effective_layer(entity, inherited)
        if entity.dxftype() == "INSERT":
            if skip_noise and contains(layer.rsplit("$0$", 1)[-1], NOISE_KEYS):
                continue
            for attribute in getattr(entity, "attribs", ()):
                yield attribute, effective_layer(attribute, layer)
            try:
                yield from walk(entity.virtual_entities(
                    skipped_entity_callback=_ignore_virtual_entity),
                    layer, depth + 1, skip_noise=skip_noise)
            except (AttributeError, ezdxf.DXFError):
                continue
        else:
            yield entity, layer


def _source_entity_handle(entity) -> str | None:
    origin = getattr(entity, "origin_of_copy", None) or entity
    handle = getattr(getattr(origin, "dxf", None), "handle", None)
    return str(handle) if handle is not None else None


def drawing_identity(document, explicit: str | None = None) -> str:
    """Return a stable identity for the exact drawing being expanded.

    Real pipeline inputs are identified by their file bytes.  The DXF
    fingerprint is only a deterministic fallback for unsaved test documents;
    callers can also supply an explicit identity for focused tests.
    """
    if explicit:
        return str(explicit)
    filename = str(getattr(document, "filename", "") or "")
    source = Path(filename)
    if source.is_file():
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    try:
        fingerprint = str(document.header.get("$FINGERPRINTGUID", "") or "")
    except (AttributeError, KeyError):
        fingerprint = ""
    if fingerprint:
        return f"dxf-fingerprint:{fingerprint}"
    return f"unsaved-dxf:{getattr(document, 'dxfversion', 'UNKNOWN')}"


def _cumulative_transform(entity) -> dict:
    """Expose a reviewable 2D affine transform for this placed INSERT."""
    try:
        matrix = entity.matrix44()
        origin = matrix.transform((0.0, 0.0, 0.0))
        x_point = matrix.transform((1.0, 0.0, 0.0))
        y_point = matrix.transform((0.0, 1.0, 0.0))
        affine = [
            [x_point.x - origin.x, y_point.x - origin.x, origin.x],
            [x_point.y - origin.y, y_point.y - origin.y, origin.y],
            [0.0, 0.0, 1.0],
        ]
        return {
            "coordinate_system": "DXF_WCS",
            "affine_2d": [
                [round(float(value), 9) for value in row]
                for row in affine
            ],
        }
    except (AttributeError, TypeError, ValueError, ezdxf.DXFError):
        return {"coordinate_system": "DXF_WCS", "affine_2d": None}


def _insert_marker(entity, *, source_insert=None,
                   array_index: dict | None = None) -> dict:
    point = getattr(entity.dxf, "insert", None)
    block_name = str(getattr(entity.dxf, "name", "") or "")
    marker = {
        # ``name`` remains for existing reports; ``block_name`` is explicit
        # in the provenance schema used by the BIM IR.
        "name": block_name,
        "block_name": block_name,
        "insert_handle": _source_entity_handle(
            source_insert if source_insert is not None else entity),
        "layer": str(getattr(entity.dxf, "layer", "") or ""),
        "insert": [round(float(getattr(point, "x", 0.0)), 3),
                   round(float(getattr(point, "y", 0.0)), 3)],
        "rotation": round(float(getattr(entity.dxf, "rotation", 0.0) or 0.0), 6),
        "xscale": round(float(getattr(entity.dxf, "xscale", 1.0) or 1.0), 6),
        "yscale": round(float(getattr(entity.dxf, "yscale", 1.0) or 1.0), 6),
        "array_index": array_index,
        "cumulative_transform": _cumulative_transform(entity),
    }
    return marker


def _identity_path(markers) -> list[dict]:
    return [{
        "insert_handle": item.get("insert_handle"),
        "block_name": item.get("block_name") or item.get("name"),
        "array_index": item.get("array_index"),
        "cumulative_transform": item.get("cumulative_transform"),
    } for item in markers]


def _stable_placed_id(prefix: str, drawing_id: str, markers,
                      source_handle: str | None, **extra) -> str:
    payload = {
        "drawing_identity": drawing_id,
        "placement_path": _identity_path(markers),
        "source_entity_handle": source_handle,
        **extra,
    }
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def placed_segment_id(provenance: dict, segment_index: int,
                      segment_role: str = "geometry_segment") -> str:
    """Build one segment identity from the same immutable placement seed."""
    return _stable_placed_id(
        "segment", str(provenance.get("drawing_identity") or "UNKNOWN"),
        provenance.get("placement_path") or [],
        provenance.get("source_entity_handle"),
        segment_index=int(segment_index), segment_role=str(segment_role))


def _entity_segment_count(entity) -> int:
    kind = entity.dxftype()
    if kind in {"LINE", "ARC"}:
        return 1
    if kind == "LWPOLYLINE":
        try:
            count = len(entity)
        except (AttributeError, TypeError):
            count = len(list(entity.get_points("xy")))
        return max(0, count - 1) + (1 if entity.closed and count > 2 else 0)
    return 0


def _placed_provenance(entity, markers, drawing_id: str) -> dict:
    source_handle = _source_entity_handle(entity)
    source_occurrence_id = _stable_placed_id(
        "source_occurrence", drawing_id, markers, source_handle)
    placed_entity_id = _stable_placed_id(
        "placed_entity", drawing_id, markers, source_handle,
        entity_type=entity.dxftype())
    structural_index = next((
        index for index in range(len(markers) - 1, -1, -1)
        if "墙柱" in str(markers[index].get("block_name") or
                         markers[index].get("name") or "")
    ), 0 if markers else None)
    structural_occurrence_id = None
    if structural_index is not None:
        structural_occurrence_id = _stable_placed_id(
            "structural_occurrence", drawing_id,
            markers[:structural_index + 1], None)
    provenance = {
        "drawing_identity": drawing_id,
        "root_plan": ((markers[0].get("block_name") or
                       markers[0].get("name")) if markers else None),
        "placement_path": list(markers),
        "source_entity_handle": source_handle,
        "source_occurrence_id": source_occurrence_id,
        "placed_entity_id": placed_entity_id,
        "structural_occurrence_id": structural_occurrence_id,
    }
    segment_count = _entity_segment_count(entity)
    segment_ids = [placed_segment_id(provenance, index)
                   for index in range(segment_count)]
    provenance["segment_ids"] = segment_ids
    provenance["segment_id"] = (
        segment_ids[0] if len(segment_ids) == 1 else
        _stable_placed_id("segment_set", drawing_id, markers, source_handle,
                          segment_count=segment_count)
    )
    return provenance


def _insert_instances(entity):
    """Yield placed INSERT instances with deterministic MINSERT indices."""
    if int(getattr(entity, "mcount", 1) or 1) <= 1:
        yield entity, None
        return
    try:
        instances = list(entity.multi_insert())
    except (AttributeError, ezdxf.DXFError):
        yield entity, None
        return
    row_count = (int(getattr(entity.dxf, "row_count", 1) or 1)
                 if float(getattr(entity.dxf, "row_spacing", 0.0) or 0.0)
                 else 1)
    column_count = (int(getattr(entity.dxf, "column_count", 1) or 1)
                    if float(getattr(entity.dxf, "column_spacing", 0.0) or 0.0)
                    else 1)
    indices = [(row, column) for row in range(row_count)
               for column in range(column_count)]
    for linear, (instance, (row, column)) in enumerate(zip(instances, indices)):
        yield instance, {
            "row": row,
            "column": column,
            "linear": linear,
            "row_count": row_count,
            "column_count": column_count,
        }


def _insert_contains_wall(entity, inherited: str, cache: dict,
                          active: set[str], depth: int = 0) -> bool:
    """Inspect a block definition without expanding its placed geometry.

    This predicate lets wall extraction enter a bathroom/kitchen container only
    when that block actually contains a semantic wall leaf. It prevents the old
    ``skip_noise=False`` path from materialising hundreds of thousands of
    furniture and hatch entities merely to find a few partitions.
    """
    if depth > 16:
        return False
    name = str(getattr(entity.dxf, "name", "") or "")
    inherited_leaf = (inherited or "").upper().rsplit("$0$", 1)[-1]
    key = (name.upper(), inherited_leaf)
    if key in cache:
        return cache[key]
    if name.upper() in active:
        return False
    active.add(name.upper())
    found = False
    try:
        block = entity.block()
        if block is None:
            return False
        for child in block:
            layer = effective_layer(child, inherited)
            if (child.dxftype() in ("LINE", "LWPOLYLINE") and
                    wall_geometry_group(layer) is not None):
                found = True
                break
            if (child.dxftype() == "INSERT" and
                    _insert_contains_wall(child, layer, cache, active, depth + 1)):
                found = True
                break
    except (AttributeError, ezdxf.DXFError, KeyError):
        found = False
    finally:
        active.discard(name.upper())
    cache[key] = found
    return found


def _insert_contains_door_vector(entity, inherited: str,
                                 semantic_layer: str | None,
                                 cache: dict, active: set[str],
                                 depth: int = 0) -> bool:
    """Inspect block definitions for explicit door-layer vector evidence.

    This is a predicate only: it does not materialise placed geometry.  It lets
    the door evidence traversal enter the few relevant INSERT branches while
    leaving furniture, hatches and other repeated detail blocks untouched.
    """
    if depth > 16:
        return False
    name = str(getattr(entity.dxf, "name", "") or "")
    inherited_leaf = (inherited or "").upper().rsplit("$0$", 1)[-1]
    semantic_leaf = (semantic_layer or "").upper().rsplit("$0$", 1)[-1]
    key = (name.upper(), inherited_leaf, semantic_leaf)
    if key in cache:
        return cache[key]
    if name.upper() in active:
        return False
    active.add(name.upper())
    found = False
    try:
        block = entity.block()
        if block is None:
            return False
        for child in block:
            layer = effective_layer(child, inherited)
            child_semantic_layer = (
                semantic_layer or (layer if explicit_door_layer(layer)
                                   else None))
            if (child.dxftype() in DOOR_VECTOR_TYPES and
                    child_semantic_layer):
                found = True
                break
            if (child.dxftype() == "INSERT" and
                    _insert_contains_door_vector(
                        child, layer, child_semantic_layer, cache, active,
                        depth + 1)):
                found = True
                break
    except (AttributeError, ezdxf.DXFError, KeyError):
        found = False
    finally:
        active.discard(name.upper())
    cache[key] = found
    return found


def walk_placed_entities(items, inherited="", path=(), depth=0,
                         block_cache=None, *, drawing_id: str | None = None,
                         wall_only=False, skip_noise=True):
    """Yield transformed entities and complete ordered INSERT provenance."""
    if depth > 16:
        return
    if block_cache is None:
        block_cache = {}
    if drawing_id is None:
        # Preserve generators: resolving the document must not consume the
        # first virtual entity before the actual traversal starts.
        items = list(items)
        document = next((getattr(item, "doc", None) for item in items
                         if getattr(item, "doc", None) is not None), None)
        drawing_id = drawing_identity(document) if document is not None else "UNKNOWN"
    for entity in items:
        layer = effective_layer(entity, inherited)
        if entity.dxftype() != "INSERT":
            if (not wall_only or
                    (entity.dxftype() in ("LINE", "LWPOLYLINE") and
                     wall_geometry_group(layer) is not None)):
                yield entity, layer, _placed_provenance(
                    entity, list(path), drawing_id)
            continue

        for instance, array_index in _insert_instances(entity):
            instance_layer = effective_layer(instance, inherited)
            leaf = instance_layer.rsplit("$0$", 1)[-1]
            if wall_only and not _insert_contains_wall(
                    instance, instance_layer, block_cache, set()):
                continue
            if skip_noise and contains(leaf, NOISE_KEYS):
                if (not wall_only or not _insert_contains_wall(
                        instance, instance_layer, block_cache, set())):
                    continue
            marker = _insert_marker(
                instance, source_insert=entity, array_index=array_index)
            markers = tuple(path) + (marker,)
            if not wall_only:
                for attribute in getattr(instance, "attribs", ()):
                    yield attribute, effective_layer(attribute, instance_layer), (
                        _placed_provenance(attribute, list(markers), drawing_id))
            try:
                yield from walk_placed_entities(
                    instance.virtual_entities(
                        skipped_entity_callback=_ignore_virtual_entity),
                    instance_layer,
                    markers, depth + 1, block_cache,
                    drawing_id=drawing_id, wall_only=wall_only,
                    skip_noise=skip_noise)
            except (AttributeError, ezdxf.DXFError):
                continue


def walk_wall_evidence(items, inherited="", path=(), depth=0,
                       block_cache=None, *, drawing_id: str | None = None):
    """Yield placed semantic wall geometry with full INSERT provenance.

    Coordinates still come only from ``virtual_entities``.  The extra record
    fields are audit metadata and do not alter or reconstruct geometry.
    """
    yield from walk_placed_entities(
        items, inherited, path, depth, block_cache,
        drawing_id=drawing_id, wall_only=True, skip_noise=True)


def walk_door_evidence(items, inherited="", path=(), depth=0,
                       block_cache=None, *, drawing_id: str | None = None,
                       semantic_layer: str | None = None):
    """Yield placed explicit-door vectors with full INSERT provenance.

    Door symbols are semantic evidence only. Coordinates are still produced by
    ezdxf ``virtual_entities`` from the current drawing; this traversal neither
    creates wall geometry nor treats a door symbol as an opening automatically.
    """
    if depth > 16:
        return
    if block_cache is None:
        block_cache = {}
    if drawing_id is None:
        items = list(items)
        document = next((getattr(item, "doc", None) for item in items
                         if getattr(item, "doc", None) is not None), None)
        drawing_id = (drawing_identity(document)
                      if document is not None else "UNKNOWN")
    for entity in items:
        layer = effective_layer(entity, inherited)
        current_semantic_layer = (
            semantic_layer or (layer if explicit_door_layer(layer) else None))
        if entity.dxftype() != "INSERT":
            if (entity.dxftype() in DOOR_VECTOR_TYPES and
                    current_semantic_layer):
                provenance = _placed_provenance(
                    entity, list(path), drawing_id)
                provenance["geometry_layer"] = layer
                provenance["door_semantic_layer"] = current_semantic_layer
                yield entity, current_semantic_layer, provenance
            continue

        for instance, array_index in _insert_instances(entity):
            instance_layer = effective_layer(instance, inherited)
            instance_semantic_layer = (
                semantic_layer or
                (instance_layer if explicit_door_layer(instance_layer)
                 else None))
            if (not instance_semantic_layer and
                    not _insert_contains_door_vector(
                        instance, instance_layer, None, block_cache, set())):
                continue
            marker = _insert_marker(
                instance, source_insert=entity, array_index=array_index)
            markers = tuple(path) + (marker,)
            try:
                yield from walk_door_evidence(
                    instance.virtual_entities(
                        skipped_entity_callback=_ignore_virtual_entity),
                    instance_layer, markers,
                    depth + 1, block_cache, drawing_id=drawing_id,
                    semantic_layer=instance_semantic_layer)
            except (AttributeError, ezdxf.DXFError):
                continue


def entity_segments(entity):
    kind = entity.dxftype()
    if kind == "LINE":
        a, b = entity.dxf.start, entity.dxf.end
        yield (float(a.x), float(a.y), float(b.x), float(b.y))
    elif kind == "LWPOLYLINE":
        points = [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
        for first, second in zip(points, points[1:]):
            yield (*first, *second)
        if entity.closed and len(points) > 2:
            yield (*points[-1], *points[0])
    elif kind in ("SOLID", "TRACE", "3DFACE"):
        points = []
        for name in ("vtx0", "vtx1", "vtx2", "vtx3"):
            point = getattr(entity.dxf, name, None)
            if point is not None:
                value = (float(point.x), float(point.y))
                if not points or value != points[-1]:
                    points.append(value)
        for first, second in zip(points, points[1:] + points[:1]):
            yield (*first, *second)
    elif kind in ("ARC", "CIRCLE"):
        center = entity.dxf.center
        radius = abs(float(entity.dxf.radius))
        start = float(getattr(entity.dxf, "start_angle", 0.0))
        end = float(getattr(entity.dxf, "end_angle", 360.0))
        if kind == "ARC" and end <= start:
            end += 360.0
        steps = max(12, min(72, int(abs(end - start) / 5.0)))
        angles = np.linspace(math.radians(start), math.radians(end), steps + 1)
        points = [(float(center.x) + radius * math.cos(value),
                   float(center.y) + radius * math.sin(value)) for value in angles]
        for first, second in zip(points, points[1:]):
            yield (*first, *second)


def entity_info_position(entity):
    for name in ("insert", "location", "defpoint"):
        point = getattr(entity.dxf, name, None)
        if point is not None:
            return float(point.x), float(point.y)
    return None


def entity_info_text(entity) -> str:
    try:
        if hasattr(entity, "plain_text"):
            return str(entity.plain_text()).strip()
    except Exception:
        pass
    return str(getattr(entity.dxf, "text", "") or "").strip()


def select_main_plan(document, floor_hint: str | None = None):
    """Select one placed main plan and return auditable selection metadata.

    A guessed first match is unsafe because converted DWGs often contain a main
    plan plus mezzanine or translated comparison copies. Ambiguity is therefore
    a hard error instead of silently mixing coordinate frames.

    Structural wall/column drawings commonly name the placed main block
    ``*层墙柱(...)`` instead of ``*层平面图``.  Use that structural semantic only
    when no ordinary floor-plan block exists, so an architectural plan keeps
    priority in mixed drawings and the fallback cannot silently select a detail
    or annotation block.
    """
    inserts = [entity for entity in document.modelspace()
               if entity.dxftype() == "INSERT"]
    plans = [entity for entity in inserts if "平面图" in entity.dxf.name]
    selection_mode = "floor_plan_block"
    if not plans:
        plans = [entity for entity in inserts if "墙柱" in entity.dxf.name]
        selection_mode = "structural_wall_column_block"
    if not plans:
        raise ValueError("no floor-plan or structural wall-column block found")
    candidates = [item for item in plans if "夹层" not in item.dxf.name]
    if floor_hint:
        hinted = [item for item in candidates
                  if floor_hint.upper() in str(item.dxf.name).upper()]
        if hinted:
            candidates = hinted
    if not candidates:
        candidates = plans
    if len(candidates) != 1:
        names = [str(item.dxf.name) for item in candidates]
        raise ValueError(f"ambiguous floor-plan blocks: {names}")
    selected = candidates[0]
    return selected, {
        "selected": _insert_marker(selected),
        "selection_mode": selection_mode,
        "candidate_count": len(plans),
        "eligible_count": len(candidates),
        "excluded": [_insert_marker(item) for item in plans if item is not selected],
    }


def main_plan(document):
    return select_main_plan(document)[0]


def render_side_by_side(original: Path, cleaned: Path, output: Path) -> bool:
    if not original.exists():
        return False
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    left = Image.open(original).convert("RGB")
    right = Image.open(cleaned).convert("RGB")
    panel_size = (1400, 800)
    left = ImageOps.contain(left, panel_size)
    right = ImageOps.contain(right, panel_size)
    result = Image.new("RGB", (2860, 900), (15, 20, 26))
    result.paste(left, (30 + (1400 - left.width) // 2, 80))
    result.paste(right, (1430 + (1400 - right.width) // 2, 80))
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = (ImageFont.truetype(str(font_path), 32) if font_path.exists()
            else ImageFont.load_default())
    draw = ImageDraw.Draw(result)
    draw.text((40, 24), "原始图面", font=font, fill=(220, 226, 232))
    draw.text((1440, 24), "清理后的纯几何画布", font=font, fill=(80, 230, 180))
    draw.line((1430, 0, 1430, 900), fill=(70, 80, 90), width=2)
    result.save(output)
    return True


def bounds_from_segments(segments):
    grid = [item for item in segments if item[4] == "grid"]
    walls = [item for item in segments if item[4] == "wall"]
    anchors = grid + walls or [item for item in segments if item[4] == "column"]
    source = anchors or segments
    xs = [value for item in source for value in (item[0], item[2])]
    ys = [value for item in source for value in (item[1], item[3])]
    if not xs:
        raise ValueError("no renderable geometry remains")
    # Converted OCS polylines can contain mirrored outliers. Lines around the
    # structural/grid evidence establish the useful plan frame robustly.
    x1, x2 = min(xs), max(xs)
    y1, y2 = min(ys), max(ys)
    pad_x = max(1.0, (x2 - x1) * 0.04)
    pad_y = max(1.0, (y2 - y1) * 0.04)
    return float(x1 - pad_x), float(y1 - pad_y), float(x2 + pad_x), float(y2 + pad_y)


def render(source: Path, output_dir: Path, whole_drawing: bool = False) -> dict:
    document = ezdxf.readfile(source)
    if whole_drawing:
        roots = [entity for entity in document.modelspace()
                 if not (entity.dxftype() == "INSERT" and
                         str(entity.dxf.name).upper().startswith("AVE_"))]
        plan_name = "WHOLE_DRAWING"
    else:
        plan = main_plan(document)
        roots = [plan]
        plan_name = plan.dxf.name
    segments = []
    information = []
    removed = Counter()
    for entity, layer in walk(roots):
        kind = entity.dxftype()
        if kind in INFO_TYPES or contains(layer, ANNOTATION_KEYS):
            position = entity_info_position(entity)
            if position:
                information.append((position[0], position[1], kind, layer,
                                    entity_info_text(entity)))
            removed["information"] += 1
            continue
        if contains(layer.rsplit("$0$", 1)[-1], NOISE_KEYS):
            removed["non_model_detail"] += 1
            continue
        category = evidence_type(layer)
        if whole_drawing and category == "unresolved":
            removed["unresolved_geometry"] += 1
            continue
        found = False
        for x1, y1, x2, y2 in entity_segments(entity):
            if all(math.isfinite(value) for value in (x1, y1, x2, y2)):
                segments.append((x1, y1, x2, y2, category, layer))
                found = True
        if not found and kind == "HATCH":
            # Filled regions are not painted onto the recognition canvas. Their
            # companion boundary/solid geometry remains, avoiding large masks.
            removed["hatch_interior"] += 1

    min_x, min_y, max_x, max_y = bounds_from_segments(segments)
    width, height, margin = 2800, 1600, 35
    scale = min((width - 2 * margin) / max(max_x - min_x, 1.0),
                (height - 2 * margin) / max(max_y - min_y, 1.0))

    def pixel(x, y):
        return (int(round(margin + (x - min_x) * scale)),
                int(round(height - margin - (y - min_y) * scale)))

    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    masks = {name: np.zeros((height, width), dtype=np.uint8)
             for name in ("column", "wall", "beam", "slab_boundary", "grid")}
    visible_segments = Counter()
    for x1, y1, x2, y2, category, _ in segments:
        if (max(x1, x2) < min_x or min(x1, x2) > max_x or
                max(y1, y2) < min_y or min(y1, y2) > max_y):
            continue
        first, second = pixel(x1, y1), pixel(x2, y2)
        cv2.line(canvas, first, second, (215, 220, 225), 1, cv2.LINE_AA)
        visible_segments[category] += 1
        if category in masks:
            cv2.line(masks[category], first, second, 255, 1, cv2.LINE_8)

    clean_path = output_dir / ("24_whole_drawing_hidden_noise.png"
                               if whole_drawing else "10_geometry_first_clean.png")
    cv2.imwrite(str(clean_path), canvas)

    overlay = (canvas.astype(np.float32) * 0.22).astype(np.uint8)
    colors = {
        "wall": (80, 230, 80),
        "column": (230, 80, 230),
        "beam": (30, 165, 255),
        "slab_boundary": (255, 220, 50),
    }
    component_stats = {}
    for category, color in colors.items():
        mask = masks[category]
        overlay[mask > 0] = color
        if category == "column":
            joined = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                      np.ones((3, 3), np.uint8))
            contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            usable = [item for item in contours if 3 <= cv2.contourArea(item) <= 5000]
            for contour in usable:
                cv2.drawContours(overlay, [contour], -1, color, 2)
            component_stats["column_contour_candidates"] = len(usable)
        else:
            lines = cv2.HoughLinesP(mask, 1, np.pi / 720.0, 15,
                                    minLineLength=16, maxLineGap=5)
            component_stats[f"{category}_line_candidates"] = (
                0 if lines is None else int(np.asarray(lines).reshape(-1, 4).shape[0]))

    # Re-associate hidden text/dimensions to the nearest component evidence.
    distance_maps = {}
    for category in ("column", "wall", "beam"):
        distance_maps[category] = cv2.distanceTransform(
            cv2.bitwise_not(masks[category]), cv2.DIST_L2, 3)
    associations = Counter()
    information_evidence = []
    for x, y, kind, layer, text in information:
        px, py = pixel(x, y)
        if not (0 <= px < width and 0 <= py < height):
            continue
        nearest = min(distance_maps,
                      key=lambda name: float(distance_maps[name][py, px]))
        if float(distance_maps[nearest][py, px]) <= 90:
            associations[nearest] += 1
            assigned = nearest
        else:
            associations["unassigned"] += 1
            assigned = "unassigned"
        information_evidence.append({
            "pixel": [px, py], "kind": kind, "text": text,
            "layer": layer, "nearest_geometry": assigned,
            "distance_px": round(float(distance_maps[nearest][py, px]), 1),
        })

    compare_path = output_dir / ("25_whole_drawing_components.png"
                                 if whole_drawing else "11_geometry_components_overlay.png")
    cv2.imwrite(str(compare_path), overlay)
    side_by_side_path = output_dir / ("26_whole_drawing_before_after.png"
                                      if whole_drawing else "12_original_vs_geometry_clean.png")
    has_side_by_side = render_side_by_side(
        output_dir / "01_original.png", clean_path, side_by_side_path)
    report = {
        "source": str(source),
        "plan": plan_name,
        "method": [
            "hide information but retain it as evidence",
            "skip repeated non-model blocks before expanding them",
            "remove hatch interiors while retaining boundary geometry",
            "extract geometry candidates",
            "use layer/text proximity only as posterior type evidence",
        ],
        "removed_from_recognition_canvas": dict(removed),
        "visible_geometry_segments": dict(visible_segments),
        "component_candidates": component_stats,
        "information_associations": dict(associations),
        "information_evidence": information_evidence,
        "render_transform": {
            "bounds": [min_x, min_y, max_x, max_y],
            "image_size": [width, height], "margin": margin, "scale": scale,
        },
        "status": "REVIEW",
        "outputs": {"clean_geometry": str(clean_path),
                    "component_overlay": str(compare_path),
                    "side_by_side": (str(side_by_side_path)
                                     if has_side_by_side else None)},
    }
    report_name = ("whole_drawing_clean_report.json" if whole_drawing
                   else "geometry_first_report.json")
    (output_dir / report_name).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--whole-drawing", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = render(args.source, args.output_dir, args.whole_drawing)
    print(json.dumps({
        "plan": result["plan"],
        "removed_from_recognition_canvas": result[
            "removed_from_recognition_canvas"],
        "visible_geometry_segments": result["visible_geometry_segments"],
        "component_candidates": result["component_candidates"],
        "information_associations": result["information_associations"],
        "status": result["status"], "outputs": result["outputs"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
