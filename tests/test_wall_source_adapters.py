"""Focused source-boundary tests for the generic PDF/DWG wall pipeline."""

from pathlib import Path

import ezdxf
import pytest

from backend.engines.wall_pipeline.adapters import (
    DXFSourceAdapter,
    PDFSourceAdapter,
    _ocr_pdf_text_entities,
    _walk_dxf_entities,
)
import backend.engines.wall_pipeline.adapters as source_adapters
import backend.engines.wall_pipeline.pipeline as wall_pipeline
from backend.engines.wall_pipeline.contracts import (
    ColumnRecognitionConfig,
    CoordinateConfig,
    GridAxisConfig,
    GridConfig,
    LevelConfig,
    RevitConfig,
    SourceEntities,
    SourceEntity,
    SourceFileConfig,
    SourceConfig,
    WallPipelineConfig,
    WallRecognitionConfig,
)
from backend.engines.wall_pipeline.geometry import build_wall_evidence, _infer_pdf_scale
from backend.engines.wall_pipeline.geometry import build_wall_model
from backend.engines.wall_pipeline.audit import render_original_source
import backend.engines.wall_pipeline.audit as wall_audit
from backend.engines.wall_pipeline.pipeline import _build_manifest


def _pdf_config(path: Path, *, scale_to_m: float = 0.02) -> WallPipelineConfig:
    return WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="pdf",
            files=[SourceFileConfig(path=str(path), role="plan")],
        ),
        coordinate=CoordinateConfig(scale_to_m=scale_to_m),
        wall=WallRecognitionConfig(
            min_thickness_m=0.075,
            max_thickness_m=0.60,
            min_length_m=0.30,
        ),
        level=LevelConfig(id="level-1", name="Main"),
        revit=RevitConfig(),
    )


def _text_entity(entity_id: str, text: str, point: tuple[float, float]) -> SourceEntity:
    return SourceEntity(
        entity_id=entity_id,
        source_file_id="source_0001",
        page_no=1,
        kind="text",
        geometry={"point": point, "text": text},
        locator=f"page:1/text:{entity_id}",
        frame_id="source_0001:page:0001",
    )


def test_pdf_scale_inference_prefers_primary_plan_title_over_detail_scale():
    entities = SourceEntities(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        manifest_sha256="a" * 64,
        source_units={"source_0001": "pt"},
        entities=[
            _text_entity("scale-main", "1:150", (100.0, 100.0)),
            _text_entity("title-main", "地下一层墙柱配筋平面图", (120.0, 100.0)),
            _text_entity("scale-detail", "游泳池机房外墙配筋平面 1:50", (800.0, 500.0)),
        ],
    )

    inference = _infer_pdf_scale(entities, "source_0001")

    assert inference is not None
    assert inference["denominator"] == 150
    assert inference["scale_to_m"] == pytest.approx(150 * 25.4 / 72 / 1000)
    assert inference["candidate_denominators"] == [150, 50]


def test_pdf_scale_inference_fails_when_top_candidates_are_ambiguous():
    entities = SourceEntities(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        manifest_sha256="a" * 64,
        source_units={"source_0001": "pt"},
        entities=[
            _text_entity("scale-100", "1:100", (0.0, 0.0)),
            _text_entity("scale-150", "1:150", (1000.0, 1000.0)),
        ],
    )

    with pytest.raises(ValueError, match="primary plan scale is ambiguous"):
        _infer_pdf_scale(entities, "source_0001")


def test_dxf_edge_hatch_is_preserved_for_irregular_column_profile(tmp_path: Path):
    """Placed HATCH edge paths must not disappear at the source boundary."""

    source = tmp_path / "edge-profile.dxf"
    document = ezdxf.new()
    document.header["$INSUNITS"] = 4
    document.layers.add("GEOMETRY-WALL")
    document.layers.add("S-WALL-HATC")
    modelspace = document.modelspace()
    hatch = modelspace.add_hatch(dxfattribs={"layer": "S-WALL-HATC"})
    edge_path = hatch.paths.add_edge_path()
    edge_path.add_line((0.0, 0.0), (800.0, 0.0))
    edge_path.add_line((800.0, 0.0), (800.0, 400.0))
    edge_path.add_line((800.0, 400.0), (400.0, 400.0))
    edge_path.add_line((400.0, 400.0), (400.0, 800.0))
    edge_path.add_line((400.0, 800.0), (0.0, 800.0))
    edge_path.add_line((0.0, 800.0), (0.0, 0.0))
    document.saveas(source)

    config = WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="dxf",
            files=[SourceFileConfig(path=str(source), role="plan")],
        ),
        coordinate=CoordinateConfig(source_unit="mm"),
        wall=WallRecognitionConfig(include_layers=["GEOMETRY-WALL"]),
        column=ColumnRecognitionConfig(profile_layers=["S-WALL-HATC"]),
        level=LevelConfig(id="level-1", name="Main"),
    )
    manifest = _build_manifest(config)
    entities = DXFSourceAdapter(config).extract(manifest, tmp_path / "output")

    profiles = [
        entity for entity in entities.entities
        if entity.kind == "hatch_boundary" and entity.layer == "S-WALL-HATC"
    ]
    assert len(profiles) == 1
    assert profiles[0].geometry["closed"] is True
    assert len(profiles[0].geometry["points"]) >= 7


