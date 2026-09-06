# -*- coding: utf-8 -*-
"""BuildMate 图纸→BIM 管线(第九期)——严格按用户标准流程重构

用户钦定标准流程(BIM 建模 Agent 规范), 脚本结构 = 标准流程结构:
  Step0 数据清洗          → clean_drawing + 单位检测 + 几何清洗
  Step1 轴网+标高         → 轴网/标高/原点 + 缺失RISK
  Step2 柱+剪力墙         → 柱 + 剪力墙 + 柱墙冲突
  Step3 梁                → 梁 + 连接检查 + 防落地
  ═══ 结构框架 Gate ═══    → 柱贴点率/梁悬空率/剪力墙, 不过 STOP
  Step4 建筑墙            → 建筑墙 + 房间闭合
  ═══ 建筑空间 Gate ═══    → 墙闭合率, 不过 STOP
  Step5 板+门窗+楼梯      → 提取 + 门窗挂墙
  Step6 全局校验          → 悬空/穿透/重叠
  Step7 风险输出          → MODEL_STATUS + 置信度 + 风险清单 + 人工确认项

规则:
- Rule 1-4: 先结构后建筑/先坐标后构件/先基准后定位/先框架后空间
- Rule 5: 不确定就标记, 不强行猜测
- Rule 6: 前置 Gate 未过禁止后续 Step
- Rule 7: 构件必须带 ID/Type/Geometry/Position/Level/Grid/Confidence/Source/Warnings
"""
import argparse
import ezdxf
import hashlib
import json
import copy
import math
import os
import re
import subprocess
import sys
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JSON_IN = os.environ.get("BIM_JSON_IN", os.path.join(
    PROJECT_ROOT, "data", "runtime", "revit", "json_in"))
RVT_OUT = os.environ.get("REVIT_OUTPUT_DIR", os.path.join(
    PROJECT_ROOT, "data", "runtime", "revit", "rvt_out"))
SHORT_WALL_REVIEW_IR = None
WALL_SOURCE_EXCLUDE_PATTERNS = []
ARCHITECTURAL_WALL_CONTINUITY_GAP_M = 4.00
ROUTES_PORT = 48884
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _wall_model_element(index, wall, level_name, base_m, top_m):
    """Serialize one architectural wall with quantity-ready parameters."""
    thickness_mm = round(wall.get('thickness', 200))
    height_mm = round((top_m - base_m) * 1000)
    start_mm = [round(wall['start'][0] * 1000, 1),
                round(wall['start'][1] * 1000, 1), 0.0]
    end_mm = [round(wall['end'][0] * 1000, 1),
              round(wall['end'][1] * 1000, 1), 0.0]
    length_m = math.dist(start_mm[:2], end_mm[:2]) / 1000.0
    wall_type_name = 'AR-墙-砌体-%dmm' % thickness_mm
    element = {
        'type': 'Wall', 'id': 'wall_%d' % index,
        'start': start_mm, 'end': end_mm,
        'thickness': thickness_mm, 'height': height_mm,
        'thickness_mm': thickness_mm,
        'level': level_name, 'level_name': level_name,
        'wall_type_name': wall_type_name, 'type_name': wall_type_name,
        'base_offset': 0.0,
        'gross_face_area_m2': round(length_m * height_mm / 1000.0, 3),
        'gross_volume_m3': round(
            length_m * thickness_mm * height_mm / 1_000_000.0, 3),
        'length_m': round(length_m, 3),
        'quantity_scope': 'GROSS_NO_OPENINGS',
    }
    # Keep the semantic/source fields on the executable model element.  The
    # Revit writer only needs start/end/thickness, but quantity review and
    # later wall consolidation need to know which wall group/layer produced
    # the geometry and whether it came from a constrained recovery rule.
    for key in ('paired', 'confidence', 'geometry_source', 'wall_group',
                'source_layers', 'warnings', 'source_candidate_id',
                'profile_occurrence_id', 'drawing_identity',
                'source_occurrence_id', 'placed_entity_id',
                'structural_occurrence_id', 'fragmented_face_recovery_id',
                'short_wall_recovery', 'terminal_face_recovery',
                'terminal_face_recovery_count', 'source_segment_ids',
                'source_segment_refs', 'derivation', 'endpoint_adjustments',
                'geometry_adjustments'):
        if wall.get(key) is not None:
            element[key] = copy.deepcopy(wall[key])
    return element


def _grid_model_entry(level_name, s1):
    """Serialize one floor grid without losing its coordinate contract.

    Element coordinates are local to the extractor-selected grid anchor
    (normally the labelled 1/A intersection).  Keep the source-frame metadata
    beside the axes; the Revit writer places this local origin at the active
    project's Project Base Point.
    """
    grid = s1.get('grid') or {}
    grid_meta = grid.get('meta') or {}
    x_axes = list(grid.get('x_axes') or [])
    y_axes = list(grid.get('y_axes') or [])
    return {
        'level': level_name,
        'x_axes': [round(float(a.get('coord', 0.0)) * 1000, 1)
                   for a in x_axes],
        'y_axes': [round(float(a.get('coord', 0.0)) * 1000, 1)
                   for a in y_axes],
        # label is retained for compatibility; source_label is populated by
        # newer extractors and is deliberately nullable for unlabeled lines.
        'x_axis_labels': [a.get('source_label', a.get('label'))
                          for a in x_axes],
        'y_axis_labels': [a.get('source_label', a.get('label'))
                          for a in y_axes],
        'grid_prefix': grid.get('grid_prefix'),
        'origin_dxf_mm': list(s1.get('origin') or [0.0, 0.0]),
        'rotation_deg': float(s1.get('rot_deg') or 0.0),
        'rotation_center_dxf_mm': [s1.get('gcx'), s1.get('gcy')],
        'origin_mode': grid_meta.get('origin_mode'),
        # None delegates placement to the top-level coordinate-system policy.
        # New models use the active Revit project's Project Base Point.
        'project_offset_mm': None,
    }

# ★ 复用 pipeline_build.py 的提取函数(梁/剪力墙/板/门窗/楼梯/连接检查)
from pipeline_build import (
    extract_beams, extract_shear_walls, check_beam_connections, check_beam_level,
    extract_slabs, extract_openings, extract_stairs,
    extract_walls_with_thickness_list,
    clean_drawing, merge_layers, build_manifest,
    trigger_revit, dump_verify, quality_gate,
)
from backend.engines.wall_geometry import (
    restore_architectural_wall_continuity,
    wall_geometry_group,
)

DROP_KEYS = ('家具', 'A-CAR', 'A-STAIR', 'A-FIRE', 'EQUIP', 'A-TECH',
             'A-FURT', 'F-FURN', 'KITCHEN', 'KJ', 'A-FLOR-OVHD', '消火栓', '3T_BAR',
             'A-DOOR', '精装', 'SANT', '设备', '集水坑', '潜污泵', '墙洞', '夹层')

# 置信度分级(标准流程 §3)
CONF = {
    'CONFIRMED': 1.0, 'HIGH': 0.9, 'MEDIUM': 0.7,
    'LOW': 0.4, 'UNKNOWN': 0.1, 'CONFLICT': 0.0,
}


def _simplify_closed_profile(points, tolerance=1.0):
    """Remove duplicate/collinear boundary vertices without changing geometry."""
    simplified = list(points)
    changed = True
    while changed and len(simplified) > 3:
        changed = False
        kept = []
        count = len(simplified)
        for index, current in enumerate(simplified):
            previous = simplified[(index - 1) % count]
            following = simplified[(index + 1) % count]
            dx = following[0] - previous[0]
            dy = following[1] - previous[1]
            length_sq = dx * dx + dy * dy
            if length_sq <= tolerance * tolerance:
                kept.append(current)
                continue
            ratio = ((current[0] - previous[0]) * dx +
                     (current[1] - previous[1]) * dy) / length_sq
            if ratio < 0.0 or ratio > 1.0:
                kept.append(current)
                continue
            projection = (previous[0] + ratio * dx,
                          previous[1] + ratio * dy)
            if math.dist(current, projection) <= tolerance:
                changed = True
                continue
            kept.append(current)
        if len(kept) < 3:
            break
        simplified = kept
    return simplified


