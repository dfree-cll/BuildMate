import copy
import math

import ezdxf

from backend.engines.wall_geometry import (
    _angle_delta,
    _line_record,
    _pair_geometry,
    audit_closed_wall_strips,
    audit_vector_wall_candidates,
    classify_dangling_endpoints,
    cv_room_enclosure_audit,
    heal_wall_junctions,
    merge_collinear_walls,
    restore_architectural_wall_continuity,
    orthogonalize_walls,
    topology_metrics,
    wall_geometry_group,
    extract_precise_walls,
    filter_wall_records_to_region,
    infer_opening_bridges,
    recover_short_wall_group_spans,
    topology_with_support,
    wall_source_coverage,
)
from scripts.geometry_first_clean import select_main_plan, walk, walk_wall_evidence
from tests.fixtures.dxf_entities import Line as _Line


def test_fragmented_wall_recovery_bridges_unique_end_caps():
    identity = "sha256:" + "a" * 64
    occurrence = "structural_occurrence_test"

    def unit(source_id, start, end):
        length = math.dist(start, end)
        return {
            "review_unit_id": "review_" + source_id,
            "source_segment_id": source_id,
            "drawing_identity": identity,
            "source_occurrence_id": "source_" + source_id,
            "placed_entity_id": "placed_" + source_id,
            "structural_occurrence_id": occurrence,
            "identity_is_complete": True,
            "identity_limitations": [],
            "entity_handle": source_id,
            "source_record_index": 1,
            "segment_index": 0,
            "source_axis_start": list(start),
            "source_axis_end": list(end),
            "start": list(start),
            "end": list(end),
            "length_m": length,
            "source_interval_m": [0.0, length],
            "layer": "B1$0$A-WALL-S",
            "placement_path": [{
                "insert_handle": source_id,
                "block_name": "TEST",
                "array_index": None,
                "cumulative_transform": {
                    "affine_2d": [[1.0, 0.0, 0.0],
                                   [0.0, 1.0, 0.0],
                                   [0.0, 0.0, 1.0]],
                },
            }],
        }

    face_a = unit("face_a", (0.0, -0.1), (0.2, -0.1))
    face_b = unit("face_b", (0.0, 0.1), (0.2, 0.1))
    face_c = unit("face_c", (1.5, -0.1), (2.0, -0.1))
    face_d = unit("face_d", (1.5, 0.1), (2.0, 0.1))
    cap_a = unit("cap_a", (0.2, -0.1), (0.2, 0.1))
    cap_b = unit("cap_b", (1.5, -0.1), (1.5, 0.1))
    review_units = [face_a, face_b, face_c, face_d, cap_a, cap_b]
    group = {
        "pair_group_id": "pair_group_test",
        "drawing_identity": identity,
        "structural_occurrence_id": occurrence,
        "wall_group": "A",
        "leaf_layers": ["A-WALL-S"],
        "thickness_mm": 200.0,
        "pairing_ambiguous": False,
        "semantic_layer_mismatch": False,
        "overlap_runs": [
            {"run_index": 0, "start": [0.1, 0.0],
             "end": [0.2, 0.0], "length_m": 0.1,
             "raw_pair_ids": ["pair_0"]},
            {"run_index": 1, "start": [1.5, 0.0],
             "end": [1.9, 0.0], "length_m": 0.4,
             "raw_pair_ids": ["pair_1"]},
        ],
        "inter_run_gaps": [{
            "after_run_index": 0, "start": [0.2, 0.0],
            "end": [1.5, 0.0], "length_m": 1.3,
        }],
        "raw_pair_evidence": [
            {"raw_pair_id": "pair_0", "face_a_source_segment_id": "face_a",
             "face_b_source_segment_id": "face_b"},
            {"raw_pair_id": "pair_1", "face_a_source_segment_id": "face_c",
             "face_b_source_segment_id": "face_d"},
        ],
    }
    result = recover_short_wall_group_spans({
        "review_units": review_units,
        "short_wall_pair_audit": {"groups": [group]},
    })

    assert result["candidate_count"] == 1
    assert result["cap_supported_gap_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["start"] == [0.1, 0.0]
    assert candidate["end"] == [1.9, 0.0]
    assert candidate["thickness"] == 200.0
def test_wall_layer_group_uses_leaf_semantics():
    assert wall_geometry_group("B1层墙柱$0$S-WALL") == "S"
    assert wall_geometry_group("B1_卫生间$0$A-PART-S") == "A"
    assert wall_geometry_group("B1_墙体$0$A-WALL-S") == "A"
    assert wall_geometry_group("B1_墙体$0$A-后砌") == "A"
    assert wall_geometry_group("B1_消火栓$0$A-DOOR-FIRE") is None


def test_wall_walk_can_keep_wall_geometry_inside_noise_container():
    doc = ezdxf.new()
    room = doc.blocks.new("卫生间块")
    room.add_line((0, 0), (2000, 0), dxfattribs={"layer": "A-PART-S"})
    insert = doc.modelspace().add_blockref(
        "卫生间块", (10000, 20000), dxfattribs={"layer": "卫生间"})

    default_layers = [layer for _entity, layer in walk([insert])]
    wall_layers = [layer for _entity, layer in walk([insert], skip_noise=False)]

    assert default_layers == []
    assert wall_layers == ["A-PART-S"]


def test_wall_evidence_walk_selectively_enters_noise_container_and_keeps_provenance():
    doc = ezdxf.new()
    bathroom = doc.blocks.new("卫生间块")
    bathroom.add_line((0, 0), (2000, 0), dxfattribs={"layer": "A-PART-S"})
    kitchen = doc.blocks.new("KITCHEN_SYMBOL")
    kitchen.add_line((0, 0), (600, 0), dxfattribs={"layer": "A-FURT2"})
    plan = doc.blocks.new("B1层平面图")
    plan.add_blockref("卫生间块", (1000, 2000), dxfattribs={"layer": "卫生间"})
    plan.add_blockref("KITCHEN_SYMBOL", (5000, 5000), dxfattribs={"layer": "KITCHEN"})
    root = doc.modelspace().add_blockref("B1层平面图", (10000, 20000))

    records = list(walk_wall_evidence([root]))

    assert len(records) == 1
    entity, layer, provenance = records[0]
    assert layer == "A-PART-S"
    assert (entity.dxf.start.x, entity.dxf.start.y) == (11000, 22000)
    assert provenance["root_plan"] == "B1层平面图"
    assert provenance["source_entity_handle"] is not None
    assert [item["name"] for item in provenance["placement_path"]] == [
        "B1层平面图", "卫生间块"]


def test_main_plan_selection_rejects_ambiguous_non_mezzanine_plans():
    doc = ezdxf.new()
    doc.blocks.new("B1层平面图")
    doc.blocks.new("B2层平面图")
    doc.modelspace().add_blockref("B1层平面图", (0, 0))
    doc.modelspace().add_blockref("B2层平面图", (0, 0))

    try:
        select_main_plan(doc)
    except ValueError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("ambiguous main plans must be blocked")


def test_main_plan_selection_accepts_unique_structural_wall_column_block():
    doc = ezdxf.new()
    doc.blocks.new("B1层墙柱(-6.5~-0.1m)")
    placed = doc.modelspace().add_blockref(
        "B1层墙柱(-6.5~-0.1m)", (0, 0))

    selected, audit = select_main_plan(doc)

    assert selected is placed
    assert audit["selection_mode"] == "structural_wall_column_block"
    assert audit["selected"]["name"] == "B1层墙柱(-6.5~-0.1m)"
    assert audit["candidate_count"] == 1


def test_floor_plan_selection_has_priority_over_structural_fallback():
    doc = ezdxf.new()
    doc.blocks.new("B1层平面图")
    doc.blocks.new("B1层墙柱(-6.5~-0.1m)")
    floor_plan = doc.modelspace().add_blockref("B1层平面图", (0, 0))
    doc.modelspace().add_blockref("B1层墙柱(-6.5~-0.1m)", (0, 0))

    selected, audit = select_main_plan(doc)

    assert selected is floor_plan
    assert audit["selection_mode"] == "floor_plan_block"
    assert audit["candidate_count"] == 1


def test_structural_plan_selection_rejects_ambiguous_wall_column_blocks():
    doc = ezdxf.new()
    doc.blocks.new("B1层墙柱(-6.5~-0.1m)")
    doc.blocks.new("B2层墙柱(-10.0~-6.5m)")
    doc.modelspace().add_blockref("B1层墙柱(-6.5~-0.1m)", (0, 0))
    doc.modelspace().add_blockref("B2层墙柱(-10.0~-6.5m)", (0, 0))

    try:
        select_main_plan(doc)
    except ValueError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("ambiguous structural plans must be blocked")


def test_architectural_partitions_are_extracted_separately():
    kept = [
        (_Line((0, 0), (10000, 0)), "B1_卫生间$0$A-PART-S"),
        (_Line((0, 200), (10000, 200)), "B1_卫生间$0$A-PART-S"),
        (_Line((0, 1000), (10000, 1000)), "B1层墙柱$0$S-WALL"),
        (_Line((0, 1300), (10000, 1300)), "B1层墙柱$0$S-WALL"),
    ]

    architecture = extract_precise_walls(
        kept, 0, 0, 0, 0, 0, wall_groups=("A",))
    structure = extract_precise_walls(
        kept, 0, 0, 0, 0, 0, wall_groups=("S",))

    assert len(architecture) == 1
    assert architecture[0]["thickness"] == 200
    assert architecture[0]["wall_group"] == "A"
    assert len(structure) == 1
    assert structure[0]["thickness"] == 300
    assert structure[0]["wall_group"] == "S"


def test_faces_from_different_nested_source_layers_are_not_paired():
    kept = [
        (_Line((0, 0), (10000, 0)), "B1_墙体$0$A-WALL-S"),
        (_Line((0, 200), (10000, 200)), "B1_核心筒$0$A-WALL-S"),
    ]

    walls = extract_precise_walls(
        kept, 0, 0, 0, 0, 0, wall_groups=("A",))

    assert len(walls) == 2
    assert all(wall["paired"] is False for wall in walls)
    assert {tuple(wall["source_layers"]) for wall in walls} == {
        ("B1_墙体$0$A-WALL-S",),
        ("B1_核心筒$0$A-WALL-S",),
    }


def test_parallel_angle_wrap_is_small():
    assert _angle_delta(179, 1) == 2


def test_pair_requires_projected_overlap_and_handles_reverse_direction():
    a = _line_record((0, 0), (10, 0))
    reversed_b = _line_record((10, 0.2), (0, 0.2))
    pair = _pair_geometry(a, reversed_b)
    assert pair is not None
    assert pair["start"] == [0, 0.1]
    assert pair["end"] == [10, 0.1]
    distant = _line_record((20, 0.2), (30, 0.2))
    assert _pair_geometry(a, distant) is None


def test_precise_paired_wall_keeps_both_exact_source_segment_refs():
    document = ezdxf.new()
    first = document.modelspace().add_line(
        (0, 0), (10000, 0), dxfattribs={"layer": "A-WALL"})
    second = document.modelspace().add_line(
        (10000, 200), (0, 200), dxfattribs={"layer": "A-WALL"})
    records = [
        (first, "A-WALL", _complete_provenance("FACE-A")),
        (second, "A-WALL", _complete_provenance("FACE-B")),
    ]
    provenance_before = [copy.deepcopy(item[2]) for item in records]

    walls = extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))
    legacy = extract_precise_walls(
        [(first, "A-WALL"), (second, "A-WALL")],
        0, 0, 0, 0, 0, wall_groups=("A",))

    assert len(walls) == len(legacy) == 1
    geometric_keys = ("start", "end", "thickness", "paired", "wall_group")
    assert {key: walls[0][key] for key in geometric_keys} == {
        key: legacy[0][key] for key in geometric_keys}
    assert walls[0]["geometry_source"] == "DXF_VECTOR"
    assert walls[0]["source_segment_ids"] == [
        "segment_FACE-A_0", "segment_FACE-B_0"]
    refs = walls[0]["source_segment_refs"]
    assert [ref["source_occurrence_id"] for ref in refs] == [
        "source_FACE-A", "source_FACE-B"]
    assert [ref["placed_entity_id"] for ref in refs] == [
        "placed_FACE-A", "placed_FACE-B"]
    assert [ref["entity_handle"] for ref in refs] == ["FACE-A", "FACE-B"]
    assert [ref["source_record_index"] for ref in refs] == [0, 1]
    assert [ref["segment_index"] for ref in refs] == [0, 0]
    assert [ref["source_interval_m"] for ref in refs] == [
        [0.0, 10.0], [0.0, 10.0]]
    assert [ref["placement_path"] for ref in refs] == [
        item["placement_path"] for item in provenance_before]
    assert all(ref["identity_is_complete"] is True for ref in refs)
    assert all(ref["identity_limitations"] == [] for ref in refs)
    assert [item[2] for item in records] == provenance_before