def test_dxf_column_scope_preserves_closed_irregular_profile(tmp_path: Path):
    source = tmp_path / "irregular-column.dxf"
    document = ezdxf.new()
    document.header["$INSUNITS"] = 4
    document.layers.add("S-COLU")
    document.modelspace().add_lwpolyline(
        [(0, 0), (800, 0), (800, 400), (400, 400), (400, 800), (0, 800)],
        close=True,
        dxfattribs={"layer": "S-COLU"},
    )
    document.saveas(source)

    config = WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="dxf",
            files=[SourceFileConfig(path=str(source), role="plan")],
        ),
        coordinate=CoordinateConfig(source_unit="mm"),
        wall=WallRecognitionConfig(include_layers=["GEOMETRY-WALL"]),
        column=ColumnRecognitionConfig(include_layers=["S-COLU"]),
        level=LevelConfig(id="level-1", name="Main"),
    )
    manifest = _build_manifest(config)
    entities = DXFSourceAdapter(config).extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)
    model = build_wall_model(config, manifest, entities, evidence)

    assert len(model.columns) == 1
    assert model.columns[0].profile_kind == "irregular"
    assert len(model.columns[0].profile_m) == 6


def test_irregular_column_local_dimensions_do_not_conflict_with_profile_bounds(
    tmp_path: Path,
):
    """GBZ limb dimensions classify a profile but do not replace its boundary."""

    source = tmp_path / "irregular-column-spec.dxf"
    document = ezdxf.new()
    document.header["$INSUNITS"] = 4
    document.layers.add("S-COLU-HATC")
    document.layers.add("S-COLU-TEXT")
    modelspace = document.modelspace()
    modelspace.add_lwpolyline(
        [(0, 0), (2200, 0), (2200, 400), (900, 400), (900, 1400), (0, 1400)],
        close=True,
        dxfattribs={"layer": "S-COLU-HATC"},
    )
    modelspace.add_text(
        "GBZ7 1000x900",
        dxfattribs={"layer": "S-COLU-TEXT", "height": 200},
    ).set_placement((100, 100))
    document.saveas(source)

    config = WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="dxf",
            files=[SourceFileConfig(path=str(source), role="structural_plan")],
        ),
        coordinate=CoordinateConfig(source_unit="mm"),
        wall=WallRecognitionConfig(include_layers=["GEOMETRY-WALL"]),
        column=ColumnRecognitionConfig(
            profile_layers=["S-COLU-HATC"],
            label_layers=["S-COLU-TEXT"],
        ),
        level=LevelConfig(id="level-1", name="Main"),
    )
    manifest = _build_manifest(config)
    entities = DXFSourceAdapter(config).extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)
    model = build_wall_model(config, manifest, entities, evidence)

    assert len(model.columns) == 1
    column = model.columns[0]
    assert column.profile_kind == "irregular"
    assert column.width_m == pytest.approx(2.2)
    assert column.depth_m == pytest.approx(1.4)
    assert column.construction is not None
    assert column.construction.type_mark == "GBZ7"
    assert column.construction.specification_status == "resolved_from_annotation"
    assert column.construction.specification_mm == {
        "width_mm": 1000.0,
        "depth_mm": 900.0,
    }


def test_pdf_line_only_column_profile_is_polygonized_and_preserved(tmp_path: Path):
    """PDF LINE exports must not discard an irregular structural profile."""

    import pymupdf as fitz

    pdf = tmp_path / "line-profile.pdf"
    document = fitz.open()
    page = document.new_page(width=400, height=400)
    oc = document.add_ocg("S-COLU-HATC")
    points = [(100, 100), (180, 100), (180, 140), (140, 140),
              (140, 180), (100, 180), (100, 100)]
    for start, end in zip(points, points[1:]):
        page.draw_line(start, end, width=1, color=(0, 0, 0), oc=oc)
    document.save(pdf)
    document.close()

    config = _pdf_config(pdf, scale_to_m=0.01)
    config = config.model_copy(update={
        "column": ColumnRecognitionConfig(
            profile_layers=["S-COLU-HATC"],
            min_profile_area_m2=0.05,
            min_width_m=0.20,
            min_depth_m=0.20,
        ),
    })
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter(config).extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)
    model = build_wall_model(config, manifest, entities, evidence)

    irregular = [column for column in model.columns
                  if column.profile_kind == "irregular"]
    assert len(irregular) == 1
    assert len(irregular[0].profile_m) == 6
    assert irregular[0].source_refs


def test_pdf_line_only_triangular_column_profile_is_not_discarded(tmp_path: Path):
    """Three-sided structural profiles must reach Model IR and DirectShape."""

    import pymupdf as fitz

    pdf = tmp_path / "triangle-profile.pdf"
    document = fitz.open()
    page = document.new_page(width=400, height=400)
    oc = document.add_ocg("S-COLU-HATC")
    points = [(100, 100), (180, 100), (140, 180), (100, 100)]
    for start, end in zip(points, points[1:]):
        page.draw_line(start, end, width=1, color=(0, 0, 0), oc=oc)
    document.save(pdf)
    document.close()

    config = _pdf_config(pdf, scale_to_m=0.01)
    config = config.model_copy(update={
        "column": ColumnRecognitionConfig(
            profile_layers=["S-COLU-HATC"],
            min_profile_area_m2=0.05,
            min_width_m=0.20,
            min_depth_m=0.20,
        ),
    })
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter(config).extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)
    model = build_wall_model(config, manifest, entities, evidence)

    irregular = [column for column in model.columns
                  if column.profile_kind == "irregular"]
    assert len(irregular) == 1
    assert len(irregular[0].profile_m) == 3
    assert irregular[0].source_refs