# ═══════════════ Step0: 数据清洗 ═══════════════
def step0_clean(doc):
    """清洗 + 单位检测 + 几何清洗(去重复线)
    输出: {drawing_id, unit, elements(kept), texts, layers, warnings, confidence}
    """
    KEEP = ('S-COLU', 'S-WALL', 'A-WALL', 'A-PART', 'WALL', 'S-STEL', 'A-GRID', 'A-ANNO', 'DIM', 'TEXT',
            '轴网', 'S-ANNO', 'GRID', 'COLUMN', 'COLU', 'S-BEAM', 'S-SLAB', 'S-STAIR')
    all_ents = [(e, e.dxf.layer if hasattr(e.dxf, 'layer') else '')
                for blk in doc.blocks for e in blk]
    total = len(all_ents)
    kept = []
    for e, l in all_ents:
        leaf = (l or '').upper().rsplit('$0$', 1)[-1]
        if not any(k.upper() in leaf for k in KEEP):
            continue
        # Wall block definitions are templates, not placed drawing evidence.
        # Both structural and architectural walls are appended below from the
        # exact same selected floor-plan INSERT and coordinate frame.
        if wall_geometry_group(l) is not None:
            continue
        if any(k in leaf for k in ('A-GRID', '轴网', 'A-ANNO', 'DIM', 'TEXT')):
            kept.append((e, l))
        elif not any(k.upper() in leaf for k in DROP_KEYS):
            kept.append((e, l))
    text_ents = [(e, l) for e, l in all_ents if e.dxftype() in ('TEXT', 'MTEXT')]

    # All walls must come from one uniquely selected, placed floor plan. The
    # specialised traversal preserves transforms and prunes symbol containers
    # only after proving that they contain no semantic wall geometry.
    wall_source_mode = 'placed_floor_plan'
    plan_selection = None
    placed_wall_records = []
    placed_vector_records = []
    placed_door_vector_records = []
    wall_symbol_source_records = []
    irregular_profile_audit = {
        'status': 'PASS', 'hatch_count': 0, 'compact_profile_count': 0,
        'non_rectangular_profile_count': 0, 'non_rectangular_profile_type_count': 0,
        'redundant_vertex_profile_count': 0,
        'column_label_count': 0,
        'samples': [],
        'candidates': [],
    }
    irregular_profile_signatures = set()
    wall_source_error = None
    door_vector_source_error = None
    source_drawing_identity = None
    try:
        from scripts.geometry_first_clean import (
            drawing_identity, placed_segment_id, select_main_plan,
            walk_door_evidence, walk_placed_entities, walk_wall_evidence,
        )
        from ezdxf.path import from_hatch
        selected_plan, plan_selection = select_main_plan(doc)
        source_drawing_identity = drawing_identity(doc)
        placed_wall_records = list(walk_wall_evidence(
            [selected_plan], drawing_id=source_drawing_identity))
        try:
            placed_door_vector_records = list(walk_door_evidence(
                [selected_plan], drawing_id=source_drawing_identity))
        except (OSError, ValueError, RuntimeError, ezdxf.DXFError) as exc:
            # Door geometry is semantic evidence only. Its failure is exposed
            # for review but must not invalidate the independently extracted
            # wall geometry or silently fall back to raster coordinates.
            door_vector_source_error = str(exc)
        column_label_pattern = re.compile(
            r'\b(?:GBZ|YBZ|GGZ|AZ|QZ|FBZ|XZ|KZ)\s*-?\s*\d+[A-Z]?\b', re.I)
        for entity, layer, provenance in walk_placed_entities(
                [selected_plan], drawing_id=source_drawing_identity):
            kind = entity.dxftype()
            if (kind == 'ARC' and
                    wall_geometry_group(layer) in {'A', 'S'}):
                # Wall-layer arcs are symbol evidence (for example a
                # mis-layered door swing), never wall modeling geometry.
                wall_symbol_source_records.append(
                    (entity, layer, provenance))
            if (kind in ('LINE', 'LWPOLYLINE') and
                    wall_geometry_group(layer) is None):
                placed_vector_records.append((entity, layer, provenance))
            if kind in ('TEXT', 'MTEXT', 'ATTRIB'):
                try:
                    text = (entity.plain_text() if hasattr(entity, 'plain_text')
                            else str(getattr(entity.dxf, 'text', '') or ''))
                except (AttributeError, ezdxf.DXFError):
                    text = ''
                if column_label_pattern.search(text):
                    irregular_profile_audit['column_label_count'] += 1
            leaf = (layer or '').upper().rsplit('$0$', 1)[-1]
            if kind != 'HATCH' or leaf != 'S-WALL-HATC':
                continue
            irregular_profile_audit['hatch_count'] += 1
            try:
                paths = from_hatch(entity)
            except (TypeError, ValueError, ezdxf.DXFError):
                continue
            for path_index, path in enumerate(paths):
                try:
                    points = [(float(point.x), float(point.y))
                              for point in path.flattening(1.0)]
                except (TypeError, ValueError):
                    continue
                cleaned_points = []
                for point in points:
                    if (not cleaned_points or
                            math.dist(point, cleaned_points[-1]) > 1.0):
                        cleaned_points.append(point)
                if len(cleaned_points) >= 4:
                    for point_index in range(3, len(cleaned_points)):
                        if math.dist(cleaned_points[point_index], cleaned_points[0]) <= 1.0:
                            cleaned_points = cleaned_points[:point_index]
                            break
                if len(cleaned_points) < 3:
                    continue
                original_vertex_count = len(cleaned_points)
                cleaned_points = _simplify_closed_profile(cleaned_points)
                if len(cleaned_points) < original_vertex_count:
                    irregular_profile_audit['redundant_vertex_profile_count'] += 1
                xs = [point[0] for point in cleaned_points]
                ys = [point[1] for point in cleaned_points]
                width, depth = max(xs) - min(xs), max(ys) - min(ys)
                area = abs(sum(
                    first[0] * second[1] - second[0] * first[1]
                    for first, second in zip(
                        cleaned_points, cleaned_points[1:] + cleaned_points[:1]))) / 2.0
                if area < 50000.0 or width > 5000.0 or depth > 5000.0:
                    continue
                irregular_profile_audit['compact_profile_count'] += 1
                if len(cleaned_points) > 4:
                    irregular_profile_audit['non_rectangular_profile_count'] += 1
                    signature = (len(cleaned_points), round(width / 10.0),
                                 round(depth / 10.0), round(area / 1000.0))
                    if signature not in irregular_profile_signatures:
                        irregular_profile_signatures.add(signature)
                    if (len(irregular_profile_audit['samples']) < 20 and
                            signature not in {
                                item.get('_signature')
                                for item in irregular_profile_audit['samples']}):
                        irregular_profile_audit['samples'].append({
                            '_signature': signature,
                            'vertex_count': len(cleaned_points),
                            'width_mm': round(width, 1),
                            'depth_mm': round(depth, 1),
                            'area_mm2': round(area, 1),
                        })
                    rounded_points = [
                        [round(point[0], 3), round(point[1], 3)]
                        for point in cleaned_points]
                    center = [
                        round(sum(point[0] for point in cleaned_points) /
                              len(cleaned_points), 3),
                        round(sum(point[1] for point in cleaned_points) /
                              len(cleaned_points), 3),
                    ]
                    entity_handle = provenance.get('source_entity_handle')
                    placement_path = provenance.get('placement_path') or []
                    placed_block = placement_path[-1] if placement_path else {}
                    block_name = (placed_block.get('block_name') or
                                  placed_block.get('name'))
                    occurrence_insert = list(
                        placed_block.get('insert') or [0.0, 0.0])
                    source_occurrence_id = provenance.get(
                        'source_occurrence_id')
                    boundary_segment_id = placed_segment_id(
                        provenance, path_index, 'hatch_boundary_path')
                    candidate_signature = json.dumps([
                        boundary_segment_id, center,
                        round(width, 1), round(depth, 1),
                        round(area, 1), len(cleaned_points),
                    ], ensure_ascii=False)
                    irregular_profile_audit['candidates'].append({
                        'candidate_id': 'irregular_profile_' + hashlib.sha256(
                            candidate_signature.encode('utf-8')).hexdigest()[:16],
                        'status': 'NEEDS_REVIEW',
                        'geometry_source': 'DXF_VECTOR',
                        'decision_reason': 'unclassified_non_rectangular_structural_hatch',
                        'entity_handle': entity_handle,
                        'source_block_handle': placed_block.get('insert_handle'),
                        'source_block_name': block_name,
                        'drawing_identity': provenance.get('drawing_identity'),
                        'source_occurrence_id': source_occurrence_id,
                        'structural_occurrence_id': provenance.get(
                            'structural_occurrence_id'),
                        'placed_entity_id': provenance.get('placed_entity_id'),
                        'segment_id': boundary_segment_id,
                        'placement_path': placement_path,
                        'source_occurrence_insert_dxf_mm': occurrence_insert,
                        'layer': layer,
                        'center_dxf_mm': center,
                        'points_dxf_mm': rounded_points,
                        'original_vertex_count': original_vertex_count,
                        'vertex_count': len(cleaned_points),
                        'width_mm': round(width, 1),
                        'depth_mm': round(depth, 1),
                        'area_mm2': round(area, 1),
                    })
        irregular_profile_audit['non_rectangular_profile_type_count'] = len(
            irregular_profile_signatures)
        for sample in irregular_profile_audit['samples']:
            sample.pop('_signature', None)
        if irregular_profile_audit['non_rectangular_profile_count']:
            irregular_profile_audit['status'] = 'PENDING_CLASSIFICATION'
    except (OSError, ValueError, RuntimeError, ezdxf.DXFError) as exc:
        wall_source_mode = 'blocked_no_unique_placed_plan'
        wall_source_error = str(exc)

    # 几何清洗: 去重复线(同图层同端点同长度)
    uniq_geometry = {}
    kept2 = []
    wall_records2 = []
    dup = 0
    for e, l in kept:
        if e.dxftype() == 'LINE':
            st, en = e.dxf.start, e.dxf.end
            key = (l, round(st.x, 1), round(st.y, 1), round(en.x, 1), round(en.y, 1))
            rkey = (l, round(en.x, 1), round(en.y, 1), round(st.x, 1), round(st.y, 1))
            if key in uniq_geometry or rkey in uniq_geometry:
                dup += 1
                continue
            uniq_geometry[key] = True
        kept2.append((e, l))

    # Keep the modeling view geometry-deduplicated exactly as before, while the
    # full placed evidence remains available in ``placed_wall_source_records``.
    # Distinct physical paths at identical coordinates are aggregated onto the
    # survivor instead of being silently erased or entering wall pairing twice.
    wall_survivor_by_geometry = {}
    for e, l, provenance in placed_wall_records:
        group = wall_geometry_group(l)
        if e.dxftype() == 'LINE':
            st, en = e.dxf.start, e.dxf.end
            edge = (round(st.x, 1), round(st.y, 1), round(en.x, 1), round(en.y, 1))
            reverse = (edge[2], edge[3], edge[0], edge[1])
            key, rkey = (group,) + edge, (group,) + reverse
        elif e.dxftype() == 'LWPOLYLINE':
            points = tuple((round(float(p[0]), 1), round(float(p[1]), 1))
                           for p in e.get_points('xy'))
            key, rkey = (group, bool(e.closed), points), (group, bool(e.closed), tuple(reversed(points)))
        else:
            continue
        survivor_index = (wall_survivor_by_geometry.get(key)
                          if key in wall_survivor_by_geometry else
                          wall_survivor_by_geometry.get(rkey))
        if key in uniq_geometry or rkey in uniq_geometry:
            dup += 1
            if survivor_index is not None:
                survivor_entity, survivor_layer, survivor_provenance = (
                    wall_records2[survivor_index])
                placements = survivor_provenance.setdefault(
                    'equivalent_placements', [])
                placements.append({
                    field: provenance.get(field) for field in (
                        'drawing_identity', 'source_entity_handle',
                        'source_occurrence_id', 'placed_entity_id',
                        'structural_occurrence_id', 'segment_id',
                        'segment_ids', 'placement_path')
                })
            continue
        uniq_geometry[key] = True
        modeling_provenance = dict(provenance)
        modeling_provenance['equivalent_placements'] = []
        wall_survivor_by_geometry[key] = len(wall_records2)
        wall_survivor_by_geometry[rkey] = len(wall_records2)
        wall_records2.append((e, l, modeling_provenance))
        kept2.append((e, l))

    warnings = []
    if wall_source_error:
        warnings.append(f'无法唯一定位已放置主平面：{wall_source_error}；禁止建模')
    if not placed_wall_records and not wall_source_error:
        warnings.append('主平面内未发现语义墙线；禁止建模')
    if door_vector_source_error:
        warnings.append(
            f'显式门图层矢量证据提取失败：{door_vector_source_error}')
    unit = 'mm'
    # 单位检测: 坐标量级 >1e6 → 可能 cm/m(工程图坐标 5e8 是共享坐标, 保持 mm)
    # 注: Revit 导出 5.16e8 = 共享坐标 mm, 不换算; 若坐标 <1000 可能是 m
    if kept2:
        xs = [e.dxf.start.x for e, l in kept2 if e.dxftype() == 'LINE'][:50]
        if xs and max(xs) < 1000:
            warnings.append("坐标量级 <1000, 疑似单位 m——需人工确认(标准§0.3)")
            unit = 'UNKNOWN'

    return {
        'drawing_id': os.path.basename(doc.filename or 'drawing.dxf'),
        'unit': unit,
        'elements': kept2, 'texts': text_ents,
        'layers': sorted({l for _, l in kept2}),
        'warnings': warnings,
        'dup_lines_removed': dup,
        'architecture_source_mode': wall_source_mode,
        'wall_source_mode': wall_source_mode,
        'wall_source_records': wall_records2,
        'placed_wall_source_records': placed_wall_records,
        'vector_source_records': placed_vector_records,
        'door_vector_source_records': placed_door_vector_records,
        'wall_symbol_source_records': wall_symbol_source_records,
        'door_vector_source_status': (
            'ERROR' if door_vector_source_error else 'OK'),
        'door_vector_source_error': door_vector_source_error,
        'source_drawing_identity': source_drawing_identity,
        'irregular_profile_audit': irregular_profile_audit,
        'plan_selection': plan_selection,
        'structural_source_entities': sum(
            1 for _, layer, _ in wall_records2 if wall_geometry_group(layer) == 'S'),
        'architecture_source_entities': sum(
            1 for _, layer, _ in wall_records2 if wall_geometry_group(layer) == 'A'),
        'confidence': 0.0 if wall_source_error or not placed_wall_records else 0.9,
    }


