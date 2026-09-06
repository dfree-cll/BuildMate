"""Small deterministic replacement for workstation runtime hotel_b01 outputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def create_hotel_b01_fixture(root: Path) -> tuple[Path, dict, Path]:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "hotel_b01_fixture.dxf"
    source.write_text(
        "0\nSECTION\n2\nENTITIES\n0\nLINE\n8\nA-WALL\n10\n0\n20\n0\n11\n1000\n21\n0\n0\nENDSEC\n0\nEOF\n",
        encoding="ascii",
        newline="\n",
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    def proposal(handles, *, status="PROPOSED_SHORT_WALL", approval="NEEDS_REVIEW", blockers=None):
        return {
            "source_entity_handles": list(handles),
            "status": status,
            "approval_status": approval,
            "blockers": list(blockers or []),
            "auto_action": "NONE",
            "model_geometry_created": False,
        }

    strict = [
        proposal(("4D020", "4D023", "4D024"), approval="ELIGIBLE_FOR_STRICT_APPROVAL"),
        proposal(("4CFD9", "4CFDA", "4CFDC"), approval="ELIGIBLE_FOR_STRICT_APPROVAL"),
    ]
    ordinary = [
        proposal(("4CFF6", "4CFFD")),
        proposal(("4CFD4", "4CFD6")),
        proposal(("4CFE1", "4CFE4")),
    ]
    single_wall = proposal(
        ("4D02F", "4D037"), blockers=["endpoint_supported_by_single_wall_only"]
    )
    upgrade = proposal(
        ("4CFE3", "4D012", "4D065", "4D066"),
        status="PROPOSED_WALL_UPGRADE",
    )
    upgrade.update({
        "start": [103.101393, 40.200303],
        "end": [103.101413, 40.700303],
        "thickness_mm": 100.0,
        "replacement_evidence": {
            "existing_wall_id": "wall_60",
            "existing_wall_paired": False,
            "existing_wall_source_segment_ids": ["segment_a4f2b6e6511f749de4a11830"],
            "opposing_source_segment_id": "segment_a5983acaf627c732a1a7310e",
            "bridge_source_segment_ids": ["segment_04307038ffdd21f717867481"],
            "core_overlap_m": 0.4,
            "old_wall_baseline_error_m": 0.000417,
            "existing_wall_geometry": {
                "start": [103.051, 40.3], "end": [103.051, 40.8],
                "thickness_mm": 100, "paired": False,
            },
            "expected_existing_wall_fingerprint": "ca9631bba24a04a90f3baefadb1e254f35a1345a566f3d8fea4dbdc7e321b7d0",
            "required_action": "ATOMIC_REPLACE_SINGLE_WALL",
        },
        "missing_material_source_segment_ids": [],
        "material_source_segment_refs": [
            {"entity_handle": handle, "source_interval_m": [0.0, 0.1]}
            for handle in ("4D012", "4D065", "4D066")
        ],
        "integrity_sha256": "a" * 64,
    })
    insufficient = [
        proposal(("46D1B",), status="INSUFFICIENT_SUPPORT"),
        proposal(("4D01F", "4D022"), status="INSUFFICIENT_SUPPORT"),
    ]
    proposals = strict + ordinary + [single_wall, upgrade]
    review_ir = {
        "source": {"path": str(source), "sha256": source_hash},
        "geometry": {"architectural_walls": [{"id": f"wall_{index}"} for index in range(71)]},
        "review_candidates": {
            "short_wall_proposals": {
                "status": "REVIEW",
                "component_count": 9,
                "proposed_short_wall_count": 7,
                "strict_approval_eligible_count": 2,
                "single_wall_upgrade_proposal_count": 1,
                "insufficient_support_count": 2,
                "proposals": proposals,
                "insufficient_support_candidates": insufficient,
                "rule": {"auto_action": "NONE", "model_geometry_created": False},
            }
        },
        "quality_gate": {
            "allow_modeling": False,
            "blocking_reasons": ["建筑墙拓扑 Gate 未通过"],
        },
    }

    revit_input = root / "revit"
    revit_input.mkdir(exist_ok=True)
    (revit_input / "model.json").write_text(
        json.dumps({"model_elements": [{"id": "existing_wall"}]}, sort_keys=True),
        encoding="utf-8",
    )
    (revit_input / "model_status.json").write_text(
        json.dumps({
            "清洗审计": {
                "最终判定": {
                    "allow_modeling": False,
                    "reasons": ["建筑墙拓扑 Gate 未通过"],
                }
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return source, review_ir, revit_input
