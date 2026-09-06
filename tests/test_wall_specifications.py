from backend.engines.wall_pipeline.specifications import (
    TextRow,
    extract,
    extract_beam_elevation_evidence,
    parse_text,
    resolve_beam_elevation,
    resolve,
)
from backend.engines.wall_pipeline.contracts import BeamRecognitionConfig


def row(entity_id, text, x=0.0, y=0.0, frame="sheet:1"):
    return TextRow(
        entity_id=entity_id,
        source_file_id="source",
        frame_id=frame,
        page_no=1,
        text=text,
        point_m=(x, y),
    )


def test_parse_wall_thickness_keeps_drawing_integer_without_geometry_rounding():
    parsed = parse_text(
        "墙厚 267",
        entity_ids=("t1",), source_file_id="source", frame_id="sheet:1",
        page_no=1, point_m=(0, 0),
    )
    assert parsed is not None
    assert parsed.component_kind == "wall"
    assert parsed.dimensions_mm == {"thickness_mm": 267.0}


def test_parse_column_two_dimension_section_from_detail():
    parsed = parse_text(
        "GBZ1 600x300 柱大样",
        entity_ids=("t1",), source_file_id="source", frame_id="sheet:1",
        page_no=1, point_m=(0, 0),
    )
    assert parsed is not None
    assert parsed.component_kind == "column"
    assert parsed.dimensions_mm == {"width_mm": 600.0, "depth_mm": 300.0}
    assert parsed.source_kind == "detail"


def test_parse_coupling_beam_section_uses_bx_h_order_and_detail_role():
    parsed = parse_text(
        "LL1 300x500",
        entity_ids=("t1",), source_file_id="source", frame_id="sheet:1",
        page_no=1, point_m=(0, 0), source_role="column_detail",
    )
    assert parsed is not None
    assert parsed.component_kind == "beam"
    assert parsed.mark == "LL1"
    assert parsed.dimensions_mm == {"width_mm": 300.0, "depth_mm": 500.0}
    assert parsed.source_kind == "detail"


def test_beam_label_lookup_has_separate_wider_bound_than_face_pairing():
    config = BeamRecognitionConfig()
    assert config.association_distance_m == 2.0
    assert config.label_association_distance_m == 8.0


def test_beam_specification_resolves_marked_schedule_dimensions():
    rows = [row("beam-spec", "LL16 300x500", 20.0, 20.0)]
    result = resolve(
        component_kind="beam", anchor_m=(0.0, 0.0), frame_id="sheet:1",
        rows=rows, parsed=extract(rows), element_mark="LL16",
        geometry_mm={"width_mm": 300.0, "depth_mm": 500.0},
    )
    assert result.status == "resolved_from_annotation"
    assert result.mark == "LL16"
    assert result.dimensions_mm == {"width_mm": 300.0, "depth_mm": 500.0}


def test_extract_links_split_ll_mark_and_dimension_schedule_cells():
    rows = [
        row("mark", "LL16", 0.0, 0.0),
        row("dimension", "300x500", 6.0, 0.05),
    ]
    values = extract(rows)
    assert any(
        item.mark == "LL16"
        and item.dimensions_mm == {"width_mm": 300.0, "depth_mm": 500.0}
        for item in values
    )


def test_parser_rejects_rebar_opening_and_concrete_grade_as_component_size():
    common = {
        "entity_ids": ("t1",), "source_file_id": "source",
        "frame_id": "sheet:1", "page_no": 1, "point_m": (0, 0),
    }
    assert parse_text("墙顶附加钢筋22@300", **common) is None
    assert parse_text("侧墙洞1000x700 JD1", **common) is None
    assert parse_text("墙厚400 砼C35 竖向筋18@150", **common).dimensions_mm == {
        "thickness_mm": 400.0,
    }


def test_compact_marked_wall_and_column_schedule_cells_are_supported():
    common = {
        "entity_ids": ("t1",), "source_file_id": "source",
        "frame_id": "sheet:1", "page_no": 1, "point_m": (0, 0),
    }
    assert parse_text("Q1 267", **common).dimensions_mm == {
        "thickness_mm": 267.0,
    }
    assert parse_text("GBZ39 800X500", **common).dimensions_mm == {
        "width_mm": 800.0, "depth_mm": 500.0,
    }


