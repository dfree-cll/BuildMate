"""Evidence-to-cut contracts and generated Revit code, without a live Revit."""
import ast
from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from shapely.geometry import Polygon

from backend.engines.wall_pipeline.contracts import (
    ColumnRecord, EvidenceSourceRef, OpeningRecord, WallPipelineConfig, WallRecord,
)
from backend.engines.wall_pipeline.geometry import _column_specification_geometry, _drawing_type_name
from backend.engines.wall_pipeline.opening_specs import extract_opening_specs, resolve_opening_cuts, infer_opening_elevation_reference
from backend.engines.wall_pipeline.specifications import TextRow
from workers.pyrevit.wall_model_contract import _normalize_opening_cut
from workers.revit_bridge.wall_compiler import compile_wall_model_script


def _rows(sill="- 2.200m", rotate=False):
    values = [("洞口编号", (0, 0)), ("洞口尺寸", (3, 0)), ("洞底标高", (6, 0)),
              ("JD3", (0, 1)), ("1800X1500", (3, 1)), (sill, (6, 1))]
    return [TextRow(str(i), "source", "frame", 1, text, (point[1], -point[0]) if rotate else point)
            for i, (text, point) in enumerate(values)]


def _ref(key):
    return EvidenceSourceRef(source_file_id="source", entity_id=key, locator=key, frame_id="frame")


def _data():
    config = WallPipelineConfig(
        tenant_id="t", project_id="p",
        source={"type": "dxf", "files": [{"path": "plan.dxf", "role": "plan"}]},
        level={"id": "b1", "name": "B1", "elevation_m": -6.4, "wall_height_m": 6.4},
        opening={"elevation_reference": "project"},
    )
    wall = WallRecord(wall_id="w", start_m=(0, 0), end_m=(10, 0), thickness_m=.4,
                      height_m=6.4, level_id="b1", source_refs=[_ref("w")], evidence_ids=["w"], confidence=.99)
    opening = OpeningRecord(opening_id="o", mark="JD3", center_m=(5, 0),
                            boundary_m=[(4.1, -.2), (5.9, -.2), (5.9, .2), (4.1, .2)],
                            width_m=1.8, depth_m=.4, host_wall_ids=["w"], source_refs=[_ref("o")],
                            confidence=.99, status="matched")
    return config, wall, opening


def _resolve(config=None, wall=None, opening=None, rows=None):
    defaults = _data()
    config, wall, opening = config or defaults[0], wall or defaults[1], opening or defaults[2]
    rows = _rows() if rows is None else rows
    return resolve_opening_cuts([opening], [wall], rows, {row.entity_id: _ref(row.entity_id) for row in rows}, config)[0]


@pytest.mark.parametrize("rotate", [False, True])
def test_signed_schedule_in_both_orientations(rotate):
    specs = extract_opening_specs(_rows(rotate=rotate), alignment_m=.25, search_m=25)
    assert [(s.mark, s.width_mm, s.height_mm, s.sill_m) for s in specs] == [("JD3", 1800, 1500, -2.2)]


def test_drawing_note_selects_project_datum_without_manual_dropdown():
    rows = _rows() + [TextRow("datum", "source", "frame", 1,
        "说明：洞口中心相对标高改为标注洞底相对标高（相对于±0.000）。", (8, 8))]
    reference, notes = infer_opening_elevation_reference(rows)
    assert reference == "project" and [row.entity_id for row in notes] == ["datum"]
    config, wall, opening = _data()
    config.opening.elevation_reference = "unresolved"
    result = _resolve(config, wall, opening, rows)
    assert result.cut_status == "ready" and result.base_elevation_m == pytest.approx(-2.2)
    assert any(ref.entity_id == "datum" for ref in result.specification_refs)


def test_unsigned_basement_table_value_uses_drawing_datum_and_becomes_negative():
    config, wall, opening = _data()
    rows = _rows("2.200m") + [TextRow("datum", "source", "frame", 1,
        "说明：图集中标注洞口中心相对标高改为标注洞底相对标高（相对于±0.000）。", (8, 8))]
    result = _resolve(config, wall, opening, rows)
    assert result.cut_status == "ready"
    assert result.base_elevation_m == pytest.approx(-2.2)
    assert result.elevation_source == "drawing_inferred_basement"


