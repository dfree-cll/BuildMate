"""Alembic 迁移环境（async engine，目标 schema 取自 backend/db/schema.py）

用法：
  生成增量迁移：alembic revision --autogenerate -m "描述"
  升级到最新：  alembic upgrade head（应用启动时也会自动执行，见 main.py lifespan）
  查看状态：    alembic current
连接串取自 DATABASE_URL（.env / 环境变量），与业务库同库。
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.config import get_settings
from backend.db.schema import METADATA

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 连接串注入：仅当调用方未显式提供（或仍是 ini 占位符）时才取自 backend 配置。
# ★ 此前无条件覆盖，导致程序化调用显式指定的目标库被悄悄替换成 .env 里的库——
#   曾把基线迁移意外跑向真实业务库（幸而迁移设计为只增不改，数据无损）。
_injected_url = config.get_main_option("sqlalchemy.url") or ""
if not _injected_url or "driver://" in _injected_url:
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = METADATA


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 不执行（alembic upgrade --sql）"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
