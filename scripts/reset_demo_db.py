"""Explicitly reset only a local, non-production SQLite demo database.

Usage (PowerShell):
  python scripts/reset_demo_db.py --confirm RESET_DEMO_DB
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from backend.config import get_settings
from backend.db.schema import METADATA
from backend.db.session import engine


def _validated_sqlite_path() -> Path:
    settings = get_settings()
    if settings.app_env.lower() in {"prod", "production"}:
        raise RuntimeError("database reset is forbidden in production")
    prefix = "sqlite+aiosqlite:///"
    if not settings.database_url.startswith(prefix):
        raise RuntimeError("demo reset only supports SQLite; PostgreSQL must use an audited migration")
    raw = settings.database_url.removeprefix(prefix)
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    path = path.resolve()
    project_root = Path(__file__).resolve().parents[1]
    if project_root not in path.parents or path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise RuntimeError("database path must be a SQLite file inside the project workspace")
    return path


async def reset() -> None:
    path = _validated_sqlite_path()
    async with engine.begin() as connection:
        await connection.run_sync(METADATA.drop_all)
        await connection.run_sync(METADATA.create_all)
    await engine.dispose()
    print(f"Demo database reset completed: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "RESET_DEMO_DB":
        raise SystemExit("confirmation phrase must be RESET_DEMO_DB")
    asyncio.run(reset())
