import copy
import json

from scripts import pipeline_std_runner as runner


SOURCE_SHA = "a" * 64
DRAWING_IDENTITY = f"sha256:{SOURCE_SHA}"
SELECTED_OCCURRENCE = "structural_selected"


def _placement_path():
    return [{
        "insert_handle": "INSERT-01",
        "block_name": "B1 wall-column plan",
        "array_index": None,
        "cumulative_transform": {
            "coordinate_system": "DXF_WCS",
            "affine_2d": [[1.0, 0.0, 0.0],
                          [0.0, 1.0, 0.0],
                          [0.0, 0.0, 1.0]],
        },
    }]


def _wall_ref(identifier, occurrence=SELECTED_OCCURRENCE,
              interval=(0.0, 1.0)):
    return {
        "drawing_identity": DRAWING_IDENTITY,
        "source_occurrence_id": f"source_{identifier}",
        "placed_entity_id": f"placed_{identifier}",
        "structural_occurrence_id": occurrence,
        "source_segment_id": f"segment_{identifier}",
        "entity_handle": f"HANDLE-{identifier}",
        "source_record_index": 1,
        "segment_index": 0,
        "source_interval_m": list(interval),
        "placement_path": _placement_path(),
        "identity_is_complete": True,
        "identity_limitations": [],
    }


def _wall(identifier, occurrence=SELECTED_OCCURRENCE, *, derivation=False):
    reference = _wall_ref(identifier, occurrence)
    wall = {
        "id": identifier,
        "start": [0.0, 0.0],
        "end": [1.0, 0.0],
        "thickness": 200,
        "source_segment_ids": [reference["source_segment_id"]],
        "source_segment_refs": [reference],
    }
    if derivation:
        wall["derivation"] = [{
            "operation": "merge_collinear_walls",
            "parent_source_segment_ids": [reference["source_segment_id"]],
        }]
    return wall


def _column():
    segment_ids = [f"column_segment_{index}" for index in range(4)]
    profile = {
        "drawing_identity": DRAWING_IDENTITY,
        "selected_structural_occurrence_id": SELECTED_OCCURRENCE,
        "source_structural_occurrence_id": "column_leaf_occurrence",
        "source_occurrence_id": "column_source_occurrence",
        "placed_entity_id": "column_placed_entity",
        "source_entity_handle": "COLUMN-HANDLE",
        "source_segment_ids": segment_ids,
        "placement_path": _placement_path(),
    }
    segments = [{
        "drawing_identity": DRAWING_IDENTITY,
        "selected_structural_occurrence_id": SELECTED_OCCURRENCE,
        "source_structural_occurrence_id": "column_leaf_occurrence",
        "source_occurrence_id": "column_source_occurrence",
        "placed_entity_id": "column_placed_entity",
        "source_entity_handle": "COLUMN-HANDLE",
        "source_segment_id": segment_id,
    } for segment_id in segment_ids]
    return {
        "id": "column_1", "center": [0.0, 0.0], "size": [0.8, 0.8],
        "source_profile_ref": profile,
        "source_segment_refs": segments,
    }


def _structural_step():
    return {
        "cols": [_column()],
        "shear_walls": [_wall("shear_1", derivation=True)],
        "source_drawing_identity": DRAWING_IDENTITY,
        "wall_occurrence_selection": {
            "valid": True,
            "selected_occurrence_id": SELECTED_OCCURRENCE,
        },
        "column_provenance_audit": {
            "status": "PASS", "one_to_one_complete": True,
            "matched_count": 1, "missing_count": 0,
            "ambiguous_count": 0, "identity_incomplete_count": 0,
        },
    }


