import json

import ezdxf
import pytest

import scripts.audit_drawing_cleaning as audit_module
from scripts.audit_drawing_cleaning import (
    associate_cv_candidates,
    build_dxf_review_candidates,
    is_suspicious_layer,
    is_structurally_named_non_wall_layer,
    known_non_wall_reason,
    render_closed_wall_profile_review_overlay,
    render_irregular_profile_review_overlay,
    render_wall_endpoint_review_overlay,
    render_wall_source_review_overlay,
)


def test_cv_candidate_associates_exact_placed_dxf_segment():
    candidates = [{
        "start_px": [10, 20],
        "end_px": [110, 20],
        "length_px": 100,
    }]
    segments = [{
        "entity_handle": "4ADF3",
        "entity_type": "LINE",
        "source_block_handle": "2D659",
        "source_block_name": "B1层平面图",
        "layer": "A-临时墙线",
        "start_px": [5, 21],
        "end_px": [120, 21],
        "start_dxf": [516000000.0, 323000000.0],
        "end_dxf": [516006000.0, 323000000.0],
    }]

    result = associate_cv_candidates(candidates, segments)

    assert result["associated_candidate_count"] == 1
    assert result["unassociated_candidate_count"] == 0
    candidate = result["candidates"][0]
    assert candidate["candidate_id"] == "cv_missing_000"
    assert candidate["has_exact_vector_source"] is True
    assert candidate["matched_dxf_segments"][0]["entity_handle"] == "4ADF3"
    assert candidate["matched_dxf_segments"][0]["start_dxf"] == [
        516000000.0, 323000000.0,
    ]
    assert candidate["geometry_authority"] is False


def test_cv_candidate_rejects_nearby_perpendicular_segment():
    candidates = [{"start_px": [0, 0], "end_px": [100, 0]}]
    segments = [{
        "entity_handle": "BAD",
        "entity_type": "LINE",
        "source_block_handle": None,
        "source_block_name": None,
        "layer": "UNKNOWN",
        "start_px": [50, -10],
        "end_px": [50, 10],
        "start_dxf": [50, -10],
        "end_dxf": [50, 10],
    }]

    result = associate_cv_candidates(candidates, segments)

    assert result["associated_candidate_count"] == 0
    assert result["candidates"][0]["matched_dxf_segments"] == []


def test_dxf_review_candidates_deduplicate_hough_lines_and_keep_stable_id():
    segment = {
        "entity_handle": "4ADF3",
        "entity_type": "LINE",
        "source_block_handle": "2D659",
        "source_block_name": "B1层平面图",
        "layer": "A-临时墙线",
        "start_dxf": [100.0, 200.0],
        "end_dxf": [500.0, 200.0],
    }
    associated = [
        {"candidate_id": "cv_missing_000", "matched_dxf_segments": [segment]},
        {"candidate_id": "cv_missing_001", "matched_dxf_segments": [segment]},
    ]

    first = build_dxf_review_candidates(
        associated, {"cv_missing_001"})
    second = build_dxf_review_candidates(list(reversed(associated)))

    assert first["candidate_count"] == 1
    assert first["needs_review_count"] == 1
    assert first["rejected_by_rule_count"] == 0
    assert first["yolo_supported_candidate_count"] == 1
    candidate = first["candidates"][0]
    assert candidate["status"] == "NEEDS_REVIEW"
    assert candidate["geometry_source"] == "DXF_VECTOR"
    assert candidate["evidence"]["cv_candidate_ids"] == [
        "cv_missing_000", "cv_missing_001",
    ]
    assert candidate["candidate_id"] == second["candidates"][0]["candidate_id"]