def test_pdf_entities_use_page_local_y_up_frame_and_preserve_metadata(
    tmp_path: Path,
):
    import pymupdf as fitz

    pdf = tmp_path / "rotated-plan.pdf"
    document = fitz.open()
    page = document.new_page(width=200, height=120)
    page.set_rotation(90)
    page.draw_line((10, 30), (110, 30), width=1)
    page.draw_line((10, 40), (110, 40), width=1)
    document.save(pdf)
    document.close()

    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter().extract(manifest, tmp_path / "output")
    lines = [entity for entity in entities.entities if entity.kind == "line"]

    assert len(lines) == 2
    assert {entity.frame_id for entity in lines} == {"source_0001:page:0001"}
    # PyMuPDF reports the source drawing in a top-left/y-down frame.  The
    # adapter boundary converts it to the documented media-box y-up frame.
    assert lines[0].geometry["start"] == pytest.approx((10.0, 90.0))
    assert lines[0].geometry["end"] == pytest.approx((110.0, 90.0))
    metadata = lines[0].style
    assert metadata["coordinate_frame"] == "pdf_media_box_y_up"
    assert metadata["frame_id"] == "source_0001:page:0001"
    assert metadata["page_width_pt"] == pytest.approx(200.0)
    assert metadata["page_height_pt"] == pytest.approx(120.0)
    assert metadata["page_rotation_deg"] == 90
    assert metadata["page_media_box_pt"] == [0.0, 0.0, 200.0, 120.0]

    evidence = build_wall_evidence(config, manifest, entities)
    rotation = next(
        item for item in evidence.transform_chain
        if item["operation"] == "rotate"
    )
    assert rotation["rotation_deg"] == pytest.approx(-90.0)
    assert rotation["pdf_page_rotation_applied_deg"] == pytest.approx(-90.0)
    assert rotation["engineering_rotation_deg"] == pytest.approx(0.0)
    assert evidence.items[0].centerline_m[0][0] == pytest.approx(
        evidence.items[0].centerline_m[1][0]
    )


def test_pdf_ocr_tiles_map_rotated_pixels_back_to_media_box(
    tmp_path: Path, monkeypatch,
):
    """OCR text evidence must share the vector entity coordinate frame."""

    import pymupdf as fitz
    import backend.engines.pdf_parser as pdf_parser

    pdf = tmp_path / "rotated-ocr.pdf"
    document = fitz.open()
    page = document.new_page(width=200, height=120)
    page.set_rotation(270)
    document.save(pdf)
    document.close()

    class FakeOCR:
        def __call__(self, _image):
            return [[[
                [[6.0, 2.0], [14.0, 2.0], [14.0, 8.0], [6.0, 8.0]],
                "墙厚267",
                0.99,
            ]], None]

    monkeypatch.setattr(pdf_parser, "_get_ocr", lambda: FakeOCR())
    monkeypatch.setattr(source_adapters, "_PDF_OCR_TARGET_DPI", 72)
    monkeypatch.setattr(source_adapters, "_PDF_OCR_TILE_SIZE_PX", 10_000)

    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    document = fitz.open(pdf)
    try:
        page = document[0]
        entities, diagnostic = _ocr_pdf_text_entities(
            page,
            source=manifest.sources[0],
            page_index=0,
            page_height_pt=float(page.mediabox.height),
            frame_id="source_0001:page:0001",
            entity_offset=0,
        )
        expected_media = fitz.Point(10.0, 5.0) * page.derotation_matrix
    finally:
        document.close()

    assert len(entities) == 1
    assert entities[0].geometry["text"] == "墙厚267"
    assert entities[0].geometry["point"] == pytest.approx((
        expected_media.x,
        120.0 - expected_media.y,
    ))
    assert diagnostic is not None
    assert diagnostic["code"] == "PDF_OCR_TEXT_EXTRACTED"
    assert diagnostic["page_rotation_deg"] == 270


def test_pdf_ocr_overlap_deduplicates_same_text_evidence(
    tmp_path: Path, monkeypatch,
):
    import pymupdf as fitz
    import backend.engines.pdf_parser as pdf_parser

    pdf = tmp_path / "overlap-ocr.pdf"
    document = fitz.open()
    document.new_page(width=400, height=100)
    document.save(pdf)
    document.close()

    class FakeOCR:
        calls = 0

        def __call__(self, _image):
            self.calls += 1
            center_x = 200.0 if self.calls == 1 else 56.0
            return [[[
                [[center_x - 4.0, 20.0], [center_x + 4.0, 20.0],
                 [center_x + 4.0, 30.0], [center_x - 4.0, 30.0]],
                "Q1 267",
                0.95,
            ]], None]

    engine = FakeOCR()
    monkeypatch.setattr(pdf_parser, "_get_ocr", lambda: engine)
    monkeypatch.setattr(source_adapters, "_PDF_OCR_TARGET_DPI", 72)
    monkeypatch.setattr(source_adapters, "_PDF_OCR_TILE_SIZE_PX", 256)
    monkeypatch.setattr(source_adapters, "_PDF_OCR_TILE_OVERLAP_PX", 40)

    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    document = fitz.open(pdf)
    try:
        page = document[0]
        entities, diagnostic = _ocr_pdf_text_entities(
            page,
            source=manifest.sources[0],
            page_index=0,
            page_height_pt=float(page.mediabox.height),
            frame_id="source_0001:page:0001",
            entity_offset=0,
        )
    finally:
        document.close()

    assert engine.calls == 2
    assert len(entities) == 1
    assert entities[0].geometry["point"] == pytest.approx((200.0, 75.0))
    assert diagnostic is not None
    assert diagnostic["tile_count"] == 2