def test_complete_traceability_accepts_distinct_architecture_occurrence():
    s0 = {
        "source_identity": {"path": "hotel_b01.dxf", "sha256": SOURCE_SHA},
        "source_drawing_identity": DRAWING_IDENTITY,
    }
    s2 = _structural_step()
    s4 = {
        "architecture_applicable": True,
        "architecture_occurrence_selection": {
            "valid": True,
            "selected_occurrence_id": "architecture_root",
        },
        "walls": [_wall("wall_1", occurrence="architecture_root")],
    }

    audit = runner._geometry_traceability_audit(s0, s2, s4)

    assert audit["status"] == "COMPLETE"
    assert audit["gate_passed"] is True
    assert audit["source"]["identity_status"] == "MATCH"
    assert audit["groups"]["columns"]["source_segment_ref_count"] == 4
    assert audit["groups"]["structural_walls"][
        "source_segment_ref_count"] == 1
    assert audit["groups"]["architectural_walls"]["status"] == "COMPLETE"


def test_architecture_traceability_rejects_selected_occurrence_mismatch():
    s0 = {
        "source_identity": {"path": "hotel_b01.dxf", "sha256": SOURCE_SHA},
        "source_drawing_identity": DRAWING_IDENTITY,
    }
    s2 = _structural_step()
    s4 = {
        "architecture_applicable": True,
        "architecture_occurrence_selection": {
            "valid": True,
            "selected_occurrence_id": "architecture_root",
        },
        "walls": [_wall("wall_1", occurrence="other_architecture")],
    }

    audit = runner._geometry_traceability_audit(s0, s2, s4)

    assert audit["status"] == "INCOMPLETE"
    architecture = audit["groups"]["architectural_walls"]
    assert architecture["occurrence_mismatch_count"] == 1
    limitations = architecture["issues"][0]["issues"][0]["limitations"]
    assert "structural_occurrence_id_mismatch" in limitations


def test_declared_complete_wall_without_structural_occurrence_fails_closed():
    wall = _wall("broken", derivation=True)
    wall["source_segment_refs"][0]["structural_occurrence_id"] = None

    audit = runner._wall_traceability_audit(
        [wall], role="structural_wall",
        expected_drawing_identity=DRAWING_IDENTITY,
        expected_occurrence_id=SELECTED_OCCURRENCE,
        require_derivation=True)

    assert audit["status"] == "INCOMPLETE"
    limitations = audit["issues"][0]["issues"][0]["limitations"]
    assert "structural_occurrence_id_missing" in limitations
    assert "structural_occurrence_id_mismatch" in limitations


def test_structure_gate_requires_wall_and_column_traceability(monkeypatch):
    monkeypatch.setattr(runner, "_original_gate1",
                        lambda *_args: ("PASS", []))
    s2 = _structural_step()
    s2["traceability"] = runner._geometry_traceability_audit({
        "source_identity": {"path": "hotel_b01.dxf", "sha256": SOURCE_SHA},
        "source_drawing_identity": DRAWING_IDENTITY,
    }, s2, {"architecture_applicable": False, "walls": []})

    status, checks = runner.strict_gate1({}, s2, {})

    assert status == "PASS"
    assert any("柱源轮廓一一追溯 1/1 (OK)" in item for item in checks)
    assert any("结构墙精确来源追溯 1/1 (OK)" in item for item in checks)

    broken = copy.deepcopy(s2)
    broken["traceability"]["groups"]["structural_walls"]["status"] = (
        "INCOMPLETE")
    assert runner.strict_gate1({}, broken, {})[0] == "FAIL"


def test_guarded_revit_blocks_old_status_without_traceability(
        tmp_path, monkeypatch):
    status = {
        "MODEL_STATUS": {
            "Step0_数据清洗": "PASS", "结构框架Gate": "PASS",
            "建筑空间Gate": "PASS",
        },
        "清洗审计": {
            "建筑空间适用": False,
            "最终判定": {"allow_modeling": True, "reasons": []},
        },
    }
    (tmp_path / "model_status.json").write_text(
        json.dumps(status, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "model.json").write_text(
        json.dumps({"model_elements": [{"type": "Wall"}]}),
        encoding="utf-8")
    called = []
    monkeypatch.setattr(runner.pipeline, "JSON_IN", str(tmp_path))
    monkeypatch.setattr(runner, "_original_trigger_revit",
                        lambda: called.append(True) or {"status": "done"})

    result = runner.guarded_trigger_revit()

    assert result["status"] == "error"
    assert "精确来源追溯未通过" in result["error"]
    assert called == []
