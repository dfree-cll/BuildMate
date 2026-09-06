"""Revit 精确建模入口的契约与防串图回归测试。"""
import copy
import json

from backend.engines import boundary_columns
from backend.engines.adaptive_pipeline import (
    _annotate_boundary_column_wall_targets,
    _append_boundary_columns,
    validate_revit_input,
)
from scripts import pipeline_std_runner as std_runner
from scripts.bim_pipeline.pipeline_std import (
    _simplify_closed_profile, _wall_model_element,
    final_cleaning_blocks_model,
)


def _valid_model(family_path=__file__):
    return {
        "schema_version": "2.0",
        "project": {
            "name": "guard-test",
            "units": "mm",
            "project_id": "PRJ-GUARD",
            "levels": [{"name": "标高 B1", "elevation": -6500}],
        },
        "build": {"floor_code": "B1"},
        "grids": [{"level": "标高 B1", "x_axes": [0, 9000], "y_axes": [0, 9000]}],
        "model_elements": [
            {"type": "Column", "id": "c1", "x": 0, "y": 0,
             "width": 800, "depth": 800, "base": 0, "top": 6400, "level": "标高 B1",
             "type_name": "BM-Concrete-800x800", "family_path": family_path},
            {"type": "Wall", "id": "w1", "start": [0, 0, 0],
             "end": [9000, 0, 0], "thickness": 200, "height": 6400, "level": "标高 B1"},
        ],
    }


def test_wall_model_element_keeps_quantity_parameters_and_lineage():
    element = _wall_model_element(
        3,
        {
            "start": [0.0, 0.0], "end": [4.0, 0.0],
            "thickness": 200, "paired": True,
            "source_segment_ids": ["seg-1"],
            "source_segment_refs": [{"source_segment_id": "seg-1"}],
        },
        "B1", -6.5, -0.1)

    assert element["id"] == "wall_3"
    assert element["start"] == [0.0, 0.0, 0.0]
    assert element["end"] == [4000.0, 0.0, 0.0]
    assert element["wall_type_name"] == "AR-墙-砌体-200mm"
    assert element["thickness"] == 200
    assert element["height"] == 6400
    assert element["gross_face_area_m2"] == 25.6
    assert element["gross_volume_m3"] == 5.12
    assert element["length_m"] == 4.0
    assert element["quantity_scope"] == "GROSS_NO_OPENINGS"
    assert element["source_segment_ids"] == ["seg-1"]


def _passing_status():
    return {"MODEL_STATUS": {
        "Step0_数据清洗": "PASS",
        "Step1_轴网标高": "PASS",
        "Step2_柱剪力墙": "PASS",
        "结构框架Gate": "PASS",
        "建筑空间Gate": "PASS",
    }, "数据质量": {"坐标准确率": 0.99, "几何准确率": 0.99}}


def test_explicit_final_cleaning_failure_blocks_formal_model_output():
    assert final_cleaning_blocks_model({}) is False
    assert final_cleaning_blocks_model({
        "清洗审计": {"最终判定": {
            "allow_modeling": True, "reasons": []}}
    }) is False
    assert final_cleaning_blocks_model({
        "清洗审计": {"最终判定": {
            "allow_modeling": False, "reasons": ["review required"]}}
    }) is True


def _complete_profile_audit(candidates=None, *, needs_review=0,
                            identity_incomplete=0):
    candidates = list(candidates or [])
    approved = sum(item.get("status") == "APPROVED_BY_RULE"
                   for item in candidates)
    return {
        "status": ("REVIEW" if needs_review or identity_incomplete else
                   "PASS"),
        "candidate_count": len(candidates),
        "approved_by_rule_count": approved,
        "needs_review_count": needs_review,
        "identity_incomplete_count": identity_incomplete,
        "candidates": candidates,
        "association": {
            "status": "COMPLETE",
            "profile_count": len(candidates),
            "child_evidence_count": 4 * len(candidates),
            "associated_source_segment_count": 4 * len(candidates),
            "missing_count": 0,
            "duplicate_count": 0,
            "approved_profile_count": approved,
            "approved_model_wall_count": approved,
            "approved_wall_one_to_one_count": approved,
            "approved_wall_missing_count": 0,
            "approved_wall_duplicate_count": 0,
        },
    }


def _complete_traceability(*, architectural_wall_count=0):
    return {
        "status": "COMPLETE",
        "gate_passed": True,
        "source": {"identity_status": "MATCH"},
        "groups": {
            "architectural_walls": {
                "status": "COMPLETE",
                "element_count": architectural_wall_count,
                "traceable_element_count": architectural_wall_count,
            },
        },
    }


def _complete_architecture_occurrence_selection():
    return {
        "valid": True,
        "selected_occurrence_id": "architecture_occurrence_1",
    }


def test_valid_revit_input_passes_strict_gate():
    assert validate_revit_input(_valid_model(), _passing_status()) == []


def test_architecture_gate_failure_blocks_revit():
    status = _passing_status()
    status["MODEL_STATUS"]["建筑空间Gate"] = "FAIL"
    errors = validate_revit_input(_valid_model(), status)
    assert "建筑空间Gate=FAIL" in errors