def test_pdf_ocr_cache_reuses_source_candidates_without_reinference(
    tmp_path: Path, monkeypatch,
):
    """Repeated uploads of the same source must not pay OCR twice."""

    import pymupdf as fitz
    import backend.engines.pdf_parser as pdf_parser

    pdf = tmp_path / "cached-ocr.pdf"
    document = fitz.open()
    page = document.new_page(width=400, height=100)
    page.insert_text((40, 40), "Q1 500")
    document.save(pdf)
    document.close()

    class FakeOCR:
        calls = 0

        def __call__(self, _image):
            self.calls += 1
            center_x = 200.0 if self.calls == 1 else 56.0
            return [[[
                [[center_x - 15.0, 10.0], [center_x + 15.0, 10.0],
                 [center_x + 15.0, 20.0], [center_x - 15.0, 20.0]],
                "Q1 500",
                0.99,
            ]], None]

    engine = FakeOCR()
    monkeypatch.setattr(pdf_parser, "_get_ocr", lambda: engine)
    monkeypatch.setattr(source_adapters, "_PDF_OCR_TARGET_DPI", 72)
    monkeypatch.setattr(source_adapters, "_PDF_OCR_TILE_SIZE_PX", 256)
    monkeypatch.setattr(source_adapters, "_PDF_OCR_TILE_OVERLAP_PX", 40)

    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    document = fitz.open(pdf)
    try:
        page = document[0]
        first, first_diagnostic = _ocr_pdf_text_entities(
            page,
            source=manifest.sources[0],
            page_index=0,
            page_height_pt=float(page.mediabox.height),
            frame_id="source_0001:page:0001",
            entity_offset=0,
            cache_dir=tmp_path / "ocr-cache",
        )
        second, second_diagnostic = _ocr_pdf_text_entities(
            page,
            source=manifest.sources[0],
            page_index=0,
            page_height_pt=float(page.mediabox.height),
            frame_id="source_0001:page:0001",
            entity_offset=10,
            cache_dir=tmp_path / "ocr-cache",
        )
    finally:
        document.close()

    assert engine.calls == 2  # the first pass sees the two overlapped tiles
    assert len(first) == len(second) == 1
    assert first_diagnostic is not None and not first_diagnostic["cache_hit"]
    assert second_diagnostic is not None and second_diagnostic["cache_hit"]
    assert first[0].geometry == second[0].geometry
    assert first[0].entity_id != second[0].entity_id


def test_pdf_mixed_page_rotations_fail_closed(tmp_path: Path):
    import pymupdf as fitz

    pdf = tmp_path / "mixed-rotation-plan.pdf"
    document = fitz.open()
    for rotation in (0, 90):
        page = document.new_page(width=200, height=120)
        page.set_rotation(rotation)
        page.draw_line((10, 30), (110, 30), width=1)
        page.draw_line((10, 40), (110, 40), width=1)
    document.save(pdf)
    document.close()

    config = _pdf_config(pdf)
    config = config.model_copy(update={
        "coordinate": config.coordinate.model_copy(update={
            "frame_offsets_m": {
                "source_0001:page:0001": (0.0, 0.0),
                "source_0001:page:0002": (5.0, 0.0),
            },
        }),
    })
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter(config).extract(manifest, tmp_path / "output")

    with pytest.raises(ValueError, match="mixed page rotations"):
        build_wall_evidence(config, manifest, entities)


def test_pdf_audit_render_always_reopens_original_page(tmp_path: Path, monkeypatch):
    import cv2
    import pymupdf as fitz

    pdf = tmp_path / "original-render.pdf"
    document = fitz.open()
    page = document.new_page(width=200, height=120)
    page.draw_line((10, 30), (180, 30), width=2)
    document.save(pdf)
    document.close()

    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter().extract(manifest, tmp_path / "output")

    def should_not_be_used(*_args, **_kwargs):
        raise AssertionError("PDF audit must not redraw SourceEntities")

    monkeypatch.setattr(wall_audit, "_render_transformed_source", should_not_be_used)
    output = tmp_path / "source-render.png"
    render_original_source(manifest, entities, output, evidence=object())

    image = cv2.imread(str(output), cv2.IMREAD_GRAYSCALE)
    assert image is not None
    assert int((image < 250).sum()) > 0


def test_pdf_pages_are_never_paired_without_same_frame_evidence(tmp_path: Path):
    import pymupdf as fitz

    pdf = tmp_path / "multi-page-plan.pdf"
    document = fitz.open()
    first = document.new_page(width=200, height=120)
    first.draw_line((10, 30), (110, 30), width=1)
    second = document.new_page(width=200, height=120)
    second.draw_line((10, 40), (110, 40), width=1)
    document.save(pdf)
    document.close()

    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter().extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)

    assert evidence.items == []
    diagnostics = {item["code"]: item for item in evidence.diagnostics}
    assert diagnostics["NO_GEOMETRIC_WALL_EVIDENCE"]["segment_count"] == 2
    assert diagnostics["SOURCE_FRAMES_NOT_REGISTERED"]["frames"] == [
        "source_0001:page:0001",
        "source_0001:page:0002",
    ]


