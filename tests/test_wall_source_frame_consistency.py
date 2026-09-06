"""Regression tests for the independent source-render frame."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.engines.wall_pipeline.audit import (
    _transformed_source_segments,
    source_geometry_bounds,
)
from backend.engines.wall_pipeline.contracts import (
    SourceEntities,
    SourceEntity,
    WallEvidence,
)
from workers.revit_bridge.wall_compiler import compile_wall_model_script


def _source_entities() -> SourceEntities:
    return SourceEntities(
        tenant_id="tenant-frame-test",
        project_id="project-frame-test",
        manifest_sha256="a" * 64,
        source_units={"source_0001": "mm"},
        entities=[
            SourceEntity(
                entity_id="line",
                source_file_id="source_0001",
                kind="line",
                geometry={"start": (1000.0, 2000.0), "end": (3000.0, 2000.0)},
                locator="entity:line",
                frame_id="source_0001",
            ),
            SourceEntity(
                entity_id="polyline",
                source_file_id="source_0001",
                kind="polyline",
                geometry={
                    "points": [(3000.0, 2000.0), (3000.0, 5000.0), (5000.0, 5000.0)]
                },
                locator="entity:polyline",
                frame_id="source_0001",
            ),
            # Point-only entities are retained for review, but the renderer
            # does not draw them and they must not expand the audit frame.
            SourceEntity(
                entity_id="text",
                source_file_id="source_0001",
                kind="text",
                geometry={"point": (100000.0, 100000.0), "text": "far away"},
                locator="entity:text",
                frame_id="source_0001",
            ),
        ],
    )


def _evidence() -> WallEvidence:
    return WallEvidence(
        tenant_id="tenant-frame-test",
        project_id="project-frame-test",
        source_entities_sha256="b" * 64,
        transform_chain=[
            {"operation": "unit_convert", "scale_to_m_by_source": {"source_0001": 0.001}},
            {"operation": "translate_origin", "source_origin": (1000.0, 1000.0)},
            {"operation": "rotate", "rotation_deg": 90.0},
            {
                "operation": "translate",
                "translation_m": (10.0, -2.0),
                "frame_offsets_m": {},
            },
        ],
        items=[],
    )


def test_source_bounds_and_renderer_share_the_same_segments():
    entities = _source_entities()
    evidence = _evidence()

    segments = _transformed_source_segments(entities, evidence)
    assert len(segments) == 3
    expected = (
        min(point[0] for segment in segments for point in segment),
        min(point[1] for segment in segments for point in segment),
        max(point[0] for segment in segments for point in segment),
        max(point[1] for segment in segments for point in segment),
    )
    assert source_geometry_bounds(entities, evidence) == pytest.approx(expected)
    # The far-away point-only text entity is not a rendered segment and must
    # not influence the source frame.
    assert expected[2] < 20.0
    assert expected[3] == pytest.approx(2.0)


def test_compiler_projects_all_source_frame_corners_before_crop():
    compiled = {
        "project": {
            "project_id": "project-frame-test",
            "tenant_id": "tenant-frame-test",
            "levels": [{"id": "level-1", "name": "Main", "elevation": 0.0}],
        },
        "build": {"floor_code": "L1", "build_id": "build-frame"},
        "coordinate_system": {
            "offset_policy": "revit_project_base_point",
            "source_bounds_m": [0.0, 0.0, 3.0, 2.0],
            "transform_chain": [],
        },
        "model_elements": [],
        "grids": [],
        "junctions": [],
    }
    script = compile_wall_model_script(
        compiled, actual_prefix=Path("actual"), expected_wall_count=0
    )
    assert "source_corners_mm" in script
    assert "projected_corners_mm = [_project_xy(point)" in script
    assert "source_bounds_ft = [" in script
    assert script.index("def _project_xy") < script.index("source_corners_mm")