# ═══════════════ Step1: 轴网+标高 ═══════════════
def step1_grid_level(dxf, kept):
    """轴网 + 标高 + 原点(调 extract_columns_v2 已验证)
    输出: {grid, cols(柱, Step2 用), origin, rot, levels, risks}
    """
    grid, cols, prefix = extract_columns(dxf)
    ox, oy = grid.get("origin_mm") or [0, 0]
    meta = grid.get("meta") or {}
    rot_deg = meta.get("rot_deg", 0.0)
    gcx = meta.get("gcx", ox)
    gcy = meta.get("gcy", oy)

    # 标高风险(标准§1.5): 块名无括号标高 → RISK
    risks = []
    if cols:
        base = cols[0].get('elev_base_m')
        top = cols[0].get('elev_top_m')
        if base is None or top is None:
            risks.append({"type": "RISK", "item": "标高",
                          "reason": "图纸缺少明确标高信息", "action": "人工确认"})
    # 轴网风险(标准§1.6)
    nx, ny = len(grid.get('x_axes', [])), len(grid.get('y_axes', []))
    if nx < 3 or ny < 3:
        risks.append({"type": "RISK", "item": "轴网",
                      "reason": f"轴网不完整 {nx}x{ny}", "action": "人工确认"})

    return {
        'grid': grid, 'cols': cols, 'prefix': prefix,
        'origin': [ox, oy], 'rot_deg': rot_deg, 'gcx': gcx, 'gcy': gcy,
        'risks': risks,
    }


