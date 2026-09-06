import json
from pathlib import Path

import cv2
import ezdxf
import numpy as np
import pytest

from backend.engines.wall_pipeline import (
    RevitResult,
    SourceEntities,
    WallEvidence,
    WallModel,
    approve_revit_write,
    approve_wall_model,
    dry_run_wall_model,
    load_wall_pipeline_config,
    run_wall_pipeline,
)
from backend.engines.wall_pipeline.io import (
    canonical_sha256,
    file_sha256,
    read_artifact,
    write_artifact,
)
from backend.engines.wall_pipeline.pipeline import finalize_audit
from backend.engines.wall_pipeline.geometry import (
    CandidateWall,
    _trim_wall_junctions,
)
from backend.engines.wall_pipeline.contracts import TopologyJunction
from workers.pyrevit.wall_model_contract import normalize_wall_model_payload


def test_t_junction_trimming_keeps_core_wall_faces_closed():
    """A split through-wall must meet a terminating wall at its outer face."""

    walls = [
        CandidateWall((0.0, 0.0), (0.0, 2.0), 0.5, [], [], 1.0),
        CandidateWall((0.0, 2.0), (0.0, 4.0), 0.5, [], [], 1.0),
        CandidateWall((0.0, 2.0), (3.0, 2.0), 0.3, [], [], 1.0),
    ]
    junction = TopologyJunction(
        junction_id="junction_0001", kind="T", point_m=(0.0, 2.0),
        wall_ids=["wall_0001", "wall_0002", "wall_0003"],
    )

    _trim_wall_junctions(walls, [junction], tolerance=0.15)

    # The split vertical faces are separated by exactly the terminating
    # wall's 300 mm thickness; their solids therefore touch it without a
    # visible open gap or a double-trimmed 800 mm void.
    assert walls[0].end[1] == pytest.approx(1.84999, abs=1.0e-6)
    assert walls[1].start[1] == pytest.approx(2.15001, abs=1.0e-6)
    assert walls[2].start[0] == pytest.approx(0.25001, abs=1.0e-6)


def test_publish_bundle_restores_previous_run_when_one_replace_fails(
    tmp_path: Path, monkeypatch
):
    """A failed publish must not leave a mixed six-file engineering bundle."""

    import backend.engines.wall_pipeline.pipeline as pipeline_module

    output = tmp_path / "output"
    staging = output / ".stage"
    staging.mkdir(parents=True)
    previous = {}
    for name in pipeline_module._PUBLISHED_FILENAMES:
        destination = output / name
        destination.write_text("previous-" + name, encoding="utf-8")
        previous[name] = destination.read_bytes()
        (staging / name).write_text("new-" + name, encoding="utf-8")

    original_replace = pipeline_module.os.replace
    staged_moves = 0

    def fail_on_second_stage_move(source, destination):
        nonlocal staged_moves
        source_path = Path(source)
        if source_path.parent == staging:
            staged_moves += 1
            if staged_moves == 2:
                raise OSError("simulated publish lock")
        return original_replace(source, destination)

    monkeypatch.setattr(pipeline_module.os, "replace", fail_on_second_stage_move)
    with pytest.raises(RuntimeError, match="previous bundle restored"):
        pipeline_module._publish_run_artifacts(staging, output)

    for name, expected in previous.items():
        assert (output / name).read_bytes() == expected