def test_grid_axis_can_select_pdf_page_frame_and_apply_registered_offset(
    tmp_path: Path,
):
    import pymupdf as fitz

    pdf = tmp_path / "multi-page-grid.pdf"
    document = fitz.open()
    for _ in range(2):
        page = document.new_page(width=200, height=120)
        page.draw_line((10, 30), (110, 30), width=1)
        page.draw_line((10, 40), (110, 40), width=1)
    document.save(pdf)
    document.close()

    page_one = "source_0001:page:0001"
    page_two = "source_0001:page:0002"
    config = _pdf_config(pdf).model_copy(update={
        "coordinate": CoordinateConfig(
            scale_to_m=0.02,
            frame_offsets_m={page_one: (0.0, 0.0), page_two: (10.0, 20.0)},
        ),
        "grid": GridConfig(axes=[GridAxisConfig(
            label="A",
            start=(10.0, 0.0),
            end=(10.0, 100.0),
            frame_id=page_two,
        )]),
    })
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter().extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)
    model = build_wall_model(config, manifest, entities, evidence)

    assert not any(
        item.get("code") == "SOURCE_FRAMES_NOT_REGISTERED"
        for item in evidence.diagnostics
    )
    assert len(model.grid) == 1
    axis = model.grid[0]
    assert axis.frame_id == page_two
    assert axis.source_file_id is None
    assert axis.start_m == pytest.approx((10.2, 20.0))
    assert axis.end_m == pytest.approx((10.2, 22.0))


def test_empty_pdf_page_can_be_registered_for_grid_without_wall_entities(
    tmp_path: Path,
):
    import pymupdf as fitz

    pdf = tmp_path / "empty-grid-page.pdf"
    document = fitz.open()
    first = document.new_page(width=200, height=120)
    first.draw_line((10, 30), (110, 30), width=1)
    first.draw_line((10, 40), (110, 40), width=1)
    document.new_page(width=200, height=120)  # intentionally no drawable entities
    document.save(pdf)
    document.close()

    page_two = "source_0001:page:0002"
    config = _pdf_config(pdf).model_copy(update={
        "coordinate": CoordinateConfig(
            scale_to_m=0.02,
            frame_offsets_m={page_two: (10.0, 20.0)},
        ),
        "grid": GridConfig(axes=[GridAxisConfig(
            label="A",
            start=(10.0, 0.0),
            end=(10.0, 100.0),
            frame_id=page_two,
        )]),
    })
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter().extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)
    model = build_wall_model(config, manifest, entities, evidence)

    assert len(model.grid) == 1
    assert model.grid[0].frame_id == page_two
    assert model.grid[0].start_m == pytest.approx((10.2, 20.0))
    assert model.grid[0].end_m == pytest.approx((10.2, 22.0))


def test_pdf_frame_offset_must_reference_an_emitted_page_frame(tmp_path: Path):
    import pymupdf as fitz

    pdf = tmp_path / "single-page-frame.pdf"
    document = fitz.open()
    page = document.new_page(width=200, height=120)
    page.draw_line((10, 30), (110, 30), width=1)
    page.draw_line((10, 40), (110, 40), width=1)
    document.save(pdf)
    document.close()

    config = _pdf_config(pdf).model_copy(update={
        "coordinate": CoordinateConfig(
            scale_to_m=0.02,
            frame_offsets_m={"source_0001:page:9999": (10.0, 20.0)},
        ),
    })
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter().extract(manifest, tmp_path / "output")

    with pytest.raises(ValueError, match="unparsed page frame"):
        build_wall_evidence(config, manifest, entities)


def test_cad_frame_offsets_may_not_introduce_page_frames(tmp_path: Path):
    source = tmp_path / "plan.dxf"
    document = ezdxf.new()
    document.header["$INSUNITS"] = 4
    document.layers.add("GEOMETRY-WALL")
    modelspace = document.modelspace()
    modelspace.add_line(
        (0.0, 0.0), (4000.0, 0.0), dxfattribs={"layer": "GEOMETRY-WALL"}
    )
    modelspace.add_line(
        (0.0, 200.0), (4000.0, 200.0), dxfattribs={"layer": "GEOMETRY-WALL"}
    )
    document.saveas(source)
    config = WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="dxf",
            files=[SourceFileConfig(path=str(source), role="plan")],
        ),
        coordinate=CoordinateConfig(
            source_unit="mm",
            frame_offsets_m={"source_0001:page:0001": (0.0, 0.0)},
        ),
        wall=WallRecognitionConfig(
            include_layers=["GEOMETRY-WALL"],
            min_thickness_m=0.075,
            max_thickness_m=0.60,
            min_length_m=0.30,
        ),
        level=LevelConfig(id="level-1", name="Main"),
    )
    manifest = _build_manifest(config)
    entities = DXFSourceAdapter(config).extract(manifest, tmp_path / "output")

    with pytest.raises(ValueError, match="CAD frame_offsets_m"):
        build_wall_evidence(config, manifest, entities)