def test_precise_single_walls_keep_their_polyline_segment_refs_in_order():
    entity = _closed_polyline(
        [(0, 0), (1000, 0), (1000, 1000)], close=False)
    provenance = _complete_provenance("OPEN-POLY", segment_count=2)

    walls = extract_precise_walls(
        [(entity, "A-WALL", provenance)],
        0, 0, 0, 0, 0, wall_groups=("A",))

    assert len(walls) == 2
    assert [wall["paired"] for wall in walls] == [False, False]
    assert [wall["source_segment_ids"] for wall in walls] == [
        ["segment_OPEN-POLY_0"], ["segment_OPEN-POLY_1"]]
    assert [wall["source_segment_refs"][0]["segment_index"]
            for wall in walls] == [0, 1]
    assert [wall["source_segment_refs"][0]["source_record_index"]
            for wall in walls] == [0, 0]
    assert [wall["source_segment_refs"][0]["source_interval_m"]
            for wall in walls] == [[0.0, 1.0], [0.0, 1.0]]


def test_reused_long_face_keeps_a_distinct_contribution_interval_per_wall():
    document = ezdxf.new()
    long_face = document.modelspace().add_line(
        (0, 0), (10000, 0), dxfattribs={"layer": "A-WALL"})
    first_short_face = document.modelspace().add_line(
        (0, 200), (4000, 200), dxfattribs={"layer": "A-WALL"})
    second_short_face = document.modelspace().add_line(
        (6000, 200), (10000, 200), dxfattribs={"layer": "A-WALL"})
    records = [
        (long_face, "A-WALL", _complete_provenance("LONG")),
        (first_short_face, "A-WALL", _complete_provenance("SHORT-A")),
        (second_short_face, "A-WALL", _complete_provenance("SHORT-B")),
    ]

    walls = extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    assert len(walls) == 2
    long_face_refs = [next(
        ref for ref in wall["source_segment_refs"]
        if ref["source_segment_id"] == "segment_LONG_0")
        for wall in walls]
    assert [ref["source_interval_m"] for ref in long_face_refs] == [
        [0.0, 4.0], [6.0, 10.0]]


def test_terminal_face_remainder_can_be_recovered_without_filling_inner_gap():
    document = ezdxf.new()
    long_face = document.modelspace().add_line(
        (0, 0), (10000, 0), dxfattribs={"layer": "A-WALL-S"})
    opposing_face = document.modelspace().add_line(
        (4000, 200), (10000, 200), dxfattribs={"layer": "A-WALL-S"})
    records = [
        (long_face, "A-WALL-S", _complete_provenance("LONG-EXT")),
        (opposing_face, "A-WALL-S", _complete_provenance("OPPOSING")),
    ]

    walls = extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",),
        recover_terminal_remainders=True)

    assert len(walls) == 2
    assert [(wall["start"], wall["end"], wall["paired"])
            for wall in walls] == [
                ([4.0, 0.1], [10.0, 0.1], True),
                ([0.0, 0.1], [4.0, 0.1], False),
            ]
    extension = walls[1]
    assert extension["geometry_source"] == (
        "DXF_VECTOR_TERMINAL_FACE_EXTENSION")
    assert extension["source_segment_refs"][0]["source_interval_m"] == [
        0.0, 4.0]


