"""Safe content-addressed local artifact storage."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from backend.domain.errors import ResourceNotFound, ValidationFailure


def _copy_into_storage(source: Path, target: Path) -> None:
    """Copy a temporary upload onto the target volume, then publish atomically.

    Uploads are commonly staged by the web server in the Windows temp directory
    (usually C:) while the project storage may be on another volume.  ``os.replace``
    only supports atomic moves within one volume, so stage the copy beside the
    final artifact before replacing it.
    """

    file_descriptor, staging_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=str(target.parent),
    )
    os.close(file_descriptor)
    staging = Path(staging_name)
    try:
        shutil.copyfile(source, staging)
        os.replace(staging, target)
    finally:
        if staging.exists():
            staging.unlink()
        if source.exists():
            source.unlink()


class LocalArtifactStorage:
    def __init__(self, root: Path):
        self._root = root.resolve()

    async def put(self, tenant_id: str, sha256: str, source: Path) -> str:
        safe_tenant = "".join(ch for ch in tenant_id if ch.isalnum() or ch in "-_")
        if not safe_tenant:
            raise ValidationFailure("invalid tenant id for artifact storage")
        target = (self._root / safe_tenant / sha256[:2] / sha256).resolve()
        if self._root not in target.parents:
            raise ValidationFailure("artifact target escapes storage root")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            await asyncio.to_thread(_copy_into_storage, source, target)
        elif source.exists():
            await asyncio.to_thread(source.unlink)
        return "local://" + target.relative_to(self._root).as_posix()

    async def resolve(self, storage_uri: str) -> Path:
        if not storage_uri.startswith("local://"):
            raise ValidationFailure("unsupported artifact storage URI")
        path = (self._root / storage_uri.removeprefix("local://")).resolve()
        if self._root not in path.parents or not path.is_file():
            raise ResourceNotFound("artifact content not found")
        return path
