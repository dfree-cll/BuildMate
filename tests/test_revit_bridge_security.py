from pathlib import Path
import hashlib
import re

import pytest

from backend.adapters.build_repository import BuildRepository
from backend.domain.contracts import RequestContext
from backend.domain.errors import DependencyFailure, PolicyFailure
from backend.domain.approval_token import (
    approval_digest,
    issue_approval_token,
    verify_approval_token,
)
from workers.revit_bridge.contracts import (
    PresentWallModelRequest,
    RunScriptRequest,
    RunWallModelRequest,
)
from workers.revit_bridge.security import has_explicit_transaction, validate_revit_script
from workers.revit_bridge.service import (
    RevitBridgeService,
    _compile_wall_model_script,
    _require_success_response,
    _wrap_working_copy_script,
    _write_delivery_receipt,
)
from backend.engines.wall_pipeline.contracts import (
    ApprovalRecord,
    ColumnRecord,
    EvidenceSourceRef,
    LevelConfig,
    RevitResult,
    ReviewGate,
    WallModel,
    WallRecord,
)
from backend.engines.wall_pipeline.io import canonical_sha256, file_sha256
from workers.pyrevit.wall_model_contract import normalize_wall_model_payload


SAFE_SCRIPT = """
from Autodesk.Revit.DB import Transaction
t = Transaction(doc, "BuildMate")
t.Start()
try:
    print("validated")
    t.Commit()
except Exception:
    t.RollBack()
    raise
"""


