"""Revit 连接器必须以本次结构化结果为准，不能只看历史 RVT。"""
import json
import os
import time

from backend.engines import revit_connector as rc


class _Response:
    def __init__(self, payload=b'{"status":"done"}'):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


def test_revit_done_requires_matching_structured_result(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "RVT_OUT_DIR", str(tmp_path))
    build_id = "RVT-TEST1"

    def fake_open(*_, **__):
        result = {"build_id": build_id, "status": "done", "created": [],
                  "direct_shape_count": 0, "rvt_path": str(tmp_path / "new.rvt")}
        (tmp_path / f"build_{build_id}_result.json").write_text(
            json.dumps(result), encoding="utf-8")
        rvt = tmp_path / "new.rvt"
        rvt.write_bytes(b"rvt")
        future = time.time() + 1
        os.utime(rvt, (future, future))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    out = rc.RevitConnector.trigger_model_build(build_id=build_id)
    assert out["status"] == "done"
    assert out["build_result"]["build_id"] == build_id


def test_revit_rejects_failed_build_result(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "RVT_OUT_DIR", str(tmp_path))
    build_id = "RVT-TEST2"

    def fake_open(*_, **__):
        result = {"build_id": build_id, "status": "error",
                  "direct_shape_count": 3, "failures": []}
        (tmp_path / f"build_{build_id}_result.json").write_text(
            json.dumps(result), encoding="utf-8")
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    out = rc.RevitConnector.trigger_model_build(build_id=build_id)
    assert out["status"] == "error"
    assert out["build_result"]["direct_shape_count"] == 3


def test_revit_rejects_route_done_without_current_result(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "RVT_OUT_DIR", str(tmp_path))
    old = tmp_path / "old.rvt"
    old.write_bytes(b"old")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Response())
    out = rc.RevitConnector.trigger_model_build(
        build_id="RVT-TEST3", timeout=1)
    assert out["status"] == "error"
    assert "结构化结果" in out["message"]