def test_publish_bundle_retains_backup_when_rollback_itself_fails(
    tmp_path: Path, monkeypatch
):
    """A failed restore must leave an operator-recoverable old bundle."""

    import backend.engines.wall_pipeline.pipeline as pipeline_module

    output = tmp_path / "output"
    staging = output / ".stage"
    staging.mkdir(parents=True)
    for name in pipeline_module._PUBLISHED_FILENAMES:
        (output / name).write_text("previous-" + name, encoding="utf-8")
        (staging / name).write_text("new-" + name, encoding="utf-8")

    original_replace = pipeline_module.os.replace
    staged_moves = 0
    retained_backup: list[Path] = []

    def fail_publish_and_restore(source, destination):
        nonlocal staged_moves
        source_path = Path(source)
        if source_path.parent == staging:
            staged_moves += 1
            if staged_moves == 2:
                raise OSError("simulated publish lock")
        # Fail one restore after the old file has already been moved into the
        # backup directory.  The publisher must retain that directory.
        if source_path.parent.name.startswith(".wall-publish-backup-") and not retained_backup:
            retained_backup.append(source_path)
            raise OSError("simulated restore lock")
        return original_replace(source, destination)

    monkeypatch.setattr(pipeline_module.os, "replace", fail_publish_and_restore)
    with pytest.raises(RuntimeError, match="rollback was incomplete") as error:
        pipeline_module._publish_run_artifacts(staging, output)

    assert retained_backup and retained_backup[0].is_file()
    assert "recovery backup retained at" in str(error.value)
    assert retained_backup[0].read_text(encoding="utf-8").startswith("previous-")


def _closed_wall(msp, start, end, thickness, offset=(0.0, 0.0)):
    x1, y1 = start[0] + offset[0], start[1] + offset[1]
    x2, y2 = end[0] + offset[0], end[1] + offset[1]
    dx, dy = x2 - x1, y2 - y1
    length = (dx * dx + dy * dy) ** 0.5
    nx, ny = -dy / length * thickness / 2, dx / length * thickness / 2
    points = [
        (x1 + nx, y1 + ny), (x2 + nx, y2 + ny),
        (x2 - nx, y2 - ny), (x1 - nx, y1 - ny),
    ]
    msp.add_lwpolyline(points, close=True, dxfattribs={"layer": "GEOMETRY-WALL"})


def _write_z_dxf(path: Path, offset=(0.0, 0.0)):
    document = ezdxf.new()
    document.header["$INSUNITS"] = 4  # millimetres
    document.layers.add("GEOMETRY-WALL")
    msp = document.modelspace()
    _closed_wall(msp, (0, 0), (2000, 0), 200, offset)
    _closed_wall(msp, (2000, 0), (2000, 2000), 200, offset)
    _closed_wall(msp, (2000, 2000), (4000, 2000), 200, offset)
    document.saveas(path)


def _write_t_dxf(path: Path):
    document = ezdxf.new()
    document.header["$INSUNITS"] = 4
    document.layers.add("GEOMETRY-WALL")
    msp = document.modelspace()
    _closed_wall(msp, (0, 0), (4000, 0), 200)
    _closed_wall(msp, (2000, 0), (2000, 2000), 200)
    document.saveas(path)


def _write_config(path: Path, source: Path, output: Path, source_origin=(0, 0)):
    path.write_text(
        f"""schema_version: buildmate.wall-pipeline-config/1.0
tenant_id: tenant-test
project_id: project-test
source:
  type: dxf
  files:
    - path: {source.name}
      role: plan
coordinate:
  origin: revit_project_base_point
  source_unit: auto
  source_origin: [{source_origin[0]}, {source_origin[1]}]
  rotation_deg: 0.0
wall:
  include_layers: [GEOMETRY-WALL]
  min_thickness_m: 0.075
  max_thickness_m: 0.6
  break_gap_m: 0.06
  junction_tolerance_m: 0.15
level:
  id: level-1
  name: Main
  elevation_m: 0.0
  wall_height_m: 3.2
revit:
  target_model_path: model.rvt
  floor_code: MAIN
output_dir: {output.name}
""",
        encoding="utf-8",
    )