def test_terminal_face_remainder_merges_back_into_supported_run():
    walls = [
        {
            "start": [4.0, 0.1], "end": [10.0, 0.1], "thickness": 200,
            "paired": True, "source_layers": ["A-WALL-S"],
            "source_segment_ids": ["OPPOSING"],
        },
        {
            "start": [0.0, 0.1], "end": [4.0, 0.1], "thickness": 200,
            "paired": False, "source_layers": ["A-WALL-S"],
            "terminal_face_recovery": True,
            "source_segment_ids": ["LONG-EXT"],
        },
    ]

    restored, audit = restore_architectural_wall_continuity(
        walls, gap_m=0.35, merge_terminal_extensions=True)

    assert len(restored) == 1
    assert restored[0]["paired"] is True
    assert restored[0]["start"] == [0.0, 0.1]
    assert restored[0]["end"] == [10.0, 0.1]
    assert restored[0]["terminal_face_recovery"] is True
    assert audit["terminal_extension_input_count"] == 1
    assert audit["terminal_extension_merged_count"] == 1


def test_overlapping_fragments_on_each_face_create_one_traceable_wall():
    document = ezdxf.new()
    records = [
        (document.modelspace().add_line((0, 0), (1000, 0)),
         "A-WALL", _complete_provenance("FACE-A-LONG")),
        (document.modelspace().add_line((0, 0), (900, 0)),
         "A-WALL", _complete_provenance("FACE-A-SHORT")),
        (document.modelspace().add_line((0, 100), (1000, 100)),
         "A-WALL", _complete_provenance("FACE-B-LONG")),
        (document.modelspace().add_line((0, 100), (900, 100)),
         "A-WALL", _complete_provenance("FACE-B-SHORT")),
    ]

    walls = extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    assert len(walls) == 1
    assert walls[0]["start"] == [0.0, 0.05]
    assert walls[0]["end"] == [1.0, 0.05]
    refs = {
        item["entity_handle"]: item["source_interval_m"]
        for item in walls[0]["source_segment_refs"]
    }
    assert refs == {
        "FACE-A-LONG": [0.0, 1.0],
        "FACE-A-SHORT": [0.0, 0.9],
        "FACE-B-LONG": [0.0, 1.0],
        "FACE-B-SHORT": [0.0, 0.9],
    }


def _closed_polyline(points, *, layer="A-WALL", close=True,
                     linetype=None, bulge_index=None):
    doc = ezdxf.new()
    if layer not in doc.layers:
        doc.layers.add(layer)
    attributes = {"layer": layer}
    if linetype:
        attributes["linetype"] = linetype
    vertices = []
    for index, (x, y) in enumerate(points):
        vertices.append((x, y, 0.2 if index == bulge_index else 0.0))
    return doc.modelspace().add_lwpolyline(
        vertices, format="xyb", close=close, dxfattribs=attributes)


def _complete_provenance(handle, *, segment_count=1,
                         structural_occurrence_id="structural_MAIN"):
    return {
        "drawing_identity": "sha256:test-drawing",
        "source_entity_handle": handle,
        "source_occurrence_id": f"source_{handle}",
        "placed_entity_id": f"placed_{handle}",
        "structural_occurrence_id": structural_occurrence_id,
        "segment_ids": [f"segment_{handle}_{index}"
                        for index in range(segment_count)],
        "placement_path": [{
            "name": "B1",
            "block_name": "B1",
            "insert": [0, 0],
            "insert_handle": "INSERT-01",
            "array_index": None,
            "cumulative_transform": {
                "coordinate_system": "DXF_WCS",
                "affine_2d": [[1.0, 0.0, 0.0],
                              [0.0, 1.0, 0.0],
                              [0.0, 0.0, 1.0]],
            },
        }],
    }


def _wall_source_ref(source_id, interval=(0.0, 1.0)):
    return {
        "drawing_identity": "sha256:test-drawing",
        "source_occurrence_id": f"occurrence_{source_id}",
        "placed_entity_id": f"placed_{source_id}",
        "structural_occurrence_id": "structural_MAIN",
        "source_segment_id": source_id,
        "entity_handle": f"handle_{source_id}",
        "source_record_index": 0,
        "segment_index": 0,
        "source_interval_m": list(interval),
        "placement_path": copy.deepcopy(
            _complete_provenance("PATH")["placement_path"]),
        "identity_is_complete": True,
        "identity_limitations": [],
    }


def test_pair_geometry_keeps_the_existing_80mm_lower_bound():
    face = _line_record((0, 0), (10, 0))

    assert _pair_geometry(face, _line_record((0, 0.04), (10, 0.04))) is None
    assert _pair_geometry(face, _line_record((0, 0.08), (10, 0.08)))


def test_closed_wall_strip_is_one_occurrence_local_wall_with_child_evidence():
    entity = _closed_polyline([
        (0, 0), (10000, 0), (10000, 200), (0, 200),
    ])
    provenance = _complete_provenance("FACE-01", segment_count=4)
    records = [(entity, "B1墙体$0$A-WALL", provenance)]

    first = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))
    second = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))
    walls = extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    assert first["status"] == "PASS"
    assert first["approved_by_rule_count"] == 1
    assert first["needs_review_count"] == 0
    candidate = first["candidates"][0]
    assert candidate["profile_group_id"] == second["candidates"][0][
        "profile_group_id"]
    assert candidate["entity_handle"] == "FACE-01"
    assert candidate["provenance"]["source_entity_handle"] == "FACE-01"
    assert candidate["provenance"]["source_occurrence_id"] == (
        provenance["source_occurrence_id"])
    assert candidate["status"] == "APPROVED_BY_RULE"
    assert candidate["decision_reason"] == "strict_closed_wall_strip"
    assert candidate["reserved_edge_count"] == 4
    assert len(candidate["child_evidence"]) == 4
    assert len({item["segment_id"]
                for item in candidate["child_evidence"]}) == 4
    assert [item["source_segment_id"]
            for item in candidate["child_evidence"]] == (
                provenance["segment_ids"])
    assert {item["source_record_index"]
            for item in candidate["child_evidence"]} == {0}
    assert [item["edge_role"] for item in candidate["child_evidence"]].count(
        "wall_face") == 2
    assert [item["edge_role"] for item in candidate["child_evidence"]].count(
        "end_cap") == 2
    assert all(item["profile_group_id"] == candidate["profile_group_id"]
               for item in candidate["child_evidence"])
    assert candidate["linetype_evidence"]["effective_linetype"].upper() == (
        "CONTINUOUS")
    assert candidate["linetype_evidence"]["resolved"] is True
    assert candidate["identity_is_complete"] is True
    assert candidate["identity_limitations"] == []
    expected_wall = {
        "start": [0.0, 0.1], "end": [10.0, 0.1],
        "thickness": 200.0, "paired": True, "wall_group": "A",
        "geometry_source": "DXF_CLOSED_WALL_STRIP",
        "source_candidate_id": candidate["candidate_id"],
        "profile_occurrence_id": candidate["occurrence_id"],
        "drawing_identity": provenance["drawing_identity"],
        "source_occurrence_id": provenance["source_occurrence_id"],
        "placed_entity_id": provenance["placed_entity_id"],
        "structural_occurrence_id": provenance["structural_occurrence_id"],
        "source_segment_ids": provenance["segment_ids"],
    }
    assert len(walls) == 1
    assert {key: walls[0][key] for key in expected_wall} == expected_wall
    closed_refs = walls[0]["source_segment_refs"]
    assert len(closed_refs) == 4
    assert [item["edge_role"] for item in closed_refs].count("wall_face") == 2
    assert [item["edge_role"] for item in closed_refs].count("end_cap") == 2
    assert [item["source_segment_id"] for item in closed_refs] == (
        provenance["segment_ids"])
    assert all(item["placement_path"] == provenance["placement_path"]
               for item in closed_refs)
    assert all(item["identity_is_complete"] is True for item in closed_refs)


def test_closed_wall_strip_audit_fails_closed_for_incomplete_occurrence_identity():
    entity = _closed_polyline([
        (0, 0), (10000, 0), (10000, 200), (0, 200),
    ])

    audit = audit_closed_wall_strips(
        [(entity, "A-WALL", {"source_entity_handle": "FACE-01"})],
        0, 0, 0, 0, 0, wall_groups=("A",))

    candidate = audit["candidates"][0]
    assert candidate["status"] == "APPROVED_BY_RULE"
    assert candidate["identity_is_complete"] is False
    assert candidate["identity_limitations"] == [
        "drawing_identity_missing", "source_occurrence_id_missing",
        "placed_entity_id_missing", "structural_occurrence_id_missing",
        "placement_path_missing",
        "source_segment_id_missing"]
    assert audit["identity_complete_count"] == 0
    assert audit["identity_incomplete_count"] == 1
    assert audit["status"] == "REVIEW"


