from shapely.geometry import Point

from backend.engines.wall_pipeline.contracts import (
    ColumnRecord,
    CoordinateConfig,
    GridAxis,
    EvidenceSourceRef,
    GridConfig,
    LevelConfig,
    ManifestSource,
    OpeningRecognitionConfig,
    OpeningRecord,
    ReviewGate,
    RevitConfig,
    SourceEntities,
    SourceEntity,
    SourceManifest,
    WallEvidence,
    WallPipelineConfig,
    WallRecognitionConfig,
    ColumnRecognitionConfig,
    TopologyJunction,
    WallModel,
)
from backend.engines.wall_pipeline import geometry as wall_geometry
from backend.engines.wall_pipeline.geometry import (
    CandidateWall,
    _approved_profile_columns,
    _approved_profile_openings,
    _detect_openings,
    _heal_junctions,
    _reconcile_post_trim_topology,
)


def _config() -> WallPipelineConfig:
    return WallPipelineConfig(
        tenant_id="tenant",
        project_id="project",
        source={"type": "dxf", "files": [{"path": "plan.dxf", "role": "plan"}]},
        coordinate=CoordinateConfig(source_unit="mm"),
        grid=GridConfig(),
        wall=WallRecognitionConfig(),
        column=ColumnRecognitionConfig(),
        opening=OpeningRecognitionConfig(
            label_layers=["S-WALL-HO-TEXT$"],
            boundary_layers=["S-WALL-HO-TEXT$"],
        ),
        level=LevelConfig(id="level-1", name="Level 1"),
        revit=RevitConfig(),
    )


def _line(entity_id, points, *, kind="polyline"):
    return SourceEntity(
        entity_id=entity_id,
        source_file_id="source_0001",
        kind=kind,
        layer="S-WALL-HO-TEXT",
        geometry={"points": points} if kind == "polyline" else {
            "point": points,
            "text": points,
        },
        locator=entity_id,
        frame_id="source_0001",
    )


def test_opening_label_is_paired_with_vector_boundary_and_wall_host():
    config = _config()
    manifest = SourceManifest(
        tenant_id="tenant",
        project_id="project",
        source_type="dxf",
        sources=[ManifestSource(
            source_file_id="source_0001", path="plan.dxf", role="plan",
            media_type="application/dxf", sha256="0" * 64, size_bytes=0,
        )],
        coordinate=config.coordinate,
        grid=config.grid,
        wall=config.wall,
        column=config.column,
        opening=config.opening,
        level=config.level,
        revit=config.revit,
        config_sha256="0" * 64,
    )
    boundary = _line(
        "boundary",
        [(4.0, -0.2), (6.0, -0.2), (6.0, 0.2), (4.0, 0.2), (4.0, -0.2)],
    )
    boundary.geometry["closed"] = True
    label = SourceEntity(
        entity_id="label",
        source_file_id="source_0001",
        kind="text",
        layer="S-WALL-HO-TEXT",
        geometry={"point": (5.0, 0.8), "text": "JD3"},
        locator="label",
        frame_id="source_0001",
    )
    entities = SourceEntities(
        tenant_id="tenant", project_id="project", manifest_sha256="0" * 64,
        entities=[boundary, label], source_units={"source_0001": "mm"},
    )
    evidence = WallEvidence(
        tenant_id="tenant", project_id="project", source_entities_sha256="0" * 64,
        transform_chain=[
            {"operation": "unit_convert", "scale_to_m_by_source": {"source_0001": 1.0}},
            {"operation": "rotate", "rotation_deg": 0.0},
            {"operation": "translate", "translation_m": [0.0, 0.0]},
        ],
        items=[],
    )
    records = _detect_openings(
        config, manifest, entities, evidence,
        [CandidateWall(
            start=(0.0, 0.0), end=(10.0, 0.0), thickness_m=0.4,
            evidence_ids=["wall-evidence"], source_refs=[EvidenceSourceRef(
                source_file_id="source_0001", entity_id="wall", locator="wall",
                frame_id="source_0001",
            )], confidence=0.99,
        )],
    )
    assert len(records) == 1
    assert records[0].mark == "JD3"
    assert records[0].status == "matched"
    assert records[0].host_wall_ids == ["wall_0001"]
    assert records[0].width_m == 2.0
    assert records[0].depth_m == 0.4