def test_unrelated_numbers_and_missing_headers_are_not_evidence():
    assert _resolve(rows=_rows()[3:]).cut_status == "review_required"
    rows = _rows() + [TextRow("extra", "source", "frame", 1, "-1.500m", (6, 1.1))]
    assert _resolve(rows=rows).cut_status == "review_required"


def test_schedule_respects_configured_opening_mark_pattern():
    config, _, opening = _data()
    config.opening.label_pattern = r"D\d+"
    opening.mark = "D3"
    rows = [replace(row, text="D3" if row.text == "JD3" else row.text) for row in _rows()]
    assert _resolve(config=config, opening=opening, rows=rows).type_name == "D3-1800x1500mm"


def test_cut_endpoints_follow_rotated_host_not_page_axes():
    config, wall, opening = _data()
    angle = .4
    def rotate(point):
        x, y = point
        return (x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle))
    wall.end_m = rotate(wall.end_m)
    opening.boundary_m = [rotate(point) for point in opening.boundary_m]
    opening.center_m = rotate(opening.center_m)
    result = _resolve(config, wall, opening)
    assert result.cut_status == "ready"
    assert result.cut_start_m == pytest.approx(rotate((4.1, 0)))
    assert result.cut_end_m == pytest.approx(rotate((5.9, 0)))


def test_duplicate_marks_with_conflicting_schedules_are_not_cut():
    rows = _rows() + [replace(row, entity_id="other_" + row.entity_id, page_no=2)
                       for row in _rows("-3.000m")]
    assert _resolve(rows=rows).cut_status == "review_required"


def test_ready_cut_uses_project_datum_and_vertical_size_not_plan_depth():
    result = _resolve()
    assert result.cut_status == "ready"
    assert result.type_name == "JD3-1800x1500mm"
    assert result.base_elevation_m == -2.2
    assert result.top_elevation_m == pytest.approx(-.7)
    assert result.cut_start_m == pytest.approx((4.1, 0))
    assert result.cut_end_m == pytest.approx((5.9, 0))
    assert len(result.specification_refs) == 6
    assert result.elevation_source == "drawing"


def test_positive_extraction_is_not_silently_negated_but_operator_can_correct():
    config, wall, opening = _data()
    assert _resolve(rows=_rows("2.200m")).cut_status == "review_required"
    config.opening.sill_elevation_overrides_m = {"JD3": -2.2}
    result = _resolve(config=config, rows=_rows("2.200m"))
    assert result.cut_status == "ready" and result.elevation_source == "input"
    assert result.base_elevation_m == -2.2


@pytest.mark.parametrize("reference,expected", [("project", -2.2), ("level", -4.2)])
def test_datum_is_explicit(reference, expected):
    config, _, _ = _data()
    config.opening.elevation_reference = reference
    result = _resolve(config=config, rows=_rows("2.200m" if reference == "level" else "-2.200m"))
    assert result.cut_status == "ready"
    assert result.base_elevation_m == pytest.approx(expected)


@pytest.mark.parametrize("condition", ["datum", "host", "boundary", "width", "end", "height"])
def test_unsafe_openings_remain_pending(condition):
    config, wall, opening = _data()
    if condition == "datum": config.opening.elevation_reference = "unresolved"
    if condition == "host": opening.host_wall_ids = []
    if condition == "boundary": opening.boundary_m = []
    if condition == "width": opening.boundary_m = [(4, -.2), (6, -.2), (6, .2), (4, .2)]
    if condition == "end": wall.end_m = (5.9, 0)
    if condition == "height": wall.height_m = 3
    result = _resolve(config, wall, opening)
    assert result.cut_status == "review_required" and result.limitations


def test_duplicate_physical_cuts_are_rejected():
    config, wall, opening = _data()
    rows = _rows()
    result = resolve_opening_cuts([opening, opening.model_copy(update={"opening_id": "other"})], [wall], rows,
                                  {row.entity_id: _ref(row.entity_id) for row in rows}, config)
    assert all(item.cut_status == "review_required" for item in result)