def test_40mm_closed_strips_are_reserved_without_cross_entity_false_pairing():
    first = _closed_polyline([
        (0, 0), (10000, 0), (10000, 40), (0, 40),
    ])
    second = _closed_polyline([
        (0, 90), (10000, 90), (10000, 130), (0, 130),
    ])
    records = [
        (first, "A-WALL", {"source_entity_handle": "THIN-1"}),
        (second, "A-WALL", {"source_entity_handle": "THIN-2"}),
    ]

    audit = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))
    walls = extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    assert audit["status"] == "REVIEW"
    assert audit["approved_by_rule_count"] == 0
    assert audit["needs_review_count"] == 2
    assert audit["reserved_occurrence_count"] == 2
    assert all(candidate["status"] == "NEEDS_REVIEW"
               for candidate in audit["candidates"])
    assert all("thickness_outside_80_600mm" in candidate["blockers"]
               for candidate in audit["candidates"])
    # Loose pairing would incorrectly see the 90 mm gap between occurrences.
    assert walls == []


def test_dashed_closed_wall_strip_is_review_only_and_fully_reserved():
    entity = _closed_polyline(
        [(0, 0), (10000, 0), (10000, 200), (0, 200)],
        linetype="DASHED")
    records = [(entity, "A-WALL", {"source_entity_handle": "DASH-1"})]

    audit = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    candidate = audit["candidates"][0]
    assert candidate["status"] == "NEEDS_REVIEW"
    assert candidate["pattern_conflicts"] == ["DASH"]
    assert "dash_hidden_pattern_conflict" in candidate["blockers"]
    assert candidate["reserved_edge_count"] == 4
    assert extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",)) == []


def test_short_closed_strip_is_review_only_instead_of_approved_then_filtered():
    entity = _closed_polyline([
        (0, 0), (400, 0), (400, 100), (0, 100),
    ])
    records = [(entity, "A-WALL", {"source_entity_handle": "SHORT-1"})]

    audit = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    candidate = audit["candidates"][0]
    assert candidate["status"] == "NEEDS_REVIEW"
    assert "centerline_below_modeling_min_length" in candidate["blockers"]
    assert extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",)) == []


def test_closed_wall_strip_strictly_rejects_non_wall_or_unsafe_outlines():
    cases = [
        (_closed_polyline([(0, 0), (10000, 0), (10000, 200), (0, 200)],
                          layer="A-DOOR"), "A-DOOR"),
        (_closed_polyline([(0, 0), (10000, 0), (10000, 200), (0, 200)],
                          close=False), "A-WALL"),
        (_closed_polyline([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]),
         "A-WALL"),
        (_closed_polyline([(0, 0), (3000, 0), (1000, 100), (1500, 50)]),
         "A-WALL"),
        (_closed_polyline([(0, 0), (3000, 0), (0, 100), (3000, 100)]),
         "A-WALL"),
        (_closed_polyline([(0, 0), (10000, 0), (10000, 200), (0, 200)],
                          bulge_index=0), "A-WALL"),
    ]

    for entity, layer in cases:
        audit = audit_closed_wall_strips(
            [(entity, layer, {})], 0, 0, 0, 0, 0, wall_groups=("A",))
        assert audit["candidate_count"] == 0
        assert audit["reserved_occurrence_count"] == 0


def test_topology_requires_both_wall_ends_connected():
    walls = [
        {"id": "a", "start": [0, 0], "end": [10, 0]},
        {"id": "b", "start": [0, 0], "end": [0, 10]},
    ]
    metrics = topology_metrics(walls, tolerance_m=0.01)
    assert metrics["endpoint_coverage"] == 0.5
    assert metrics["fully_connected_rate"] == 0.0


def test_closed_rectangle_has_full_topology():
    walls = [
        {"start": [0, 0], "end": [10, 0]},
        {"start": [10, 0], "end": [10, 10]},
        {"start": [10, 10], "end": [0, 10]},
        {"start": [0, 10], "end": [0, 0]},
    ]
    metrics = topology_metrics(walls, tolerance_m=0.01)
    assert metrics["endpoint_coverage"] == 1.0
    assert metrics["fully_connected_rate"] == 1.0


def test_architectural_topology_accepts_structural_wall_and_column_support():
    architecture = [
        {"id": "a", "start": [0, 0], "end": [10, 0], "thickness": 200},
    ]
    structure = [
        {"start": [0, -5], "end": [0, 5], "thickness": 300},
    ]
    columns = [{"center": [10, 0], "size": [0.6, 0.6]}]

    metrics = topology_with_support(
        architecture, structure, columns, tolerance_m=0.01, column_margin_m=0.01)

    assert metrics["endpoint_coverage"] == 1.0
    assert metrics["fully_connected_rate"] == 1.0


def test_collinear_door_gap_is_topology_bridge_not_wall_element():
    walls = [
        {"start": [0, 0], "end": [4, 0], "thickness": 200},
        {"start": [5, 0], "end": [10, 0], "thickness": 200},
        {"start": [10, 0], "end": [10, 8], "thickness": 200},
        {"start": [10, 8], "end": [0, 8], "thickness": 200},
        {"start": [0, 8], "end": [0, 0], "thickness": 200},
    ]

    bridges = infer_opening_bridges(walls)
    metrics = topology_with_support(walls, opening_bridges=bridges,
                                    tolerance_m=0.01)

    assert len(bridges) == 1
    assert bridges[0]["kind"] == "inferred_opening"
    assert metrics["fully_connected_rate"] == 1.0
    assert metrics["room_polygon_count"] == 1
    cv_audit = cv_room_enclosure_audit(walls, bridges, resolution_m=0.1)
    assert cv_audit["status"] == "OK"
    assert cv_audit["enclosed_region_count"] == 1


def test_wall_source_region_filter_drops_translated_copy():
    records = [
        (_Line((0, 0), (10000, 0)), "A-WALL", {"copy": "main"}),
        (_Line((50000, 50000), (60000, 50000)), "A-WALL", {"copy": "translated"}),
    ]

    kept, dropped = filter_wall_records_to_region(
        records, 0, 0, 0, 0, 0, (0, -1, 20, 1), margin_m=0)

    assert [item[2]["copy"] for item in kept] == ["main"]
    assert [item[2]["copy"] for item in dropped] == ["translated"]


def test_wall_source_coverage_reports_exact_unmapped_faces():
    records = [
        (_Line((0, 0), (10000, 0)), "A-WALL", {"source_entity_handle": "A"}),
        (_Line((0, 200), (10000, 200)), "A-WALL", {"source_entity_handle": "B"}),
        (_Line((0, 2000), (10000, 2000)), "A-WALL", {"source_entity_handle": "C"}),
    ]
    walls = [{
        "start": [0.0, 0.1],
        "end": [10.0, 0.1],
        "thickness": 200.0,
        "paired": True,
    }]

    coverage = wall_source_coverage(records, walls, 0, 0, 0, 0, 0)

    assert coverage["source_segment_count"] == 3
    assert coverage["fully_mapped_source_segment_count"] == 2
    assert coverage["uncovered_source_segment_count"] == 1
    assert coverage["exact_face_length_coverage"] == 0.6667
    candidate = coverage["uncovered_source_segments"][0]
    assert candidate["entity_handle"] == "C"
    assert candidate["candidate_id"].startswith("semantic_face_")
    assert candidate["status"] == "NEEDS_REVIEW"
    assert candidate["decision_reason"] == "semantic_wall_face_unmapped"
    assert candidate["unmapped_ranges"] == [{
        "start": [0.0, 2.0],
        "end": [10.0, 2.0],
        "length_m": 10.0,
        "source_interval_m": [0.0, 10.0],
        "opening_bridge_supported": False,
    }]


def test_wall_source_coverage_retains_short_semantic_faces_for_review():
    records = [
        (_Line((0, 0), (300, 0)), "A-PART-S",
         {"source_entity_handle": "SHORT"}),
    ]

    coverage = wall_source_coverage(records, [], 0, 0, 0, 0, 0)

    assert coverage["audit_min_length_m"] == 0.01
    assert coverage["modeling_min_length_m"] == 0.5
    assert coverage["raw_source_segment_count"] == 1
    assert coverage["source_segment_count"] == 1
    assert coverage["short_source_segment_count"] == 1
    assert coverage["ignored_degenerate_source_segment_count"] == 0
    assert coverage["uncovered_source_segment_count"] == 1
    assert coverage["uncovered_source_segments"][0]["entity_handle"] == "SHORT"
    assert coverage["uncovered_source_segments"][0]["length_m"] == 0.3