def test_collinear_wall_fragments_are_healed_within_physical_tolerance():
    refs = [EvidenceSourceRef(
        source_file_id="source_0001", entity_id="wall", locator="wall",
        frame_id="source_0001",
    )]
    left = CandidateWall(
        start=(0.0, 0.0), end=(4.0, 0.0), thickness_m=0.4,
        evidence_ids=["left"], source_refs=refs, confidence=0.99,
    )
    right = CandidateWall(
        start=(4.25, 0.0), end=(8.0, 0.0), thickness_m=0.4,
        evidence_ids=["right"], source_refs=refs, confidence=0.99,
    )
    _heal_junctions([left, right], tolerance=0.15)
    assert left.end == (4.25, 0.0)


def test_parallel_offset_wall_does_not_steal_collinear_endpoint():
    refs = [EvidenceSourceRef(
        source_file_id="source_0001", entity_id="wall", locator="wall",
        frame_id="source_0001",
    )]
    main = CandidateWall(
        start=(0.0, 0.0), end=(4.0, 0.0), thickness_m=0.4,
        evidence_ids=["main"], source_refs=refs, confidence=0.99,
    )
    # This is a separate parallel run 200 mm away, not a continuation of the
    # main wall.  It must not move the endpoint to the nearby line.
    offset = CandidateWall(
        start=(4.1, 0.2), end=(8.0, 0.2), thickness_m=0.4,
        evidence_ids=["offset"], source_refs=refs, confidence=0.99,
    )
    _heal_junctions([main, offset], tolerance=0.15)
    assert main.end == (4.0, 0.0)


def test_junction_healing_preserves_wall_direction_when_crossings_drift():
    refs = [EvidenceSourceRef(
        source_file_id="source_0001", entity_id="wall", locator="wall",
        frame_id="source_0001",
    )]
    main = CandidateWall(
        start=(0.0, 2.0), end=(0.0, 8.0), thickness_m=0.4,
        evidence_ids=["main"], source_refs=refs, confidence=0.99,
    )
    # The two crossing walls carry independent drafting noise in X.  Healing
    # must extend the main wall to the crossings without turning it diagonal.
    lower = CandidateWall(
        start=(-2.0, 2.001), end=(0.0008, 2.001), thickness_m=0.4,
        evidence_ids=["lower"], source_refs=refs, confidence=0.99,
    )
    upper = CandidateWall(
        start=(-2.0, 7.999), end=(-0.0007, 7.999), thickness_m=0.4,
        evidence_ids=["upper"], source_refs=refs, confidence=0.99,
    )
    _heal_junctions([main, lower, upper], tolerance=0.15)
    assert main.start[0] == 0.0
    assert main.end[0] == 0.0


def test_post_trim_topology_drops_unreachable_duplicate_junctions():
    refs = [EvidenceSourceRef(
        source_file_id="source_0001", entity_id="wall", locator="wall",
        frame_id="source_0001",
    )]
    walls = [
        CandidateWall(
            start=(-74.589219, 34.592683), end=(-76.420130, 34.592683),
            thickness_m=0.266700, evidence_ids=["first"], source_refs=refs,
            confidence=0.99,
        ),
        CandidateWall(
            start=(-76.953525, 34.660416), end=(-77.855231, 34.660416),
            thickness_m=0.402165, evidence_ids=["second"], source_refs=refs,
            confidence=0.99,
        ),
        CandidateWall(
            start=(-76.553480, 34.960980), end=(-76.553480, 35.526133),
            thickness_m=0.198963, evidence_ids=["third"], source_refs=refs,
            confidence=0.99,
        ),
    ]
    junctions = [
        TopologyJunction(
            junction_id="junction_0022", kind="T",
            point_m=(-76.652961, 34.592683),
            wall_ids=["wall_0001", "wall_0002", "wall_0003"],
        ),
        TopologyJunction(
            junction_id="junction_0048", kind="T",
            point_m=(-76.553480, 34.660416),
            wall_ids=["wall_0001", "wall_0002", "wall_0003"],
        ),
    ]

    retained, labels = _reconcile_post_trim_topology(walls, junctions, 0.15)

    assert retained == []
    assert labels == {0: [], 1: [], 2: []}


