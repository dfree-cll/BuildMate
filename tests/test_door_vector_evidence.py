import ezdxf

from scripts.bim_pipeline import pipeline_std
from scripts.geometry_first_clean import walk_door_evidence
import scripts.pipeline_std_runner as std_runner


DRAWING_ID = "sha256:" + "d" * 64


def _door_plan():
    document = ezdxf.new()
    geometry = document.blocks.new("DOOR_GEOMETRY")
    geometry.add_line((0, 0), (900, 0),
                      dxfattribs={"layer": "SYMBOL-DETAIL"})
    door = document.blocks.new("DOOR_CONTAINER")
    door.add_blockref("DOOR_GEOMETRY", (100, 200))

    furniture = document.blocks.new("FURNITURE_DETAIL")
    for index in range(25):
        furniture.add_line((0, index * 10), (500, index * 10),
                           dxfattribs={"layer": "A-FURT"})

    plan = document.blocks.new("B1层平面图")
    plan.add_blockref(
        "DOOR_CONTAINER", (1000, 2000),
        dxfattribs={"layer": "A-DOOR"})
    plan.add_blockref(
        "FURNITURE_DETAIL", (5000, 6000),
        dxfattribs={"layer": "A-FURT"})
    root = document.modelspace().add_blockref(
        "B1层平面图", (10000, 20000))
    return document, root


def _s1():
    return {
        "prefix": "B1", "origin": [10000, 20000], "rot_deg": 0,
        "gcx": 0, "gcy": 0,
        "grid": {
            "x_axes": [{"coord": 0}, {"coord": 3}],
            "y_axes": [{"coord": 0}, {"coord": 3}],
        },
    }


def test_selective_door_traversal_keeps_semantics_and_full_provenance():
    _document, root = _door_plan()

    records = list(walk_door_evidence([root], drawing_id=DRAWING_ID))

    assert len(records) == 1
    entity, layer, provenance = records[0]
    assert layer == "A-DOOR"
    assert provenance["geometry_layer"] == "SYMBOL-DETAIL"
    assert provenance["door_semantic_layer"] == "A-DOOR"
    assert provenance["drawing_identity"] == DRAWING_ID
    assert provenance["source_entity_handle"]
    assert provenance["source_occurrence_id"]
    assert provenance["placed_entity_id"]
    assert len(provenance["placement_path"]) == 3
    assert all(item["insert_handle"]
               for item in provenance["placement_path"])
    assert (entity.dxf.start.x, entity.dxf.start.y) == (11100.0, 22200.0)
    assert (entity.dxf.end.x, entity.dxf.end.y) == (12000.0, 22200.0)


def test_step0_exposes_door_vectors_separately_from_wall_geometry():
    document, _root = _door_plan()

    cleaned = pipeline_std.step0_clean(document)

    assert cleaned["door_vector_source_status"] == "OK"
    assert len(cleaned["door_vector_source_records"]) == 1
    assert cleaned["wall_source_records"] == []
    assert all("DOOR" not in layer.upper().rsplit("$0$", 1)[-1]
               for _entity, layer, _provenance in
               cleaned["vector_source_records"])


def test_door_vector_conversion_uses_current_dxf_and_selected_region():
    _document, root = _door_plan()
    records = list(walk_door_evidence([root], drawing_id=DRAWING_ID))

    evidence = std_runner._extract_door_vector_evidence(
        records, _s1(), bounds=(0.0, 0.0, 3.0, 3.0))

    assert evidence["status"] == "OK"
    assert evidence["explicit_door_vector_count"] == 1
    assert evidence["identity_incomplete_count"] == 0
    vector = evidence["vectors"][0]
    assert vector["start"] == [1.1, 2.2]
    assert vector["end"] == [2.0, 2.2]
    assert vector["geometry_source"] == "DXF_VECTOR"
    assert vector["semantic_role"] == "EXPLICIT_DOOR_VECTOR"
    assert vector["auto_action"] == "NONE"
    assert vector["source_segment_id"] == records[0][2]["segment_ids"][0]
    assert vector["drawing_identity"] == DRAWING_ID

    excluded = std_runner._extract_door_vector_evidence(
        records, _s1(), bounds=(30.0, 30.0, 35.0, 35.0), margin_m=0.0)
    assert excluded["explicit_door_vector_count"] == 0
    assert excluded["excluded_out_of_region_segment_count"] == 1