def _script_sha256(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def test_static_gate_blocks_filesystem_process_network_and_dynamic_code():
    script = "import os\nopen('x', 'w')\nos.system('whoami')\n"
    codes = {item.code for item in validate_revit_script(script)}
    assert "import_not_allowed" in codes
    assert "name_not_allowed" in codes
    assert "attribute_not_allowed" in codes


def test_safe_revit_transaction_is_accepted():
    assert validate_revit_script(SAFE_SCRIPT) == []
    assert has_explicit_transaction(SAFE_SCRIPT)


@pytest.mark.asyncio
async def test_dry_run_never_calls_revit(tmp_path: Path):
    model = tmp_path / "source.rvt"
    model.write_bytes(b"deterministic-fixture")
    result = await RevitBridgeService().run_script(RunScriptRequest(
        build_id="build_001",
        source_model_path=str(model),
        script=SAFE_SCRIPT,
        dry_run=True,
    ))
    assert result.status == "validated"
    assert result.dry_run is True
    assert result.output_path is None


@pytest.mark.asyncio
async def test_write_is_fail_closed_without_enabled_bridge(tmp_path: Path):
    model = tmp_path / "source.rvt"
    model.write_bytes(b"fixture")
    with pytest.raises(PolicyFailure):
        await RevitBridgeService().run_script(RunScriptRequest(
            build_id="build_002",
            source_model_path=str(model),
            script=SAFE_SCRIPT,
            dry_run=False,
            approval_token="not-valid",
        ))


@pytest.mark.asyncio
async def test_live_legacy_script_is_disabled_by_default(tmp_path: Path):
    """The generic arbitrary-script endpoint stays deny-by-default."""

    source = tmp_path / "source.rvt"
    source.write_bytes(b"fixture")
    service = RevitBridgeService()
    service.settings.revit_bridge_execution_enabled = True
    service.settings.revit_bridge_legacy_script_execution_enabled = False
    service.settings.revit_bridge_approval_secret = "d" * 32

    with pytest.raises(PolicyFailure, match="legacy arbitrary Revit script execution is disabled"):
        await service.run_script(RunScriptRequest(
            build_id="build-legacy-disabled",
            source_model_path=str(source),
            script=SAFE_SCRIPT,
            dry_run=False,
        ))


@pytest.mark.asyncio
async def test_live_legacy_script_rejects_a_script_swap(tmp_path: Path):
    """A token for one script cannot authorize a different script."""

    source = tmp_path / "source.rvt"
    source.write_bytes(b"fixture")
    secret = "e" * 32
    service = RevitBridgeService()
    service.settings.revit_bridge_execution_enabled = True
    service.settings.revit_bridge_legacy_script_execution_enabled = True
    service.settings.revit_bridge_approval_secret = secret
    service.settings.revit_bridge_work_root = str(tmp_path / "bridge")

    original_hash = _script_sha256(SAFE_SCRIPT)
    source_hash = file_sha256(source)
    token = issue_approval_token(
        secret,
        build_id="build-script-swap",
        action="run_revit_script",
        script_sha256=original_hash,
        target_model_path=str(source),
        source_model_sha256=source_hash,
    )
    changed_script = SAFE_SCRIPT + "\n# changed after approval\n"
    changed_hash = _script_sha256(changed_script)

    with pytest.raises(PolicyFailure, match="invalid or expired Revit approval token"):
        await service.run_script(RunScriptRequest(
            build_id="build-script-swap",
            source_model_path=str(source),
            script=changed_script,
            script_sha256=changed_hash,
            source_model_sha256=source_hash,
            dry_run=False,
            approval_token=token,
        ))


@pytest.mark.asyncio
async def test_build_repository_never_mints_unbound_generic_script_token(monkeypatch):
    """The persistence adapter must require all generic-script bindings."""

    repository = BuildRepository()

    async def _get_approved(*_args, **_kwargs):
        return _approved_build()

    monkeypatch.setattr(
        repository,
        "get",
        _get_approved,
    )
    context = RequestContext(
        tenant_id="tenant-security",
        project_id="project-security",
        user_id="operator",
        role="project",
        trace_id="trace-security",
        correlation_id="corr-security",
    )

    with pytest.raises(PolicyFailure, match="requires script_sha256"):
        await repository.issue_bridge_token(
            context,
            "build-security",
            "run_revit_script",
        )


def _approved_build() -> dict:
    return {"status": "approved", "approval_id": "approval-security"}


@pytest.mark.asyncio
async def test_run_script_rejects_success_marker_when_working_copy_is_unchanged(
    tmp_path: Path, monkeypatch
):
    """A marker in an MCP response is insufficient without a changed RVT."""

    source = tmp_path / "source.rvt"
    source.write_bytes(b"unchanged-fixture")
    secret = "u" * 32
    service = RevitBridgeService()
    service.settings.revit_bridge_execution_enabled = True
    service.settings.revit_bridge_legacy_script_execution_enabled = True
    service.settings.revit_bridge_approval_secret = secret
    service.settings.revit_bridge_work_root = str(tmp_path / "bridge")
    service.settings.revit_bridge_timeout_seconds = 2.0

    class _EchoOnlyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool(self, _name, _arguments):
            return {"text": "BUILDMATE_WORKING_COPY_SAVED", "raw": {}}

    monkeypatch.setattr(
        "workers.revit_bridge.service.RevitMCPClient",
        lambda **_kwargs: _EchoOnlyClient(),
    )
    script_hash = _script_sha256(SAFE_SCRIPT)
    source_hash = file_sha256(source)
    token = issue_approval_token(
        secret,
        build_id="build-unchanged",
        action="run_revit_script",
        script_sha256=script_hash,
        target_model_path=str(source),
        source_model_sha256=source_hash,
    )

    with pytest.raises(DependencyFailure, match="working copy is unchanged"):
        await service.run_script(
            RunScriptRequest(
                build_id="build-unchanged",
                source_model_path=str(source),
                script=SAFE_SCRIPT,
                script_sha256=script_hash,
                source_model_sha256=source_hash,
                dry_run=False,
                approval_token=token,
            )
        )


@pytest.mark.asyncio
async def test_failed_revit_attempt_restores_isolated_working_copy(
    tmp_path: Path, monkeypatch
):
    """A save-before-render failure must not leave a mutated RVT behind."""

    source = tmp_path / "source.rvt"
    source.write_bytes(b"authoritative-source")
    secret = "r" * 32
    service = RevitBridgeService()
    service.settings.revit_bridge_execution_enabled = True
    service.settings.revit_bridge_legacy_script_execution_enabled = True
    service.settings.revit_bridge_approval_secret = secret
    service.settings.revit_bridge_work_root = str(tmp_path / "bridge")

    class _MutatingErrorClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool(self, _name, arguments):
            code = str(arguments.get("code") or "")
            match = re.search(r"OpenDocumentFile\((['\"])(.+?)\1\)", code)
            assert match is not None
            Path(match.group(2)).write_bytes(b"partially-mutated")
            return {"text": "Status: error\nError during code execution", "raw": {}}

    monkeypatch.setattr(
        "workers.revit_bridge.service.RevitMCPClient",
        lambda **_kwargs: _MutatingErrorClient(),
    )
    script_hash = _script_sha256(SAFE_SCRIPT)
    source_hash = file_sha256(source)
    token = issue_approval_token(
        secret,
        build_id="build-rollback",
        action="run_revit_script",
        script_sha256=script_hash,
        target_model_path=str(source),
        source_model_sha256=source_hash,
    )

    with pytest.raises(DependencyFailure, match="error response"):
        await service.run_script(RunScriptRequest(
            build_id="build-rollback",
            source_model_path=str(source),
            script=SAFE_SCRIPT,
            script_sha256=script_hash,
            source_model_sha256=source_hash,
            dry_run=False,
            approval_token=token,
        ))

    working_copy = tmp_path / "bridge" / "build-rollback" / "working_copy.rvt"
    assert working_copy.read_bytes() == source.read_bytes()


def test_approval_token_is_scoped_to_build_and_action():
    secret = "s" * 32
    token = issue_approval_token(
        secret, build_id="build_001", action="run_revit_script"
    )
    verify_approval_token(
        token, secret, build_id="build_001", action="run_revit_script"
    )
    with pytest.raises(PolicyFailure):
        verify_approval_token(
            token, secret, build_id="build_other", action="run_revit_script"
        )


def test_approval_token_binds_model_target_source_and_human_decision(tmp_path: Path):
    """A copied token must not authorize a different model or approval record."""

    secret = "b" * 32
    target = tmp_path / "target.rvt"
    approval = {
        "status": "approved",
        "actor_id": "reviewer-1",
        "reason": "geometry checked",
        "decided_at": "2026-08-30T00:00:00Z",
    }
    claims = {
        "build_id": "build-bound",
        "action": "run_wall_model",
        "wall_model_sha256": "a" * 64,
        "target_model_path": str(target),
        "source_model_sha256": "c" * 64,
        "approval_digest": approval_digest(approval),
    }
    token = issue_approval_token(secret, **claims)

    # Equivalent path spellings resolve to the same capability target.
    verify_approval_token(
        token,
        secret,
        build_id="build-bound",
        action="run_wall_model",
        wall_model_sha256=claims["wall_model_sha256"],
        target_model_path=str(target.parent / "." / target.name),
        source_model_sha256=claims["source_model_sha256"],
        approval_digest=claims["approval_digest"],
    )

    for field, wrong in (
        ("wall_model_sha256", "d" * 64),
        ("target_model_path", str(tmp_path / "other.rvt")),
        ("source_model_sha256", "e" * 64),
        ("approval_digest", approval_digest({**approval, "actor_id": "other"})),
    ):
        expected = dict(claims)
        expected[field] = wrong
        with pytest.raises(PolicyFailure, match="invalid or expired"):
            verify_approval_token(secret=secret, token=token, **expected)


@pytest.mark.parametrize(
    "response",
    [
        {
            "text": "=== ERROR DETAILS ===\nBUILDMATE_WALL_MODEL_APPLIED walls=1 grids=0 element_ids=42",
            "raw": {"isError": True},
        },
        {
            "text": "Status: error\nBUILDMATE_WORKING_COPY_SAVED",
            "raw": {},
        },
        {
            "text": "Error during code execution\nBUILDMATE_WALL_MODEL_APPLIED walls=1 grids=0 element_ids=42",
            "raw": {},
        },
    ],
)
def test_revit_error_envelope_cannot_echo_a_success_marker(response):
    """MCP diagnostics may contain source text; markers alone are not success."""

    with pytest.raises(DependencyFailure, match="error response"):
        _require_success_response(response)


def test_wall_model_compiler_emits_parseable_deterministic_revit_code(tmp_path: Path):
    compiled = {
        "project": {
            "project_id": "project-test",
            "tenant_id": "tenant-test",
            "levels": [{"name": "Main", "elevation": 0.0}],
        },
        "build": {"floor_code": "L1"},
        "grids": [],
        "model_elements": [{
            "type": "Wall", "id": "wall-1",
            "start": [0.0, 0.0, 0.0], "end": [1000.0, 0.0, 0.0],
            "thickness": 200.0, "height": 3000.0,
        }],
    }
    script = _compile_wall_model_script(
        compiled, actual_prefix=tmp_path / "actual", expected_wall_count=1,
    )
    compile(script, "wall_model_bridge.py", "exec")
    assert "BUILDMATE_WALL_MODEL_APPLIED" in script
    assert "from Autodesk.Revit.DB.Structure import StructuralType" in script
    assert 'representation in ("profile_directshape", "directshape", "exact_profile")' in script
    assert "BuiltInParameter.FAMILY_TOP_LEVEL_PARAM" in script
    assert "abs(actual_height - expected_height) > 5.0" in script
    assert "OpenDocumentFile" not in script
    wrapped = _wrap_working_copy_script(script, tmp_path / "source.rvt")
    assert wrapped.index("from Autodesk.Revit.DB.Structure import StructuralType") < wrapped.index("try:")
    # Revit may be open on its start screen, where pyRevit supplies no active
    # document.  The Bridge must fall back to HOST_APP.app instead of calling
    # None.Application and returning an opaque HTTP 503.
    assert "getattr(_buildmate_host_app, 'app', None)" in wrapped
    assert "Revit application is unavailable" in wrapped


def test_wall_model_compiler_preserves_coordinate_and_topology_gates(tmp_path: Path):
    """The generated program must carry the deterministic delivery controls."""

    compiled = {
        "project": {
            "project_id": "project-test",
            "tenant_id": "tenant-test",
            "levels": [{"id": "level-1", "name": "Main", "elevation": 3000.0}],
        },
        "build": {
            "build_id": "build-test",
            "floor_code": "L1",
            "wall_model_sha256": "f" * 64,
        },
        "coordinate_system": {
            "offset_policy": "revit_project_base_point",
            "project_offset_mm": None,
            "apply_base_point_rotation": True,
        },
        "grids": [{
            "x_axes": [0.0, 1000.0],
            "y_axes": [0.0],
            "x_axis_labels": ["A", "B"],
            "y_axis_labels": ["1"],
        }],
        "model_elements": [
            {
                "type": "Wall",
                "id": "wall-1",
                "start": [0.0, 0.0, 0.0],
                "end": [1000.0, 0.0, 0.0],
                "thickness": 200.0,
                "height": 3000.0,
            },
            {
                "type": "Wall",
                "id": "wall-2",
                "start": [1000.0, 0.0, 0.0],
                "end": [1000.0, 1000.0, 0.0],
                "thickness": 200.0,
                "height": 3000.0,
            },
        ],
        "junctions": [{
            "id": "junction-1",
            "kind": "L",
            "point": [1000.0, 0.0],
            "wall_ids": ["wall-1", "wall-2"],
        }],
        "openings": [{
            "id": "opening-1",
            "mark": "JD3",
            "status": "matched",
            "host_wall_ids": ["wall-1"],
            "center": [500.0, 0.0],
            "boundary": [[400.0, -100.0], [600.0, -100.0],
                         [600.0, 100.0], [400.0, 100.0]],
        }],
    }
    script = _compile_wall_model_script(
        compiled,
        actual_prefix=tmp_path / "actual",
        expected_wall_count=2,
    )
    compile(script, "wall_model_delivery.py", "exec")

    assert "revit_project_base_point" in script
    assert "apply_base_point_rotation" in script
    assert "_project_xy" in script
    assert 'wall_prefix = "BUILDMATE_AUTO:T=%s;P=%s;F=%s;"' in script
    assert '"BUILDMATE_AUTO:T=%s;P=%s;F=%s;M=%s;I=%s"' in script
    assert "JoinGeometryUtils.JoinGeometry" in script
    assert "WallUtils.AllowWallJoinAtEnd" in script
    assert "WallUtils.IsWallJoinAllowedAtEnd" in script
    assert "ElementsAtJoin" in script
    assert "_wall_pair_joined" in script
    assert "BUILDMATE_WALL_TOPOLOGY_VERIFIED" in script
    assert "WallUtils.DisallowWallJoinAtEnd" in script
    wall_create = script.index("wall = Wall.Create")
    disallow = script.index("WallUtils.DisallowWallJoinAtEnd", wall_create)
    topology_transaction = script.index("topology_transaction = Transaction")
    topology_apply = script.index("_apply_topology(", topology_transaction)
    # Join permission now lives inside the bounded pair helper; prove that the
    # helper is invoked only after the dedicated topology transaction starts.
    assert wall_create < disallow < topology_transaction < topology_apply
    assert '";VOL_M3="' in script
    assert '";MAT="' in script
    assert '";FOOTPRINT_M2="' in script
    assert ";OPENINGS=JD3" in script
    assert ";QTY_SCOPE=GROSS_NO_OPENINGS" in script
    assert "SolidOptions(material_id" in script
    assert "BUILDMATE_GEOMETRY_READBACK" in script
    assert "BUILDMATE_BEAM_ELEVATION_READBACK" in script
    assert "_align_beam_vertical_extent" in script
    assert "beam vertical envelope mismatch" in script
    assert "ElementTransformUtils.MoveElement" in script
    assert "expected_wall_top_z" in script
    assert "expected_top_z" in script
    assert "BM_BeamBaseElevationMm" in script
    assert "BM_BeamTopElevationMm" in script
    assert "MATERIAL_ID_CACHE" in script
    assert "WALL_TYPE_CACHE" in script
    assert "BUILDMATE_GRID_MARKER_FALLBACK" in script
    assert "BUILDMATE_SCHEDULE_DATA_APPLIED" in script
    assert "BUILDMATE_OPENING_SEMANTICS_APPLIED" in script
    assert "混凝土 - 矩形梁.rfa" in script
    assert "doc.LoadFamily(family_path, family_ref)" in script
    assert "BUILDMATE_WALL_TOPOLOGY_APPLIED" in script
    assert "BUILDMATE_WALL_TOPOLOGY_REFS" in script
    assert "BUILDMATE_PERSISTED_BEFORE_RENDER" in script
    assert "candidate.GenLevel" in script
    assert "candidate_level.Id != level.Id" in script
    assert script.index("BUILDMATE_PERSISTED_BEFORE_RENDER") < script.index(
        "BUILDMATE_WALL_MODEL_APPLIED"
    )


def test_wall_model_preflight_rejects_revit_short_column_edge(tmp_path: Path):
    source_ref = EvidenceSourceRef(
        source_file_id="source-1", entity_id="entity-1", locator="profile:1",
    )
    model = WallModel(
        tenant_id="tenant-test",
        project_id="project-test",
        wall_evidence_sha256="a" * 64,
        coordinate_origin="source_origin",
        level=LevelConfig(id="level-1", name="Main", wall_height_m=3.0),
        walls=[WallRecord(
            wall_id="wall-1", start_m=(0.0, 0.0), end_m=(1.0, 0.0),
            thickness_m=0.2, height_m=3.0, level_id="level-1",
            evidence_ids=["e-1"], source_refs=[source_ref], confidence=1.0,
        )],
        columns=[ColumnRecord(
            column_id="column-1", center_m=(0.0, 0.0),
            profile_m=[(0.0, 0.0), (0.0005, 0.0), (1.0, 0.0), (1.0, 1.0)],
            width_m=1.0, depth_m=1.0, height_m=3.0, level_id="level-1",
            source_refs=[source_ref], confidence=1.0,
        )],
        gate=ReviewGate(status="pass", checks={"geometry": True}, metrics={}),
        review_status="approved",
        approval=ApprovalRecord(status="approved", actor_id="reviewer", reason="ok"),
        revit={"target_model_path": str(tmp_path / "target.rvt"), "floor_code": "L1"},
    )

    with pytest.raises(ValueError, match="Revit-short edge"):
        normalize_wall_model_payload(model.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_wall_model_bridge_dry_run_is_typed_and_side_effect_free(tmp_path: Path):
    source = tmp_path / "target.rvt"
    source.write_bytes(b"fixture-rvt")
    wall = WallModel(
        tenant_id="tenant-test",
        project_id="project-test",
        wall_evidence_sha256="a" * 64,
        coordinate_origin="revit_project_base_point",
        level=LevelConfig(id="level-1", name="Main", wall_height_m=3.0),
        walls=[WallRecord(
            wall_id="wall-1", start_m=(0.0, 0.0), end_m=(1.0, 0.0),
            thickness_m=0.2, height_m=3.0, level_id="level-1",
            evidence_ids=["e-1"], source_refs=[EvidenceSourceRef(
                source_file_id="source-1", entity_id="entity-1", locator="line:1",
            )], confidence=1.0,
        )],
        gate=ReviewGate(status="pass", checks={"geometry": True}, metrics={}),
        review_status="approved",
        approval=ApprovalRecord(status="approved", actor_id="reviewer", reason="ok"),
        revit={"target_model_path": str(source), "floor_code": "L1"},
    )
    revit_result = RevitResult(
        tenant_id=wall.tenant_id, project_id=wall.project_id,
        wall_model_sha256=canonical_sha256(wall), status="write_approved",
    )
    result = await RevitBridgeService().run_wall_model(RunWallModelRequest(
        build_id="build_wall_001", source_model_path=str(source),
        wall_model=wall.model_dump(mode="json"),
        revit_result=revit_result.model_dump(mode="json"), dry_run=True,
    ))
    assert result.status == "validated"
    assert result.dry_run is True
    assert result.details["wall_count"] == 1
    assert result.output_path is None


@pytest.mark.asyncio
async def test_wall_model_delivery_replays_completed_build_without_second_revit_call(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "target.rvt"
    source.write_bytes(b"fixture-rvt-for-replay")
    approval = ApprovalRecord(status="approved", actor_id="reviewer", reason="ok")
    wall = WallModel(
        tenant_id="tenant-test",
        project_id="project-test",
        wall_evidence_sha256="a" * 64,
        coordinate_origin="source_origin",
        level=LevelConfig(id="level-1", name="Main", wall_height_m=3.0),
        walls=[WallRecord(
            wall_id="wall-1", start_m=(0.0, 0.0), end_m=(1.0, 0.0),
            thickness_m=0.2, height_m=3.0, level_id="level-1",
            evidence_ids=["e-1"], source_refs=[EvidenceSourceRef(
                source_file_id="source-1", entity_id="entity-1", locator="line:1",
            )], confidence=1.0,
        )],
        gate=ReviewGate(status="pass", checks={"geometry": True}, metrics={}),
        review_status="approved", approval=approval,
        revit={"target_model_path": str(source), "floor_code": "L1"},
    )
    model_hash = canonical_sha256(wall)
    source_hash = file_sha256(source)
    revit_result = RevitResult(
        tenant_id=wall.tenant_id, project_id=wall.project_id,
        wall_model_sha256=model_hash, status="write_approved",
        approval=approval,
        readback={"source_model_sha256": source_hash},
    )
    secret = "z" * 32
    service = RevitBridgeService()
    service.settings.revit_bridge_execution_enabled = True
    service.settings.revit_bridge_approval_secret = secret
    service.settings.revit_bridge_work_root = str(tmp_path / "bridge")
    calls = {"count": 0}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool(self, _name, arguments):
            calls["count"] += 1
            code = str(arguments.get("code") or "")
            prefix_match = re.search(r"ACTUAL_PREFIX\s*=\s*(['\"])(.*?)\1", code)
            assert prefix_match is not None
            Path(prefix_match.group(2) + ".png").write_bytes(b"independent-png")
            working_match = re.search(
                r"OpenDocumentFile\((['\"])(.+?)\1\)", code
            )
            assert working_match is not None
            Path(working_match.group(2)).write_bytes(b"changed-rvt")
            return {"text": (
                "BUILDMATE_COORDINATE_APPLIED offset_x_mm=0.000000 "
                "offset_y_mm=0.000000 angle_rad=0.000000000000\n"
                "BUILDMATE_GEOMETRY_READBACK wall_xy_mm=0.000000 "
                "wall_thickness_mm=0.000000 column_xy_mm=0.000000 "
                "column_height_mm=0.000000\n"
                "BUILDMATE_SCHEDULE_DATA_APPLIED elements=1 walls=1 columns=0\n"
                "BUILDMATE_OPENING_SEMANTICS_APPLIED openings=0 matched=0 "
                "hosts=0 marks=\n"
                "BUILDMATE_WALL_TOPOLOGY_APPLIED junctions=0 joined_pairs=0 kinds=\n"
                "BUILDMATE_WALL_TOPOLOGY_VERIFIED joined_pairs=0 "
                "snapped_endpoints=0 max_snap_mm=0.000000\n"
                "BUILDMATE_WALL_TOPOLOGY_REFS refs=\n"
                "BUILDMATE_PERSISTED_BEFORE_RENDER\n"
                "BUILDMATE_WALL_MODEL_APPLIED walls=1 grids=0 element_ids=123\n"
                "BUILDMATE_WORKING_COPY_SAVED"
            ), "raw": {}}

    monkeypatch.setattr(
        "workers.revit_bridge.service.RevitMCPClient",
        lambda **_kwargs: _FakeClient(),
    )
    token = issue_approval_token(
        secret,
        build_id="build-replay",
        action="run_wall_model",
        wall_model_sha256=model_hash,
        target_model_path=str(source),
        source_model_sha256=source_hash,
        approval_digest=approval_digest(approval),
    )
    request = RunWallModelRequest(
        build_id="build-replay", source_model_path=str(source),
        wall_model=wall.model_dump(mode="json"),
        revit_result=revit_result.model_dump(mode="json"),
        source_model_sha256=source_hash, dry_run=False, approval_token=token,
    )
    first = await service.run_wall_model(request)
    second = await service.run_wall_model(request)
    assert first.status == second.status == "executed"
    assert second.message.startswith("previously completed")
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_present_wall_model_opens_verified_output_and_checks_visible_counts(
    tmp_path: Path, monkeypatch
):
    service = RevitBridgeService()
    service.settings.revit_bridge_work_root = str(tmp_path / "bridge")
    build_id = "build-present"
    work_dir = Path(service.settings.revit_bridge_work_root) / build_id
    work_dir.mkdir(parents=True)
    target = tmp_path / "target.rvt"
    target.write_bytes(b"source")
    output = work_dir / "working_copy.rvt"
    output.write_bytes(b"audited-output")
    actual_view = work_dir / "build-present_actual_view - BM-ACTUAL-L1.png"
    actual_view.write_bytes(b"png")
    output_hash = file_sha256(output)
    details = {
        "after_sha256": output_hash,
        "wall_count": 2,
        "column_count": 3,
        "grid_count": 4,
    }
    _write_delivery_receipt(
        work_dir / "delivery_receipt.json",
        build_id=build_id,
        wall_model_sha256="a" * 64,
        source_model_sha256=file_sha256(target),
        approval_digest="b" * 64,
        target_model_path=target,
        output_path=output,
        actual_view_path=actual_view,
        after_sha256=output_hash,
        actual_view_sha256=file_sha256(actual_view),
        details=details,
    )
    calls = []

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            description = str(arguments.get("description") or "")
            if name == "open_document":
                assert arguments["file_path"] == str(output.resolve())
                return {"text": "Document opened successfully.", "raw": {}}
            if "inspect active" in description:
                return {
                    "text": "BUILDMATE_ACTIVE_DOCUMENT_PATH=C:\\other.rvt",
                    "raw": {},
                }
            assert name == "execute_revit_code"
            assert "BM-ACTUAL-L1" in arguments["code"]
            return {
                "text": (
                    "BUILDMATE_DELIVERY_PRESENTED view=BM-ACTUAL-L1 "
                    "walls=2 columns=3 grids=4"
                ),
                "raw": {},
            }

    monkeypatch.setattr(
        "workers.revit_bridge.service.RevitMCPClient",
        lambda **_kwargs: _FakeClient(),
    )
    result = await service.present_wall_model(PresentWallModelRequest(
        build_id=build_id,
        output_sha256=output_hash,
    ))
    assert result.status == "presented"
    assert result.output_path == str(output.resolve())
    assert result.details["view_name"] == "BM-ACTUAL-L1"
    assert result.details["visible_counts"] == {
        "walls": 2,
        "columns": 3,
        "grids": 4,
    }
    assert [name for name, _ in calls] == [
        "execute_revit_code",
        "open_document",
        "execute_revit_code",
    ]