def test_config_only_source_switch_keeps_contract_and_z_topology(tmp_path: Path):
    first_dxf, second_dxf = tmp_path / "first.dxf", tmp_path / "second.dxf"
    _write_z_dxf(first_dxf)
    _write_z_dxf(second_dxf, offset=(10000, 5000))
    first_config, second_config = tmp_path / "first.yaml", tmp_path / "second.yaml"
    _write_config(first_config, first_dxf, tmp_path / "first-output")
    _write_config(
        second_config, second_dxf, tmp_path / "second-output",
        source_origin=(10000, 5000),
    )

    first_paths = run_wall_pipeline(first_config)
    second_paths = run_wall_pipeline(second_config)

    assert set(first_paths) == {
        "source_manifest.json", "source_entities.json", "wall_evidence.json",
        "wall_model.json", "revit_result.json", "audit_report.json",
    }
    assert all(path.is_file() for path in first_paths.values())
    first = read_artifact(first_paths["wall_model.json"], WallModel)
    second = read_artifact(second_paths["wall_model.json"], WallModel)
    assert first.schema_version == second.schema_version == "buildmate.wall-model/1.0"
    assert len(first.walls) == len(second.walls) == 3
    assert [wall.start_m for wall in first.walls] == [wall.start_m for wall in second.walls]
    assert any(junction.kind == "Z" for junction in first.junctions)
    assert any(junction.kind == "L" for junction in first.junctions)
    assert all(wall.evidence_ids and wall.source_refs for wall in first.walls)
    assert all(wall.construction is not None for wall in first.walls)
    assert all(wall.quantities is not None for wall in first.walls)
    assert all(wall.quantities.gross_volume_m3 > 0 for wall in first.walls)
    assert all(wall.construction.material_status == "unspecified" for wall in first.walls)
    assert first.gate.status == "pass"
    pending = json.loads(first_paths["revit_result.json"].read_text(encoding="utf-8"))
    assert pending["status"] == "pending_approval"


@pytest.mark.parametrize("declarations,width_mm,expected", [
    (["Q6 800"], 800.09, 2),
    (["Q9 950"], 950.09, 2),
    ([], 800.09, 0),
    (["Q6 800"], 700.0, 0),
    (["Q6 800", "Q6 700"], 800.09, 0),
])
def test_declared_thick_walls_are_paired_without_widening_unproven_sizes(
    tmp_path: Path, declarations, width_mm, expected,
):
    drawing, config_path = tmp_path / "thick.dxf", tmp_path / "pipeline.yaml"
    document = ezdxf.new()
    document.header["$INSUNITS"] = 4
    document.layers.add("GEOMETRY-WALL")
    space = document.modelspace()
    for y in (0, 5000):
        _closed_wall(space, (0, y), (4000, y), width_mm)
    for index, text in enumerate(declarations):
        space.add_text(text, dxfattribs={"insert": (20000, 20000 + index * 5000)})
    document.saveas(drawing)
    _write_config(config_path, drawing, tmp_path / "output")

    paths = run_wall_pipeline(config_path)
    evidence = read_artifact(paths["wall_evidence.json"], WallEvidence)
    model = read_artifact(paths["wall_model.json"], WallModel)

    assert len(evidence.items) == len(model.walls) == expected
    assert load_wall_pipeline_config(config_path).wall.max_thickness_m == .6
    if expected:
        assert all(item.thickness_m == pytest.approx(width_mm / 1000) for item in evidence.items)
        assert all(len(item.source_refs) >= 2 for item in evidence.items)
        declaration = next(item for item in evidence.diagnostics if item["code"] == "WALL_DECLARED_THICKNESS_SEARCH")
        assert declaration["declarations"][0]["evidence_entity_ids"]


def test_thick_wall_pair_does_not_borrow_another_source_frame_schedule(tmp_path: Path):
    drawing, legend, config_path = tmp_path / "wall.dxf", tmp_path / "legend.dxf", tmp_path / "pipeline.yaml"
    document = ezdxf.new()
    document.header["$INSUNITS"] = 4
    document.layers.add("GEOMETRY-WALL")
    _closed_wall(document.modelspace(), (0, 0), (4000, 0), 800)
    document.saveas(drawing)
    legend_document = ezdxf.new()
    legend_document.header["$INSUNITS"] = 4
    legend_document.modelspace().add_text("Q6 800", dxfattribs={"insert": (0, 0)})
    legend_document.saveas(legend)
    _write_config(config_path, drawing, tmp_path / "output")
    config = load_wall_pipeline_config(config_path)
    config.source.files.append(config.source.files[0].model_copy(update={"path": str(legend)}))

    paths = run_wall_pipeline(config)

    assert read_artifact(paths["wall_evidence.json"], WallEvidence).items == []


