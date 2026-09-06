import json
from pathlib import Path

import pytest

from scripts.compare_pdf_model_walls import (
    Face,
    _clip_wall_to_bounds,
    _linear_fit,
    _reference_layer_family,
    load_model_preview,
    merge_wall_centerlines,
    pair_wall_faces,
)


def test_linear_fit_recovers_grid_scale_and_offset():
    scale, offset = _linear_fit([(0.0, 100.0), (9.0, 355.15), (18.0, 610.30)])

    assert scale == pytest.approx(28.35)
    assert offset == pytest.approx(100.0)


def test_pair_wall_faces_finds_standard_wall_and_ignores_single_face():
    faces = [
        Face("horizontal", 10.0, 0.0, 5.0, "B1|A-WALL-S"),
        Face("horizontal", 10.2, 0.2, 4.8, "B1|A-WALL-S"),
        Face("horizontal", 12.0, 0.0, 5.0, "B1|A-WALL-S"),
    ]

    walls = pair_wall_faces(faces)

    assert len(walls) == 1
    assert walls[0]["start"] == pytest.approx([0.2, 10.1])
    assert walls[0]["end"] == pytest.approx([4.8, 10.1])
    assert walls[0]["thickness_m"] == pytest.approx(0.2)


def test_merge_wall_centerlines_removes_sheet_overlap_and_joins_small_gap():
    walls = [
        {"start": [0.0, 1.0], "end": [5.0, 1.0], "thickness_m": 0.2,
         "source_layers": ["A-WALL-S"], "source_sheet_ids": ["201"]},
        {"start": [4.0, 1.005], "end": [8.0, 1.005], "thickness_m": 0.2,
         "source_layers": ["A-WALL-S"], "source_sheet_ids": ["202"]},
        {"start": [8.05, 1.0], "end": [10.0, 1.0], "thickness_m": 0.2,
         "source_layers": ["A-WALL-S"], "source_sheet_ids": ["202"]},
    ]

    merged = merge_wall_centerlines(walls)

    assert len(merged) == 1
    assert merged[0]["start"] == pytest.approx([0.0, 1.0], abs=0.01)
    assert merged[0]["end"] == pytest.approx([10.0, 1.0], abs=0.01)
    assert merged[0]["source_sheet_ids"] == ["201", "202"]


def test_load_model_preview_converts_millimetres_to_metres(tmp_path: Path):
    path = tmp_path / "preview.json"
    path.write_text(json.dumps({
        "model_elements": [{
            "type": "Wall", "id": "w1", "start": [1000, 2000, 0],
            "end": [4000, 2000, 0], "thickness": 250, "paired": True,
        }]
    }), encoding="utf-8")

    walls = load_model_preview(path)

    assert walls[0]["start"] == [1.0, 2.0]
    assert walls[0]["end"] == [4.0, 2.0]
    assert walls[0]["thickness"] == 250.0


def test_clip_wall_to_bounds_prevents_sheet_overlay_overrun():
    wall = {"id": "w1", "start": [-5.0, 2.0], "end": [15.0, 2.0]}

    clipped = _clip_wall_to_bounds(wall, (0.0, 10.0, 0.0, 5.0))

    assert clipped is not None
    assert clipped["start"] == [0.0, 2.0]
    assert clipped["end"] == [10.0, 2.0]


def test_reference_layer_family_prefers_structural_provenance():
    assert _reference_layer_family({
        "source_layers": ["B1|A-WALL-S", "B1|S-WALL"],
    }) == "structural_wall"