def test_ocr_compact_column_dimension_is_split_without_rounding():
    common = {
        "entity_ids": ("t1",), "source_file_id": "source",
        "frame_id": "sheet:1", "page_no": 1, "point_m": (0, 0),
    }
    parsed = parse_text("GBZ42 9001000", **common)
    assert parsed is not None
    assert parsed.dimensions_mm == {"width_mm": 1000.0, "depth_mm": 900.0}


def test_extract_links_column_mark_to_rotated_dimension_cell():
    rows = [
        row("mark", "GBZ39", 0.0, 0.0),
        # OCR may keep a short unreadable prefix before a real dimension.
        row("dimension", "����1600x1100", 0.8, 0.4),
    ]
    values = extract(rows, column_association_radius_m=1.5)
    assert any(
        item.mark == "GBZ39"
        and item.dimensions_mm == {"width_mm": 1600.0, "depth_mm": 1100.0}
        for item in values
    )


def test_wall_schedule_row_ignores_reinforcement_count_parenthetical():
    common = {
        "entity_ids": ("t1", "t2", "t3"), "source_file_id": "source",
        "frame_id": "sheet:1", "page_no": 1, "point_m": (0, 0),
    }
    parsed = parse_text("Q1（3排）500", **common)
    assert parsed is not None
    assert parsed.mark == "Q1"
    assert parsed.dimensions_mm == {"thickness_mm": 500.0}


def test_extract_links_rotated_schedule_mark_to_aligned_thickness_cell():
    rows = [
        row("mark", "Q1（3排）", 0.0, 0.0),
        # Page rotation can turn the table's vertical row spacing into X.
        row("thickness", "500", 2.8, 0.02),
    ]
    values = extract(rows)
    assert any(
        item.mark == "Q1" and item.dimensions_mm == {"thickness_mm": 500.0}
        for item in values
    )


def test_extract_combines_split_pdf_words_and_prefers_detail():
    rows = [row("t1", "墙厚", 1.0), row("t2", "267", 1.1), row("t3", "大样", 1.05)]
    values = extract(rows)
    assert any(item.dimensions_mm == {"thickness_mm": 267.0} for item in values)
    assert any(item.source_kind == "detail" for item in values)


def test_resolve_reports_geometry_conflict_instead_of_rounding():
    rows = [row("t1", "墙厚267")]
    parsed = extract(rows)
    result = resolve(
        component_kind="wall", anchor_m=(0, 0), frame_id="sheet:1",
        rows=rows, parsed=parsed, geometry_mm={"thickness_mm": 250.0},
    )
    assert result.status == "conflict"
    assert result.dimensions_mm == {"thickness_mm": 267.0}
    assert "differs" in (result.reason or "")


def test_resolve_is_explicit_when_no_legend_or_detail_exists():
    rows = [row("t1", "Q1")]
    assert resolve(
        component_kind="wall", anchor_m=(0, 0), frame_id="sheet:1",
        rows=rows, parsed=extract(rows),
    ).status == "unresolved"


def test_resolve_links_plan_mark_to_detail_specification():
    rows = [
        row("plan", "Q1", 0.0, 0.0, frame="plan:1"),
        row("detail", "Q1墙厚267大样", 20.0, 20.0, frame="detail:1"),
    ]
    parsed = extract(rows)
    result = resolve(
        component_kind="wall", anchor_m=(0.0, 0.0), frame_id="plan:1",
        rows=rows, parsed=parsed, geometry_mm={"thickness_mm": 267.0},
    )
    assert result.status == "resolved_from_detail"
    assert result.mark == "Q1"
    assert result.dimensions_mm == {"thickness_mm": 267.0}


def test_resolve_can_link_leadered_explicit_size_by_geometric_agreement():
    rows = [row("legend", "墙厚400", 50.0, 50.0)]
    result = resolve(
        component_kind="wall", anchor_m=(0.0, 0.0), frame_id="sheet:1",
        rows=rows, parsed=extract(rows),
        geometry_mm={"thickness_mm": 400.05},
    )
    assert result.status == "resolved_from_annotation"
    assert result.dimensions_mm == {"thickness_mm": 400.0}


