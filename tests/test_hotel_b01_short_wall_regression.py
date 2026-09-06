"""hotel_b01 短墙提案与正式 Revit Gate 的真实产物回归保护。"""

import hashlib
from pathlib import Path

from scripts import pipeline_std_runner as std_runner
from tests.fixtures.hotel_b01 import create_hotel_b01_fixture


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _handles(candidate: dict) -> frozenset[str]:
    return frozenset(candidate.get("source_entity_handles") or [])


def test_hotel_b01_fixture_keeps_nine_review_components_classified(tmp_path):
    input_dxf, ir, _ = create_hotel_b01_fixture(tmp_path / "hotel_b01")
    source = ir["source"]

    assert Path(source["path"]).resolve() == input_dxf.resolve()
    assert source["sha256"] == _sha256(input_dxf)
    assert len(ir["geometry"]["architectural_walls"]) == 71

    audit = ir["review_candidates"]["short_wall_proposals"]
    proposals = audit["proposals"]
    insufficient = audit["insufficient_support_candidates"]
    strict = [item for item in proposals
              if item["approval_status"] ==
              "ELIGIBLE_FOR_STRICT_APPROVAL"]
    upgrades = [item for item in proposals
                if item["status"] == "PROPOSED_WALL_UPGRADE"]
    ambiguous = [item for item in proposals
                 if "endpoint_support_ambiguous" in item["blockers"]]
    single_wall = [item for item in proposals
                   if "endpoint_supported_by_single_wall_only" in
                   item["blockers"]]
    ordinary_unique = [
        item for item in proposals
        if item["status"] == "PROPOSED_SHORT_WALL"
        and item["approval_status"] == "NEEDS_REVIEW"
        and not item["blockers"]
    ]

    assert audit["status"] == "REVIEW"
    assert audit["component_count"] == 9
    assert audit["proposed_short_wall_count"] == 7
    assert audit["strict_approval_eligible_count"] == len(strict) == 2
    assert len(ordinary_unique) == 3
    assert len(ambiguous) == 0
    assert len(single_wall) == 1
    assert audit["single_wall_upgrade_proposal_count"] == len(upgrades) == 1
    assert audit["insufficient_support_count"] == len(insufficient) == 2

    assert {_handles(item) for item in strict} == {
        frozenset(("4D020", "4D023", "4D024")),
        frozenset(("4CFD9", "4CFDA", "4CFDC")),
    }
    assert {_handles(item) for item in ordinary_unique} == {
        frozenset(("4CFF6", "4CFFD")),
        frozenset(("4CFD4", "4CFD6")),
        frozenset(("4CFE1", "4CFE4")),
    }
    assert _handles(single_wall[0]) == frozenset(("4D02F", "4D037"))
    assert {_handles(item) for item in insufficient} == {
        frozenset(("46D1B",)),
        frozenset(("4D01F", "4D022")),
    }

    upgrade = upgrades[0]
    assert _handles(upgrade) == frozenset(
        ("4CFE3", "4D012", "4D065", "4D066"))
    assert upgrade["start"] == [103.101393, 40.200303]
    assert upgrade["end"] == [103.101413, 40.700303]
    assert upgrade["thickness_mm"] == 100.0
    assert upgrade["replacement_evidence"] == {
        "existing_wall_id": "wall_60",
        "existing_wall_paired": False,
        "existing_wall_source_segment_ids": [
            "segment_a4f2b6e6511f749de4a11830"],
        "opposing_source_segment_id":
            "segment_a5983acaf627c732a1a7310e",
        "bridge_source_segment_ids": [
            "segment_04307038ffdd21f717867481"],
        "core_overlap_m": 0.4,
        "old_wall_baseline_error_m": 0.000417,
        "existing_wall_geometry": {
            "start": [103.051, 40.3],
            "end": [103.051, 40.8],
            "thickness_mm": 100,
            "paired": False,
        },
        "expected_existing_wall_fingerprint": (
            "ca9631bba24a04a90f3baefadb1e254f35a1345a566f3d8fea4dbdc7e321b7d0"
        ),
        "required_action": "ATOMIC_REPLACE_SINGLE_WALL",
    }
    assert upgrade["blockers"] == []
    assert upgrade["missing_material_source_segment_ids"] == []
    assert {item["entity_handle"] for item in
            upgrade["material_source_segment_refs"]} == {
        "4D012", "4D065", "4D066"}
    assert all(item["source_interval_m"]
               for item in upgrade["material_source_segment_refs"])
    assert len(upgrade["integrity_sha256"]) == 64

    all_components = proposals + insufficient
    assert all(item["auto_action"] == "NONE" for item in all_components)
    assert all(item["model_geometry_created"] is False
               for item in all_components)
    assert audit["rule"]["auto_action"] == "NONE"
    assert audit["rule"]["model_geometry_created"] is False
    assert ir["quality_gate"]["allow_modeling"] is False
    assert "建筑墙拓扑 Gate 未通过" in ir["quality_gate"][
        "blocking_reasons"]


def test_hotel_b01_failed_gate_does_not_touch_formal_revit_model(monkeypatch, tmp_path):
    _, _, revit_input = create_hotel_b01_fixture(tmp_path / "hotel_b01")
    formal_model = revit_input / "model.json"
    before = formal_model.read_bytes()
    expected_hash = hashlib.sha256(before).hexdigest()
    called = []
    monkeypatch.setattr(std_runner.pipeline, "JSON_IN", str(revit_input))
    monkeypatch.setattr(
        std_runner, "_original_trigger_revit",
        lambda: called.append(True) or {"status": "done"})

    result = std_runner.guarded_trigger_revit()

    after = formal_model.read_bytes()
    assert result["status"] == "error"
    assert "建筑墙拓扑 Gate 未通过" in result["error"]
    assert called == []
    assert after == before
    assert hashlib.sha256(after).hexdigest() == expected_hash