def test_source_entity_cache_reuses_immutable_parse_across_task_outputs(
    tmp_path: Path, monkeypatch
):
    """Retries must not pay the PDF/DXF extraction cost twice."""

    import backend.engines.wall_pipeline.pipeline as pipeline_module

    drawing = tmp_path / "cached.dxf"
    _write_z_dxf(drawing)
    first_config = tmp_path / "first.yaml"
    second_config = tmp_path / "second.yaml"
    _write_config(first_config, drawing, tmp_path / "first-output")
    _write_config(second_config, drawing, tmp_path / "second-output")

    original_extract = pipeline_module.extract_source_entities
    calls = 0

    def counted_extract(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "extract_source_entities", counted_extract)
    run_wall_pipeline(first_config)
    run_wall_pipeline(second_config)

    assert calls == 1
    assert list((tmp_path / ".source-cache").glob("*.json"))


def test_wall_model_carries_bounds_from_source_entities(tmp_path: Path):
    """The audit frame must be derived from source geometry, not generated walls."""

    dxf = tmp_path / "drawing.dxf"
    config = tmp_path / "pipeline.yaml"
    _write_z_dxf(dxf)
    _write_config(config, dxf, tmp_path / "output")

    paths = run_wall_pipeline(config)
    model = read_artifact(paths["wall_model.json"], WallModel)
    entities = read_artifact(paths["source_entities.json"], SourceEntities)
    bounds = model.source_bounds_m
    assert bounds is not None
    source_points = [
        point
        for entity in entities.entities
        for point in (
            [entity.geometry["start"], entity.geometry["end"]]
            if "start" in entity.geometry and "end" in entity.geometry
            else entity.geometry.get("points") or []
        )
    ]
    assert bounds[0] <= min(float(point[0]) / 1000 for point in source_points)
    assert bounds[1] <= min(float(point[1]) / 1000 for point in source_points)
    assert bounds[2] >= max(float(point[0]) / 1000 for point in source_points)
    assert bounds[3] >= max(float(point[1]) / 1000 for point in source_points)


def test_required_grid_excludes_detached_same_layer_detail_from_model_and_audit_frame(
    tmp_path: Path,
):
    """A detail box outside the main grid is not a second floor plan."""

    document = ezdxf.new()
    document.header["$INSUNITS"] = 4
    document.layers.add("GEOMETRY-WALL")
    document.layers.add("GRID")
    modelspace = document.modelspace()
    _closed_wall(modelspace, (0, 0), (4000, 0), 200)
    _closed_wall(modelspace, (0, -20000), (4000, -20000), 200)
    for x in (0, 4000):
        modelspace.add_line((x, -1000), (x, 5000), dxfattribs={"layer": "GRID"})
    for y in (0, 4000):
        modelspace.add_line((-1000, y), (5000, y), dxfattribs={"layer": "GRID"})
    drawing = tmp_path / "main-with-detail.dxf"
    document.saveas(drawing)
    config = tmp_path / "pipeline.yaml"
    config.write_text(
        f"""schema_version: buildmate.wall-pipeline-config/1.0
tenant_id: tenant-test
project_id: project-test
source:
  type: dxf
  files:
    - path: {drawing.name}
      role: plan
coordinate:
  origin: source_origin
  source_unit: auto
  rotation_deg: 0.0
grid:
  include_layers: [GRID]
  min_length_m: 4.0
  required: true
wall:
  include_layers: [GEOMETRY-WALL]
level:
  id: level-1
  name: Main
  wall_height_m: 3.0
output_dir: output
""",
        encoding="utf-8",
    )

    paths = run_wall_pipeline(config)
    model = read_artifact(paths["wall_model.json"], WallModel)

    assert len(model.walls) == 1
    assert model.gate.metrics["wall_candidate_count_before_grid_frame"] == 2
    assert model.gate.metrics["wall_rejected_outside_grid_frame_count"] == 1
    assert model.source_bounds_m is not None
    assert model.source_bounds_m[1] > -10.0