def test_inferred_opening_bridge_is_diagnostic_not_wall_source_semantics():
    records = [
        (_Line((0, 0), (10000, 0)), "A-WALL", {"source_entity_handle": "A"}),
    ]
    walls = [
        {"start": [0.0002, 0.1], "end": [4.0, 0.1],
         "thickness": 200.0, "paired": True},
        {"start": [6.0, 0.1], "end": [10.0, 0.1],
         "thickness": 200.0, "paired": True},
    ]
    bridges = [{"start": [4.0, 0.1], "end": [6.0, 0.1]}]

    coverage = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0, opening_bridges=bridges)

    candidate = coverage["partially_mapped_source_segments"][0]
    assert candidate["mapped_ratio"] == 0.8
    assert candidate["opening_bridge_supported"] is True
    assert candidate["opening_bridge_semantic_authority"] is False
    assert candidate["decision_reason"] == (
        "semantic_wall_face_partially_mapped")
    unit = coverage["review_units"][0]
    assert unit["review_bucket"] == "POSSIBLE_OMISSION"
    assert unit["opening_bridge_supported"] is True
    assert coverage["inferred_opening_bridge_rule"] == {
        "classification": "TOPOLOGY_EVIDENCE_ONLY",
        "changes_wall_source_review_bucket": False,
        "suppresses_wall_pair_or_proposal_analysis": False,
        "auto_action": "NONE",
    }


def test_closed_profile_coverage_excludes_caps_and_links_approved_wall_exactly():
    entity = _closed_polyline([
        (0, 0), (10000, 0), (10000, 200), (0, 200),
    ])
    provenance = _complete_provenance("PROFILE-200", segment_count=4)
    records = [(entity, "A-WALL", provenance)]
    audit = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))
    walls = extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    coverage = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0,
        closed_profile_audit=audit)

    profile_id = audit["candidates"][0]["candidate_id"]
    association = coverage["closed_profile_association"]
    assert association["status"] == "COMPLETE"
    assert association["associated_source_segment_count"] == 4
    assert association["missing_count"] == 0
    assert association["duplicate_count"] == 0
    assert association["approved_profile_count"] == 1
    assert association["approved_model_wall_count"] == 1
    assert association["approved_wall_one_to_one_count"] == 1
    assert coverage["closed_wall_profile_audit"]["association"] == association
    assert coverage["source_face_length_m"] == 20.4
    assert coverage["exact_face_length_coverage"] == 0.9804
    assert coverage["gate_source_face_length_m"] == 20.0
    assert coverage["gate_exact_face_length_coverage"] == 1.0
    assert coverage["raw_source_review_count"] == 2
    assert coverage["raw_needs_review_source_segment_count"] == 0
    assert coverage["resolved_by_profile_source_segment_count"] == 2
    assert coverage["logical_review_unit_count"] == 0
    caps = coverage["uncovered_source_segments"]
    assert {item["parent_profile_id"] for item in caps} == {profile_id}
    assert {item["edge_role"] for item in caps} == {"end_cap"}
    assert all(item["rollup_counted"] is False for item in caps)
    assert all(item["status"] == "RESOLVED_BY_PROFILE" for item in caps)
    assert all(item["decision_reason"] ==
               "approved_closed_wall_strip_end_cap" for item in caps)


def test_exact_rejected_door_leaf_profile_is_retained_but_excluded_from_gate():
    entity = _closed_polyline([
        (0, 0), (762.5, 0), (762.5, 40), (0, 40),
    ])
    provenance = _complete_provenance("DOOR-LEAF", segment_count=4)
    records = [(entity, "A-PART-S", provenance)]
    audit = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))
    profile = audit["candidates"][0]
    profile.update({
        "status": "REJECTED_BY_RULE",
        "decision_reason": "exact_door_leaf_swing_topology",
        "review_bucket": "EXACT_DOOR_LEAF_SWING",
        "auto_action": "NONE",
    })
    audit.update({
        "status": "PASS",
        "needs_review_count": 0,
        "rejected_by_rule_count": 1,
    })

    coverage = wall_source_coverage(
        records, [], 0, 0, 0, 0, 0, closed_profile_audit=audit)

    retained = coverage["uncovered_source_segments"]
    assert len(retained) == 4
    assert coverage["raw_source_review_count"] == 4
    assert coverage["raw_needs_review_source_segment_count"] == 0
    assert coverage["rejected_by_rule_source_segment_count"] == 4
    assert coverage["logical_review_unit_count"] == 0
    assert coverage["gate_source_face_length_m"] == 0.0
    assert all(item["status"] == "REJECTED_BY_RULE" for item in retained)
    assert all(item["decision_reason"] ==
               "exact_door_leaf_swing_topology" for item in retained)
    assert all(item["rollup_counted"] is False for item in retained)


def test_rejected_non_wall_source_cannot_support_dangling_endpoint():
    walls = [{
        "id": "wall_1", "start": [0.0, 0.0], "end": [2.0, 0.0],
        "thickness": 100, "paired": True,
    }]
    coverage = {"uncovered_source_segments": [{
        "status": "REJECTED_BY_RULE",
        "candidate_id": "semantic_door_leaf", "entity_handle": "LEAF",
        "layer": "A-PART-S", "unmapped_ranges": [{
            "start": [0.0, -0.1], "end": [0.0, 0.1],
            "length_m": 0.2, "opening_bridge_supported": True,
        }],
    }]}

    review = classify_dangling_endpoints(
        [{"id": "wall_1", "ends": [False, True]}], walls,
        source_coverage=coverage)

    start = next(item for item in review["candidates"]
                 if item["endpoint"] == "start")
    assert start.get("source_match") is None


def test_review_closed_profile_keeps_four_children_but_rolls_up_once():
    entity = _closed_polyline([
        (0, 0), (10000, 0), (10000, 40), (0, 40),
    ])
    records = [(entity, "A-WALL", _complete_provenance(
        "PROFILE-040", segment_count=4))]
    audit = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    coverage = wall_source_coverage(
        records, [], 0, 0, 0, 0, 0,
        closed_profile_audit=audit)

    candidates = coverage["uncovered_source_segments"]
    assert audit["needs_review_count"] == 1
    assert coverage["closed_profile_association"]["status"] == "COMPLETE"
    assert coverage["closed_profile_association"][
        "approved_profile_count"] == 0
    assert len(candidates) == 4
    assert coverage["raw_source_review_count"] == 4
    assert coverage["logical_review_unit_count"] == 1
    assert sum(item["rollup_counted"] for item in candidates) == 1
    assert all(item["parent_profile_id"] for item in candidates)
    assert {item["edge_role"] for item in candidates} == {
        "wall_face", "end_cap"}
    assert coverage["gate_source_face_length_m"] == 20.0
    assert coverage["gate_exact_face_length_coverage"] == 0.0


def test_closed_profile_exact_association_reports_missing_and_duplicate_errors():
    entity = _closed_polyline([
        (0, 0), (10000, 0), (10000, 200), (0, 200),
    ])
    records = [(entity, "A-WALL", _complete_provenance(
        "PROFILE-ERROR", segment_count=4))]
    audit = audit_closed_wall_strips(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))
    walls = extract_precise_walls(
        records, 0, 0, 0, 0, 0, wall_groups=("A",))

    missing_audit = copy.deepcopy(audit)
    missing_audit["candidates"][0]["child_evidence"][0][
        "segment_index"] = 99
    missing = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0,
        closed_profile_audit=missing_audit)["closed_profile_association"]
    assert missing["status"] == "ERROR"
    assert missing["missing_count"] == 1
    assert missing["missing"][0]["reason"] == "source_segment_not_found"

    duplicate_audit = copy.deepcopy(audit)
    duplicate_audit["candidates"][0]["child_evidence"].append(
        copy.deepcopy(duplicate_audit["candidates"][0]["child_evidence"][0]))
    duplicate = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0,
        closed_profile_audit=duplicate_audit)["closed_profile_association"]
    assert duplicate["status"] == "ERROR"
    assert duplicate["duplicate_count"] == 1
    assert duplicate["duplicate"][0]["association_count"] == 2

    missing_wall = wall_source_coverage(
        records, [], 0, 0, 0, 0, 0,
        closed_profile_audit=audit)["closed_profile_association"]
    assert missing_wall["status"] == "ERROR"
    assert missing_wall["approved_wall_missing_count"] == 1
    assert missing_wall["approved_wall_one_to_one_count"] == 0

    duplicate_wall = wall_source_coverage(
        records, walls + copy.deepcopy(walls), 0, 0, 0, 0, 0,
        closed_profile_audit=audit)["closed_profile_association"]
    assert duplicate_wall["status"] == "ERROR"
    assert duplicate_wall["approved_wall_duplicate_count"] == 1
    assert duplicate_wall["approved_model_wall_count"] == 2


