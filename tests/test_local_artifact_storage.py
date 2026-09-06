from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import httpx

from backend.adapters.local_artifact_storage import LocalArtifactStorage
from backend.api.v2 import artifacts as artifacts_api
from backend.application.artifact_service import ArtifactService
from backend.main import app


async def test_put_copies_staged_upload_and_resolves_content():
    test_root = Path("tests") / f".artifact-storage-{uuid4().hex}"
    test_root.mkdir(parents=True)
    source = test_root / "source.bin"
    source.write_bytes(b"artifact-content")
    storage = LocalArtifactStorage(test_root / "storage")
    try:
        uri = await storage.put("tenant_demo", "ab" * 32, source)

        assert not source.exists()
        resolved = await storage.resolve(uri)
        assert resolved.read_bytes() == b"artifact-content"
    finally:
        rmtree(test_root, ignore_errors=True)


async def test_v2_upload_succeeds_when_temp_and_storage_are_on_different_volumes(monkeypatch):
    """The API path must handle Windows' C: temp directory → F: project storage."""
    test_root = Path("tests") / f".artifact-api-{uuid4().hex}"
    test_root.mkdir(parents=True)
    monkeypatch.setattr(
        artifacts_api, "service", ArtifactService(LocalArtifactStorage(test_root))
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "demo123"},
            )
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            response = await client.post(
                "/api/v2/artifacts",
                headers=headers,
                data={"kind": "wall_source"},
                files={"file": ("plan.pdf", b"%PDF-1.7\nwall", "application/pdf")},
            )
        assert response.status_code == 201, response.text
        assert response.json()["filename"] == "plan.pdf"
    finally:
        rmtree(test_root, ignore_errors=True)