def test_opening_label_does_not_turn_an_open_polyline_into_geometry():
    config = _config()
    manifest = SourceManifest(
        tenant_id="tenant",
        project_id="project",
        source_type="dxf",
        sources=[ManifestSource(
            source_file_id="source_0001", path="plan.dxf", role="plan",
            media_type="application/dxf", sha256="0" * 64, size_bytes=0,
        )],
        coordinate=config.coordinate,
        grid=config.grid,
        wall=config.wall,
        column=config.column,
        opening=config.opening,
        level=config.level,
        revit=config.revit,
        config_sha256="0" * 64,
    )
    open_marker = _line(
        "open-marker", [(4.0, -0.2), (6.0, -0.2), (6.0, 0.2), (4.0, 0.2)],
    )
    label = SourceEntity(
        entity_id="label-open",
        source_file_id="source_0001",
        kind="text",
        layer="S-WALL-HO-TEXT",
        geometry={"point": (5.0, 0.8), "text": "JD3"},
        locator="label-open",
        frame_id="source_0001",
    )
    entities = SourceEntities(
        tenant_id="tenant", project_id="project", manifest_sha256="0" * 64,
        entities=[open_marker, label], source_units={"source_0001": "mm"},
    )
    evidence = WallEvidence(
        tenant_id="tenant", project_id="project", source_entities_sha256="0" * 64,
        transform_chain=[
            {"operation": "unit_convert", "scale_to_m_by_source": {"source_0001": 1.0}},
            {"operation": "rotate", "rotation_deg": 0.0},
            {"operation": "translate", "translation_m": [0.0, 0.0]},
        ],
        items=[],
    )
    records = _detect_openings(
        config, manifest, entities, evidence,
        [CandidateWall(
            start=(0.0, 0.0), end=(10.0, 0.0), thickness_m=0.4,
            evidence_ids=["wall-evidence"], source_refs=[EvidenceSourceRef(
                source_file_id="source_0001", entity_id="wall", locator="wall",
                frame_id="source_0001",
            )], confidence=0.99,
        )],
    )
    assert len(records) == 1
    assert records[0].status == "review_required"
    assert records[0].boundary_m == []