def test_source_level_frame_offset_is_default_for_pdf_pages(tmp_path: Path):
    import pymupdf as fitz

    pdf = tmp_path / "source-level-offset.pdf"
    document = fitz.open()
    for _ in range(2):
        page = document.new_page(width=200, height=120)
        page.draw_line((10, 30), (110, 30), width=1)
        page.draw_line((10, 40), (110, 40), width=1)
    document.save(pdf)
    document.close()

    config = _pdf_config(pdf).model_copy(update={
        "coordinate": CoordinateConfig(
            scale_to_m=0.02, frame_offsets_m={"source_0001": (3.0, 4.0)}
        ),
    })
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter().extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)

    assert not any(
        item.get("code") == "SOURCE_FRAMES_NOT_REGISTERED"
        for item in evidence.diagnostics
    )
    # Both pages retain separate frames (no cross-page pairing), but each
    # transformed coordinate receives the source-level default offset.
    assert tuple(evidence.transform_chain[-1]["frame_offsets_m"]["source_0001"]) == (3.0, 4.0)


def test_explicitly_registered_source_frames_can_pair_opposing_wall_faces(
    tmp_path: Path,
):
    """Registration is the opt-in needed to combine supplementary sheets."""

    def write_face(path: Path, y_mm: float) -> None:
        document = ezdxf.new()
        document.header["$INSUNITS"] = 4
        document.layers.add("GEOMETRY-WALL")
        modelspace = document.modelspace()
        modelspace.add_line(
            (0.0, y_mm), (4000.0, y_mm),
            dxfattribs={"layer": "GEOMETRY-WALL"},
        )
        document.saveas(path)

    first = tmp_path / "architectural-face.dxf"
    second = tmp_path / "structural-face.dxf"
    write_face(first, 0.0)
    write_face(second, 200.0)
    config = WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="dxf",
            files=[
                SourceFileConfig(path=str(first), role="plan"),
                SourceFileConfig(path=str(second), role="structural_supplement"),
            ],
        ),
        coordinate=CoordinateConfig(
            source_unit="mm",
            frame_offsets_m={"source_0001": (0.0, 0.0), "source_0002": (0.0, 0.0)},
        ),
        wall=WallRecognitionConfig(
            include_layers=["GEOMETRY-WALL"],
            min_length_m=0.30,
            min_thickness_m=0.075,
            max_thickness_m=0.60,
        ),
        level=LevelConfig(id="level-1", name="Main"),
    )
    manifest = _build_manifest(config)
    entities = DXFSourceAdapter(config).extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)

    assert len(evidence.items) == 1
    assert len(evidence.items[0].source_refs) == 2
    assert evidence.transform_chain[-1]["cross_frame_pairing"] is True
    assert not any(
        item.get("code") == "SOURCE_FRAMES_NOT_REGISTERED"
        for item in evidence.diagnostics
    )


def test_unregistered_frames_cannot_create_cross_sheet_topology(
    tmp_path: Path,
):
    """Coincident local coordinates on two sheets must stay independent.

    Pairing is already frame-scoped, but the same rule must also hold for
    junction healing/classification.  Otherwise two identical supplementary
    plans would manufacture an X/T junction merely because both start at the
    local origin.
    """

    def write_t(path: Path) -> None:
        document = ezdxf.new()
        document.header["$INSUNITS"] = 4
        document.layers.add("GEOMETRY-WALL")
        modelspace = document.modelspace()
        # Horizontal and vertical closed strips form one T in each local
        # source frame; both files deliberately use the same coordinates.
        modelspace.add_lwpolyline(
            [(0, -100), (4000, -100), (4000, 100), (0, 100)],
            close=True,
            dxfattribs={"layer": "GEOMETRY-WALL"},
        )
        modelspace.add_lwpolyline(
            [(1900, 0), (2100, 0), (2100, 2000), (1900, 2000)],
            close=True,
            dxfattribs={"layer": "GEOMETRY-WALL"},
        )
        document.saveas(path)

    first = tmp_path / "plan-a.dxf"
    second = tmp_path / "plan-b.dxf"
    write_t(first)
    write_t(second)
    config = WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="dxf",
            files=[
                SourceFileConfig(path=str(first), role="plan"),
                SourceFileConfig(path=str(second), role="supplementary"),
            ],
        ),
        coordinate=CoordinateConfig(source_unit="mm"),
        wall=WallRecognitionConfig(
            include_layers=["GEOMETRY-WALL"],
            min_thickness_m=0.075,
            max_thickness_m=0.60,
            min_length_m=0.30,
        ),
        level=LevelConfig(id="level-1", name="Main"),
    )
    manifest = _build_manifest(config)
    entities = DXFSourceAdapter(config).extract(manifest, tmp_path / "output")
    evidence = build_wall_evidence(config, manifest, entities)
    model = build_wall_model(config, manifest, entities, evidence)

    assert len(model.walls) >= 4
    for junction in model.junctions:
        referenced_frames = {
            ref.frame_id or ref.source_file_id
            for wall_id in junction.wall_ids
            for wall in model.walls
            if wall.wall_id == wall_id
            for ref in wall.source_refs
        }
        assert len(referenced_frames) == 1


def test_pdf_page_limit_fails_closed(tmp_path: Path, monkeypatch):
    import pymupdf as fitz

    pdf = tmp_path / "too-many-pages.pdf"
    document = fitz.open()
    document.new_page(width=100, height=100)
    document.new_page(width=100, height=100)
    document.save(pdf)
    document.close()
    monkeypatch.setattr(source_adapters, "_MAX_PDF_PAGES", 1)

    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    with pytest.raises(ValueError, match="PDF_PAGE_LIMIT_EXCEEDED"):
        PDFSourceAdapter().extract(manifest, tmp_path / "output")