def test_input_identity_hashes_ignore_task_and_output_paths(tmp_path: Path):
    """The same source bytes produce the same upstream lineage in new workspaces."""

    first_root = tmp_path / "worker-a"
    second_root = tmp_path / "worker-b"
    first_root.mkdir()
    second_root.mkdir()
    first_dxf = first_root / "plan.dxf"
    second_dxf = second_root / "renamed-plan.dxf"
    _write_z_dxf(first_dxf)
    second_dxf.write_bytes(first_dxf.read_bytes())

    first_config = first_root / "pipeline.yaml"
    second_config = second_root / "pipeline.yaml"
    _write_config(first_config, first_dxf, first_root / "output")
    _write_config(second_config, second_dxf, second_root / "output")
    first_paths = run_wall_pipeline(first_config)
    second_paths = run_wall_pipeline(second_config)

    for name in (
        "source_manifest.json",
        "source_entities.json",
        "wall_evidence.json",
        "wall_model.json",
    ):
        first_payload = json.loads(first_paths[name].read_text(encoding="utf-8"))
        second_payload = json.loads(second_paths[name].read_text(encoding="utf-8"))
        assert canonical_sha256(first_payload) == canonical_sha256(second_payload)

    first_model = approve_wall_model(
        first_paths["wall_model.json"], actor_id="reviewer", reason="checked"
    )
    second_model = approve_wall_model(
        second_paths["wall_model.json"], actor_id="reviewer", reason="checked"
    )
    from workers.pyrevit.wall_model_contract import normalize_wall_model_payload
    from workers.revit_bridge.wall_compiler import compiled_plan_sha256

    assert compiled_plan_sha256(
        normalize_wall_model_payload(first_model.model_dump(mode="json"))
    ) == compiled_plan_sha256(
        normalize_wall_model_payload(second_model.model_dump(mode="json"))
    )

    # Operational paths stay available for replay, but are deliberately not
    # part of the deterministic identity.
    first_manifest = json.loads(
        first_paths["source_manifest.json"].read_text(encoding="utf-8")
    )
    second_manifest = json.loads(
        second_paths["source_manifest.json"].read_text(encoding="utf-8")
    )
    assert first_manifest["sources"][0]["path"] != second_manifest["sources"][0]["path"]