def test_exact_hash_profile_registers_reviewed_opening_against_current_wall(
    tmp_path, monkeypatch,
):
    model_path = tmp_path / "wall_model.json"
    model_path.write_text("{}", encoding="utf-8")
    model_path.with_name("source_manifest.json").write_text(
        "{}", encoding="utf-8"
    )
    reference_grid = [
        GridAxis(axis_id="x0", label="A", start_m=(0.0, 0.0), end_m=(0.0, 10.0)),
        GridAxis(axis_id="x1", label="B", start_m=(10.0, 0.0), end_m=(10.0, 10.0)),
        GridAxis(axis_id="y0", label="1", start_m=(0.0, 0.0), end_m=(10.0, 0.0)),
        GridAxis(axis_id="y1", label="2", start_m=(0.0, 10.0), end_m=(10.0, 10.0)),
    ]
    source_ref = EvidenceSourceRef(
        source_file_id="source_0001",
        entity_id="opening-boundary",
        locator="entity:opening",
        layer="S-WALL-HO-TEXT",
        frame_id="source_0001",
    )
    reference_model = WallModel(
        tenant_id="tenant",
        project_id="project",
        wall_evidence_sha256="a" * 64,
        coordinate_origin="source_origin",
        level=LevelConfig(id="level-1", name="Level 1"),
        grid=reference_grid,
        walls=[],
        openings=[OpeningRecord(
            opening_id="opening-reference",
            mark="JD3",
            center_m=(5.0, 0.0),
            boundary_m=[(4.0, -0.2), (6.0, -0.2), (6.0, 0.2), (4.0, 0.2)],
            width_m=2.0,
            depth_m=0.4,
            host_wall_ids=["reference-wall"],
            source_refs=[source_ref],
            confidence=0.95,
            status="matched",
        )],
        gate=ReviewGate(status="pass", checks={"geometry": True}, metrics={}),
    )
    reference_manifest = SourceManifest(
        tenant_id="tenant",
        project_id="project",
        source_type="dxf",
        sources=[ManifestSource(
            source_file_id="source_0001",
            path="reference.dxf",
            role="plan",
            media_type="application/dxf",
            sha256="b" * 64,
            size_bytes=0,
        )],
        coordinate=CoordinateConfig(source_unit="mm"),
        grid=GridConfig(),
        wall=WallRecognitionConfig(),
        column=ColumnRecognitionConfig(),
        opening=OpeningRecognitionConfig(),
        level=LevelConfig(id="level-1", name="Level 1"),
        revit=RevitConfig(),
        config_sha256="0" * 64,
    )
    monkeypatch.setattr(
        wall_geometry,
        "read_artifact",
        lambda _path, schema: (
            reference_model if schema is WallModel else reference_manifest
        ),
    )
    monkeypatch.setattr(wall_geometry, "file_sha256", lambda _path: "m" * 64)
    config = _config()
    config = config.model_copy(update={
        "column": config.column.model_copy(update={
            "approved_profile_model_path": str(model_path),
            "approved_profile_model_sha256": "m" * 64,
            "approved_profile_input_sha256": "c" * 64,
            "approved_profile_source_sha256": "b" * 64,
            "approved_profile_min_axis_match_ratio": 0.75,
        }),
    })
    manifest = SourceManifest(
        tenant_id="tenant",
        project_id="project",
        source_type="pdf",
        sources=[ManifestSource(
            source_file_id="source_0001",
            path="plan.pdf",
            role="plan",
            media_type="application/pdf",
            sha256="c" * 64,
            size_bytes=0,
            page_no=1,
        )],
        coordinate=config.coordinate,
        grid=config.grid,
        wall=config.wall,
        column=config.column,
        opening=config.opening,
        level=config.level,
        revit=config.revit,
        config_sha256="0" * 64,
    )
    walls = [CandidateWall(
        start=(0.0, 0.0),
        end=(10.0, 0.0),
        thickness_m=0.4,
        evidence_ids=["wall-evidence"],
        source_refs=[source_ref],
        confidence=0.99,
    )]

    records = _approved_profile_openings(
        config, manifest, reference_grid, [], walls, (-1.0, -1.0, 11.0, 11.0)
    )

    assert len(records) == 1
    assert records[0].mark == "JD3"
    assert records[0].status == "matched"
    assert records[0].host_wall_ids == ["wall_0001"]
    assert records[0].source_refs[0].source_file_id.startswith(
        "approved_training_"
    )


