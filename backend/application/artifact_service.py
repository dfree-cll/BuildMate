"""Artifact upload validation, hashing and metadata persistence."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import text

from backend.db.session import engine
from backend.domain.contracts import RequestContext
from backend.domain.errors import ResourceNotFound, ValidationFailure
from backend.ports.artifact_storage import ArtifactStorage
from backend.config import get_settings
from backend.adapters.local_artifact_storage import LocalArtifactStorage

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")
_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".ifc": (b"ISO-10303-21",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
}
_ALLOWED_EXTENSIONS = frozenset({
    ".pdf", ".ifc", ".dxf", ".dwg", ".png", ".jpg", ".jpeg", ".docx", ".txt", ".md", ".json"
})


class ArtifactService:
    def __init__(self, storage: ArtifactStorage, max_upload_mb: int = 200):
        self._storage = storage
        self._max_bytes = max_upload_mb * 1024 * 1024

    async def _find_existing_content(
        self,
        context: RequestContext,
        *,
        kind: str,
        filename: str,
        sha256: str,
    ) -> dict | None:
        """Return an active identical artifact in the same tenant/project.

        Worker retries are deliberately idempotent: immutable pipeline
        outputs should not create a new database row merely because the
        message was delivered twice.  Project scope is part of the key so a
        file from one project can never satisfy another project's lookup.
        """

        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, kind, filename, media_type,
                       storage_uri, sha256, size_bytes, status, created_by,
                       created_at, updated_at, version
                FROM artifacts
                WHERE tenant_id=:tenant_id
                  AND kind=:kind AND filename=:filename AND sha256=:sha256
                  AND status='active'
                  AND ((CAST(:project_id AS TEXT) IS NULL AND project_id IS NULL)
                       OR project_id=CAST(:project_id AS TEXT))
                ORDER BY created_at ASC, id ASC
                LIMIT 1
            """), {
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
                "kind": kind,
                "filename": filename,
                "sha256": sha256,
            })).mappings().first()
        return dict(row) if row else None

    async def _persist_staged(
        self,
        context: RequestContext,
        staged: Path,
        *,
        kind: str,
        filename: str,
        media_type: str | None,
        sha256: str,
        size_bytes: int,
    ) -> dict:
        """Deduplicate, store and register one already validated staged file."""
        existing = await self._find_existing_content(
            context, kind=kind, filename=filename, sha256=sha256
        )
        if existing is not None:
            return existing
        storage_uri = await self._storage.put(context.tenant_id, sha256, staged)
        artifact_id = "art_" + uuid.uuid4().hex
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO artifacts (
                    id, tenant_id, project_id, kind, filename, media_type,
                    storage_uri, sha256, size_bytes, status, created_by, version
                ) VALUES (
                    :id, :tenant_id, :project_id, :kind, :filename, :media_type,
                    :storage_uri, :sha256, :size_bytes, 'active', :created_by, 1
                )
            """), {
                "id": artifact_id,
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
                "kind": kind,
                "filename": filename,
                "media_type": media_type,
                "storage_uri": storage_uri,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "created_by": context.user_id,
            })
        return await self.get(context, artifact_id)

    async def store_upload(
        self,
        context: RequestContext,
        upload: UploadFile,
        *,
        kind: str,
    ) -> dict:
        filename = _SAFE_NAME.sub("_", Path(upload.filename or "upload.bin").name)[:256]
        extension = Path(filename).suffix.lower()
        if extension not in _ALLOWED_EXTENSIONS:
            raise ValidationFailure(f"unsupported artifact type: {extension or 'unknown'}")

        digest = hashlib.sha256()
        total = 0
        first = b""
        descriptor, temporary_name = tempfile.mkstemp(prefix="buildmate-artifact-")
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with temporary.open("wb") as stream:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    if not first:
                        first = chunk[:32]
                    total += len(chunk)
                    if total > self._max_bytes:
                        raise ValidationFailure("artifact exceeds configured upload limit")
                    digest.update(chunk)
                    stream.write(chunk)
            if total == 0:
                raise ValidationFailure("artifact is empty")
            expected_magic = _MAGIC.get(extension)
            if expected_magic and not any(first.startswith(item) for item in expected_magic):
                raise ValidationFailure("artifact content does not match its extension")
            sha256 = digest.hexdigest()
            return await self._persist_staged(
                context,
                temporary,
                kind=kind,
                filename=filename,
                media_type=upload.content_type,
                sha256=sha256,
                size_bytes=total,
            )
        finally:
            if temporary.exists():
                temporary.unlink()

    async def store_file(
        self,
        context: RequestContext,
        source: Path,
        *,
        kind: str,
        filename: str | None = None,
    ) -> dict:
        """Persist a worker-produced file as an immutable artifact.

        The source file belongs to a pipeline workspace and must remain there
        for later audit/debugging, so the storage adapter receives a temporary
        copy instead of the original path.
        """
        source = Path(source)
        if not source.is_file():
            raise ResourceNotFound("artifact source file not found")
        safe_name = _SAFE_NAME.sub("_", filename or source.name).strip("._")[:256]
        if not safe_name:
            safe_name = "artifact.bin"
        descriptor, temporary_name = tempfile.mkstemp(prefix="buildmate-artifact-")
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(source, temporary)
            # Hash the immutable staged copy, not the source before copying;
            # an uploader may replace the source concurrently.
            digest = hashlib.sha256()
            with temporary.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
            size_bytes = temporary.stat().st_size
            return await self._persist_staged(
                context,
                temporary,
                kind=kind,
                filename=safe_name,
                media_type=mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
                sha256=sha256,
                size_bytes=size_bytes,
            )
        finally:
            if temporary.exists():
                temporary.unlink()

    async def get(self, context: RequestContext, artifact_id: str) -> dict:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, kind, filename, media_type,
                       storage_uri, sha256, size_bytes, status, created_by,
                       created_at, updated_at, version
                FROM artifacts
                WHERE id=:id AND tenant_id=:tenant_id
                  AND (
                    project_id IS NULL
                    OR (CAST(:project_id AS TEXT) IS NOT NULL AND project_id=CAST(:project_id AS TEXT))
                  )
            """), {
                "id": artifact_id,
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
            })).mappings().first()
        if not row:
            raise ResourceNotFound("artifact not found")
        return dict(row)

    async def resolve(self, context: RequestContext, artifact_id: str) -> tuple[dict, Path]:
        artifact = await self.get(context, artifact_id)
        return artifact, await self._storage.resolve(artifact["storage_uri"])


@lru_cache()
def get_artifact_service() -> ArtifactService:
    settings = get_settings()
    project_root = Path(__file__).resolve().parents[2]
    root = Path(settings.artifact_storage_root)
    if not root.is_absolute():
        root = project_root / root
    if settings.artifact_storage_backend == "local":
        storage = LocalArtifactStorage(root)
    elif settings.artifact_storage_backend == "s3":
        from backend.adapters.s3_artifact_storage import S3ArtifactStorage

        cache_root = Path(settings.s3_cache_root)
        if not cache_root.is_absolute():
            cache_root = project_root / cache_root
        storage = S3ArtifactStorage(
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            cache_root=cache_root,
        )
    else:
        raise ValidationFailure(
            f"artifact storage backend is not installed: {settings.artifact_storage_backend}"
        )
    return ArtifactService(storage, settings.bim_max_upload_mb)
