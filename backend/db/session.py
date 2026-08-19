"""异步数据库引擎（默认 SQLite，可切 PostgreSQL；统一从 config 读取连接串）
"""
from sqlalchemy.ext.asyncio import create_async_engine
from backend.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
