"""图纸→BIM 成长闭环的持久化契约。"""
import json
import uuid

import pytest
from sqlalchemy import text

from backend.core.bim_learning import (
    add_feedback,
    create_bim_project,
    create_build_run,
    create_profile,
    drawing_fingerprint,
    get_project_levels,
    match_drawing_profile,
    persist_element_mappings,
    register_project_level,
    update_build_run,
)
from backend.db.session import engine


@pytest.mark.asyncio
async def test_build_run_records_quality_result_and_element_mapping(tmp_path):
    source = tmp_path / "drawing.dxf"
    source.write_text("DXF fixture", encoding="utf-8")
    build_id = "RVT-" + uuid.uuid4().hex[:12].upper()

    await create_build_run(build_id, "review-1", "tenant-a", "user-a", str(source))
    await update_build_run(
        build_id, "validating", model_scope="structural",
        quality={"coordinate_accuracy": 0.98}, gates={"结构Gate": "PASS"},
    )
    await persist_element_mappings(build_id, "tenant-a", [{
        "input_id": "COL-001", "kind": "Column",
        "revit_element_id": 12345, "mode": "native",
    }])
    await update_build_run(build_id, "done", result={"created_count": 1})

    async with engine.connect() as conn:
        run = (await conn.execute(text(
            "SELECT status, model_scope, quality, result, finished_at "
            "FROM build_runs WHERE id=:id"), {"id": build_id})).fetchone()
        mapping = (await conn.execute(text(
            "SELECT ir_element_id, revit_element_id, element_type, creation_mode "
            "FROM element_mappings WHERE build_id=:id"), {"id": build_id})).fetchone()

    assert run[0:2] == ("done", "structural")
    assert json.loads(run[2])["coordinate_accuracy"] == 0.98
    assert json.loads(run[3])["created_count"] == 1
    assert run[4] is not None
    assert mapping == ("COL-001", "12345", "Column", "native")


@pytest.mark.asyncio
async def test_profile_and_feedback_are_tenant_scoped():
    signature = "signature-" + uuid.uuid4().hex
    profile_id = await create_profile(
        "tenant-b", "reviewer-b", "设计院 A 结构图", signature,
        "structural", {"column_layers": ["S-COLS"]}, "设计院 A",
    )
    feedback_id = await add_feedback("tenant-b", "reviewer-b", {
        "profile_id": profile_id, "build_id": "RVT-LEARN2",
        "source_element_id": "WALL-3", "predicted_type": "Wall",
        "correct_type": "ShearWall", "reason": "结构图中的双线墙",
    })

    async with engine.connect() as conn:
        profile = (await conn.execute(text(
            "SELECT tenant_id, config FROM drawing_profiles WHERE id=:id"),
            {"id": profile_id})).fetchone()
        feedback = (await conn.execute(text(
            "SELECT tenant_id, correct_type, reason FROM extraction_feedback WHERE id=:id"),
            {"id": feedback_id})).fetchone()

    assert profile[0] == "tenant-b"
    assert json.loads(profile[1])["column_layers"] == ["S-COLS"]
    assert feedback == ("tenant-b", "ShearWall", "结构图中的双线墙")


def _make_dxf(path, layers, line_count=10):
    import ezdxf
    doc = ezdxf.new("R2010")
    for layer in layers:
        doc.layers.add(layer)
    msp = doc.modelspace()
    for i in range(line_count):
        msp.add_line((i, 0), (i, 10), dxfattribs={"layer": layers[i % len(layers)]})
    doc.saveas(path)


@pytest.mark.asyncio
async def test_similar_drawing_automatically_matches_profile(tmp_path):
    first = tmp_path / "floor-1.dxf"
    second = tmp_path / "floor-2.dxf"
    _make_dxf(first, ["S-COLS", "S-WALL", "S-GRID"], 12)
    _make_dxf(second, ["S-COLS", "S-WALL", "S-GRID"], 18)
    fingerprint = drawing_fingerprint(str(first))
    signature = "signature-" + uuid.uuid4().hex
    tenant_id = "tenant-match-" + uuid.uuid4().hex
    profile_id = await create_profile(
        tenant_id, "reviewer", "结构标准模板", signature, "structural",
        {"fingerprint": fingerprint, "column_family_path": "column.rfa"},
    )

    current, match = await match_drawing_profile(tenant_id, str(second))

    assert current["signature"] == fingerprint["signature"]
    assert match["profile_id"] == profile_id
    assert match["match_score"] >= 0.65
    assert match["config"]["column_family_path"] == "column.rfa"


@pytest.mark.asyncio
async def test_drawing_profile_does_not_cross_tenants(tmp_path):
    drawing = tmp_path / "tenant.dxf"
    _make_dxf(drawing, ["ARCH-WALL", "ARCH-DOOR"], 10)
    fingerprint = drawing_fingerprint(str(drawing))
    await create_profile(
        "tenant-owner", "reviewer", "建筑模板", "owner-" + uuid.uuid4().hex,
        "architectural", {"fingerprint": fingerprint},
    )

    _, match = await match_drawing_profile("tenant-other", str(drawing))
    assert match is None


@pytest.mark.asyncio
async def test_project_level_ledger_is_project_scoped_and_immutable():
    tenant_id = "tenant-project-" + uuid.uuid4().hex
    project_id = await create_bim_project(
        tenant_id, "reviewer", "地下室到首层", "PRJ-" + uuid.uuid4().hex[:10])

    b1 = await register_project_level(tenant_id, project_id, "b1", -4500)
    first_floor = await register_project_level(tenant_id, project_id, "1f", 0)
    duplicate = await register_project_level(tenant_id, project_id, "B1", -4500)

    assert b1 == {"floor_code": "B1", "elevation_mm": -4500, "created": True}
    assert first_floor == {"floor_code": "1F", "elevation_mm": 0, "created": True}
    assert duplicate["created"] is False
    assert await get_project_levels(tenant_id, project_id) == [
        {"floor_code": "B1", "name": "标高 B1", "elevation": -4500},
        {"floor_code": "1F", "name": "标高 1F", "elevation": 0},
    ]

    with pytest.raises(ValueError, match="同名标高"):
        await register_project_level(tenant_id, project_id, "B1", -4400)
    with pytest.raises(ValueError, match="相同高程"):
        await register_project_level(tenant_id, project_id, "2F", 0)
