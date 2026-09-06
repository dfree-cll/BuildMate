"""Ingest bundled Markdown documents through the canonical RAG v2 pipeline.

Scans ``data/knowledge`` and ``data/knowledge_real``. Re-running is safe:
document versions are content-addressed and existing versions are reused.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from backend.config import get_settings
from backend.domain.contracts import RequestContext
from backend.rag.contracts import KnowledgeDocumentCreate, KnowledgeScope
from backend.rag.service import RAGService


async def seed_markdown_directories(
    directories: list[Path],
    *,
    tenant_id: str,
) -> tuple[int, int]:
    """Ingest Markdown files and return ``(document_count, new_chunk_count)``."""

    context = RequestContext(
        tenant_id=tenant_id,
        project_id=None,
        user_id="system:knowledge-seed",
        role="system",
        trace_id="knowledge-seed",
        correlation_id="knowledge-seed",
    )
    service = RAGService()
    document_count = 0
    new_chunk_count = 0
    for directory in directories:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            result, created = await service.ingest(
                context,
                KnowledgeDocumentCreate(
                    scope=KnowledgeScope.TENANT,
                    source_type="markdown",
                    source_uri=path.resolve().as_uri(),
                    title=path.stem,
                    content=path.read_text(encoding="utf-8"),
                    metadata={"filename": path.name, "seed": True},
                ),
            )
            document_count += 1
            chunk_count = int(result.get("chunk_count") or 0) if created else 0
            new_chunk_count += chunk_count
            state = f"{chunk_count} chunks" if created else "unchanged"
            print(f"  {path.name}: {state}")
    return document_count, new_chunk_count


async def main() -> None:
    settings = get_settings()
    documents, chunks = await seed_markdown_directories(
        [_ROOT / "data" / "knowledge", _ROOT / "data" / "knowledge_real"],
        tenant_id=settings.default_tenant_id,
    )
    print(f"Knowledge seed complete: {documents} documents, {chunks} new chunks")


if __name__ == "__main__":
    asyncio.run(main())
