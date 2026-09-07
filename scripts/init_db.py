"""初始化数据库：建表 + 灌种子数据
用法：python scripts/init_db.py

企业化说明：建表 DDL 的唯一事实源是 backend/db/schema.py；
应用启动时 Alembic 也会做基线收敛（migrations/），本脚本用于 demo/离线环境
一键建库 + 灌种子用户。生产部署用 `alembic upgrade head` 即可，无需本脚本。
"""
import asyncio
import sys
import os
import uuid

# Windows 控制台 UTF-8 输出
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from backend.db.session import engine
from backend.db.schema import METADATA
from backend.config import get_settings


async def init():
    # 建表（幂等；唯一事实源 backend/db/schema.py）
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    # 灌种子用户（密码都是 demo123，bcrypt 哈希；角色与 mock_users 对齐）。
    async with engine.begin() as conn:
        from backend.core.security import hash_password
        ph = hash_password("demo123")
        # 审核角色命名迁移：保留用户主键和历史引用，只更新登录名/角色并
        # bump token_version，使携带旧角色的令牌立即失效。
        reviewer_exists = (await conn.execute(text(
            "SELECT 1 FROM users WHERE username = 'reviewer01'"
        ))).fetchone()
        if reviewer_exists is None:
            await conn.execute(text(
                "UPDATE users SET username = 'reviewer01', email = 'reviewer01@buildmate.local', "
                "role = 'reviewer', token_version = token_version + 1 "
                "WHERE username = 'teacher01'"
            ))
        await conn.execute(text(
            "UPDATE users SET role = 'reviewer', token_version = token_version + 1 "
            "WHERE role = 'teacher'"
        ))
        await conn.execute(text(
            "UPDATE users SET is_active = false, token_version = token_version + 1 "
            "WHERE username = 'teacher01'"
        ))
        for name, role in [("admin", "admin"), ("buyer01", "buyer"), ("pm01", "project"), ("reviewer01", "reviewer")]:
            # 存在即更新（幂等修复：旧库占位哈希/旧角色重跑即被纠正；DO NOTHING 修不了陈旧种子）
            sql = ("INSERT INTO users (id, tenant_id, username, email, password_hash, role) "
                   "VALUES (:id, 'tenant_default', :name, :email, :ph, :role) "
                   "ON CONFLICT (username) DO UPDATE SET password_hash=excluded.password_hash, role=excluded.role")
            await conn.execute(text(sql),
                {"id": str(uuid.uuid4()), "name": name, "email": f"{name}@buildmate.local", "role": role, "ph": ph})
    print(f"✅ 数据库初始化完成（{get_settings().database_url}）")


async def _dispose_engine():
    from backend.db.session import engine
    await engine.dispose()


async def _main():
    await init()
    # 同一事件循环内 dispose（此前分两次 asyncio.run：Windows Proactor 下
    # asyncpg 连接跨循环关闭会抛 AttributeError 噪音 traceback）
    await _dispose_engine()


if __name__ == "__main__":
    asyncio.run(_main())