def test_resolve_prefers_existing_column_mark_over_nearby_schedule_row():
    rows = [
        row("c1", "GBZ1 600x300", 10.0, 10.0),
        row("c2", "GBZ2 800x500", 10.2, 10.0),
    ]
    result = resolve(
        component_kind="column", anchor_m=(0.0, 0.0), frame_id="sheet:1",
        rows=rows, parsed=extract(rows), element_mark="GBZ2",
        geometry_mm={"width_mm": 800.0, "depth_mm": 500.0},
    )
    assert result.status == "resolved_from_annotation"
    assert result.mark == "GBZ2"
    assert result.dimensions_mm == {"width_mm": 800.0, "depth_mm": 500.0}


def test_resolve_does_not_attach_unrelated_spec_to_generated_geometry_mark():
    rows = [
        row("nearby", "GBZ42 900x1000", 0.2, 0.0),
    ]
    result = resolve(
        component_kind="column", anchor_m=(0.0, 0.0), frame_id="sheet:1",
        rows=rows, parsed=extract(rows), element_mark="BM-C-800x800",
        geometry_mm={"width_mm": 800.0, "depth_mm": 800.0},
    )
    assert result.status == "unresolved"
    assert result.dimensions_mm is None


def test_extracts_direct_beam_top_and_base_elevations_with_evidence():
    values = extract_beam_elevation_evidence([
        row("top", "LL1 梁顶相对标高 +0.150m"),
        row("base", "LL1 梁底相对标高 -0.350m"),
    ])
    assert {(item.reference, item.mark, item.elevation_m) for item in values} == {
        ("top", "LL1", 0.15),
        ("base", "LL1", -0.35),
    }
    result = resolve_beam_elevation(
        element_mark="LL1", anchor_m=(0.0, 0.0), frame_id="sheet:1",
        rows=[row("top", "LL1 梁顶相对标高 +0.150m"),
              row("base", "LL1 梁底相对标高 -0.350m")],
        beam_depth_m=0.5,
    )
    assert result.status == "resolved"
    assert result.top_elevation_m == 0.15
    assert result.base_elevation_m == -0.35
    assert set(result.entity_ids) == {"top", "base"}


def test_marked_beam_elevation_can_resolve_from_detail_sheet():
    rows = [
        row("plan-mark", "LL16", 0.0, 0.0, frame="plan:1"),
        row("detail-top", "LL16 梁顶相对标高 +4.200m", 20.0, 20.0, frame="detail:1"),
        row("detail-base", "LL16 梁底相对标高 +3.700m", 20.0, 21.0, frame="detail:1"),
    ]
    result = resolve_beam_elevation(
        element_mark="LL16", anchor_m=(0.0, 0.0), frame_id="plan:1",
        rows=rows, beam_depth_m=0.5,
    )
    assert result.status == "resolved"
    assert result.top_elevation_m == 4.2
    assert result.base_elevation_m == 3.7


def test_extracts_marked_h_notation_from_beam_detail_annotation():
    evidence = extract_beam_elevation_evidence([
        row("top", "LL11 h+5. 000"),
        row("base", "LL12 h-0,200"),
    ])
    assert {(item.mark, item.reference, item.elevation_m) for item in evidence} == {
        ("LL11", "top", 5.0),
        ("LL12", "base", -0.2),
    }


def test_keeps_unmarked_explicit_elevation_for_later_anchor_association():
    values = extract_beam_elevation_evidence([
        row("heading-value", "梁顶相对标高 +0.150m"),
    ])
    assert len(values) == 1
    assert values[0].mark is None
    assert values[0].reference == "top"
    assert values[0].elevation_m == 0.15


def test_links_beam_schedule_elevation_column_to_ll_mark_and_derives_missing_side():
    rows = [
        row("header", "梁顶相对标高", 10.0, 0.0),
        # The schedule's elevation column is more than 8 m from its LL mark.
        row("value", "0.150", 10.1, 2.0),
        row("mark", "LL1", 0.0, 2.02),
    ]
    evidence = extract_beam_elevation_evidence(rows)
    assert any(
        item.reference == "top" and item.mark == "LL1"
        and item.elevation_m == 0.15
        for item in evidence
    )
    result = resolve_beam_elevation(
        element_mark="LL1", anchor_m=(0.0, 2.0), frame_id="sheet:1",
        rows=rows, beam_depth_m=0.5,
    )
    assert result.status == "resolved"
    assert result.top_elevation_m == 0.15
    assert result.base_elevation_m == -0.35