# ═══════════════ Step2: 柱+剪力墙 ═══════════════
def step2_col_shear(kept, s1):
    """柱 + 剪力墙 + 柱墙冲突
    输出: {cols(带置信度), shear_walls, conflicts}
    """
    kept = kept['elements'] if isinstance(kept, dict) else kept
    cols = s1['cols']
    ox, oy = s1['origin']
    shear = extract_shear_walls(kept, ox, oy, s1['rot_deg'], s1['gcx'], s1['gcy'])

    # 置信度标注(标准 Rule 7)
    for c in cols:
        c['confidence'] = CONF['HIGH'] if c.get('grid_ref') else CONF['MEDIUM']
        c['source'] = ['drawing', 'column_block']
        c['warnings'] = []
        if not c.get('grid_ref'):
            c['warnings'].append('未贴轴网交点, 低置信')
            c['confidence'] = CONF['LOW']

    # 柱墙冲突(标准§2.6): 柱心落在剪力墙内(柱 center 与墙 start/end 同为相对坐标)
    conflicts = []
    for c in cols:
        cc = c['center']
        for sw in shear:
            sx0, sy0 = sw['start'][0], sw['start'][1]
            ex0, ey0 = sw['end'][0], sw['end'][1]
            wx, wy = ex0 - sx0, ey0 - sy0
            wl = math.hypot(wx, wy)
            if wl < 1e-6:
                continue
            ux, uy = wx / wl, wy / wl
            vx, vy = cc[0] - sx0, cc[1] - sy0
            proj = max(0, min(wl, vx * ux + vy * uy))
            px, py = sx0 + proj * ux, sy0 + proj * uy
            if math.hypot(cc[0] - px, cc[1] - py) < sw['thickness'] / 2000:
                conflicts.append({"item": c.get('id', '?'), "type": "CONFLICT",
                                  "reason": "柱落在剪力墙内", "action": "人工确认"})
                break
    return {'cols': cols, 'shear_walls': shear, 'conflicts': conflicts}


# ═══════════════ Step3: 梁 ═══════════════
def step3_beam(doc, s1, s2):
    """梁 + 连接检查 + 防落地
    输出: {beams(带置信度), floating, level_issues}
    """
    ox, oy = s1['origin']
    beams = extract_beams(doc, ox, oy, s1['rot_deg'], s1['gcx'], s1['gcy'])
    grid_xy = ([a["coord"] for a in s1['grid']["x_axes"]],
               [a["coord"] for a in s1['grid']["y_axes"]])
    conn, total, floating = check_beam_connections(beams, s2['cols'], s2['shear_walls'], grid_xy)
    base_m = s2['cols'][0]['elev_base_m'] if s2['cols'] else 0.0
    top_m = s2['cols'][0]['elev_top_m'] if s2['cols'] else 3.0
    lv_ok, susp = check_beam_level(beams, base_m, top_m)

    # 置信度
    for i, b in enumerate(beams):
        b['confidence'] = CONF['HIGH'] if i not in floating else CONF['LOW']
        b['source'] = ['drawing', 'S-BEAM']
        b['warnings'] = ['悬空(无支撑)'] if i in floating else []
    return {'beams': beams, 'floating': floating, 'level_issues': susp,
            'conn': conn, 'total': total}


# ═══════════════ Gate1: 结构框架 ═══════════════
def gate1_structure(s1, s2, s3):
    """结构框架 Gate(标准): 柱贴点率≥70% + 梁悬空率<30% + 剪力墙识别
    不过 → STOP(禁进 Step4)
    """
    checks = []
    ok = True
    cols = s2['cols']
    grid = s1['grid']
    if cols and grid.get("x_axes") and grid.get("y_axes"):
        gx = [a["coord"] * 1000 for a in grid["x_axes"]]
        gy = [a["coord"] * 1000 for a in grid["y_axes"]]
        # 贴轴判定：任一向贴轴即算定位（工程实际：柱由两向定位，常有一向在
        # 次轴/梁线上而该轴未被轴网提取覆盖；要求 x、y 同时贴轴会误杀这类柱，
        # 实测 hotel 图 x向贴 98% / y向 67% 因 y 轴线漏提——改 OR 语义后通过）
        attached = sum(1 for c in cols
                       if min(abs(v - c["center"][0] * 1000) for v in gx) < 300
                       or min(abs(v - c["center"][1] * 1000) for v in gy) < 300)
        rate = attached / len(cols)
        checks.append(f"柱贴轴网率 {rate:.0%} ({'OK' if rate >= 0.7 else 'FAIL'})")
        ok = ok and rate >= 0.7
    beams = s3['beams']
    if beams:
        float_rate = len(s3['floating']) / len(beams)
        checks.append(f"梁悬空率 {float_rate:.0%} ({'OK' if float_rate < 0.3 else 'FAIL'})")
        ok = ok and float_rate < 0.3
    if s2['shear_walls']:
        checks.append(f"剪力墙 {len(s2['shear_walls'])} 段")
    return ("PASS" if ok else "FAIL"), checks


# ═══════════════ Step4: 建筑墙 ═══════════════
def step4_arch_wall(kept, s1, s2):
    """建筑墙(双线配对取厚 + 剪力墙分离) + 房间闭合检查
    输出: {walls(带置信度), rooms, closed_rate}
    ★ 复用算法层验证过的双线配对版(extract_walls_with_thickness)——之前简化版
      全部 S-WALL 线直接收 + 厚度写死 200, B1 闭合率 80%→54%(实测倒退), 已换回
    """
    kept = kept['elements'] if isinstance(kept, dict) else kept
    ox, oy = s1['origin']
    walls = extract_walls_with_thickness_list(kept, ox, oy, s1['rot_deg'], s1['gcx'], s1['gcy'])

    # 剪力墙分离(防重复建模): 用 start 坐标集去重(标准: 结构墙已在 Step2 建模)
    shear_keys = set()
    for sw in s2['shear_walls']:
        shear_keys.add((round(sw['start'][0], 1), round(sw['start'][1], 1)))
    walls = [w for w in walls
             if (round(w['start'][0], 1), round(w['start'][1], 1)) not in shear_keys]

    # Restore gross wall runs after removing structural-wall duplicates.  The
    # raw extractor returns each visible fragment; without this pass, doors,
    # columns and tiny drawing gaps become separate Revit wall instances even
    # though the requested quantity scope is gross wall (openings ignored).
    walls, continuity_restoration = restore_architectural_wall_continuity(
        walls, gap_m=ARCHITECTURAL_WALL_CONTINUITY_GAP_M)

    for w in walls:
        w['confidence'] = CONF['HIGH'] if w.get('paired') else CONF['MEDIUM']
        w['source'] = ['drawing', '%s-WALL' % w.get('wall_group', 'S'), 'double-line']
        w['warnings'] = [] if w.get('paired') else ['未配对单线, 厚度取众数']

    # 房间闭合检查(标准§4.4): 墙段端点与其他墙段端点/**中点**距离 <300mm 算连接
    # ★ 中点判定必须有——双线配对后是中线段, 端头常落在邻墙中点附近(实测漏判: 54%→29%)
    # 闭合率 = 端点有连接的墙 / 总墙段
    connected = 0
    for i, w in enumerate(walls):
        hit = False
        for j, w2 in enumerate(walls):
            if i == j:
                continue
            pts2 = [w2['start'], w2['end'],
                    [(w2['start'][0] + w2['end'][0]) / 2, (w2['start'][1] + w2['end'][1]) / 2]]
            for p1 in (w['start'], w['end']):
                for p2 in pts2:
                    if math.hypot(p1[0] - p2[0], p1[1] - p2[1]) < 0.3:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
        if hit:
            connected += 1
    closed_rate = connected / len(walls) if walls else 0
    return {'walls': walls, 'closed_rate': closed_rate,
            'continuity_restoration': continuity_restoration}