def test_endpoint_match_is_joined_to_one_exact_placed_dxf_segment():
    _document, root = _door_plan()
    records = list(walk_door_evidence([root], drawing_id=DRAWING_ID))
    vector = std_runner._extract_door_vector_evidence(
        records, _s1(), bounds=(0.0, 0.0, 3.0, 3.0))["vectors"][0]
    review = {"candidates": [{
        "point": [1.1, 2.2],
        "blockers": ["human_review_required"],
        "door_vector_match": {
            "entity_handle": vector["entity_handle"],
            "layer": vector["layer"],
            "distance_m": 0.0,
            "closest_point": [1.1, 2.2],
        },
    }]}

    result = std_runner._associate_endpoint_door_vectors(
        review, [vector])

    match = result["candidates"][0]["door_vector_match"]
    assert result["door_vector_association"] == {
        "status": "COMPLETE", "exact_count": 1,
        "ambiguous_count": 0, "missing_count": 0,
    }
    assert match["association_status"] == "EXACT"
    assert match["door_vector_id"] == vector["door_vector_id"]
    assert match["source_segment_id"] == vector["source_segment_id"]
    assert match["placed_entity_id"] == vector["placed_entity_id"]


def test_precise_step4_passes_door_vectors_to_review_only_endpoint_audit(
        monkeypatch):
    _document, root = _door_plan()
    door_records = list(walk_door_evidence(
        [root], drawing_id=DRAWING_ID))
    profile_audit = {
        "status": "PASS", "candidate_count": 0,
        "approved_by_rule_count": 0, "needs_review_count": 0,
        "identity_complete_count": 0, "identity_incomplete_count": 0,
        "candidates": [],
    }
    wall = {
        "start": [0.0, 2.2], "end": [1.0, 2.2],
        "thickness": 200, "paired": True,
    }
    seen = {}

    monkeypatch.setattr(
        std_runner, "filter_wall_records_to_region",
        lambda records, *_args, **_kwargs: (list(records), []))
    monkeypatch.setattr(
        std_runner, "audit_closed_wall_strips",
        lambda *_args, **_kwargs: profile_audit)
    monkeypatch.setattr(
        std_runner, "extract_precise_walls",
        lambda *_args, **_kwargs: [dict(wall)])
    monkeypatch.setattr(
        std_runner, "audit_vector_wall_candidates",
        lambda *_args, **_kwargs: {"promoted_walls": [], "review_count": 0})
    monkeypatch.setattr(std_runner, "infer_opening_bridges", lambda _walls: [])
    monkeypatch.setattr(
        std_runner, "wall_source_coverage",
        lambda *_args, **_kwargs: {
            "closed_wall_profile_audit": profile_audit,
            "uncovered_source_segments": [],
            "partially_mapped_source_segments": [],
        })
    monkeypatch.setattr(
        std_runner, "topology_with_support",
        lambda *_args, **_kwargs: {
            "fully_connected_rate": 0.0, "endpoint_coverage": 0.5,
            "dangling": [{"id": "wall_0", "ends": [True, False]}],
            "room_polygon_count": 0, "room_polygon_area_m2": 0.0,
            "polygonize_status": "OK",
        })

    def endpoint_review(*_args, door_vectors=None, **_kwargs):
        seen["door_vectors"] = door_vectors
        return {"status": "REVIEW", "candidate_count": 1,
                "auto_action": "NONE"}

    monkeypatch.setattr(
        std_runner, "classify_dangling_endpoints", endpoint_review)
    monkeypatch.setattr(
        std_runner, "cv_room_enclosure_audit",
        lambda *_args, **_kwargs: {
            "status": "OK", "enclosed_region_count": 0})
    cleaned = {
        "wall_source_records": [(object(), "A-WALL", {})],
        "vector_source_records": [],
        "door_vector_source_records": door_records,
        "door_vector_source_status": "OK",
        "plan_selection": {"selected": "main"},
    }
    s2 = {
        "shear_walls": [{
            "id": "shear", "start": [0.0, 0.0], "end": [3.0, 0.0],
            "thickness": 200,
        }],
        "cols": [], "wall_occurrence_selection": {"valid": True},
    }

    result = std_runner.precise_step4(cleaned, _s1(), s2)

    assert len(seen["door_vectors"]) == 1
    assert seen["door_vectors"][0]["geometry_source"] == "DXF_VECTOR"
    assert result["door_vector_evidence"]["semantic_evidence_only"] is True
    assert result["endpoint_review"]["auto_action"] == "NONE"
    assert result["walls"][0]["start"] == wall["start"]
    assert result["walls"][0]["end"] == wall["end"]