def test_wall_source_coverage_retains_more_than_200_review_candidates():
    records = [
        (_Line((index * 1000, 0), (index * 1000 + 300, 0)),
         "A-PART-S", {"source_entity_handle": f"SHORT-{index}"})
        for index in range(205)
    ]

    coverage = wall_source_coverage(records, [], 0, 0, 0, 0, 0)

    assert coverage["raw_source_review_count"] == 205
    assert coverage["logical_review_unit_count"] == 205
    assert len(coverage["uncovered_source_segments"]) == 205
    assert len({item["candidate_id"]
                for item in coverage["uncovered_source_segments"]}) == 205


def _strict_end_cap_records(*, top_angle_deg=0.0,
                            support_occurrence="structural_MAIN",
                            first_support_end_x=-500.0,
                            support_length_mm=500.0,
                            opposite_support_direction=False):
    top_dx = math.cos(math.radians(top_angle_deg)) * support_length_mm
    top_dy = math.sin(math.radians(top_angle_deg)) * support_length_mm
    top_start, top_end = ((0, 100), (top_dx, 100 + top_dy)) if (
        opposite_support_direction) else (
            (-top_dx, 100 - top_dy), (0, 100))
    return [
        (_Line((0, 0), (0, 100)), "A-WALL",
         _complete_provenance("CAP")),
        (_Line((first_support_end_x, 0),
               (first_support_end_x + support_length_mm, 0)), "A-WALL",
         _complete_provenance("SUPPORT-START")),
        (_Line(top_start, top_end), "A-PART-S",
         _complete_provenance(
             "SUPPORT-END", structural_occurrence_id=support_occurrence)),
    ]


def _empty_closed_profile_audit():
    return {"status": "PASS", "candidate_count": 0,
            "approved_by_rule_count": 0, "needs_review_count": 0,
            "identity_complete_count": 0, "identity_incomplete_count": 0,
            "reserved_occurrence_count": 0, "candidates": []}


def test_strict_topology_end_cap_is_evidence_resolved_without_geometry():
    records = _strict_end_cap_records()
    walls = []

    coverage = wall_source_coverage(
        records, walls, 0, 0, 0, 0, 0,
        closed_profile_audit=_empty_closed_profile_audit())

    candidate = next(item for item in coverage["uncovered_source_segments"]
                     if item["entity_handle"] == "CAP")
    assert candidate["status"] == "NEEDS_REVIEW"
    assert candidate["prior_decision_reason"] == (
        "semantic_wall_face_unmapped")
    assert candidate["decision_reason"] == "strict_topology_end_cap"
    assert candidate["review_bucket"] == "STRICT_TOPOLOGY_END_CAP"
    assert candidate["auto_action"] == "NONE"
    assert candidate["rollup_counted"] is True
    assert [item["entity_handle"] for item in
            candidate["support_evidence"]] == [
                "SUPPORT-START", "SUPPORT-END"]
    assert all(item["geometry_source"] == "DXF_VECTOR"
               for item in candidate["support_evidence"])
    assert all(item["endpoint_distance_m"] == 0.0
               for item in candidate["support_evidence"])
    assert candidate["strict_topology_evidence"]["support_count"] == 2
    assert candidate["strict_topology_evidence"][
        "same_side_extension_dot"] == 1.0
    assert coverage["strict_topology_end_cap_count"] == 1
    unit = next(item for item in coverage["review_units"]
                if item["entity_handle"] == "CAP")
    assert unit["status"] == "RESOLVED_STRICT_TOPOLOGY_END_CAP"
    assert unit["resolution_type"] == "STRICT_TOPOLOGY_END_CAP"
    assert unit["resolution_evidence"] == {
        "classification": "EVIDENCE_RESOLUTION_ONLY",
        "support_count": 2,
        "model_geometry_created": False,
    }
    assert unit["model_geometry_created"] is False
    assert coverage["resolved_strict_topology_end_cap_count"] == 1
    assert coverage[
        "resolved_strict_topology_end_cap_face_length_m"] == 0.1
    # A proven transverse cap is not a wall face and cannot become a model
    # centerline. The source geometry and the two supporting faces are intact.
    assert walls == []
    assert coverage["raw_source_review_count"] == 3
    assert coverage["logical_review_unit_count"] == 2
    assert coverage["gate_source_face_length_m"] == 1.0
    assert coverage["gate_exact_face_length_coverage"] == 0.0


def test_strict_topology_end_cap_rejects_interior_angle_and_identity_evidence():
    incomplete_identity = _strict_end_cap_records()
    incomplete_identity[0] = (
        incomplete_identity[0][0], incomplete_identity[0][1],
        {"source_entity_handle": "CAP"})
    ambiguous_support = _strict_end_cap_records()
    ambiguous_support.append((
        _Line((-500, 100), (0, 100)), "A-PART-S",
        _complete_provenance("SUPPORT-END-DUPLICATE")))
    cases = [
        # Candidate endpoint hits the middle, not a real support endpoint.
        _strict_end_cap_records(first_support_end_x=-250.0),
        # Supports are outside the explicit two-degree parallel tolerance.
        _strict_end_cap_records(top_angle_deg=3.0),
        # Geometrically identical evidence from another placement is unsafe.
        _strict_end_cap_records(support_occurrence="OTHER"),
        # A support shorter than max(150 mm, 1.5 * candidate length).
        _strict_end_cap_records(support_length_mm=140.0,
                                first_support_end_x=-140.0),
        # Opposite support extensions are a Z-shaped junction, not a cap.
        _strict_end_cap_records(opposite_support_direction=True),
        # Missing placed identity cannot be rescued by exact coordinates.
        incomplete_identity,
        # Two equally valid support pairs are ambiguous, not exact proof.
        ambiguous_support,
    ]

    for records in cases:
        coverage = wall_source_coverage(
            records, [], 0, 0, 0, 0, 0,
            closed_profile_audit=_empty_closed_profile_audit())
        candidate = next(
            item for item in coverage["uncovered_source_segments"]
            if item["entity_handle"] == "CAP")
        assert candidate["decision_reason"] == (
            "semantic_wall_face_unmapped")
        assert "review_bucket" not in candidate
        assert "auto_action" not in candidate
        assert coverage["strict_topology_end_cap_count"] == 0


def test_strict_topology_end_cap_excludes_open_u_same_entity_and_profiles():
    open_u = _closed_polyline([
        (0, 0), (400, 0), (400, 100), (0, 100),
    ], close=False)
    open_records = [(open_u, "A-WALL", _complete_provenance(
        "OPEN-U", segment_count=3))]
    open_coverage = wall_source_coverage(
        open_records, [], 0, 0, 0, 0, 0,
        closed_profile_audit=_empty_closed_profile_audit())

    middle = next(
        item for item in open_coverage["uncovered_source_segments"]
        if item["segment_index"] == 1)
    assert middle["length_m"] == 0.1
    assert middle["decision_reason"] == "semantic_wall_face_unmapped"
    assert open_coverage["strict_topology_end_cap_count"] == 0

    profile = _closed_polyline([
        (0, 0), (10000, 0), (10000, 100), (0, 100),
    ], linetype="DASHED")
    profile_records = [(profile, "A-WALL", _complete_provenance(
        "CLOSED-PROFILE", segment_count=4))]
    profile_audit = audit_closed_wall_strips(
        profile_records, 0, 0, 0, 0, 0, wall_groups=("A",))
    profile_coverage = wall_source_coverage(
        profile_records, [], 0, 0, 0, 0, 0,
        closed_profile_audit=profile_audit)

    caps = [item for item in profile_coverage["uncovered_source_segments"]
            if item["edge_role"] == "end_cap"]
    assert len(caps) == 2
    assert all(item["length_m"] == 0.1 for item in caps)
    assert all("review_bucket" not in item for item in caps)
    assert profile_coverage["strict_topology_end_cap_count"] == 0