# ═══════════════ Gate2: 建筑空间 ═══════════════
def gate2_arch(s4):
    """建筑空间 Gate: 墙闭合率 ≥70% + 墙数>0
    """
    checks = []
    ok = True
    walls = s4['walls']
    if not walls:
        checks.append("建筑墙 0 条 (FAIL)")
        return "FAIL", checks
    rate = s4['closed_rate']
    checks.append(f"墙闭合率 {rate:.0%} ({'OK' if rate >= 0.7 else 'FAIL'})")
    ok = rate >= 0.7
    return ("PASS" if ok else "FAIL"), checks


# ═══════════════ Step5: 板+门窗+楼梯 ═══════════════
def step5_slab_door_stair(doc, s1, s4):
    """板 + 门窗 + 楼梯 + 挂墙检查
    输出: {slabs, openings, stairs, open_on_wall}
    """
    ox, oy = s1['origin']
    slabs = extract_slabs(doc, ox, oy, s1['rot_deg'], s1['gcx'], s1['gcy'])
    openings = extract_openings(doc, ox, oy, s1['rot_deg'], s1['gcx'], s1['gcy'])
    stairs = extract_stairs(doc, ox, oy, s1['rot_deg'], s1['gcx'], s1['gcy'])

    # 门窗挂墙检查(标准§5.2)
    open_on_wall = 0
    for o in openings:
        for w in s4['walls']:
            sx, sy = w['start']
            ex, ey = w['end']
            wx, wy = ex - sx, ey - sy
            wl = math.hypot(wx, wy)
            if wl < 1e-6:
                continue
            ux, uy = wx / wl, wy / wl
            vx, vy = o['x'] - sx, o['y'] - sy
            proj = max(0, min(wl, vx * ux + vy * uy))
            px, py = sx + proj * ux, sy + proj * uy
            if math.hypot(o['x'] - px, o['y'] - py) < 0.6:
                open_on_wall += 1
                break
    return {'slabs': slabs, 'openings': openings, 'stairs': stairs,
            'open_on_wall': open_on_wall}


# ═══════════════ Step6: 全局校验 ═══════════════
def step6_global_check(s1, s2, s3, s4, s5):
    """全局校验(标准§6): 悬空/穿透/重叠
    输出: {issues}
    """
    issues = []
    # ① 梁悬空(已有)
    for i in s3['floating']:
        issues.append({"item": f"beam_{i}", "type": "TOPOLOGY_ERROR",
                       "reason": "梁悬空(无支撑)", "action": "人工确认"})
    # ② 门窗未挂墙
    total_open = len(s5['openings'])
    if total_open:
        not_on_wall = total_open - s5['open_on_wall']
        if not_on_wall > 0:
            issues.append({"item": f"openings x{not_on_wall}", "type": "TOPOLOGY_ERROR",
                           "reason": f"{not_on_wall} 个门窗未挂在墙上", "action": "人工确认"})
    # ③ 柱墙冲突(已有)
    issues.extend(s2['conflicts'])
    # ④ 几何异常(标准§6.3): 柱尺寸异常
    for c in s2['cols']:
        w, d = c['size'][0] * 1000, c['size'][1] * 1000
        if w < 100 or d < 100 or w > 3000 or d > 3000:
            issues.append({"item": c.get('id', '?'), "type": "GEOMETRY_ERROR",
                           "reason": f"柱尺寸异常 {w}x{d}mm", "action": "人工确认"})
    return {'issues': issues}