def test_standard_runner_trigger_fails_closed_on_cleaning_gate(tmp_path, monkeypatch):
    status = {
        "MODEL_STATUS": {
            "Step0_数据清洗": "FAIL",
            "结构框架Gate": "PASS",
            "建筑空间Gate": "FAIL",
        },
        "清洗审计": {"最终判定": {
            "allow_modeling": False,
            "reasons": ["建筑墙拓扑 Gate 未通过"],
        }},
    }
    (tmp_path / "model_status.json").write_text(
        json.dumps(status, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "model.json").write_text(
        json.dumps({"model_elements": [{"type": "Wall"}]}), encoding="utf-8")
    called = []
    monkeypatch.setattr(std_runner.pipeline, "JSON_IN", str(tmp_path))
    monkeypatch.setattr(std_runner, "_original_trigger_revit",
                        lambda: called.append(True) or {"status": "done"})

    result = std_runner.guarded_trigger_revit()

    assert result["status"] == "error"
    assert "建筑墙拓扑 Gate 未通过" in result["error"]
    assert called == []


def test_standard_runner_trigger_fails_closed_on_profile_association(
        tmp_path, monkeypatch):
    profile_audit = _complete_profile_audit()
    profile_audit["gate_passed"] = False
    status = _passing_status()
    status["清洗审计"] = {
        "建筑空间适用": True,
        "墙柱几何溯源": _complete_traceability(),
        "闭合墙轮廓审核": profile_audit,
        "最终判定": {"allow_modeling": True, "reasons": []},
    }
    (tmp_path / "model_status.json").write_text(
        json.dumps(status, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "model.json").write_text(
        json.dumps({"model_elements": [{"type": "Wall"}]}),
        encoding="utf-8")
    called = []
    monkeypatch.setattr(std_runner.pipeline, "JSON_IN", str(tmp_path))
    monkeypatch.setattr(std_runner, "_original_trigger_revit",
                        lambda: called.append(True) or {"status": "done"})

    result = std_runner.guarded_trigger_revit()

    assert result["status"] == "error"
    assert "闭合建筑墙轮廓审核" in result["error"]
    assert called == []


def test_review_bim_ir_keeps_source_hash_exact_geometry_and_candidates():
    candidate = {
        "candidate_id": "semantic_face_1",
        "entity_handle": "4CFE8",
        "status": "NEEDS_REVIEW",
        "geometry_source": "DXF_VECTOR",
        "start": [1.0, 2.0],
        "end": [1.0, 2.5],
    }
    ordinary_source_refs = [{
        "drawing_identity": "sha256:abc",
        "source_occurrence_id": "source_face_a",
        "placed_entity_id": "placed_face_a",
        "structural_occurrence_id": "structural_1",
        "source_segment_id": "segment_face_a_0",
        "entity_handle": "FACE-A",
        "source_record_index": 4,
        "segment_index": 0,
        "source_interval_m": [0.0, 1.0],
    }, {
        "drawing_identity": "sha256:abc",
        "source_occurrence_id": "source_face_b",
        "placed_entity_id": "placed_face_b",
        "structural_occurrence_id": "structural_1",
        "source_segment_id": "segment_face_b_0",
        "entity_handle": "FACE-B",
        "source_record_index": 5,
        "segment_index": 0,
        "source_interval_m": [0.0, 1.0],
    }]
    placement_path = [{
        "insert_handle": "INSERT-01", "block_name": "B1",
        "array_index": None,
        "cumulative_transform": {
            "coordinate_system": "DXF_WCS",
            "affine_2d": [[1.0, 0.0, 0.0],
                          [0.0, 1.0, 0.0],
                          [0.0, 0.0, 1.0]],
        },
    }]
    for reference in ordinary_source_refs:
        reference.update({
            "placement_path": copy.deepcopy(placement_path),
            "identity_is_complete": True,
            "identity_limitations": [],
        })
    structure_derivation = [{
        "operation": "merge_collinear_walls",
        "parent_source_segment_ids": ["segment_face_a_0"],
    }]
    steps = (
        {"source_identity": {"path": "hotel_b01.dxf", "sha256": "abc"}},
        {"origin": [1000, 2000], "rot_deg": 0.0, "gcx": 1, "gcy": 2},
        {"cols": [], "shear_walls": [{
            "id": "shear_1", "start": [0, 1], "end": [1, 1],
            "thickness": 300,
            "geometry_source": "DXF_VECTOR",
            "source_segment_ids": ["segment_face_a_0"],
            "source_segment_refs": [copy.deepcopy(ordinary_source_refs[0])],
            "derivation": structure_derivation,
        }]},
        {"beams": []},
        ("PASS", ["structure OK"]),
         {"walls": [{"id": "wall_1", "start": [0, 0], "end": [1, 0],
                     "thickness": 200,
                     "geometry_source": "DXF_CLOSED_WALL_STRIP",
                     "source_candidate_id": "closed_1",
                     "profile_occurrence_id": "profile_occurrence_1",
                     "drawing_identity": "sha256:abc",
                     "source_occurrence_id": "source_occurrence_1",
                     "placed_entity_id": "placed_entity_1",
                     "structural_occurrence_id": "structural_1",
                     "source_segment_ids": ["segment_1", "segment_2",
                                            "segment_3", "segment_4"]},
                    {"id": "wall_2", "start": [2, 0], "end": [3, 0],
                     "thickness": 200, "paired": True,
                     "geometry_source": "DXF_VECTOR",
                     "source_segment_ids": ["segment_face_a_0",
                                            "segment_face_b_0"],
                     "source_segment_refs": ordinary_source_refs}],
         "source_coverage": {"uncovered_source_segments": [candidate]},
         "closed_wall_profile_audit": _complete_profile_audit([{
             "candidate_id": "closed_1", "status": "APPROVED_BY_RULE",
         }]),
         "dxf_review_candidates": {}},
        ("FAIL", ["semantic source review"]),
        {},
        {},
    )
    report = {"清洗审计": {"最终判定": {
        "allow_modeling": False,
        "reasons": ["semantic source review"],
    }}}
    steps[5]["short_wall_gate_base"] = {
        "schema_version": "buildmate.short-wall-gate-base/1.0",
        "rule_version": "strict-additive-vector-replay/1.0",
        "sha256": "d" * 64,
    }
    steps[5]["short_wall_replay"] = {
        "requested": True,
        "status": "REPLAYED",
        "gate_base_sha256": "c" * 64,
        "replayed_proposal_ids": ["short_wall_1"],
        "decision_ids": ["decision_1"],
        "errors": [],
    }

    review_ir = std_runner.build_review_bim_ir(steps, report)

    assert review_ir["artifact_role"] == "REVIEW_ONLY"
    assert review_ir["artifact_status"] == "BLOCKED"
    assert review_ir["source"]["sha256"] == "abc"
    assert review_ir["geometry"]["architectural_walls"][0]["start"] == [0, 0]
    assert review_ir["geometry"]["architectural_walls"][0][
        "geometry_source"] == "DXF_CLOSED_WALL_STRIP"
    assert review_ir["geometry"]["architectural_walls"][0][
        "source_candidate_id"] == "closed_1"
    assert review_ir["geometry"]["architectural_walls"][0][
        "source_occurrence_id"] == "source_occurrence_1"
    assert review_ir["geometry"]["architectural_walls"][0][
        "profile_occurrence_id"] == "profile_occurrence_1"
    assert review_ir["geometry"]["architectural_walls"][0][
        "placed_entity_id"] == "placed_entity_1"
    assert review_ir["geometry"]["architectural_walls"][0][
        "source_segment_ids"] == [
            "segment_1", "segment_2", "segment_3", "segment_4"]
    assert review_ir["geometry"]["architectural_walls"][1][
        "source_segment_refs"] == ordinary_source_refs
    assert review_ir["geometry"]["architectural_walls"][1][
        "source_segment_refs"][0]["placement_path"] == placement_path
    assert review_ir["geometry"]["structural_walls"][0][
        "source_segment_refs"][0]["placement_path"] == placement_path
    assert review_ir["geometry"]["structural_walls"][0][
        "derivation"] == structure_derivation
    assert review_ir["review_candidates"]["semantic_wall_sources"] == [candidate]
    assert review_ir["review_candidates"]["closed_wall_profiles"][
        "candidate_count"] == 1
    assert review_ir["short_wall_gate_base"]["sha256"] == "d" * 64
    assert review_ir["short_wall_replay"]["replayed_proposal_ids"] == [
        "short_wall_1"]


def test_architecture_gate_uses_resolved_dxf_candidates_not_raw_cv_or_yolo():
    s4 = {
        "architecture_applicable": True,
        "plan_selection": {"selected": "main"},
        "wall_occurrence_selection": {"valid": True},
        "architecture_occurrence_selection": (
            _complete_architecture_occurrence_selection()),
        "architecture_source_count": 10,
        "traceability": _complete_traceability(),
        "source_coverage": {
            "exact_face_length_coverage": 1.0,
            "gate_exact_face_length_coverage": 1.0,
            "raw_source_review_count": 0,
            "logical_review_unit_count": 0,
            "uncovered_source_segment_count": 0,
            "partially_mapped_source_segment_count": 0,
        },
        "closed_wall_profile_audit": _complete_profile_audit(),
        "walls": [],
        "endpoint_coverage": 0.95,
        "closed_rate": 0.9,
        "room_polygon_count": 2,
        "cv_topology_audit": {"status": "OK", "enclosed_region_count": 2},
        "vector_candidate_audit": {"review_count": 0},
        "full_drawing_audit_status": "OK",
        "cleaning_decision": {
            "allow_modeling": True,
            "yolo_required": False,
        },
        "full_cv_audit": {
            "candidate_missing_line_count": 50,
            "vector_association": {"unassociated_candidate_count": 0},
        },
        "dxf_review_candidates": {
            "needs_review_count": 0,
            "rejected_by_rule_count": 54,
        },
        "yolo_wall_audit": {
            "inference_status": "OK",
            "quality": {"status": "REVIEW"},
        },
    }

    status, checks = std_runner.scoped_gate2(s4)

    assert status == "PASS"
    assert any("CV线候选 50" in check and "(OK)" in check for check in checks)
    assert any("YOLO语义证据 OK/REVIEW (ADVISORY)" in check for check in checks)


def test_architecture_gate_explicitly_blocks_short_wall_proposals():
    base = {
        "architecture_applicable": True,
        "plan_selection": {"selected": "main"},
        "wall_occurrence_selection": {"valid": True},
        "architecture_occurrence_selection": (
            _complete_architecture_occurrence_selection()),
        "architecture_source_count": 10,
        "traceability": _complete_traceability(),
        "source_coverage": {
            "gate_exact_face_length_coverage": 1.0,
            "raw_source_review_count": 0,
            "logical_review_unit_count": 0,
        },
        "closed_wall_profile_audit": _complete_profile_audit(),
        "walls": [],
        "endpoint_coverage": 1.0,
        "closed_rate": 1.0,
        "endpoint_review": {"needs_review_count": 0},
        "room_polygon_count": 1,
        "cv_topology_audit": {"status": "OK", "enclosed_region_count": 1},
        "vector_candidate_audit": {"review_count": 0},
    }
    audits = [({
        "proposed_short_wall_count": 1,
        "proposals": [],
    }, "提案 1"), ({
        "proposed_short_wall_count": 0,
        "proposals": [{
            "status": "PROPOSED_SHORT_WALL",
            "review_application": {"mode": "IR_PREVIEW_ONLY"},
            "formal_model_eligible": False,
        }],
    }, "提案 1"), ({
        "proposed_short_wall_count": 0,
        "insufficient_support_count": 1,
        "proposals": [],
    }, "支撑不足 1"), ({
        "proposed_short_wall_count": 0,
        "single_wall_upgrade_proposal_count": 0,
        "proposals": [{
            "status": "PROPOSED_WALL_UPGRADE",
            "blockers": [],
        }],
    }, "墙升级 1"), ({
        "proposed_short_wall_count": 0,
        "proposals": [],
        "insufficient_support_candidates": [{
            "status": "INSUFFICIENT_SUPPORT",
            "blockers": [
                "atomic_single_wall_replacement_not_implemented"],
        }],
    }, "原子替换未实现 1")]

    for audit, expected_check in audits:
        s4 = copy.deepcopy(base)
        s4["source_coverage"]["short_wall_proposal_audit"] = audit

        status, checks = std_runner.scoped_gate2(s4)

        assert status == "FAIL"
        assert any("短墙正式建模阻断" in item and "(FAIL)" in item
                   for item in checks)
        assert any(expected_check in item for item in checks)


def test_architecture_gate_fails_closed_when_explicit_replay_is_blocked():
    s4 = {
        "architecture_applicable": True,
        "plan_selection": {"selected": "main"},
        "wall_occurrence_selection": {"valid": True},
        "architecture_occurrence_selection": (
            _complete_architecture_occurrence_selection()),
        "architecture_source_count": 10,
        "traceability": _complete_traceability(),
        "source_coverage": {
            "gate_exact_face_length_coverage": 1.0,
            "raw_source_review_count": 0,
            "logical_review_unit_count": 0,
            "short_wall_proposal_audit": {
                "proposed_short_wall_count": 0,
                "proposals": [],
            },
        },
        "closed_wall_profile_audit": _complete_profile_audit(),
        "walls": [],
        "endpoint_coverage": 1.0,
        "closed_rate": 1.0,
        "endpoint_review": {"needs_review_count": 0},
        "room_polygon_count": 1,
        "cv_topology_audit": {"status": "OK", "enclosed_region_count": 1},
        "vector_candidate_audit": {"review_count": 0},
        "short_wall_replay": {
            "requested": True,
            "status": "BLOCKED",
            "errors": ["short_wall_gate_base_mismatch"],
        },
    }

    blocked, checks = std_runner.scoped_gate2(s4)
    assert blocked == "FAIL"
    assert "短墙人工审批重放 FAIL (BLOCKED)" in checks

    s4["short_wall_replay"] = {
        "requested": True,
        "status": "REPLAYED",
        "errors": [],
    }
    passed, checks = std_runner.scoped_gate2(s4)
    assert passed == "PASS"
    assert "短墙人工审批重放 OK (REPLAYED)" in checks


def test_architecture_gate_fails_closed_when_profile_audit_is_missing():
    s4 = {
        "architecture_applicable": True,
        "plan_selection": {"selected": "main"},
        "wall_occurrence_selection": {"valid": True},
        "architecture_occurrence_selection": (
            _complete_architecture_occurrence_selection()),
        "architecture_source_count": 10,
        "source_coverage": {
            "gate_exact_face_length_coverage": 1.0,
            "raw_source_review_count": 0,
            "logical_review_unit_count": 0,
        },
        "walls": [],
        "endpoint_coverage": 1.0,
        "closed_rate": 1.0,
        "room_polygon_count": 1,
        "cv_topology_audit": {"status": "OK", "enclosed_region_count": 1},
        "vector_candidate_audit": {"review_count": 0},
    }

    status, checks = std_runner.scoped_gate2(s4)

    assert status == "FAIL"
    assert "闭合墙轮廓审核已运行 (FAIL)" in checks


def test_architecture_gate_uses_logical_profile_units_not_raw_child_edges():
    candidate = {
        "candidate_id": "closed_1",
        "status": "APPROVED_BY_RULE",
    }
    profile_audit = _complete_profile_audit([candidate])
    s4 = {
        "architecture_applicable": True,
        "plan_selection": {"selected": "main"},
        "wall_occurrence_selection": {"valid": True},
        "architecture_occurrence_selection": (
            _complete_architecture_occurrence_selection()),
        "architecture_source_count": 1,
        "traceability": _complete_traceability(
            architectural_wall_count=1),
        "source_coverage": {
            "gate_exact_face_length_coverage": 1.0,
            "raw_source_review_count": 4,
            "logical_review_unit_count": 0,
            "uncovered_source_segment_count": 4,
        },
        "closed_wall_profile_audit": profile_audit,
        "walls": [{
            "geometry_source": "DXF_CLOSED_WALL_STRIP",
            "source_candidate_id": "closed_1",
        }],
        "endpoint_coverage": 1.0,
        "closed_rate": 1.0,
        "room_polygon_count": 1,
        "cv_topology_audit": {"status": "OK", "enclosed_region_count": 1},
        "vector_candidate_audit": {"review_count": 0},
    }

    status, checks = std_runner.scoped_gate2(s4)

    assert status == "PASS"
    assert any("原始线段 4 / 逻辑审核单元 0 (OK)" in item
               for item in checks)


def test_architecture_gate_blocks_review_identity_and_non_unique_wall_mapping():
    candidate = {
        "candidate_id": "closed_1",
        "status": "APPROVED_BY_RULE",
    }
    profile_audit = _complete_profile_audit(
        [candidate], needs_review=1, identity_incomplete=1)
    profile_audit["association"]["approved_model_wall_count"] = 2
    profile_audit["association"]["approved_wall_duplicate_count"] = 1
    s4 = {
        "architecture_applicable": True,
        "plan_selection": {"selected": "main"},
        "wall_occurrence_selection": {"valid": True},
        "architecture_occurrence_selection": (
            _complete_architecture_occurrence_selection()),
        "architecture_source_count": 1,
        "source_coverage": {
            "gate_exact_face_length_coverage": 1.0,
            "raw_source_review_count": 1,
            "logical_review_unit_count": 0,
        },
        "closed_wall_profile_audit": profile_audit,
        "walls": [{
            "geometry_source": "DXF_CLOSED_WALL_STRIP",
            "source_candidate_id": "closed_1",
        }],
        "endpoint_coverage": 1.0,
        "closed_rate": 1.0,
        "room_polygon_count": 1,
        "cv_topology_audit": {"status": "OK", "enclosed_region_count": 1},
        "vector_candidate_audit": {"review_count": 0},
    }

    status, checks = std_runner.scoped_gate2(s4)

    assert status == "FAIL"
    assert any("待复核 1 / 身份不完整 1 (REVIEW)" in item
               for item in checks)
    assert any("一一对应 1 (FAIL)" in item for item in checks)


def test_precise_step4_passes_closed_profile_audit_into_coverage(
        monkeypatch):
    candidate = {
        "candidate_id": "closed_1",
        "status": "APPROVED_BY_RULE",
    }
    initial_audit = _complete_profile_audit([candidate])
    enriched_audit = dict(initial_audit)
    enriched_audit["association"] = dict(initial_audit["association"])
    seen = {}
    wall = {
        "start": [0, 0], "end": [1, 0], "thickness": 200,
        "paired": True, "geometry_source": "DXF_CLOSED_WALL_STRIP",
        "source_candidate_id": "closed_1",
    }
    monkeypatch.setattr(
        std_runner, "filter_wall_records_to_region",
        lambda records, *_args, **_kwargs: (list(records), []))
    monkeypatch.setattr(
        std_runner, "audit_closed_wall_strips",
        lambda *_args, **_kwargs: initial_audit)
    monkeypatch.setattr(
        std_runner, "extract_precise_walls",
        lambda *_args, **_kwargs: [dict(wall)])
    monkeypatch.setattr(std_runner, "audit_vector_wall_candidates",
                        lambda *_args, **_kwargs: {
                            "promoted_walls": [], "review_count": 0,
                        })
    monkeypatch.setattr(std_runner, "infer_opening_bridges", lambda _walls: [])
    classified_audit = copy.deepcopy(initial_audit)
    symbol_statistics = {"status": "COMPLETE", "matched_profile_count": 0}
    monkeypatch.setattr(
        std_runner, "classify_door_leaf_swing_profiles",
        lambda audit, *_args, **_kwargs: (
            classified_audit if audit is initial_audit else None,
            symbol_statistics))

    def coverage(*_args, **kwargs):
        seen["closed_profile_audit"] = kwargs.get("closed_profile_audit")
        return {
            "closed_wall_profile_audit": enriched_audit,
            "gate_exact_face_length_coverage": 1.0,
            "raw_source_review_count": 4,
            "logical_review_unit_count": 0,
        }

    monkeypatch.setattr(std_runner, "wall_source_coverage", coverage)
    monkeypatch.setattr(std_runner, "topology_with_support",
                        lambda *_args, **_kwargs: {
                            "fully_connected_rate": 1.0,
                            "endpoint_coverage": 1.0,
                            "dangling": [],
                            "room_polygon_count": 1,
                            "room_polygon_area_m2": 1.0,
                            "polygonize_status": "OK",
                        })
    monkeypatch.setattr(std_runner, "classify_dangling_endpoints",
                        lambda *_args, **_kwargs: {"candidate_count": 0})
    monkeypatch.setattr(std_runner, "cv_room_enclosure_audit",
                        lambda *_args, **_kwargs: {
                            "status": "OK", "enclosed_region_count": 1,
                        })
    s1 = {
        "prefix": "B1", "origin": [0, 0], "rot_deg": 0,
        "gcx": 0, "gcy": 0,
        "grid": {"x_axes": [{"coord": 0}, {"coord": 1}],
                 "y_axes": [{"coord": 0}, {"coord": 1}]},
    }
    cleaned = {
        "wall_source_records": [(object(), "A-WALL", {})],
        "vector_source_records": [],
        "plan_selection": {"selected": "main"},
    }
    s2 = {
        "shear_walls": [], "cols": [],
        "wall_occurrence_selection": {"valid": True},
    }

    result = std_runner.precise_step4(cleaned, s1, s2)

    assert seen["closed_profile_audit"] is classified_audit
    assert seen["closed_profile_audit"]["door_leaf_swing_audit"] == (
        symbol_statistics)
    assert result["closed_wall_profile_audit"] == enriched_audit
    assert result["walls"][0]["source_candidate_id"] == "closed_1"


def test_architectural_continuity_restores_whole_paired_walls_only():
    walls = [{
        "start": [0.0, 0.0], "end": [3.0, 0.0], "thickness": 200,
        "paired": True, "source_layers": ["A-WALL"],
    }, {
        "start": [4.2, 0.01], "end": [8.0, 0.01], "thickness": 200,
        "paired": True, "source_layers": ["A-WALL"],
    }, {
        "start": [8.1, 0.0], "end": [10.0, 0.0], "thickness": 300,
        "paired": True, "source_layers": ["A-WALL"],
    }, {
        "start": [12.0, 0.0], "end": [14.0, 0.0], "thickness": 200,
        "paired": False, "source_layers": ["A-WALL"],
    }, {
        "start": [15.0, 0.0], "end": [15.5, 0.0], "thickness": 600,
        "paired": True, "source_layers": ["A-PART-S"],
    }]

    restored, audit = std_runner._restore_architectural_wall_continuity(walls)

    paired = [wall for wall in restored if wall["paired"]]
    proposals = [wall for wall in restored if not wall["paired"]]
    assert [(wall["start"], wall["end"], wall["thickness"])
            for wall in paired] == [
                ([0.0, 0.0], [8.0, 0.0], 200),
                ([8.1, 0.0], [10.0, 0.0], 300),
            ]
    assert proposals == [walls[-2]]
    assert audit["mode"] == (
        "GROSS_WALL_IGNORE_OPENINGS_AND_CONSTRUCTION_COLUMNS")
    assert audit["merged_paired_wall_count"] == 1
    assert audit["unpaired_review_wall_count"] == 1
    assert audit["excluded_compact_column_like_count"] == 1


def test_wall_source_filter_is_explicit_and_applied_before_pairing(monkeypatch):
    monkeypatch.setattr(std_runner.pipeline, "WALL_SOURCE_EXCLUDE_PATTERNS",
                        ["人防东登"])
    kept, excluded, audit = std_runner._filter_wall_records_by_source_layers([
        (object(), "B1_墙体$0$A-WALL", {}),
        (object(), "B1_人防东登$0$A-WALL", {}),
    ])

    assert len(kept) == 1
    assert len(excluded) == 1
    assert audit["mode"] == "explicit_source_filter"
    assert audit["excluded_count"] == 1
    assert audit["matched_pattern_counts"] == {"人防东登": 1}
    assert audit["excluded_layer_counts"] == {
        "B1_人防东登$0$A-WALL": 1,
    }


def test_risk_report_persists_profile_gate_and_raw_logical_counts(
        tmp_path, monkeypatch):
    candidate = {
        "candidate_id": "closed_1",
        "status": "APPROVED_BY_RULE",
    }
    profile_audit = _complete_profile_audit([candidate])
    s4 = {
        "architecture_applicable": True,
        "walls": [{
            "geometry_source": "DXF_CLOSED_WALL_STRIP",
            "source_candidate_id": "closed_1",
        }],
        "closed_wall_profile_audit": profile_audit,
        "source_coverage": {
            "raw_source_review_count": 4,
            "logical_review_unit_count": 0,
        },
        "traceability": _complete_traceability(
            architectural_wall_count=1),
        "endpoint_review": {},
        "opening_bridges": [],
    }
    steps = (
        {"full_drawing_audit_dir": str(tmp_path)},
        {"prefix": "B1", "origin": [0, 0], "rot_deg": 0,
         "gcx": 0, "gcy": 0},
        {"cols": [], "shear_walls": []},
        {"beams": []},
        ("PASS", []),
        s4,
        ("PASS", []),
        {},
        {},
    )
    monkeypatch.setattr(std_runner, "_original_risk_report", lambda _steps: {
        "MODEL_STATUS": {"Step0_数据清洗": "PASS"},
        "数据质量": {},
    })
    monkeypatch.setattr(std_runner.pipeline, "JSON_IN", str(tmp_path))

    report = std_runner.scoped_risk_report(steps)

    cleaning = report["清洗审计"]
    assert cleaning["闭合墙轮廓审核"]["gate_passed"] is True
    assert cleaning["墙源复核计数"] == {
        "raw_source_review_count": 4,
        "raw_needs_review_source_segment_count": None,
        "rejected_by_rule_source_segment_count": None,
        "resolved_by_profile_source_segment_count": None,
        "logical_review_unit_count": 0,
    }
    assert cleaning["最终判定"]["allow_modeling"] is True


def test_irregular_profile_classification_keeps_geometry_review_only():
    audit = {"candidates": [
        {"candidate_id": "profile_col", "points_dxf_mm": [
            [-300, -300], [300, -300], [300, 0], [100, 0],
            [100, 300], [-300, 300]],
         "status": "NEEDS_REVIEW"},
        {"candidate_id": "profile_wall", "points_dxf_mm": [
            [4700, -300], [5300, -300], [5300, 0], [5100, 0],
            [5100, 300], [4700, 300]],
         "status": "NEEDS_REVIEW"},
    ]}
    s1 = {"origin": [0, 0], "rot_deg": 0, "gcx": 0, "gcy": 0}
    columns = [{"id": "col_1", "center": [0, 0], "size": [0.8, 0.8]}]
    walls = [{"id": "shear_1", "start": [4, 0], "end": [6, 0],
              "thickness": 200}]

    result = std_runner.classify_irregular_profiles(
        audit, s1, columns, walls)

    assert result["candidate_count"] == 2
    assert result["needs_review_count"] == 1
    assert result["rejected_by_rule_count"] == 1
    assert result["reason_counts"] == {
        "matched_structural_wall_hatch": 1,
        "spatially_overlaps_extracted_column": 1,
    }
    assert all(item["geometry_source"] == "DXF_VECTOR"
               for item in result["candidates"])
    assert {item["status"] for item in result["candidates"]} == {
        "NEEDS_REVIEW", "REJECTED_BY_RULE"}


def test_irregular_profile_selection_uses_structural_occurrence_not_entity_id():
    points = [[-300, -300], [300, -300], [300, 0], [100, 0],
              [100, 300], [-300, 300]]
    audit = {"candidates": [
        {"candidate_id": "selected", "points_dxf_mm": points,
         "structural_occurrence_id": "structural_selected",
         "source_occurrence_id": "source_entity_selected",
         "placed_entity_id": "placed_selected"},
        {"candidate_id": "other", "points_dxf_mm": points,
         "structural_occurrence_id": "structural_other",
         "source_occurrence_id": "source_entity_other",
         "placed_entity_id": "placed_other"},
    ]}

    result = std_runner.classify_irregular_profiles(
        audit, {"origin": [0, 0], "rot_deg": 0, "gcx": 0, "gcy": 0},
        [], [], selected_occurrence_id="structural_selected")

    assert result["selected_occurrence_raw_candidate_count"] == 1
    assert result["excluded_other_occurrence_candidate_count"] == 1
    assert [item["candidate_id"] for item in result["candidates"]] == [
        "selected"]


def test_redundant_collinear_vertex_does_not_make_rectangle_irregular():
    points = [(0.0, 0.0), (0.0, 200.0), (0.0, 400.0),
              (800.0, 400.0), (800.0, 0.0), (300.0, 0.0)]

    simplified = _simplify_closed_profile(points)

    assert simplified == [
        (0.0, 0.0), (0.0, 400.0), (800.0, 400.0), (800.0, 0.0)]


def test_structural_scope_does_not_require_architecture_gate():
    model = _valid_model()
    model["project"]["model_scope"] = "structural"
    status = _passing_status()
    status["MODEL_STATUS"]["建筑空间Gate"] = "N/A"
    assert validate_revit_input(model, status, require_architecture=False) == []


def test_mixed_unit_or_incomplete_element_is_rejected():
    model = _valid_model()
    model["project"]["units"] = "m"
    del model["model_elements"][1]["thickness"]
    errors = validate_revit_input(model, _passing_status())
    assert "project.units 必须明确为 mm" in errors
    assert any("Wall[1] 字段不完整" in e for e in errors)


def test_unsupported_semantic_element_is_not_silently_ignored():
    model = _valid_model()
    model["model_elements"].append(
        {"type": "Opening", "id": "o1", "x": 1, "y": 1, "level": "B1"})
    assert "当前 Revit 执行器不支持 Opening" in validate_revit_input(
        model, _passing_status())


def test_structural_element_without_family_type_is_rejected():
    model = _valid_model()
    del model["model_elements"][0]["type_name"]
    assert "Column[0] 缺少精确族类型 type_name" in validate_revit_input(
        model, _passing_status())


def test_profile_column_is_valid_without_rectangular_family():
    model = _valid_model()
    model["model_elements"].append({
        "type": "Column", "id": "gbz1", "x": 1000, "y": 2000,
        "base": -6500, "top": -100, "level": "标高 B1",
        "profile": {"kind": "poly", "points": [[-200, -200], [200, -200],
                                                       [200, 200], [-200, 200]]},
    })
    assert validate_revit_input(model, _passing_status()) == []


def test_boundary_columns_join_the_standard_model_at_source_level(monkeypatch):
    model = _valid_model()
    monkeypatch.setattr(boundary_columns, "extract_boundary_columns", lambda _: {
        "columns": [{"type": "Column", "id": "boundary_gbz1", "x": 1000,
                     "y": 2000, "label_distance_mm": 100,
                     "profile": {"kind": "poly", "points": []}}],
        "meta": {"transform": {"elev_m": [-6.5, -0.1]}},
    })

    assert _append_boundary_columns(model, "plan.dxf") == 1
    column = model["model_elements"][-1]
    assert column["base"] == -6500
    assert column["top"] == -100
    assert column["level"] == "标高 B1"


def test_boundary_columns_are_floor_scoped_and_replace_rectangular_placeholder(monkeypatch):
    model = _valid_model()
    model["model_elements"][0].update({"id": "rect", "x": 1000, "y": 2000,
                                         "level": "标高 B1"})
    monkeypatch.setattr(boundary_columns, "extract_boundary_columns", lambda _: {
        "columns": [
            {"type": "Column", "id": "boundary_gbz1", "x": 1000, "y": 2000,
             "label_distance_mm": 100,
             "profile": {"kind": "poly", "points": [
                 [-300, -300], [300, -300], [300, 300], [-300, 300]]}},
            {"type": "Column", "id": "other_floor", "x": 1000, "y": 30000,
             "label_distance_mm": 100,
             "profile": {"kind": "poly", "points": [
                 [-300, -300], [300, -300], [300, 300], [-300, 300]]}},
            {"type": "Column", "id": "weak_match", "x": 2000, "y": 2000,
             "label_distance_mm": 2500,
             "profile": {"kind": "poly", "points": [
                 [-300, -300], [300, -300], [300, 300], [-300, 300]]}},
        ],
        "meta": {"transform": {"elev_m": [-6.5, -0.1]}},
    })

    assert _append_boundary_columns(model, "plan.dxf") == 1
    columns = [item for item in model["model_elements"] if item["type"] == "Column"]
    assert [item["id"] for item in columns] == ["boundary_gbz1"]


def test_boundary_columns_apply_explicit_detail_to_main_plan_translation(monkeypatch):
    model = _valid_model()
    model["build"]["boundary_column_offset_mm"] = [3000, -1000]
    monkeypatch.setattr(boundary_columns, "extract_boundary_columns", lambda _: {
        "columns": [{"type": "Column", "id": "boundary_gbz1",
                     "x": -2000, "y": 3000, "label_distance_mm": 100,
                     "profile": {"kind": "poly", "points": [
                         [-200, -200], [200, -200], [200, 200], [-200, 200]]}}],
        "meta": {"transform": {"elev_m": [-6.5, -0.1]}},
    })

    assert _append_boundary_columns(model, "plan.dxf") == 1
    column = model["model_elements"][-1]
    assert (column["x"], column["y"]) == (1000.0, 2000.0)
    assert column["placement_transform"]["offset_mm"] == [3000.0, -1000.0]


def test_boundary_column_wall_targets_use_positive_source_solid_overlap():
    model = _valid_model()
    model["model_elements"].append({
        "type": "Column", "subtype": "BoundaryColumn", "id": "gbz1",
        "x": 1000, "y": 0, "base": 0, "top": 6400,
        "level": "标高 B1", "profile": {"kind": "poly", "points": [
            [-300, -300], [300, -300], [300, 300], [-300, 300]]},
    })
    model["model_elements"].append({
        "type": "Wall", "id": "touch-only", "start": [1300, 1000, 0],
        "end": [1300, 2000, 0], "thickness": 200,
        "height": 6400, "level": "标高 B1",
    })

    assert _annotate_boundary_column_wall_targets(model) == 1
    column = next(item for item in model["model_elements"]
                  if item.get("id") == "gbz1")
    assert column["cut_wall_ids"] == ["w1"]


def test_column_conflict_or_low_coordinate_accuracy_blocks_build():
    status = _passing_status()
    status["MODEL_STATUS"]["Step2_柱剪力墙"] = "WARN"
    status["数据质量"]["坐标准确率"] = 0.69
    errors = validate_revit_input(_valid_model(), status)
    assert "Step2_柱剪力墙=WARN" in errors
    assert any("坐标准确率=0.69" in e for e in errors)


def test_low_geometry_accuracy_or_missing_architecture_blocks_floor_build():
    status = _passing_status()
    status["数据质量"]["几何准确率"] = 0.52
    status["MODEL_STATUS"]["建筑空间Gate"] = "N/A"
    errors = validate_revit_input(_valid_model(), status)
    assert any("几何准确率=0.52" in e for e in errors)
    assert "建筑空间Gate=N/A" in errors


def test_project_and_floor_contract_is_required():
    model = _valid_model()
    del model["project"]["project_id"]
    model["build"]["floor_code"] = "LEVEL-1"
    errors = validate_revit_input(model, _passing_status())
    assert "project.project_id 必须明确" in errors
    assert "build.floor_code 必须是 B1、1F 或 RF" in errors