def test_dangling_endpoint_review_links_source_gap_without_moving_wall():
    walls = [{
        "id": "wall_1", "start": [0.0, 0.0], "end": [2.0, 0.0],
        "thickness": 100, "paired": True,
    }]
    support = [{
        "id": "support_1", "start": [2.5, -1.0], "end": [2.5, 1.0],
        "thickness": 200,
    }]
    coverage = {"uncovered_source_segments": [{
        "candidate_id": "semantic_face_1", "entity_handle": "FACE1",
        "layer": "A-PART-S", "unmapped_ranges": [{
            "start": [0.0, -0.1], "end": [0.0, 0.1],
            "length_m": 0.2, "opening_bridge_supported": True,
        }],
    }]}

    review = classify_dangling_endpoints(
        [{"id": "wall_1", "ends": [False, False]}],
        walls, support, source_coverage=coverage)

    assert walls[0]["start"] == [0.0, 0.0]
    assert review["candidate_count"] == 2
    assert review["reason_counts"] == {
        "interior_junction": 1,
        "opening_topology_evidence_endpoint": 1,
    }
    opening = next(item for item in review["candidates"]
                   if item["endpoint"] == "start")
    assert opening["source_match"]["entity_handle"] == "FACE1"
    assert opening["geometry_source"] == "DXF_VECTOR"
    assert opening["door_vector_supported_endpoint"] is False
    assert opening["opening_bridge_topology_evidence"] is True
    assert opening["auto_action"] == "NONE"
    assert "explicit_door_vector_missing" in opening["blockers"]


def test_dangling_offset_mutual_nearest_endpoints_are_clustered_not_connected():
    walls = [
        {"id": "left", "start": [0.0, 0.0], "end": [1.0, 0.0]},
        {"id": "right", "start": [1.25, 0.12], "end": [2.25, 0.12]},
    ]

    review = classify_dangling_endpoints(
        [{"id": "left", "ends": [True, False]},
         {"id": "right", "ends": [False, True]}],
        walls, nearby_tolerance_m=0.5)

    assert walls[0]["end"] == [1.0, 0.0]
    assert walls[1]["start"] == [1.25, 0.12]
    assert review["mutual_nearest_cluster_count"] == 1
    cluster = review["mutual_nearest_clusters"][0]
    assert cluster["outward_facing"] is False
    assert cluster["lateral_offset_m"] == 0.12
    assert cluster["direct_connection_supported"] is False
    for candidate in review["candidates"]:
        assert candidate["auto_action"] == "NONE"
        assert math.isclose(math.hypot(*candidate["outward_tangent"]), 1.0)
        assert "mutual_nearest_pair_laterally_offset" in candidate["blockers"]


def test_dangling_endpoint_records_interior_junction_without_moving_wall():
    walls = [{
        "id": "partition", "start": [0.0, 0.0], "end": [2.0, 0.0],
    }]
    support = [{
        "id": "cross_wall", "start": [2.2, -1.0], "end": [2.2, 1.0],
    }]

    review = classify_dangling_endpoints(
        [{"id": "partition", "ends": [True, False]}], walls, support,
        nearby_tolerance_m=0.3)

    candidate = review["candidates"][0]
    assert candidate["endpoint"] == "end"
    assert candidate["decision_reason"] == "interior_junction"
    assert candidate["nearest_network"]["interior_junction"] is True
    assert candidate["nearest_network"]["junction_position"] == "interior"
    assert candidate["nearest_network"]["closest_point"] == [2.2, 0.0]
    assert candidate["auto_action"] == "NONE"
    assert walls[0]["end"] == [2.0, 0.0]


def test_opening_bridge_is_not_door_semantics_without_explicit_dxf_vector():
    walls = [{
        "id": "wall", "start": [0.0, 0.0], "end": [1.0, 0.0],
    }]
    bridges = [{
        "id": "inferred_gap", "start": [1.1, 0.0], "end": [2.0, 0.0],
    }]
    non_door_vectors = [{
        "entity_handle": "TEXT1", "layer": "A-TEXT",
        "geometry_source": "DXF_VECTOR", "start": [1.0, 0.0],
        "end": [1.0, 0.5],
    }, {
        "entity_handle": "DOOR1", "layer": "A-DOOR",
        "geometry_source": "CV_RASTER", "start": [1.0, 0.0],
        "end": [1.0, 0.5],
    }]

    review = classify_dangling_endpoints(
        [{"id": "wall", "ends": [True, False]}], walls,
        opening_bridges=bridges, door_vectors=non_door_vectors,
        nearby_tolerance_m=0.3)

    candidate = review["candidates"][0]
    assert candidate["decision_reason"] == "opening_topology_evidence_endpoint"
    assert candidate["opening_bridge_topology_evidence"] is True
    assert candidate["door_vector_supported_endpoint"] is False
    assert candidate["nearest_network"]["topology_evidence_only"] is True
    assert "opening_bridge_is_topology_evidence_only" in candidate["blockers"]


def test_only_explicit_door_layer_dxf_vector_supports_endpoint():
    walls = [{
        "id": "wall", "start": [0.0, 0.0], "end": [1.0, 0.0],
    }]
    doors = [{
        "entity_handle": "DOOR2", "layer": "B1$0$A-DOOR",
        "geometry_source": "DXF_VECTOR", "start": [1.1, -0.2],
        "end": [1.1, 0.2],
    }]

    review = classify_dangling_endpoints(
        [{"id": "wall", "ends": [True, False]}], walls,
        door_vectors=doors, source_tolerance_m=0.2)

    candidate = review["candidates"][0]
    assert candidate["decision_reason"] == "door_vector_supported_endpoint"
    assert candidate["door_vector_supported_endpoint"] is True
    assert candidate["door_vector_match"]["entity_handle"] == "DOOR2"
    assert candidate["door_vector_match"]["geometry_source"] == "DXF_VECTOR"
    assert candidate["auto_action"] == "NONE"


def test_oblique_structural_wall_is_never_snap_eligible():
    walls = [{
        "id": "partition", "start": [0.0, 0.0], "end": [1.0, 0.0],
    }]
    support = [{
        "id": "diagonal", "start": [0.9, -0.2], "end": [1.5, 0.4],
    }]

    review = classify_dangling_endpoints(
        [{"id": "partition", "ends": [True, False]}], walls, support,
        nearby_tolerance_m=0.3)

    candidate = review["candidates"][0]
    assert candidate["nearest_network"]["kind"] == "structural_wall"
    assert candidate["nearest_network"]["oblique_structural_wall"] is True
    assert candidate["nearest_network"]["snap_eligible"] is False
    assert candidate["auto_action"] == "NONE"
    assert "oblique_structural_wall_no_snap" in candidate["blockers"]
    assert walls[0]["end"] == [1.0, 0.0]


def test_unknown_vector_wall_requires_semantics_and_topology_before_promotion():
    records = [
        (_Line((0, 0), (10000, 0)), "A-临时墙线", {}),
        (_Line((0, 200), (10000, 200)), "A-临时墙线", {}),
        (_Line((0, 1000), (10000, 1000)), "A-FURT2", {}),
        (_Line((0, 1200), (10000, 1200)), "A-FURT2", {}),
    ]
    support = [
        {"start": [0.1, -2], "end": [0.1, 2], "thickness": 300},
        {"start": [9.9, -2], "end": [9.9, 2], "thickness": 300},
    ]

    audit = audit_vector_wall_candidates(records, 0, 0, 0, 0, 0, support, [])

    assert audit["pair_candidate_count"] == 1
    assert audit["promoted_count"] == 1
    assert audit["review_count"] == 0
    assert audit["promoted_walls"][0]["layer"] == "A-临时墙线"
    assert audit["rejected_layers"]["A-FURT2"]["reason"] == "known_non_wall_layer"