def test_revit_accepts_valid_result_when_route_transport_times_out(
        tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "RVT_OUT_DIR", str(tmp_path))
    build_id = "RVT-TEST-TIMEOUT"

    def fake_open(*_, **__):
        rvt = tmp_path / "new.rvt"
        rvt.write_bytes(b"rvt")
        result = {"build_id": build_id, "status": "done", "created": [],
                  "direct_shape_count": 0, "rvt_path": str(rvt)}
        (tmp_path / f"build_{build_id}_result.json").write_text(
            json.dumps(result), encoding="utf-8")
        raise TimeoutError("route response timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    out = rc.RevitConnector.trigger_model_build(build_id=build_id, timeout=1)
    assert out["status"] == "done"
    assert "transport_error" in out["detail"]


def test_revit_script_cleanup_is_marker_scoped():
    script = (rc.os.path.join(rc.os.path.dirname(rc.os.path.dirname(
        rc.os.path.dirname(rc.os.path.abspath(rc.__file__)))),
        "workers", "pyrevit", "json2rvt_script.py"))
    text = open(script, encoding="utf-8").read()
    assert "BUILDMATE_AUTO:" in text
    assert "is_buildmate_auto(el, PROJECT_ID, FLOOR_CODE)" in text
    assert "for et in (Wall, Floor, FamilyInstance):" in text
    assert "for et in (Wall, Floor, Grid, FamilyInstance):" not in text
    assert "target_model_path" in text
    assert "请先在 Revit 中打开项目工作模型" in text
    assert "doc.Save()" in text
    assert "doc.SaveAs(out)" not in text
    assert 'TaskDialog.Show("JSON2RVT Done"' not in text


def test_revit_script_anchors_new_models_to_project_base_point():
    root = rc.os.path.dirname(rc.os.path.dirname(rc.os.path.dirname(
        rc.os.path.abspath(rc.__file__))))
    script = rc.os.path.join(root, "workers", "pyrevit", "json2rvt_script.py")
    text = open(script, encoding="utf-8").read()

    assert "BuiltInCategory.OST_ProjectBasePoint" in text
    assert "base_point.Position" in text
    assert 'if policy == "revit_project_base_point":' in text
    assert 'return candidate, "revit_project_base_point"' in text
    assert "无法读取 Revit 项目基点，已停止建模" in text
    assert 'policy = "revit_project_base_point"' in text


def test_standard_pipeline_requests_project_base_point_placement():
    root = rc.os.path.dirname(rc.os.path.dirname(rc.os.path.dirname(
        rc.os.path.abspath(rc.__file__))))
    pipeline = rc.os.path.join(root, "scripts", "bim_pipeline",
                               "pipeline_std.py")
    text = open(pipeline, encoding="utf-8").read()

    assert "'offset_policy': 'revit_project_base_point'" in text
    assert "'project_offset_mm': None" in text


def test_retired_drawing_pipeline_is_not_reintroduced():
    root = rc.os.path.dirname(rc.os.path.dirname(rc.os.path.dirname(
        rc.os.path.abspath(rc.__file__))))
    pipeline = rc.os.path.join(root, "scripts", "drawing_pipeline.py")
    assert not rc.os.path.exists(pipeline)


def test_revit_route_does_not_call_self_running_main_twice():
    connector = open(rc.__file__, encoding="utf-8").read()
    assert '{"script_path": script, "call": ""}' in connector
    assert '{"script_path": script, "call": "main"}' not in connector


def test_revit_wall_ids_stay_bound_to_sources_and_joins_are_enabled():
    root = rc.os.path.dirname(rc.os.path.dirname(rc.os.path.dirname(
        rc.os.path.abspath(rc.__file__))))
    script = rc.os.path.join(root, "workers", "pyrevit", "json2rvt_script.py")
    text = open(script, encoding="utf-8").read()

    assert "wall_records.append((created, e))" in text
    assert "for wid, source_wall in wall_records:" in text
    assert "zip(wall_ids, source_walls)" not in text
    assert "JoinGeometryUtils.JoinGeometry(doc, walls[i], walls[j])" in text
    assert "BuildMate Wall Topology" in text
    assert "transverse_offset > 2.0 / MM" in text
    assert "direction_dot > 0.0175" in text
    assert 'wi_axis = "H" if abs(wi_dx) >= abs(wi_dy) else "V"' in text
    assert "Move each endpoint only along its own wall axis" in text
    assert "mid = [(a[k] + b[k]) / 2.0 for k in range(3)]" not in text
    assert "WallUtils.AllowWallJoinAtEnd(built_wall" not in text
    assert "wall auto-join disabled" not in text


def test_boundary_column_patch_replaces_geometry_for_existing_shapes():
    root = rc.os.path.dirname(rc.os.path.dirname(rc.os.path.dirname(
        rc.os.path.abspath(rc.__file__))))
    script = rc.os.path.join(root, "workers", "pyrevit",
                             "patch_boundary_columns_script.py")
    text = open(script, encoding="utf-8").read()
    existing_branch = text.split('if item["id"] in existing:', 1)[1].split(
        "continue", 1)[0]
    assert "shape.SetShape(geometry)" in existing_branch
    assert '"status": "geometry_updated"' in existing_branch


def test_boundary_columns_are_physical_families_that_cut_walls():
    root = rc.os.path.dirname(rc.os.path.dirname(rc.os.path.dirname(
        rc.os.path.abspath(rc.__file__))))
    script = rc.os.path.join(root, "workers", "pyrevit", "json2rvt_script.py")
    text = open(script, encoding="utf-8").read()
    profile_branch = text.split('def create_column(doc, e, level):', 1)[1].split(
        'sym, sym_err = resolve_sized_family_symbol', 1)[0]

    assert "prepare_boundary_column_families(elements)" in text
    assert '"profile-family"' in profile_branch
    assert '"void-cutter"' in text
    assert "DirectShape.CreateElement" not in profile_branch
    assert "cut_boundary_columns_from_walls" in text
    assert "FAMILY_ALLOW_CUT_WITH_VOIDS" in text
    assert "not bool(is_void), profiles" in text
    assert "InstanceVoidCutUtils.AddInstanceVoidCut" in text
    assert "InstanceVoidCutUtils.InstanceVoidCutExists" in text
    assert "InstanceVoidCutUtils.IsVoidInstanceCuttingElement" in text
    assert "_elements_have_positive_solid_overlap" in text
    assert "source_only_pairs" in text
    assert "candidate_pairs" in text
    assert "is_buildmate_boundary_family_instance" in text
    assert "BOUNDARY_COLUMN_CUTTER_TARGETS" in text
    assert "count_persisted_boundary_wall_cuts" in text
    assert "physical_cut_post_commit" in text
    assert "_orthogonal_profile_points" in text
    assert "_DeleteNonFatalFamilyWarnings" in text
    assert "accessor.DeleteWarning(message)" in text
    assert "ElementTransformUtils.MoveElement" in profile_branch
    assert "profile column base geometry misaligned" in profile_branch


def test_revit_connector_waits_for_queued_build_result_without_retriggering():
    connector = open(rc.__file__, encoding="utf-8").read()
    assert "while (not os.path.isfile(result_path)" in connector
    assert "time.sleep(0.25)" in connector
