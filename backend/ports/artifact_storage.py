"""Content-addressed artifact storage port."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ArtifactStorage(Protocol):
    async def put(self, tenant_id: str, sha256: str, source: Path) -> str:
        """Store an immutable artifact and return its storage URI."""

    async def resolve(self, storage_uri: str) -> Path:
        """Resolve an artifact for local delivery or bridge handoff."""