def test_ready_schema_rejects_invented_size_or_evidence():
    data = _resolve().model_dump()
    with pytest.raises(ValidationError):
        OpeningRecord.model_validate({**data, "top_elevation_m": 0})
    with pytest.raises(ValidationError):
        OpeningRecord.model_validate({**data, "specification_refs": [_ref("fake")]})


def test_ironpython_handoff_preserves_exact_mm_and_checks_host():
    config, wall, _ = _data()
    data = _resolve().model_dump(mode="json")
    result = _normalize_opening_cut(data, [wall.model_dump()], config.level.model_dump())
    assert result["base_elevation"] == -2200
    assert result["top_elevation"] == pytest.approx(-700)
    assert result["cut_height"] == 1500
    with pytest.raises(ValueError, match="outside its host"):
        _normalize_opening_cut({**data, "cut_start_m": [4.1, 1], "cut_end_m": [5.9, 1]},
                               [wall.model_dump()], config.level.model_dump())


def test_common_drawing_naming_uses_integer_specs_not_measured_decimals():
    assert _drawing_type_name("Q1", {"thickness_mm": 500}, ("thickness_mm",), "w") == "Q1-500mm"
    for mark in ("KZ1", "LL1"):
        assert _drawing_type_name(mark, {"width_mm": 500., "depth_mm": 600.},
                                  ("width_mm", "depth_mm"), "c") == mark + "-500x600mm"
    first = _drawing_type_name("KZ1", {}, ("width_mm", "depth_mm"), "c1")
    assert "规格待核定" in first and first != _drawing_type_name("KZ1", {}, ("width_mm", "depth_mm"), "c2")
    assert "规格待核定" in _drawing_type_name("Q1", {"thickness_mm": 495.3}, ("thickness_mm",), "w")


def test_rectangular_column_size_is_from_spec_and_keeps_rotation():
    angle = .4
    points = [(-.3001, -.2502), (.3001, -.2502), (.3001, .2502), (-.3001, .2502)]
    rotated = [(x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)) for x, y in points]
    column = ColumnRecord(column_id="c", center_m=(2, 5), profile_m=rotated,
                          width_m=.6002, depth_m=.5004, height_m=6.4, level_id="b1",
                          source_refs=[_ref("c")], confidence=.99)
    update = _column_specification_geometry(column, {"width_mm": 600, "depth_mm": 500})
    assert update["width_m"] == .6 and update["depth_m"] == .5
    assert Polygon(update["profile_m"]).area == pytest.approx(.3)
    assert column.center_m == (2, 5)
    column.profile_kind = "irregular"
    assert _column_specification_geometry(column, {"width_mm": 600, "depth_mm": 500}) == {}


def _script():
    return compile_wall_model_script({
        "project": {"project_id": "p", "tenant_id": "t", "levels": [{"id": "b1", "name": "B1", "elevation": -6400}]},
        "build": {"floor_code": "B1", "build_id": "b", "wall_model_sha256": "h"},
        "model_elements": [], "openings": [],
    }, actual_prefix=Path("actual"), expected_wall_count=0)