def test_dxf_floor_boundary_is_retained_but_not_sent_for_wall_review():
    associated = [{
        "candidate_id": "cv_missing_000",
        "matched_dxf_segments": [{
            "entity_handle": "SLAB1",
            "entity_type": "LWPOLYLINE",
            "source_block_handle": "PLAN1",
            "source_block_name": "B1层平面图",
            "layer": "B1_降板区 板边$0$C-结构降板",
            "start_dxf": [0.0, 0.0],
            "end_dxf": [1000.0, 0.0],
        }],
    }]

    result = build_dxf_review_candidates(associated)

    assert result["candidate_count"] == 1
    assert result["needs_review_count"] == 0
    assert result["rejected_by_rule_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["status"] == "REJECTED_BY_RULE"
    assert candidate["decision_reason"] == "floor_or_slab_boundary"


def test_structurally_named_slab_layer_is_audited_as_non_wall_not_suspicious():
    layer = "B1_降板区 板边$0$C-结构降板2"

    assert known_non_wall_reason(layer) == "floor_or_slab_boundary"
    assert is_structurally_named_non_wall_layer(layer) is True
    assert is_suspicious_layer(layer) is False
    assert is_structurally_named_non_wall_layer("A-CAR-SITE") is False


def test_wall_source_review_overlay_renders_exact_gap_evidence(tmp_path):
    walls = [{"start": [0.0, 0.1], "end": [4.0, 0.1]}]
    source_coverage = {
        "uncovered_source_segments": [{
            "start": [4.0, 0.0],
            "end": [6.0, 0.0],
            "entity_handle": "FACE1",
            "decision_reason": "possible_opening_gap",
            "unmapped_ranges": [{
                "start": [4.0, 0.0],
                "end": [6.0, 0.0],
                "length_m": 2.0,
                "opening_bridge_supported": True,
            }],
        }],
        "partially_mapped_source_segments": [],
    }
    output = tmp_path / "review.png"

    result = render_wall_source_review_overlay(
        walls, source_coverage, output)

    assert result["status"] == "OK"
    assert result["possible_opening_count"] == 1
    assert result["possible_omission_count"] == 0
    assert output.stat().st_size > 0


def test_wall_endpoint_review_overlay_renders_local_candidate(tmp_path):
    walls = [
        {"id": "wall_1", "start": [0.0, 0.0], "end": [2.0, 0.0]},
        {"id": "support_1", "start": [2.5, -1.0], "end": [2.5, 1.0]},
    ]
    review = {
        "reason_counts": {"nearby_network_gap": 1},
        "candidates": [{
            "wall_id": "wall_1", "endpoint": "end", "point": [2.0, 0.0],
            "decision_reason": "nearby_network_gap",
            "nearest_network": {
                "distance_m": 0.5, "closest_point": [2.5, 0.0]},
        }],
    }
    output = tmp_path / "endpoint-review.png"

    result = render_wall_endpoint_review_overlay(walls, review, output)

    assert result["status"] == "OK"
    assert result["candidate_count"] == 1
    assert output.stat().st_size > 0


def test_closed_wall_profile_review_overlay_renders_exact_edges_and_counts(
        tmp_path):
    def candidate(handle, status, offset, blockers=None, identity=True):
        points = [[offset, 0.0], [offset + 4.0, 0.0],
                  [offset + 4.0, 0.2], [offset, 0.2]]
        roles = ["wall_face", "end_cap", "wall_face", "end_cap"]
        return {
            "candidate_id": f"closed_{handle}",
            "entity_handle": handle,
            "status": status,
            "thickness_mm": 200.0,
            "identity_is_complete": identity,
            "identity_limitations": ([] if identity else
                                     ["source_occurrence_id_missing"]),
            "linetype_evidence": {
                "effective_linetype": "CONTINUOUS", "resolved": True},
            "blockers": blockers or [],
            "points": points,
            "centerline": {
                "start": [offset, 0.1], "end": [offset + 4.0, 0.1]},
            "child_evidence": [{
                "segment_index": index,
                "edge_role": roles[index],
                "start": points[index],
                "end": points[(index + 1) % 4],
            } for index in range(4)],
        }

    audit = {"candidates": [
        candidate("APPROVED-1", "APPROVED_BY_RULE", 0.0),
        candidate("REVIEW-1", "NEEDS_REVIEW", 10.0,
                  ["dash_hidden_pattern_conflict"], identity=False),
    ]}
    walls = [
        {"start": [0.0, 0.1], "end": [4.0, 0.1]},
        {"start": [10.0, 0.1], "end": [14.0, 0.1]},
    ]
    output = tmp_path / "closed-wall-profile-review.png"

    result = render_closed_wall_profile_review_overlay(walls, audit, output)

    assert result == {
        "status": "OK",
        "path": str(output),
        "candidate_count": 2,
        "approved_by_rule_count": 1,
        "needs_review_count": 1,
        "rejected_by_rule_count": 0,
        "review_count": 1,
        "wall_face_count": 4,
        "end_cap_count": 4,
        "cap_count": 4,
    }
    assert output.stat().st_size > 0


def test_closed_wall_profile_review_overlay_requires_candidates(tmp_path):
    with pytest.raises(ValueError, match="no closed wall-strip candidates"):
        render_closed_wall_profile_review_overlay(
            [], {"candidates": []}, tmp_path / "empty.png")


def test_irregular_profile_review_overlay_renders_exact_polygon(tmp_path):
    columns = [{"center": [0.0, 0.0], "size": [0.8, 0.8]}]
    walls = [{"id": "shear_1", "start": [-2.0, 0.0], "end": [2.0, 0.0]}]
    audit = {"reason_counts": {"matched_structural_wall_hatch": 1},
             "raw_candidate_count": 2, "candidates": [{
                 "candidate_id": "profile_1", "entity_handle": "AB12",
                 "points_local_m": [[-1.0, -0.2], [1.0, -0.2],
                                    [1.0, 0.2], [0.2, 0.2],
                                    [0.2, 1.0], [-0.2, 1.0],
                                    [-0.2, 0.2], [-1.0, 0.2]],
                 "width_mm": 2000.0, "depth_mm": 1200.0,
                 "structural_wall_support_ratio": 0.75,
                 "decision_reason": "matched_structural_wall_hatch",
             }]}
    output = tmp_path / "irregular-profile-review.png"

    result = render_irregular_profile_review_overlay(
        columns, walls, audit, output)

    assert result["status"] == "OK"
    assert result["candidate_count"] == 1
    assert output.stat().st_size > 0


def test_full_audit_cache_reuses_evidence_but_recalculates_gate_and_misses_on_input(
        tmp_path, monkeypatch):
    source = tmp_path / "hotel_b01.dxf"
    source.write_bytes(b"drawing-version-1")
    output_dir = tmp_path / "audit"
    cache_dir = tmp_path / "cache"
    calls = {"render": 0, "decision": 0}
    opened_document = object()

    monkeypatch.setenv("DRAWING_AUDIT_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv(
        "DRAWING_YOLO_MODEL_PATH", str(tmp_path / "missing-yolo-model.pt"))

    def fake_compute(current_source, current_output, document):
        assert document is opened_document
        calls["render"] += 1
        monkeypatch.setenv(
            "YOLO_CONFIG_DIR",
            str(audit_module.PROJECT_ROOT / "data" / "runtime"),
        )
        current_output.mkdir(parents=True, exist_ok=True)
        for filename in audit_module.AUDIT_ARTIFACT_FILES.values():
            (current_output / filename).write_bytes(
                f"pixels-{calls['render']}-{filename}".encode())
        return {
            "source": str(current_source),
            "source_sha256": audit_module._file_sha256(current_source),
            "source_size_bytes": current_source.stat().st_size,
            "current_step0": {"kept_after_duplicates": 10},
            "audit": {
                "warnings": [],
                "suspicious_unclassified_layers": [],
                "layers": [],
            },
            "preview_error": None,
            "cv_auxiliary": {
                "status": "PASS",
                "candidates": [],
                "vector_association": {"unassociated_candidate_count": 0},
            },
            "plan_selection": {"selected": {"name": "B1层平面图"}},
            "yolo_wall_audit": {
                "inference_status": "DISABLED",
                "geometry_authority": False,
            },
            "yolo_cv_fusion": {"candidates": []},
            "dxf_review_candidates": {
                "status": "PASS", "needs_review_count": 0,
                "candidates": [],
            },
            "known_non_wall_vector_candidates": {
                "status": "PASS", "candidates": [],
            },
            "render_transform": {"scale": 1.0},
        }

    original_decision = audit_module._build_cleaning_decision

    def tracked_decision(evidence):
        calls["decision"] += 1
        decision = original_decision(evidence)
        decision["test_decision_run"] = calls["decision"]
        return decision

    monkeypatch.setattr(audit_module, "_compute_audit_evidence", fake_compute)
    monkeypatch.setattr(audit_module, "_build_cleaning_decision", tracked_decision)

    first = audit_module.audit(
        source, output_dir, document=opened_document)
    second = audit_module.audit(
        source, output_dir, document=opened_document)

    assert first["audit_cache"]["state"] == "MISS"
    assert second["audit_cache"]["state"] == "HIT"
    assert calls == {"render": 1, "decision": 2}
    assert second["cleaning_decision"]["allow_modeling"] is True
    assert second["cleaning_decision"]["test_decision_run"] == 2
    cache_record = json.loads(next(cache_dir.glob("*.json")).read_text("utf-8"))
    assert "cleaning_decision" not in cache_record["evidence"]
    assert "allow_modeling" not in json.dumps(cache_record["evidence"])

    source.write_bytes(b"drawing-version-2")
    third = audit_module.audit(
        source, output_dir, document=opened_document)

    assert third["audit_cache"]["state"] == "MISS"
    assert calls == {"render": 2, "decision": 3}
    assert third["source_sha256"] != first["source_sha256"]


def test_pipeline_step0_passes_the_already_open_document_to_full_audit(
        tmp_path, monkeypatch):
    import scripts.pipeline_std_runner as runner

    source = tmp_path / "hotel_b01.dxf"
    source.write_bytes(b"drawing")

    class OpenDocument:
        filename = str(source)

    document = OpenDocument()
    received = {}
    monkeypatch.setenv("DRAWING_FULL_CV_AUDIT", "1")
    monkeypatch.setenv("DRAWING_CLEANING_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(runner, "_original_step0", lambda value: {
        "drawing_id": value.filename,
    })

    def fake_audit(current_source, output_dir, document=None):
        received.update({
            "source": current_source,
            "output_dir": output_dir,
            "document": document,
        })
        return {
            "source": str(current_source),
            "source_sha256": "a" * 64,
            "source_size_bytes": current_source.stat().st_size,
            "cv_auxiliary": {},
            "yolo_wall_audit": {},
            "yolo_cv_fusion": {},
            "dxf_review_candidates": {},
            "known_non_wall_vector_candidates": {},
            "cleaning_decision": {
                "status": "PASS", "allow_modeling": True, "reasons": [],
            },
        }

    monkeypatch.setattr(audit_module, "audit", fake_audit)

    result = runner.audited_step0(document)

    assert received["document"] is document
    assert received["source"] == source
    assert result["full_drawing_audit_status"] == "OK"


