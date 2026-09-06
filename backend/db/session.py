"""异步数据库引擎（默认 SQLite，可切 PostgreSQL；统一从 config 读取连接串）
"""
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from backend.config import get_settings

settings = get_settings()

_database_url = make_url(settings.database_url)
if _database_url.get_backend_name() == "sqlite" and _database_url.database not in {None, "", ":memory:"}:
    Path(_database_url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)


@event.listens_for(engine.sync_engine, "begin")
def _set_postgres_rls_context(connection):
    if connection.dialect.name != "postgresql":
        return
    from backend.db.rls import current_rls_context

    tenant_id, user_id = current_rls_context()
    # Empty values deliberately fail closed under RLS policies.
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true), "
             "set_config('app.user_id', :user_id, true)"),
        {"tenant_id": tenant_id, "user_id": user_id},
    )