def _function(name, namespace):
    node = next(node for node in ast.parse(_script()).body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "generated", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("bad_readback", [False, True])
def test_generated_cut_calls_native_api_and_checks_readback(bad_readback):
    config, wall, _ = _data()
    cut = _normalize_opening_cut(_resolve().model_dump(), [wall.model_dump()], config.level.model_dump())
    item = {**cut, "id": "o", "mark": "JD3", "host_wall_ids": ["w"], "type_name": "JD3-1800x1500mm"}
    host = SimpleNamespace(Id=42)
    calls = []
    def new_opening(target, bottom, top):
        calls.append((target, bottom, top))
        return SimpleNamespace(Host=target, BoundaryRect=[bottom, SimpleNamespace(X=top.X, Y=top.Y, Z=top.Z + (1 if bad_readback else 0))])
    doc = SimpleNamespace(Create=SimpleNamespace(NewOpening=new_opening), Regenerate=lambda: None)
    namespace = dict(DATA={"openings": [item]}, MM_PER_FOOT=304.8, math=math,
                     _project_xy=lambda point: [point[0] + 1000, point[1] + 2000],
                     XYZ=lambda x, y, z: SimpleNamespace(X=x, Y=y, Z=z),
                     _set_opening_marker=lambda *args: "host_and_receipt",
                     TENANT_ID="t", PROJECT_ID="p", FLOOR_CODE="B1")
    function = _function("_create_opening_cuts", namespace)
    if bad_readback:
        with pytest.raises(Exception, match="read-back mismatch"): function(doc, {"w": host})
    else:
        created, own_marker_count = function(doc, {"w": host})
        assert len(created) == 1
        assert own_marker_count == 0
        assert calls[0][1].Z * 304.8 == pytest.approx(-2200)
        assert calls[0][1].X * 304.8 == pytest.approx(5100)


def test_generated_opening_marker_falls_back_to_verified_host_and_bridge_receipt():
    opening = SimpleNamespace()
    function = _function("_set_opening_marker", {
        "TENANT_ID": "t", "PROJECT_ID": "p", "FLOOR_CODE": "B1",
        "_try_set_comments": lambda *args: False,
        "_set_schedule_mark": lambda *args: False,
    })
    assert function(opening, {
        "id": "o", "mark": "JD3", "type_name": "JD3-1800x1500mm",
        "elevation_source": "drawing",
    }) == "host_and_receipt"


def test_generated_script_reports_opening_marker_storage():
    assert "BUILDMATE_OPENING_MARKERS_APPLIED own=%d host_receipt=%d" in _script()


def test_gray_presentation_does_not_change_independent_export():
    script = _script()
    compile(script, "generated", "exec")
    assert script.index("_create_opening_cuts(doc, wall_by_id)", script.index("opening_transaction =")) < script.index("doc.ExportImage(options)")
    assert "scope_ids.Add(opening.Id)" in script
    assert "created_openings,\n)" in script
    assert "PlanViewPlane.CutPlane" in script
    assert script.index("doc.ExportImage(options)") < script.index("gray_view_count = _apply_delivery_gray")
    assert 'name = "BM-3D-" + FLOOR_CODE' in script
    assert 'BuildMate - Delivery Gray' in script
    assert "created_walls + created_columns + created_beams + created_openings" in script


def test_plan_presentation_uses_black_edges_without_fill_while_3d_stays_gray():
    class Settings:
        def __init__(self):
            self.values = {}

        def __getattr__(self, name):
            def record(value):
                self.values[name] = value
            return record

    class View:
        def __init__(self, template=False):
            self.IsTemplate = template
            self.overrides = {}

        def SetElementOverrides(self, element_id, settings):
            self.overrides[element_id] = settings.values

    class Plan(View):
        pass

    class ThreeD(View):
        pass

    plan, three_d, template = Plan(), ThreeD(), Plan(template=True)
    pattern_class = type("FillPatternElement", (), {})
    pattern = SimpleNamespace(Id=10, GetFillPattern=lambda: SimpleNamespace(IsSolidFill=True))
    collector = SimpleNamespace(OfClass=lambda cls: [pattern] if cls is pattern_class else [plan, three_d, template])
    function = _function("_apply_delivery_gray", {
        "FilteredElementCollector": lambda doc: collector,
        "FillPatternElement": pattern_class,
        "OverrideGraphicSettings": Settings,
        "Color": lambda r, g, b: (r, g, b),
        "View": View, "ViewPlan": Plan, "View3D": ThreeD,
    })

    assert function(object(), [(SimpleNamespace(Id=42), {})]) == 2
    assert plan.overrides[42]["SetProjectionLineColor"] == (0, 0, 0)
    assert plan.overrides[42]["SetCutLineColor"] == (0, 0, 0)
    assert plan.overrides[42]["SetSurfaceForegroundPatternVisible"] is False
    assert plan.overrides[42]["SetCutForegroundPatternVisible"] is False
    for scope in ("Surface", "Cut"):
        for layer in ("Foreground",):
            assert three_d.overrides[42][f"Set{scope}{layer}PatternVisible"] is True
            assert three_d.overrides[42][f"Set{scope}{layer}PatternColor"] == (180, 180, 180)
    assert template.overrides == {}