def test_unplaced_suspicious_layer_is_inventory_not_a_modeling_blocker(
        tmp_path, monkeypatch):
    document = ezdxf.new()
    plan = document.blocks.new("B1层墙柱(-6.5~-0.1m)")
    plan.add_line((0, 0), (4000, 0), dxfattribs={"layer": "S-WALL"})
    plan.add_line((0, 300), (4000, 300),
                  dxfattribs={"layer": "S-WALL"})
    unused = document.blocks.new("UNPLACED_STRUCTURAL_DETAIL")
    unused.add_line((0, 0), (4000, 0), dxfattribs={"layer": "砼墙"})
    document.modelspace().add_blockref("B1层墙柱(-6.5~-0.1m)", (0, 0))
    source = tmp_path / "structure.dxf"
    document.saveas(source)
    reloaded = ezdxf.readfile(source)
    monkeypatch.setattr(
        audit_module, "run_yolo_drawing_audit",
        lambda *args, **kwargs: {
            "inference_status": "DISABLED",
            "geometry_authority": False,
            "detections": [],
        })
    monkeypatch.delenv("DRAWING_YOLO_REQUIRED", raising=False)

    result = audit_module._compute_audit_evidence(
        source, tmp_path / "audit", reloaded)
    decision = audit_module._build_cleaning_decision(result)

    assert [item["layer"] for item in
            result["audit"]["all_block_suspicious_unclassified_layers"]] == [
                "砼墙"]
    assert result["audit"]["suspicious_unclassified_layers"] == []
    assert [item["layer"] for item in
            result["audit"]["unplaced_suspicious_unclassified_layers"]] == [
                "砼墙"]
    assert decision["allow_modeling"] is True