def test_recovers_dropped_minus_from_structural_relative_elevation_table():
    rows = [
        row("header", "\u6881\u9876\u76f8\u5bf9\u6807\u9ad8", 10.0, 0.0, frame="sheet:1"),
        TextRow(
            entity_id="value", source_file_id="source", frame_id="sheet:1",
            page_no=1, text="0.150", point_m=(10.1, 2.0),
            source_role="structural_plan",
        ),
        row("mark", "LL01", 0.0, 2.02, frame="sheet:1"),
    ]
    result = resolve_beam_elevation(
        element_mark="LL01", anchor_m=(0.0, 2.0), frame_id="sheet:1",
        rows=rows, beam_depth_m=0.5,
        infer_unsigned_relative_negative=True,
    )
    assert result.status == "resolved"
    assert result.top_elevation_m == -0.15
    assert result.base_elevation_m == -0.65


def test_links_rotated_beam_table_when_ocr_splits_decimal_separator():
    """A PDF table cell such as ``0. 150`` must remain a real elevation."""
    rows = [
        # The source table is rotated: the header and value share a baseline,
        # while the LL mark is separated along the table's other axis.
        row("header", "梁顶相对标高", 5.458, 1.8956),
        row("value", "0. 150", 2.014, 1.8985),
        row("mark", "LL16", 2.039, 2.2254),
    ]
    result = resolve_beam_elevation(
        element_mark="LL16", anchor_m=(2.039, 2.2254), frame_id="sheet:1",
        rows=rows, beam_depth_m=0.5,
    )
    assert result.status == "resolved"
    assert result.top_elevation_m == 0.15
    assert result.base_elevation_m == -0.35
    assert set(result.entity_ids) == {"header", "value", "mark"}


def test_uses_explicit_level_top_for_unmarked_beams_when_drawing_note_allows_it():
    rows = [
        row("mark", "LL99", 0.0, 0.0),
        row(
            "note",
            "说明：未注明连梁梁顶标高（相对于±0.000）同该层顶板标高",
            1.0,
            1.0,
        ),
    ]
    result = resolve_beam_elevation(
        element_mark="LL99", anchor_m=(0.0, 0.0), frame_id="sheet:1",
        rows=rows, beam_depth_m=0.5, default_top_elevation_m=0.0,
    )
    assert result.status == "level_default"
    assert result.top_elevation_m == 0.0
    assert result.base_elevation_m == -0.5
    assert result.entity_ids == ("note",)


def test_table_heading_does_not_cross_bind_notes_or_pipe_heights():
    rows = [
        row("header", "梁顶相对标高", 0.0, 0.0),
        row("value", "0.150", 0.1, 1.0),
        row("mark", "LL16", 0.0, 1.02),
        row(
            "note",
            "说明：未注明连梁梁顶标高（相对于±0.000）同该层顶板标高",
            0.2,
            0.5,
        ),
        row("pipe", "0N150,h+5.100", 0.3, 0.6),
    ]
    evidence = extract_beam_elevation_evidence(rows)
    table_values = [
        item for item in evidence if item.mark == "LL16"
    ]
    assert [(item.reference, item.elevation_m) for item in table_values] == [
        ("top", 0.15),
    ]
    assert not any("5.100" in item.text for item in evidence)


def test_supports_abbreviated_h_elevation_but_rejects_pipe_height_note():
    evidence = extract_beam_elevation_evidence([
        row("beam", "梁顶标高h+4.200"),
        row("base", "���б��h-0.200"),
        row("pipe", "DN150_h+5_100", 20.0, 0.0),
        row("pipe-label", "DN150", 20.0, 0.0),
        row("pipe-height", "h+5.000", 20.2, 0.0),
    ])
    assert any(item.reference == "top" and item.elevation_m == 4.2 for item in evidence)
    assert any(item.reference == "base" and item.elevation_m == -0.2 for item in evidence)
    assert not any("DN150" in item.text for item in evidence)
    assert not any(item.elevation_m == 5.0 for item in evidence)