def test_cli_model_approval_rejects_tampered_upstream_evidence(tmp_path: Path):
    """A local CLI gate must not approve a model whose evidence sibling changed."""

    dxf = tmp_path / "drawing.dxf"
    config = tmp_path / "pipeline.yaml"
    _write_z_dxf(dxf)
    _write_config(config, dxf, tmp_path / "output")
    paths = run_wall_pipeline(config)

    evidence_path = paths["wall_evidence.json"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["diagnostics"].append({"severity": "info", "code": "tampered"})
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(ValueError, match="source_entities|wall_model"):
        approve_wall_model(
            paths["wall_model.json"], actor_id="reviewer", reason="checked"
        )


def test_cli_model_approval_rejects_changed_source_bytes(tmp_path: Path):
    """Changing the drawing after preparation invalidates the local approval gate."""

    dxf = tmp_path / "drawing.dxf"
    config = tmp_path / "pipeline.yaml"
    _write_z_dxf(dxf)
    _write_config(config, dxf, tmp_path / "output")
    paths = run_wall_pipeline(config)
    dxf.write_bytes(dxf.read_bytes() + b"changed-after-run")

    with pytest.raises(ValueError, match="source file no longer matches"):
        approve_wall_model(
            paths["wall_model.json"], actor_id="reviewer", reason="checked"
        )


def test_cli_revit_approval_rechecks_upstream_lineage(tmp_path: Path):
    """The second CLI gate must re-check sources after the dry-run pause."""

    dxf = tmp_path / "drawing.dxf"
    config = tmp_path / "pipeline.yaml"
    _write_z_dxf(dxf)
    _write_config(config, dxf, tmp_path / "output")
    (tmp_path / "model.rvt").write_bytes(b"deterministic-rvt-fixture")
    paths = run_wall_pipeline(config)
    approve_wall_model(paths["wall_model.json"], actor_id="reviewer", reason="checked")
    dry_run_wall_model(paths["wall_model.json"])

    dxf.write_bytes(dxf.read_bytes() + b"changed-before-write-approval")
    with pytest.raises(ValueError, match="source file no longer matches"):
        approve_revit_write(
            paths["wall_model.json"], actor_id="revit-reviewer", reason="release"
        )


def test_t_wall_topology_is_audited_without_project_specific_logic(tmp_path: Path):
    dxf = tmp_path / "t-junction.dxf"
    config = tmp_path / "pipeline.yaml"
    _write_t_dxf(dxf)
    _write_config(config, dxf, tmp_path / "output")

    paths = run_wall_pipeline(config)
    model = read_artifact(paths["wall_model.json"], WallModel)

    assert len(model.walls) == 2
    assert any(junction.kind == "T" for junction in model.junctions)


def test_revit_worker_rejects_pending_and_accepts_approved_wall_model(tmp_path: Path):
    dxf = tmp_path / "drawing.dxf"
    config = tmp_path / "pipeline.yaml"
    _write_z_dxf(dxf)
    _write_config(config, dxf, tmp_path / "output")
    # Dry-run is intentionally bound to a real target snapshot.  Keep the
    # fixture deterministic instead of relying on a developer workstation
    # path or a hidden Revit installation.
    (tmp_path / "model.rvt").write_bytes(b"deterministic-rvt-fixture")
    paths = run_wall_pipeline(config)
    pending = json.loads(paths["wall_model.json"].read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="has not been approved"):
        normalize_wall_model_payload(pending)

    approved = approve_wall_model(
        paths["wall_model.json"], actor_id="reviewer-1",
        reason="geometry and topology reviewed",
    )
    payload = normalize_wall_model_payload(approved.model_dump(mode="json"))

    assert payload["build"]["source_artifact_type"] == "wall_model"
    assert payload["build"]["approval"]["actor_id"] == "reviewer-1"
    assert payload["build"]["wall_model_sha256"] == canonical_sha256(approved)
    assert [item["type"] for item in payload["model_elements"]] == ["Wall"] * 3
    assert payload["model_elements"][0]["source_evidence_ids"]

    ready = read_artifact(paths["revit_result.json"], RevitResult)
    assert ready.status == "ready_for_dry_run"
    dry_run = dry_run_wall_model(paths["wall_model.json"])
    assert dry_run.status == "dry_run_passed"
    assert dry_run.readback["wall_count"] == 3
    write_approved = approve_revit_write(
        paths["wall_model.json"], actor_id="revit-approver",
        reason="dry-run reviewed",
    )
    assert write_approved.status == "write_approved"
    assert write_approved.approval.actor_id == "revit-approver"


def test_pyrevit_exports_actual_view_and_new_revit_result_contract():
    script = (Path(__file__).resolve().parents[1] / "workers" / "pyrevit" /
              "json2rvt_script.py").read_text(encoding="utf-8")

    assert "export_actual_plan_view" in script
    assert "doc.ExportImage(options)" in script
    assert '"buildmate.revit-result/1.0"' in script
    assert 'revit_gate.get("status") != "write_approved"' in script
    assert 'os.path.join(JSON_DIR, "revit_result.json")' in script


def test_pdf_adapter_needs_scale_and_produces_same_evidence_contract(tmp_path: Path):
    import pymupdf as fitz

    pdf = tmp_path / "plan.pdf"
    document = fitz.open()
    page = document.new_page(width=200, height=120)
    page.draw_line((10, 30), (110, 30), width=1)
    page.draw_line((10, 40), (110, 40), width=1)
    document.save(pdf)
    document.close()
    config = tmp_path / "pdf.yaml"
    config.write_text(
        f"""schema_version: buildmate.wall-pipeline-config/1.0
tenant_id: tenant-test
project_id: pdf-project
source:
  type: pdf
  files:
    - path: {pdf.name}
      role: plan
coordinate:
  source_unit: auto
  scale_to_m: 0.02
level:
  id: level-1
  name: Main
revit:
  target_model_path: model.rvt
  floor_code: MAIN
output_dir: pdf-output
""",
        encoding="utf-8",
    )

    paths = run_wall_pipeline(config)
    evidence = json.loads(paths["wall_evidence.json"].read_text(encoding="utf-8"))

    assert evidence["schema_version"] == "buildmate.wall-evidence/1.0"
    assert len(evidence["items"]) == 1
    assert evidence["items"][0]["thickness_m"] == pytest.approx(0.2)
    assert evidence["items"][0]["source_refs"][0]["page_no"] == 1


def test_independent_overlay_requires_actual_revit_render(tmp_path: Path):
    dxf = tmp_path / "drawing.dxf"
    config = tmp_path / "pipeline.yaml"
    _write_z_dxf(dxf)
    _write_config(config, dxf, tmp_path / "output")
    paths = run_wall_pipeline(config)
    wall_model = approve_wall_model(
        paths["wall_model.json"], actor_id="reviewer-1", reason="approved"
    )
    source_render = paths["wall_model.json"].parent / "source_render.png"
    source_image = cv2.imread(str(source_render))
    # An independently written view with one harmless pixel difference keeps
    # provenance distinct while retaining the same geometry for this test.
    revit_image = source_image.copy()
    revit_image[0, 0] = (254, 254, 254)
    revit_view = tmp_path / "actual-revit-view.png"
    assert cv2.imwrite(str(revit_view), revit_image)
    result = RevitResult(
        tenant_id=wall_model.tenant_id, project_id=wall_model.project_id,
        wall_model_sha256=canonical_sha256(wall_model), status="succeeded",
        transaction_id="tx-1",
        created_element_ids=["revit-wall-1", "revit-wall-2", "revit-wall-3"],
        readback={"wall_count": 3}, actual_view_path=str(revit_view),
        actual_view_sha256=file_sha256(revit_view),
    )
    write_artifact(paths["revit_result.json"], result)

    report = finalize_audit(
        paths["wall_model.json"], paths["revit_result.json"],
        minimum_edge_iou=0.99,
    )

    assert report.status == "pass"
    assert report.independent_sources is True
    assert report.overlay_path and Path(report.overlay_path).is_file()


def test_yaml_paths_are_resolved_relative_to_config(tmp_path: Path):
    dxf = tmp_path / "drawing.dxf"
    config = tmp_path / "pipeline.yaml"
    _write_z_dxf(dxf)
    _write_config(config, dxf, tmp_path / "relative-output")

    loaded = load_wall_pipeline_config(config)

    assert Path(loaded.source.files[0].path) == dxf.resolve()
    assert Path(loaded.output_dir) == (tmp_path / "relative-output").resolve()


def test_dwg_adapter_uses_oda_directory_contract(tmp_path: Path, monkeypatch):
    dwg = tmp_path / "plan.dwg"
    dwg.write_bytes(b"synthetic-dwg-placeholder")
    executable = tmp_path / "ODAFileConverter.exe"
    executable.write_bytes(b"placeholder")
    output = tmp_path / "dwg-output"
    config = tmp_path / "dwg.yaml"
    config.write_text(
        f"""schema_version: buildmate.wall-pipeline-config/1.0
tenant_id: tenant-test
project_id: dwg-project
source:
  type: dwg
  files:
    - path: {dwg.name}
      role: plan
  converter:
    executable: {executable.as_posix()}
coordinate:
  source_unit: mm
wall:
  include_layers: [GEOMETRY-WALL]
level:
  id: level-1
  name: Main
revit:
  target_model_path: model.rvt
  floor_code: MAIN
output_dir: {output.name}
""",
        encoding="utf-8",
    )
    calls = []

    def fake_oda(command, **kwargs):
        calls.append((command, kwargs))
        converted = Path(command[2]) / "plan.dxf"
        _write_t_dxf(converted)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(
        "backend.engines.wall_pipeline.adapters.subprocess.run", fake_oda
    )

    paths = run_wall_pipeline(config)

    assert calls
    command = calls[0][0]
    assert Path(command[0]).resolve() == executable.resolve()
    assert command[-4:] == ["ACAD2018", "DXF", "0", "1"]
    model = read_artifact(paths["wall_model.json"], WallModel)
    assert len(model.walls) == 2