def test_selected_plan_suspicious_layer_still_blocks_modeling(
        tmp_path, monkeypatch):
    document = ezdxf.new()
    plan = document.blocks.new("B1层墙柱(-6.5~-0.1m)")
    plan.add_line((0, 0), (4000, 0), dxfattribs={"layer": "S-WALL"})
    plan.add_line((0, 300), (4000, 300),
                  dxfattribs={"layer": "S-WALL"})
    plan.add_line((0, 600), (4000, 600), dxfattribs={"layer": "砼墙"})
    document.modelspace().add_blockref("B1层墙柱(-6.5~-0.1m)", (0, 0))
    source = tmp_path / "structure.dxf"
    document.saveas(source)
    reloaded = ezdxf.readfile(source)
    monkeypatch.setattr(
        audit_module, "run_yolo_drawing_audit",
        lambda *args, **kwargs: {
            "inference_status": "DISABLED",
            "geometry_authority": False,
            "detections": [],
        })
    monkeypatch.delenv("DRAWING_YOLO_REQUIRED", raising=False)

    result = audit_module._compute_audit_evidence(
        source, tmp_path / "audit", reloaded)
    decision = audit_module._build_cleaning_decision(result)

    assert [item["layer"] for item in
            result["audit"]["suspicious_unclassified_layers"]] == ["砼墙"]
    assert decision["allow_modeling"] is False
    assert decision["reasons"] == [
        "1 structurally named layers were not kept"]