def test_merge_collinear_walls_unions_only_each_actual_run_sources():
    def wall(wall_id, start, end, references):
        return {
            "id": wall_id, "start": start, "end": end,
            "thickness": 300, "paired": True,
            "source_segment_refs": copy.deepcopy(references),
            "source_segment_ids": [
                item["source_segment_id"] for item in references],
        }

    ref_a = _wall_source_ref("A", (0.0, 1.0))
    ref_b = _wall_source_ref("B", (0.0, 0.4))
    ref_c = _wall_source_ref("C", (0.0, 0.9))
    ref_d = _wall_source_ref("D", (0.0, 0.68))
    walls = [
        wall("a", [0.0, 0.0], [1.0, 0.0], [ref_a, ref_a]),
        wall("b", [0.5, 0.0], [0.9, 0.0], [ref_b]),
        wall("c", [1.1, 0.0], [2.0, 0.0], [ref_c]),
        wall("d", [1.12, 0.0], [1.8, 0.0], [ref_d]),
    ]
    before = copy.deepcopy(walls)

    # offset > gap deliberately creates two actual runs inside one geometric
    # group; provenance must be collected per run, not per group.
    merged = merge_collinear_walls(walls, gap_m=0.05, offset_m=0.15)

    assert [(item["start"], item["end"]) for item in merged] == [
        ([0.0, 0.0], [1.0, 0.0]),
        ([1.1, 0.0], [2.0, 0.0]),
    ]
    assert [item["source_segment_ids"] for item in merged] == [
        ["A", "B"], ["C", "D"]]
    assert [item["derivation"][-1]["parent_source_segment_ids"]
            for item in merged] == [["A", "B"], ["C", "D"]]
    assert [item["derivation"][-1]["operation"]
            for item in merged] == [
                "merge_collinear_walls", "merge_collinear_walls"]
    assert walls == before


def test_merge_keeps_distinct_intervals_of_one_source_id():
    first_ref = _wall_source_ref("SHARED", (0.0, 1.0))
    second_ref = _wall_source_ref("SHARED", (1.0, 2.0))
    walls = [{
        "start": [0.0, 0.0], "end": [1.0, 0.0], "thickness": 300,
        "source_segment_refs": [first_ref],
    }, {
        "start": [1.05, 0.0], "end": [2.0, 0.0], "thickness": 300,
        "source_segment_refs": [second_ref],
    }]

    merged = merge_collinear_walls(walls)

    assert len(merged) == 1
    assert [item["source_interval_m"]
            for item in merged[0]["source_segment_refs"]] == [
                [0.0, 1.0], [1.0, 2.0]]


def test_merge_collinear_walls_bridges_configured_opening_gap():
    walls = [{
        "start": [0.0, 0.0], "end": [3.0, 0.0], "thickness": 200,
        "source_layers": ["B1_墙体$0$A-WALL-S"],
    }, {
        "start": [4.2, 0.01], "end": [8.0, 0.01], "thickness": 200,
        "source_layers": ["B1_墙体$0$A-WALL-S"],
    }]

    separated = merge_collinear_walls(walls, gap_m=1.0)
    restored = merge_collinear_walls(walls, gap_m=1.5)

    assert len(separated) == 2
    assert len(restored) == 1
    assert restored[0]["start"] == [0.0, 0.0]
    assert restored[0]["end"] == [8.0, 0.0]


def test_merge_collinear_walls_does_not_cross_source_layers():
    walls = [{
        "start": [0.0, 0.0], "end": [3.0, 0.0], "thickness": 200,
        "source_layers": ["B1_墙体$0$A-WALL-S"],
    }, {
        "start": [3.2, 0.0], "end": [8.0, 0.0], "thickness": 200,
        "source_layers": ["B1_核心筒$0$A-WALL-S"],
    }]

    assert len(merge_collinear_walls(walls, gap_m=2.2)) == 2


def test_heal_records_constraint_sources_without_mixing_material_refs():
    own_ref = _wall_source_ref("OWN", (0.0, 0.42))
    constraint_ref = _wall_source_ref("CONSTRAINT", (0.0, 4.0))
    walls = [{
        "id": "short", "start": [0.0, 2.0], "end": [0.42, 2.0],
        "thickness": 200, "source_segment_refs": [own_ref],
        "source_segment_ids": ["OWN"],
    }, {
        "id": "vertical", "start": [0.5, 0.0], "end": [0.5, 4.0],
        "thickness": 200, "source_segment_refs": [constraint_ref],
        "source_segment_ids": ["CONSTRAINT"],
    }]
    before = copy.deepcopy(walls)

    healed = heal_wall_junctions(walls)

    assert healed[0]["end"] == [0.5, 2.0]
    assert healed[0]["source_segment_refs"] == [own_ref]
    assert healed[0]["source_segment_ids"] == ["OWN"]
    adjustment = healed[0]["endpoint_adjustments"][-1]
    assert adjustment["constraint_wall_id"] == "vertical"
    assert adjustment["constraint_source_segment_ids"] == ["CONSTRAINT"]
    assert adjustment["constraint_source_refs"] == [constraint_ref]
    assert "CONSTRAINT" not in healed[0]["source_segment_ids"]
    assert walls == before


def test_orthogonalize_records_one_to_one_adjustment_and_inherits_refs():
    reference = _wall_source_ref("SKEW", (0.0, 10.0))
    walls = [{
        "id": "slightly_skewed", "start": [0.0, 0.0],
        "end": [10.0, 0.15], "source_segment_refs": [reference],
        "source_segment_ids": ["SKEW"],
    }]
    before = copy.deepcopy(walls)

    corrected = orthogonalize_walls(walls, angle_tolerance_deg=2.0)

    assert corrected[0]["source_segment_refs"] == [reference]
    assert corrected[0]["source_segment_refs"] is not walls[0][
        "source_segment_refs"]
    assert corrected[0]["geometry_adjustments"] == [{
        "operation": "orthogonalize_walls",
        "before": {"start": [0.0, 0.0], "end": [10.0, 0.15]},
        "after": {"start": [0.0, 0.075], "end": [10.0, 0.075]},
        "angle_before_deg": 0.859372,
    }]
    assert corrected[0]["derivation"][-1]["operation"] == (
        "orthogonalize_walls")
    assert walls == before


def test_orthogonalize_walls_only_corrects_small_axis_angle_errors():
    walls = [
        {"id": "slightly_skewed", "start": [0, 0], "end": [10, 0.15]},
        {"id": "intentional_diagonal", "start": [0, 0], "end": [10, 3]},
    ]

    corrected = orthogonalize_walls(walls, angle_tolerance_deg=2.0)

    assert corrected[0]["start"] == [0, 0.075]
    assert corrected[0]["end"] == [10, 0.075]
    assert corrected[1] == walls[1]


def test_heal_wall_junction_extends_without_rotating_short_wall():
    walls = [
        {"id": "short", "start": [0.0, 2.0], "end": [0.42, 2.0],
         "thickness": 0.2},
        {"id": "vertical", "start": [0.5, 0.0], "end": [0.5, 4.0],
         "thickness": 0.2},
    ]

    healed = heal_wall_junctions(walls)

    assert healed[0]["start"] == [0.0, 2.0]
    assert healed[0]["end"] == [0.5, 2.0]
    assert healed[1] == walls[1]


def test_heal_wall_junction_ignores_nearby_parallel_wall():
    walls = [
        {"start": [0.0, 0.0], "end": [1.0, 0.0], "thickness": 0.2},
        {"start": [1.1, 0.1], "end": [2.0, 0.1], "thickness": 0.2},
    ]

    assert heal_wall_junctions(walls) == walls


def test_heal_wall_junction_closes_corner_between_thick_wall_faces():
    walls = [
        {"start": [0.0, 0.0], "end": [2.75, 0.0], "thickness": 400},
        {"start": [3.0, 0.6], "end": [3.0, 4.0], "thickness": 500},
    ]

    healed = heal_wall_junctions(walls)

    assert healed[0]["end"] == [3.0, 0.0]
    assert healed[1]["start"] == [3.0, 0.0]


def test_heal_wall_junction_does_not_collapse_short_stub():
    walls = [
        {"id": "stub", "start": [0.0, 1.0], "end": [0.7, 1.0],
         "thickness": 300},
        {"id": "side", "start": [0.0, 0.0], "end": [0.0, 2.0],
         "thickness": 500},
    ]

    healed = heal_wall_junctions(walls)

    assert len(healed) == 2
    assert healed[0]["start"] == [0.0, 1.0]
    assert healed[0]["end"] == [0.7, 1.0]