# ═══════════════ Step7: 风险输出 ═══════════════
def step7_risk_report(steps):
    """风险输出 + MODEL_STATUS(标准§11)
    输出: 完整报告(落盘 model_status.json)
    """
    s0, s1, s2, s3, g1, s4, g2, s5, s6 = steps
    # MODEL_STATUS
    status = {
        "Step0_数据清洗": "PASS" if s0['confidence'] >= 0.5 else "FAIL",
        "Step1_轴网标高": "PASS" if not any(r['type'] == 'RISK' for r in s1['risks']) else "FAIL",
        "Step2_柱剪力墙": "PASS" if not s2['conflicts'] else "WARN",
        "Step3_梁": "PASS" if s3['level_issues'] == 0 else "WARN",
        "结构框架Gate": g1[0],
        "Step4_建筑墙": ("N/A" if g2[0] == "N/A" else
                         "PASS" if g2[0] == "PASS" else "FAIL"),
        "建筑空间Gate": g2[0],
        "Step5_板门窗楼梯": ("SKIPPED(建筑空间Gate未过)" if g2[0] == "FAIL"
                         else ("PASS" if s5['open_on_wall'] >= len(s5['openings']) * 0.5 else "WARN")),
        "Step6_全局校验": "FAIL" if len(s6['issues']) > 20 else "PASS",
    }
    # 数据质量
    n_high = sum(1 for c in s2['cols'] if c.get('confidence', 0) >= 0.7)
    col_conf = n_high / len(s2['cols']) if s2['cols'] else 0
    quality = {
        "总体置信度": round(col_conf * 0.6 + (1 - len(s3['floating']) / max(len(s3['beams']), 1)) * 0.4, 2),
        "几何准确率": round(s4['closed_rate'], 2),
        "构件识别率": round(len(s2['cols']) / max(len(s2['cols']), 1), 2),
        "坐标准确率": round(col_conf, 2),
        "拓扑准确率": round(1 - len(s3['floating']) / max(len(s3['beams']), 1), 2),
    }
    # 风险分级
    high = [i for i in s6['issues'] if i['type'] in ('TOPOLOGY_ERROR', 'CONFLICT')]
    med = [i for i in s6['issues'] if i['type'] == 'GEOMETRY_ERROR']
    low = s1['risks']
    # 人工确认项(UNKNOWN/LOW/CONFLICT 构件)
    human = [c.get('id', '?') for c in s2['cols'] if c.get('confidence', 1) < 0.5]
    human += [f"beam_{i}" for i in s3['floating'][:20]]

    report = {
        "MODEL_STATUS": status,
        "数据质量": quality,
        "风险": {"高风险": len(high), "中风险": len(med), "低风险": len(low)},
        "人工确认项": human[:50],
        "构件统计": {
            "柱": len(s2['cols']), "剪力墙": len(s2['shear_walls']),
            "梁": len(s3['beams']), "建筑墙": len(s4['walls']),
            "板": len(s5['slabs']), "门窗": len(s5['openings']),
            "楼梯": len(s5['stairs']),
        },
    }
    os.makedirs(JSON_IN, exist_ok=True)
    json.dump(report, open(os.path.join(JSON_IN, "model_status.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return report


# ═══════════════ 复用函数(extract_columns 等) ═══════════════
def extract_columns(dxf_path):
    """subprocess 调 extract_columns_v2.py → (grid, cols, prefix)"""
    r = subprocess.run([sys.executable, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "extract_columns_v2.py"), dxf_path],
                       capture_output=True, timeout=600, text=True)
    if r.returncode != 0:
        raise RuntimeError("提取失败: %s" % (r.stderr or r.stdout)[-300:])
    prefix = None
    for line in r.stdout.splitlines():
        if "[OK]" in line:
            prefix = line.split("输出")[-1].strip()
    if not prefix:
        raise RuntimeError("无输出: %s" % r.stdout[-300:])
    grid = json.load(open(os.path.join(JSON_IN, f"{prefix}_grid.json"), encoding="utf-8"))
    cols = json.load(open(os.path.join(JSON_IN, f"{prefix}_columns.json"), encoding="utf-8"))
    return grid, cols, prefix


def final_cleaning_blocks_model(report):
    """Return whether an explicit final cleaning decision blocks delivery."""
    decision = ((report.get("清洗审计") or {}).get("最终判定")
                if isinstance(report, dict) else None)
    return bool(
        isinstance(decision, dict) and
        decision.get("allow_modeling") is not True
    )


# ═══════════════ 主调度器(标准流程) ═══════════════
def run_standard_pipeline(dxf_paths, skip_build=False, debug=False,
                          short_wall_review_ir=None,
                          wall_source_exclude_patterns=None,
                          architectural_wall_gap_m=4.00):
    """严格按标准流程执行: Step0→7, Gate 不过 STOP
    返回: 最终报告
    """
    global SHORT_WALL_REVIEW_IR
    global WALL_SOURCE_EXCLUDE_PATTERNS
    global ARCHITECTURAL_WALL_CONTINUITY_GAP_M
    SHORT_WALL_REVIEW_IR = short_wall_review_ir
    WALL_SOURCE_EXCLUDE_PATTERNS = [str(value) for value in (
        wall_source_exclude_patterns or []) if str(value).strip()]
    try:
        gap_m = float(architectural_wall_gap_m)
    except (TypeError, ValueError):
        gap_m = 4.00
    ARCHITECTURAL_WALL_CONTINUITY_GAP_M = min(5.0, max(0.0, gap_m))
    os.makedirs(JSON_IN, exist_ok=True)
    print("=" * 62)
    print("BuildMate 图纸→BIM 管线(标准流程第九期)")
    print("轴网→标高→柱+剪力墙→梁→[结构Gate]→建筑墙→[空间Gate]→板门窗楼梯→全局校验→风险")
    print("=" * 62)

    all_layers = []  # 多层合并数据
    all_reports = []
    all_elems, all_levels, all_grids = [], [], []  # ★ 多层合并(Step8): 各层构件/标高/轴网统一收集, 循环后一次写 model.json
    build_blocked = False
    for dxf in dxf_paths:
        import ezdxf
        print(f"\n### {os.path.basename(dxf)}")
        doc = ezdxf.readfile(dxf)

        # ── Step0 数据清洗 ──
        print("[Step0] 数据清洗(图层+几何+单位)")
        s0 = step0_clean(doc)
        print(f"  实体 {len(s0['elements'])} (删重复线 {s0['dup_lines_removed']}) | "
              f"文字 {len(s0['texts'])} | 单位 {s0['unit']}")
        print(f"  已放置墙源: 结构 {s0.get('structural_source_entities', 0)} | "
              f"建筑 {s0.get('architecture_source_entities', 0)} | "
              f"模式 {s0.get('wall_source_mode', 'unknown')}")
        if s0.get('plan_selection'):
            selected_plan = s0['plan_selection']['selected']
            print(f"  主平面: {selected_plan['name']} @ {selected_plan['insert']} | "
                  f"排除 {len(s0['plan_selection'].get('excluded', []))} 个其他平面")
        irregular_audit = s0.get('irregular_profile_audit') or {}
        if irregular_audit.get('hatch_count'):
            print(f"  非矩形结构填充初筛: 紧凑填充 {irregular_audit.get('compact_profile_count', 0)} | "
                  f"共线冗余 {irregular_audit.get('redundant_vertex_profile_count', 0)} | "
                  f"非矩形 {irregular_audit.get('non_rectangular_profile_count', 0)} | "
                  f"柱标号 {irregular_audit.get('column_label_count', 0)} | "
                  f"{irregular_audit.get('status')}")
        for w in s0['warnings']:
            print(f"  ⚠️ {w}")

        # ── 阶段① 模型策划 #1 建模标准(标准§0: 命名/图层/精度规范) ──
        print("[阶段①] 建模标准(project_manifest)")
        import datetime
        manifest = {
            "schema": "BIM建模策划-标准流程",
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "项目名": os.path.basename(os.path.dirname(os.path.dirname(dxf))),
            "图纸": os.path.basename(dxf),
            "建模标准": {
                "命名规范": "{层}_{构件}_{序号}",
                "单位": "mm(坐标)/m²(面积)",
                "坐标系": "真实1/A轴优先锚定；无轴号时回退最小轴交点",
                "精度规范": "LOD300(构件级: 几何+尺寸+位置)",
                "图层规范": {
                    "柱": "S-COLU/砼柱/COLUMN", "剪力墙": "S-WALL/砼墙",
                    "梁": "S-BEAM", "板": "S-SLAB", "楼梯": "S-STAIR",
                },
            },
            "LOD等级": {
                "柱": "LOD300", "剪力墙": "LOD300", "梁": "LOD300",
                "建筑墙": "LOD300", "板": "LOD300",
                "门窗洞口": "LOD300", "楼梯": "LOD200(轮廓)",
            },
        }
        json.dump(manifest, open(os.path.join(JSON_IN, "project_manifest.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"  命名规范/坐标系/精度规范/LOD 已定义 → project_manifest.json")

        # ── Step1 轴网+标高 ──
        print("[Step1] 轴网+标高(统一坐标系)")
        s1 = step1_grid_level(dxf, s0['elements'])
        nx, ny = len(s1['grid']['x_axes']), len(s1['grid']['y_axes'])
        print(f"  轴网 {nx}x{ny} | 原点 {s1['origin']} | 扶正 {s1['rot_deg']}°")
        for r in s1['risks']:
            print(f"  ⚠️ RISK: {r['reason']} → {r['action']}")

        # ── Step2 柱+剪力墙 ──
        print("[Step2] 柱+剪力墙")
        s2 = step2_col_shear(s0, s1)
        print(f"  柱 {len(s2['cols'])} | 剪力墙 {len(s2['shear_walls'])} 段 | 冲突 {len(s2['conflicts'])}")
        occurrence = s2.get('wall_occurrence_selection')
        if occurrence:
            print(f"  墙柱实例: {occurrence.get('selected_occurrence_id')} | "
                  f"分差 {occurrence.get('score_gap')} | {occurrence.get('reason')}")
        profile_audit = s2.get('irregular_profile_audit') or {}
        if profile_audit.get('raw_candidate_count'):
            print(f"  非矩形填充归类: 主实例唯一 {profile_audit.get('deduplicated_candidate_count', 0)} | "
                  f"墙填充排除 {profile_audit.get('rejected_by_rule_count', 0)} | "
                  f"待复核 {profile_audit.get('needs_review_count', 0)} | "
                  f"矢量异形柱 {profile_audit.get('extracted_profile_column_count', 0)}")

        # ── Step3 梁 ──
        print("[Step3] 梁(连接检查)")
        s3 = step3_beam(doc, s1, s2)
        print(f"  梁 {s3['total']} 条 | 有支撑 {s3['conn']} | 悬空 {len(s3['floating'])}"
              f" | 异常梁高 {s3['level_issues']}")

        # ── Gate1 结构框架 ──
        print("[结构框架 Gate]")
        g1 = gate1_structure(s1, s2, s3)
        for gc in g1[1]:
            print(f"  {gc}")
        print(f"  → {g1[0]}")
        if g1[0] == "FAIL":
            print("  ⛔ 结构框架未通过, 禁止进入 Step4(标准 Rule 6)")
            # Persist a fresh fail-closed report instead of leaving a previous
            # model_status.json in place when Gate1 stops the layer early.
            s4 = {
                'walls': [], 'closed_rate': 0.0, 'endpoint_coverage': 0.0,
                'room_polygon_count': 0, 'opening_bridges': [],
                'architecture_source_count': 0,
                'architecture_excluded_source_count': 0,
                'architecture_applicable': bool(
                    s0.get('architecture_source_entities')),
                'source_coverage': {}, 'endpoint_review': {},
                'traceability': s2.get('traceability') or {},
            }
            g2 = ("NOT_RUN", ["结构框架 Gate 未通过，建筑空间 Gate 未运行"])
            s5 = {'slabs': [], 'openings': [], 'stairs': [],
                  'open_on_wall': 0}
            s6 = {'issues': []}
            steps = (s0, s1, s2, s3, g1, s4, g2, s5, s6)
            report = step7_risk_report(steps)
            all_reports.append(report)
            json.dump(
                report,
                open(os.path.join(JSON_IN,
                                  f"model_status_{s1['prefix']}.json"),
                     'w', encoding='utf-8'),
                ensure_ascii=False, indent=1)
            build_blocked = True
            continue

        # ── Step4 建筑墙 ──
        print("[Step4] 建筑墙(剪力墙分离+房间闭合)")
        s4 = step4_arch_wall(s0, s1, s2)
        print(f"  建筑墙 {len(s4['walls'])} 条 | 闭合率 {s4['closed_rate']:.0%}")
        print(f"  主布局源实体 {s4.get('architecture_source_count', 0)} | "
              f"排除异布局 {s4.get('architecture_excluded_source_count', 0)} | "
              f"源线覆盖 {(s4.get('source_coverage') or {}).get('face_length_coverage', 0):.0%} | "
              f"端点连接 {s4.get('endpoint_coverage', 0):.0%} | "
              f"门洞桥接 {len(s4.get('opening_bridges', []))} | "
              f"成环房间 {s4.get('room_polygon_count', 0)} | "
              f"CV封闭区 {(s4.get('cv_topology_audit') or {}).get('enclosed_region_count', 0)}")
        vector_audit = s4.get('vector_candidate_audit') or {}
        if vector_audit:
            print(f"  非标准图层双线候选 {vector_audit.get('pair_candidate_count', 0)} | "
                  f"拓扑确认 {vector_audit.get('promoted_count', 0)} | "
                  f"待复核 {vector_audit.get('review_count', 0)}")

        # ── Gate2 建筑空间 ──
        print("[建筑空间 Gate]")
        g2 = gate2_arch(s4)
        for gc in g2[1]:
            print(f"  {gc}")
        print(f"  → {g2[0]}")
        arch_ok = (g2[0] == "PASS")
        if not arch_ok:
            # ★ Rule 6 语义修正(8-26): 禁的是 Step5(板/门窗/楼梯), 不是整层——
            #   结构 Gate 已过, 结构构件(柱/剪力墙/梁)置信度成立, 建模照常
            #   之前 continue 整层跳过 → 结构图(B1/L1/SG20/G50)永远建不出模型(实测)
            if s4.get('architecture_applicable', True):
                print("  ⛔ 完整平面图的建筑墙未通过: 本层禁止建模")
            else:
                print("  ⛔ 建筑空间不适用: 本层仅建模结构构件(柱/剪力墙/梁)")
            s5 = {'slabs': [], 'openings': [], 'stairs': [], 'open_on_wall': 0}
        else:
            # ── Step5 板+门窗+楼梯 ──
            print("[Step5] 板+门窗+楼梯(挂墙检查)")
            s5 = step5_slab_door_stair(doc, s1, s4)
            print(f"  板 {len(s5['slabs'])} | 门窗 {len(s5['openings'])}"
                  f"(挂墙 {s5['open_on_wall']}) | 楼梯 {len(s5['stairs'])}")

        # ── Step6 全局校验 ──
        print("[Step6] 全局校验(悬空/穿透/重叠)")
        s6 = step6_global_check(s1, s2, s3, s4, s5)
        print(f"  问题 {len(s6['issues'])} 项")
        for iss in s6['issues'][:5]:
            print(f"  ⚠️ {iss['type']}: {iss['reason']}")

        # ── Step7 风险输出 ──
        steps = (s0, s1, s2, s3, g1, s4, g2, s5, s6)
        report = step7_risk_report(steps)
        all_reports.append(report)

        # 分阶段输出
        json.dump(report, open(os.path.join(JSON_IN, f"model_status_{s1['prefix']}.json"),
                               "w", encoding="utf-8"), ensure_ascii=False, indent=1)

        if final_cleaning_blocks_model(report):
            reasons = ((report.get("清洗审计") or {}).get(
                "最终判定") or {}).get("reasons") or ["清洗审计未批准建模"]
            print("  ⛔ 清洗审计未通过，禁止输出正式模型: " +
                  "; ".join(str(reason) for reason in reasons))
            # Keep a quantity-ready wall preview when formal modeling is
            # blocked.  It is written only as model_review.json below and is
            # never passed to Revit, so unresolved gates remain fail-closed.
            if s4.get('walls'):
                review_base = s2['cols'][0]['elev_base_m'] if s2['cols'] else 0.0
                review_top = s2['cols'][0]['elev_top_m'] if s2['cols'] else 3.0
                review_level = s1['prefix']
                all_levels.extend([
                    {'name': review_level, 'elevation': int(review_base * 1000)},
                    {'name': review_level + '顶',
                     'elevation': int(review_top * 1000)},
                ])
                all_grids.append(_grid_model_entry(review_level, s1))
                all_elems.extend([
                    _wall_model_element(i, wall, review_level,
                                        review_base, review_top)
                    for i, wall in enumerate(s4['walls'])
                ])
            build_blocked = True
            continue

        # A full floor plan must never degrade into a structural-only delivery.
        # Structural-only continuation is valid only when architecture is truly
        # not applicable (Gate=N/A), not when architectural walls were found
        # but failed topology/coverage checks.
        if s4.get('architecture_applicable', True) and g2[0] != 'PASS':
            print("  ⛔ 完整平面图的建筑墙未通过，禁止输出本层模型")
            build_blocked = True
            continue

        all_layers.append((s1, s2, s3, s4, s5))

        # ── Step8 本层构件构造(mm 口径, 收集到 all_* 多层合并) ──
        # ★ 全构件 mm 口径(json2rvt: MM=304.8, xyz()/create_floor 全部按 mm 入参)
        #   之前只写柱+剪力墙且用米制 → 梁/墙/板全建不出(实测 8-25 dump: walls 108 vs 128 + 偏差 73m)
        print("[Step8] 本层构件(全构件, mm 口径, 多层合并收集)")
        base = s2['cols'][0]['elev_base_m'] if s2['cols'] else 0.0
        top = s2['cols'][0]['elev_top_m'] if s2['cols'] else 3.0
        lv_name = s1['prefix']
        all_levels.append({'name': lv_name, 'elevation': int(base * 1000)})
        all_levels.append({'name': lv_name + '顶', 'elevation': int(top * 1000)})
        all_grids.append(_grid_model_entry(lv_name, s1))
        elems = []
        # 柱(mm)
        for i, c in enumerate(s2['cols']):
            e_col = {'type': 'Column', 'id': 'col_%d' % i,
                     'x': round(c['center'][0] * 1000), 'y': round(c['center'][1] * 1000),
                     'width': round(c['size'][0] * 1000), 'depth': round(c['size'][1] * 1000),
                     'base': 0, 'top': round((top - base) * 1000), 'level': lv_name}
            if c.get('mark'):
                e_col['mark'] = c['mark']
                e_col['type_name'] = '%s_%dx%d' % (
                    c['mark'], e_col['width'], e_col['depth'])
                e_col['mark_source'] = c.get('mark_source', 'drawing_text')
                if c.get('mark_confidence') is not None:
                    e_col['mark_confidence'] = c['mark_confidence']
            else:
                # 未识别型号不得按尺寸冒充正式类型，否则算量会错误合并。
                e_col['mark'] = 'UNRESOLVED'
                e_col['type_name'] = 'UNRESOLVED_%s_%dx%d' % (
                    c.get('grid_ref') or ('COL%d' % i), e_col['width'], e_col['depth'])
            if c.get('profile'):
                # 异形截面(L/T/圆)——json2rvt 用 DirectShape 建真实轮廓(块内坐标, mm)
                e_col['profile'] = c['profile']
            elems.append(e_col)
        # 剪力墙(结构墙, mm)
        for i, sw in enumerate(s2['shear_walls']):
            elems.append({'type': 'Wall', 'id': 'shear_%d' % i,
                          'start': [round(sw['start'][0] * 1000, 1), round(sw['start'][1] * 1000, 1), 0.0],
                          'end': [round(sw['end'][0] * 1000, 1), round(sw['end'][1] * 1000, 1), 0.0],
                          'thickness': round(sw['thickness']), 'height': round((top - base) * 1000),
                          'level': lv_name})
        # 建筑墙(mm; 仅建筑空间 Gate 通过后建模——未过则结构图上只建结构构件)
        if arch_ok:
            for i, w in enumerate(s4['walls']):
                elems.append(_wall_model_element(i, w, lv_name, base, top))
        # 梁(mm; 截面 300x600 为默认值, 从标注提取留待后续)
        for i, b in enumerate(s3['beams']):
            elems.append({'type': 'Beam', 'id': 'beam_%d' % i,
                          'start': [round(b['start'][0] * 1000, 1), round(b['start'][1] * 1000, 1), 0.0],
                          'end': [round(b['end'][0] * 1000, 1), round(b['end'][1] * 1000, 1), 0.0],
                          'width': 300, 'height': 600, 'level': lv_name})
        # 板(mm; 轮廓 outline, json2rvt create_floor 必需)
        for i, s in enumerate(s5['slabs']):
            e_slab = {'type': 'Slab', 'id': 'slab_%d' % i,
                      'thickness': s.get('thickness_mm', 120), 'level': lv_name}
            if s.get('outline'):
                e_slab['outline'] = [[round(p[0] * 1000, 1), round(p[1] * 1000, 1)]
                                     for p in s['outline']]
            else:
                e_slab['center'] = [round(s['center'][0] * 1000, 1), round(s['center'][1] * 1000, 1)]
                e_slab['warnings'] = ['无轮廓, Revit 端不建模']
            elems.append(e_slab)
        # 门窗/楼梯(进 model.json 保 IR 完整; json2rvt 当前不支持建模——如实标注)
        for i, o in enumerate(s5['openings']):
            elems.append({'type': 'Opening', 'id': 'open_%d' % i,
                          'x': round(o['x'] * 1000, 1), 'y': round(o['y'] * 1000, 1),
                          'width': round(o['width'] * 1000, 1), 'height': round(o['height'] * 1000, 1),
                          'kind': o.get('kind', 'opening'), 'level': lv_name})
        for i, st in enumerate(s5['stairs']):
            elems.append({'type': 'Stair', 'id': 'stair_%d' % i,
                          'x': round(st['x'] * 1000, 1), 'y': round(st['y'] * 1000, 1),
                          'level': lv_name})
        all_elems.extend(elems)
        n_c = sum(1 for e in elems if e['type'] == 'Column')
        n_w = sum(1 for e in elems if e['type'] == 'Wall')
        n_b = sum(1 for e in elems if e['type'] == 'Beam')
        n_s = sum(1 for e in elems if e['type'] == 'Slab')
        n_o = sum(1 for e in elems if e['type'] == 'Opening')
        n_st = sum(1 for e in elems if e['type'] == 'Stair')
        print(f"  本层: 柱 {n_c} | 剪力墙+建筑墙 {n_w} | 梁 {n_b} | "
              f"板 {n_s} | 门窗 {n_o} | 楼梯 {n_st}(mm 口径)")

    # ── 多层合并: 统一写 model.json(全部层, mm 口径) + 触发 Revit(一次建全量) ──
    print("\n[Step8] 多层合并 → model.json")
    model = {
        'schema_version': 'buildmate.standard-pipeline-model/1.1',
        'status': 'REVIEW_ONLY' if build_blocked else 'FORMAL',
        'project': {'name': " + ".join(os.path.basename(p) for p in dxf_paths)
                    or "标准流程模型", 'levels': all_levels},
        'coordinate_system': {
            'unit': 'mm',
            'space': 'PROJECT_LOCAL',
            'project_offset_mm': None,
            'offset_policy': 'revit_project_base_point',
        },
        'grids': all_grids, 'model_elements': all_elems,
        'wall_quantity_scope': 'GROSS_NO_OPENINGS',
        'wall_continuity_gap_m': ARCHITECTURAL_WALL_CONTINUITY_GAP_M,
        'wall_processing': [
            {
                'source_layer_filter': (report.get('清洗审计') or {}).get(
                    '建筑墙来源图层过滤', {}),
                'continuity_restoration': (report.get('清洗审计') or {}).get(
                    '整墙连续性还原', {}),
            }
            for report in all_reports
        ],
    }
    model_name = 'model_review.json' if build_blocked else 'model.json'
    json.dump(model, open(os.path.join(JSON_IN, model_name), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    from collections import Counter as _Cnt
    _tc = _Cnt(e['type'] for e in all_elems)
    print(f"  合并后: {dict(_tc)} | 标高 {len(all_levels)} | 轴网 {len(all_grids)} 套")
    if build_blocked:
        print("  ⛔ 至少一个建模 Gate 未通过，仅输出 model_review.json；正式 model.json 和 Revit 均不修改")
    elif not all_elems:
        print("  ⛔ 无可建模构件，禁止触发 Revit")
    elif not skip_build:
        r2 = trigger_revit()
        if r2['status'] == 'done':
            print(f"  ✅ Revit 建模成功: {r2.get('rvt')}")
            try:
                v = dump_verify()
                print(f"  dump 回读: {json.dumps(v, ensure_ascii=False)}")
            except Exception as ex:
                print(f"  ⚠️ dump 失败: {ex}")
        else:
            print(f"  ⛔ 建模失败: {r2.get('error')}")

    # 汇总输出
    print("\n" + "=" * 62)
    for rep in all_reports:
        if 'MODEL_STATUS' in rep:
            print(f"\nMODEL_STATUS:")
            for k, v in rep['MODEL_STATUS'].items():
                print(f"  {k}: {v}")
            print(f"数据质量: {json.dumps(rep['数据质量'], ensure_ascii=False)}")
            print(f"风险: {json.dumps(rep['风险'], ensure_ascii=False)}")
            print(f"构件: {json.dumps(rep['构件统计'], ensure_ascii=False)}")
            if rep['人工确认项']:
                print(f"⚠️ 人工确认 {len(rep['人工确认项'])} 项: {rep['人工确认项'][:10]}")
    print("\n" + "=" * 62)
    return all_reports


def main():
    ap = argparse.ArgumentParser(description="BuildMate 标准流程建模管线")
    ap.add_argument("dxfs", nargs="+", help="DXF 图纸(每张=一层)")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument(
        "--short-wall-review-ir",
        help=("显式指定已审批的短墙 review IR JSON；不指定时不会搜索或"
              "重放任何历史审批"),
    )
    ap.add_argument(
        "--exclude-wall-source-layer-pattern", action="append", default=[],
        help="显式排除建筑墙来源图层片段，可重复指定；不会默认猜测",
    )
    ap.add_argument(
        "--gross-wall-gap-m", type=float, default=4.00,
        help="整墙还原时允许跨越的同轴缺口（米），默认2.2",
    )
    args = ap.parse_args()
    reports = run_standard_pipeline(
        args.dxfs, args.skip_build, args.debug,
        short_wall_review_ir=args.short_wall_review_ir,
        wall_source_exclude_patterns=args.exclude_wall_source_layer_pattern,
        architectural_wall_gap_m=args.gross_wall_gap_m)
    return 0 if reports else 1


if __name__ == "__main__":
    sys.exit(main())