@pytest.mark.parametrize(
    ("page_count", "error_code"),
    [(-1, "PDF_PAGE_COUNT_INVALID"), (float("inf"), "PDF_PAGE_COUNT_FAILED")],
)
def test_pdf_corrupt_page_count_is_explicit(
    tmp_path: Path, monkeypatch, page_count, error_code: str,
):
    import pymupdf as fitz

    pdf = tmp_path / "corrupt-page-count.pdf"
    document = fitz.open()
    document.new_page(width=100, height=100)
    document.save(pdf)
    document.close()

    class FakeDocument:
        @property
        def page_count(self):
            if isinstance(page_count, BaseException):
                raise page_count
            return page_count

        def close(self):
            return None

    monkeypatch.setattr(fitz, "open", lambda _path: FakeDocument())
    config = _pdf_config(pdf)
    manifest = _build_manifest(config)

    with pytest.raises(RuntimeError, match=error_code):
        PDFSourceAdapter().extract(manifest, tmp_path / "output")


def test_manifest_rejects_oversized_source_before_hash(tmp_path: Path, monkeypatch):
    import pymupdf as fitz

    pdf = tmp_path / "oversized-before-hash.pdf"
    document = fitz.open()
    document.new_page(width=100, height=100)
    document.save(pdf)
    document.close()
    monkeypatch.setattr(source_adapters, "_MAX_SOURCE_BYTES", 1)

    hash_called = False

    def unexpected_hash(_path):
        nonlocal hash_called
        hash_called = True
        raise AssertionError("oversized source was hashed")

    monkeypatch.setattr(wall_pipeline, "file_sha256", unexpected_hash)
    config = _pdf_config(pdf)

    with pytest.raises(ValueError, match="PDF_SOURCE_TOO_LARGE"):
        wall_pipeline._build_manifest(config)
    assert hash_called is False


def test_manifest_reports_source_changed_during_hash(tmp_path: Path, monkeypatch):
    import pymupdf as fitz

    pdf = tmp_path / "changed-during-hash.pdf"
    document = fitz.open()
    document.new_page(width=100, height=100)
    document.save(pdf)
    document.close()
    config = _pdf_config(pdf)
    original_hash = wall_pipeline.file_sha256

    def hash_then_mutate(path):
        digest = original_hash(path)
        path.write_bytes(path.read_bytes() + b"changed")
        return digest

    monkeypatch.setattr(wall_pipeline, "file_sha256", hash_then_mutate)

    with pytest.raises(RuntimeError, match="SOURCE_CHANGED_DURING_HASH"):
        wall_pipeline._build_manifest(config)


def test_pdf_adapter_rejects_source_changed_after_manifest(tmp_path: Path):
    import pymupdf as fitz

    pdf = tmp_path / "changed-after-manifest.pdf"
    document = fitz.open()
    document.new_page(width=100, height=100)
    document.save(pdf)
    document.close()
    config = _pdf_config(pdf)
    manifest = _build_manifest(config)

    # Simulate a queue boundary or operator replacing the uploaded source
    # after the manifest has been persisted.
    pdf.write_bytes(pdf.read_bytes() + b"replacement")

    with pytest.raises(RuntimeError, match="PDF_SOURCE_CHANGED_AFTER_MANIFEST"):
        PDFSourceAdapter().extract(manifest, tmp_path / "output")


def test_pdf_text_fallback_rechecks_manifest_before_audit(tmp_path: Path):
    import pymupdf as fitz

    pdf = tmp_path / "text-only-changed.pdf"
    document = fitz.open()
    page = document.new_page(width=100, height=100)
    page.insert_text((10, 20), "text-only source")
    document.save(pdf)
    document.close()
    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter().extract(manifest, tmp_path / "output")
    assert not any(item.kind in {"line", "polyline", "mline", "hatch_boundary"}
                   for item in entities.entities)
    pdf.write_bytes(pdf.read_bytes() + b"replacement")

    with pytest.raises(RuntimeError, match="PDF_SOURCE_CHANGED_AFTER_MANIFEST"):
        render_original_source(manifest, entities, tmp_path / "source.png")


def test_pdf_text_fallback_enforces_page_limit(tmp_path: Path, monkeypatch):
    import pymupdf as fitz

    pdf = tmp_path / "text-only-too-many-pages.pdf"
    document = fitz.open()
    for index in range(2):
        page = document.new_page(width=100, height=100)
        page.insert_text((10, 20), f"page {index + 1}")
    document.save(pdf)
    document.close()
    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    entities = PDFSourceAdapter().extract(manifest, tmp_path / "output")
    monkeypatch.setattr(source_adapters, "_MAX_PDF_PAGES", 1)

    with pytest.raises(ValueError, match="PDF_PAGE_LIMIT_EXCEEDED"):
        render_original_source(manifest, entities, tmp_path / "source.png")


def test_dwg_staged_copy_is_checked_against_manifest(tmp_path: Path, monkeypatch):
    dwg = tmp_path / "changed-during-copy.dwg"
    dwg.write_bytes(b"synthetic-dwg")
    executable = tmp_path / "ODAFileConverter.exe"
    executable.write_bytes(b"placeholder")
    config = WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="dwg",
            files=[SourceFileConfig(path=str(dwg), role="plan")],
            converter={"executable": str(executable)},
        ),
        coordinate=CoordinateConfig(source_unit="mm"),
        level=LevelConfig(id="level-1", name="Main"),
    )
    manifest = _build_manifest(config)

    def copy_and_tamper(source, destination):
        Path(destination).write_bytes(Path(source).read_bytes() + b"tampered")

    monkeypatch.setattr(source_adapters.shutil, "copy2", copy_and_tamper)

    with pytest.raises(RuntimeError, match="DWG_SOURCE_CHANGED_AFTER_MANIFEST"):
        DXFSourceAdapter(config).extract(manifest, tmp_path / "output")


