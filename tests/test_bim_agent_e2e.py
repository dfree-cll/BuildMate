"""Public BIM Agent vertical-slice E2E test.

Exercises the same contract used by the frontend: project context, artifact
upload, durable wall_pipeline execution, both persisted approvals and task
event history.  Revit execution is intentionally disabled here; the Windows
Bridge is covered by the dedicated bridge/audit tests and is an external
process in local CI.
"""

import asyncio
import uuid
from pathlib import Path

import ezdxf
import httpx
import pytest

from backend.db.schema import METADATA
from backend.db.session import engine
from backend.main import app


def _write_fixture(path: Path) -> None:
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    doc.layers.add("A-GRID")
    doc.layers.add("GEOMETRY-WALL")
    modelspace = doc.modelspace()
    for x in (0, 2000):
        modelspace.add_line((x, 0), (x, 2000), dxfattribs={"layer": "A-GRID"})
    for y in (0, 2000):
        modelspace.add_line((0, y), (2000, y), dxfattribs={"layer": "A-GRID"})
    modelspace.add_lwpolyline(
        [(0, 0), (2000, 0), (2000, 200), (0, 200)],
        close=True,
        dxfattribs={"layer": "GEOMETRY-WALL"},
    )
    doc.saveas(path)


@pytest.fixture
async def api():
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    await engine.dispose()


async def _login(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "demo123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


async def _wait_status(
    client: httpx.AsyncClient, headers: dict[str, str], task_id: str, project_id: str,
    expected: set[str], tries: int = 90,
) -> dict:
    for _ in range(tries):
        response = await client.get(
            f"/api/v2/tasks/{task_id}",
            params={"project_id": project_id},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        task = response.json()
        if task["status"] in expected:
            return task
        await asyncio.sleep(0.2)
    pytest.fail(f"task {task_id} did not reach {expected}")


@pytest.mark.asyncio
async def test_frontend_contract_reaches_wallmodel_and_delivery_handoff(api, tmp_path):
    headers = await _login(api)
    project_response = await api.post(
        "/api/v2/projects", json={"name": "E2E BIM " + uuid.uuid4().hex}, headers=headers
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["id"]

    source = tmp_path / "e2e-plan.dxf"
    _write_fixture(source)
    target = tmp_path / "e2e-model.rvt"
    target.write_bytes(b"deterministic-rvt-fixture")
    with source.open("rb") as stream:
        artifact_response = await api.post(
            "/api/v2/artifacts",
            params={"project_id": project_id},
            files={"file": (source.name, stream, "application/dxf")},
            data={"kind": "wall_source", "project_id": project_id},
            headers=headers,
        )
    assert artifact_response.status_code == 201, artifact_response.text
    artifact_id = artifact_response.json()["id"]

    task_response = await api.post(
        "/api/v2/workflows",
        json={
            "project_id": project_id,
            "workflow": "wall_pipeline",
            "input_artifact_ids": [artifact_id],
            "options": {
                "execute_revit": False,
                "wall": {"include_layers": ["GEOMETRY-WALL"]},
                "grid": {
                    "include_layers": ["A-GRID"],
                    "required": True,
                    "min_length_m": 1.0,
                },
                "revit": {
                    "target_model_path": str(target),
                    "floor_code": "L1",
                },
            },
        },
        headers=headers,
    )
    assert task_response.status_code == 202, task_response.text
    task_id = task_response.json()["task"]["id"]

    waiting_model = await _wait_status(
        api, headers, task_id, project_id, {"waiting_human", "failed"}
    )
    assert waiting_model["status"] == "waiting_human", waiting_model
    assert waiting_model["result"]["structured_output"]["pipeline"] == "wall_pipeline"
    assert waiting_model["result"]["next_action"] == "approve_wall_model"
    assert waiting_model["result"]["structured_output"]["wall_count"] >= 1
    assert waiting_model["result"]["structured_output"]["modeling_standard"]["profile"] == "cn_gb_bim_delivery_v1"

    approve_model = await api.post(
        f"/api/v2/tasks/{task_id}/resume",
        json={"project_id": project_id, "decision": "approved", "reason": "E2E model review"},
        headers=headers,
    )
    assert approve_model.status_code == 200, approve_model.text
    waiting_write = await _wait_status(
        api, headers, task_id, project_id, {"waiting_human", "failed"}
    )
    assert waiting_write["status"] == "waiting_human", {
        "error": waiting_write.get("error_message"),
        "result": waiting_write.get("result"),
    }
    assert waiting_write["result"]["next_action"] == "approve_revit_write"

    approve_write = await api.post(
        f"/api/v2/tasks/{task_id}/resume",
        json={"project_id": project_id, "decision": "approved", "reason": "E2E delivery handoff"},
        headers=headers,
    )
    assert approve_write.status_code == 200, approve_write.text
    completed = await _wait_status(api, headers, task_id, project_id, {"succeeded", "failed"})
    assert completed["status"] == "succeeded", completed
    assert completed["result"]["structured_output"]["stage"] == "revit_bridge_handoff"

    events = await api.get(
        f"/api/v2/tasks/{task_id}/events", params={"project_id": project_id}, headers=headers
    )
    assert events.status_code == 200, events.text
    assert "task.waiting_human" in events.text
    assert "task.succeeded" in events.text