def test_approved_irregular_profile_requires_current_one_to_one_mark_evidence(
    tmp_path, monkeypatch,
):
    model_path = tmp_path / "wall_model.json"
    model_path.write_text("{}", encoding="utf-8")
    model_path.with_name("source_manifest.json").write_text("{}", encoding="utf-8")
    grid = [
        GridAxis(axis_id="x0", label="A", start_m=(0.0, 0.0), end_m=(0.0, 10.0)),
        GridAxis(axis_id="x1", label="B", start_m=(10.0, 0.0), end_m=(10.0, 10.0)),
        GridAxis(axis_id="y0", label="1", start_m=(0.0, 0.0), end_m=(10.0, 0.0)),
        GridAxis(axis_id="y1", label="2", start_m=(0.0, 10.0), end_m=(10.0, 10.0)),
    ]
    reference_ref = EvidenceSourceRef(
        source_file_id="source_0001", entity_id="reference-profile",
        locator="profile:reference", layer="S-WALL-HATC", frame_id="source_0001",
    )
    reference_columns = [
        ColumnRecord(
            column_id="reference-gbz11", center_m=(5.0, 5.0),
            profile_m=[(-0.5, -0.5), (0.5, -0.5), (0.5, 0.0),
                       (0.0, 0.0), (0.0, 0.5), (-0.5, 0.5)],
            width_m=1.0, depth_m=1.0, height_m=3.0, level_id="level-1",
            source_refs=[reference_ref], confidence=0.98,
            profile_kind="irregular", type_mark="GBZ11",
        ),
        ColumnRecord(
            column_id="reference-gbz12", center_m=(5.2, 5.2),
            profile_m=[(-0.8, -0.8), (0.8, -0.8), (0.8, 0.2),
                       (0.2, 0.2), (0.2, 0.8), (-0.8, 0.8)],
            width_m=1.6, depth_m=1.6, height_m=3.0, level_id="level-1",
            source_refs=[reference_ref], confidence=0.98,
            profile_kind="irregular", type_mark="GBZ12",
        ),
    ]
    reference_model = WallModel(
        tenant_id="tenant", project_id="project",
        wall_evidence_sha256="a" * 64, coordinate_origin="source_origin",
        level=LevelConfig(id="level-1", name="Level 1"), grid=grid,
        walls=[], columns=reference_columns,
        gate=ReviewGate(status="pass", checks={"geometry": True}, metrics={}),
    )
    reference_manifest = SourceManifest(
        tenant_id="tenant", project_id="project", source_type="dxf",
        sources=[ManifestSource(
            source_file_id="source_0001", path="reference.dxf", role="plan",
            media_type="application/dxf", sha256="b" * 64, size_bytes=0,
        )],
        coordinate=CoordinateConfig(source_unit="mm"), grid=GridConfig(),
        wall=WallRecognitionConfig(), column=ColumnRecognitionConfig(),
        opening=OpeningRecognitionConfig(),
        level=LevelConfig(id="level-1", name="Level 1"), revit=RevitConfig(),
        config_sha256="0" * 64,
    )
    monkeypatch.setattr(
        wall_geometry, "read_artifact",
        lambda _path, schema: reference_model if schema is WallModel else reference_manifest,
    )
    monkeypatch.setattr(wall_geometry, "file_sha256", lambda _path: "d" * 64)
    config = _config().model_copy(update={
        "column": ColumnRecognitionConfig(
            label_layers=["OCR"],
            approved_profile_model_path=str(model_path),
            approved_profile_model_sha256="d" * 64,
            approved_profile_input_sha256="c" * 64,
            approved_profile_source_sha256="b" * 64,
        ),
    })
    manifest = SourceManifest(
        tenant_id="tenant", project_id="project", source_type="pdf",
        sources=[ManifestSource(
            source_file_id="source_0001", path="plan.pdf", role="plan",
            media_type="application/pdf", sha256="c" * 64, size_bytes=0,
            page_no=1,
        )],
        coordinate=config.coordinate, grid=config.grid, wall=config.wall,
        column=config.column, opening=config.opening, level=config.level,
        revit=config.revit, config_sha256="0" * 64,
    )
    current_label = SourceEntity(
        entity_id="current-gbz12", source_file_id="source_0001", page_no=1,
        kind="text", layer="OCR", geometry={"point": [5.2, 5.2], "text": "GBZ12"},
        locator="page:1/ocr:gbz12", frame_id="source_0001:page:0001",
    )

    records = _approved_profile_columns(
        config, manifest, grid, [], (-1.0, -1.0, 11.0, 11.0),
        [(current_label, "GBZ12", Point(5.2, 5.2))],
    )

    assert len(records) == 1
    assert records[0][0].type_mark == "GBZ12"
    assert any(ref.entity_id == "current-gbz12" for ref in records[0][0].source_refs)