def test_pdf_entity_limit_fails_closed(tmp_path: Path, monkeypatch):
    import pymupdf as fitz

    pdf = tmp_path / "too-many-entities.pdf"
    document = fitz.open()
    page = document.new_page(width=100, height=100)
    page.draw_line((10, 10), (90, 10))
    page.draw_line((10, 20), (90, 20))
    document.save(pdf)
    document.close()
    monkeypatch.setattr(source_adapters, "_MAX_PDF_ENTITIES", 1)

    config = _pdf_config(pdf)
    manifest = _build_manifest(config)
    with pytest.raises(ValueError, match="PDF_ENTITY_LIMIT_EXCEEDED"):
        PDFSourceAdapter().extract(manifest, tmp_path / "output")


def test_pdf_layer_scope_filters_annotations_before_entity_budget(
    tmp_path: Path, monkeypatch
):
    import pymupdf as fitz

    pdf = tmp_path / "layered-structural-plan.pdf"
    document = fitz.open()
    page = document.new_page(width=200, height=120)
    wall_ocg = document.add_ocg("xref|S-WALL")
    text_ocg = document.add_ocg("xref|S-WALL-TEXT")
    page.draw_line((10, 30), (110, 30), width=1, oc=wall_ocg)
    for offset in range(20):
        page.draw_line(
            (10, 50 + offset), (110, 50 + offset), width=1, oc=text_ocg
        )
    document.save(pdf)
    document.close()

    config = _pdf_config(pdf).model_copy(update={
        "wall": WallRecognitionConfig(
            include_layers=[r"(?:^|\|)(?:S-WALL)$"],
            min_thickness_m=0.075,
            max_thickness_m=0.60,
            min_length_m=0.30,
        ),
    })
    manifest = _build_manifest(config)
    monkeypatch.setattr(source_adapters, "_MAX_PDF_ENTITIES", 1)

    entities = PDFSourceAdapter(config).extract(manifest, tmp_path / "output")

    assert len(entities.entities) == 1
    assert entities.entities[0].layer == "xref|S-WALL"
    skipped = next(
        item for item in entities.diagnostics
        if item.get("code") == "PDF_ENTITIES_SKIPPED_BY_LAYER_SCOPE"
    )
    assert skipped["item_count"] == 20


def test_source_file_anomalies_are_explicit(tmp_path: Path, monkeypatch):
    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"")
    config = _pdf_config(empty_pdf)
    with pytest.raises(ValueError, match="PDF_SOURCE_EMPTY"):
        _build_manifest(config)

    valid_pdf = tmp_path / "valid.pdf"
    import pymupdf as fitz
    document = fitz.open()
    document.new_page(width=100, height=100)
    document.save(valid_pdf)
    document.close()
    monkeypatch.setattr(source_adapters, "_MAX_SOURCE_BYTES", 1)
    config = _pdf_config(valid_pdf)
    with pytest.raises(ValueError, match="PDF_SOURCE_TOO_LARGE"):
        _build_manifest(config)


def test_malformed_dxf_reports_parser_failure(tmp_path: Path):
    dxf = tmp_path / "malformed.dxf"
    dxf.write_text("this is not a DXF", encoding="ascii")
    config = WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="dxf",
            files=[SourceFileConfig(path=str(dxf), role="plan")],
        ),
        coordinate=CoordinateConfig(source_unit="mm"),
        level=LevelConfig(id="level-1", name="Main"),
    )
    manifest = _build_manifest(config)
    with pytest.raises(RuntimeError, match="DXF parsing failed"):
        DXFSourceAdapter(config).extract(manifest, tmp_path / "output")


def test_dxf_unsupported_entities_are_reported(tmp_path: Path):
    dxf = tmp_path / "unsupported.dxf"
    document = ezdxf.new()
    document.modelspace().add_circle((0, 0), radius=1)
    document.saveas(dxf)

    config = WallPipelineConfig(
        tenant_id="tenant-adapter-test",
        project_id="project-adapter-test",
        source=SourceConfig(
            type="dxf",
            files=[SourceFileConfig(path=str(dxf), role="plan")],
        ),
        coordinate=CoordinateConfig(source_unit="mm"),
        level=LevelConfig(id="level-1", name="Main"),
    )
    manifest = _build_manifest(config)
    entities = DXFSourceAdapter(config).extract(manifest, tmp_path / "output")

    warnings = [
        item for item in entities.diagnostics
        if item.get("code") == "UNSUPPORTED_DXF_ENTITY"
    ]
    assert any(item.get("entity_type") == "CIRCLE" for item in warnings)


class _CyclicInsert:
    class dxf:
        name = "CYCLIC_BLOCK"

    def dxftype(self):
        return "INSERT"

    def virtual_entities(self):
        return [self]


def test_dxf_insert_cycle_is_bounded_and_diagnosable():
    expanded = list(_walk_dxf_entities([_CyclicInsert()]))

    assert expanded
    errors = [detail for _, _, detail in expanded if detail]
    assert any("cycle detected" in detail for detail in errors)
