import hashlib
import uuid

import pytest

from backend.adapters.build_repository import BuildRepository
from backend.adapters.model_ir_repository import ModelIRRepository
from backend.adapters.review_repository import ReviewRepository
from backend.domain.contracts import RequestContext
from backend.domain.errors import PolicyFailure, ResourceNotFound
from backend.domain.model_ir import ModelIRV2


def context(suffix: str, tenant: str | None = None) -> RequestContext:
    return RequestContext(
        tenant_id=tenant or f"tenant_model_{suffix}",
        project_id=f"project_model_{suffix}", user_id="engineer", role="project",
        trace_id=suffix, correlation_id=suffix,
    )


def model(suffix: str) -> ModelIRV2:
    return ModelIRV2.model_validate({
        "project": {"id": f"project_model_{suffix}"},
        "levels": [{"id": "L1", "name": "一层", "elevation_m": 0}],
        "model_elements": [{
            "element_id": "wall_1", "type": "Wall",
            "geometry": {"start": [0, 0, 0], "end": [3, 0, 0], "thickness_m": 0.2},
            "source_refs": [{"kind": "drawing", "artifact_id": "art_source", "locator": "A-WALL:1"}],
            "confidence": 1.0,
        }],
        "provenance": {
            "source_artifact_ids": ["art_source"],
            "source_sha256": hashlib.sha256(suffix.encode()).hexdigest(),
            "parser_version": "fixture-1", "pipeline_version": "fixture-1",
        },
    })


async def test_ir_review_approval_and_build_dry_run_are_persisted():
    suffix = uuid.uuid4().hex
    ctx = context(suffix)
    ir, created = await ModelIRRepository().save(ctx, "art_source", model(suffix))
    assert created is True

    with pytest.raises(PolicyFailure):
        await BuildRepository().create(ctx, ir["id"])

    review = await ReviewRepository().create(ctx, ir["id"], [{
        "rule_id": "HR-001", "track": "hard", "severity": "warning",
        "confidence": 1.0, "description": "fixture finding",
        "evidence_refs": [{"kind": "drawing", "artifact_id": "art_source", "locator": "A-WALL:1"}],
    }], {"verdict": "review"})
    decided = await ReviewRepository().decide(ctx, review["id"], "approved", "verified")
    assert decided["verdict"] == "approved"

    build = await BuildRepository().create(ctx, ir["id"])
    assert build["status"] == "waiting_approval"
    assert build["dry_run_report"]["valid"] is True
    approved = await BuildRepository().approve(ctx, build["id"], "approved", "operator approved")
    assert approved["status"] == "approved"
    assert approved["approval_id"].startswith("approval_")


async def test_model_ir_is_tenant_scoped():
    suffix = uuid.uuid4().hex
    owner = context(suffix)
    other = context(suffix, tenant=f"other_{suffix}")
    ir, _ = await ModelIRRepository().save(owner, "art_source", model(suffix))
    with pytest.raises(ResourceNotFound):
        await ModelIRRepository().get(other, ir["id"])
