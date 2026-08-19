"""baseline: 收敛存量库到 schema.py 基线（建缺失表 + 补缺失列，幂等）

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-19

背景：项目历史上用「启动时幂等补丁」（db/migrations.py）+ init_db 字符串 DDL 管表，
无版本记录。本迁移把任意状态的存量库（含全新库）收敛到 backend/db/schema.py 基线：
- 表不存在 → 创建（含索引/唯一约束）
- 表存在但缺列 → ALTER ADD COLUMN（带默认值；SQLite/PG 均兼容）
这样 `upgrade head` 对新旧库都安全，等价于「自动 stamp + 补齐」。

此后新的结构变更请用 `alembic revision --autogenerate` 生成常规差分迁移，
不要再写这种收敛式迁移。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.db.schema import METADATA

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ★ 基线冻结表清单：只收敛基线时期已存在的表。
# schema.py 后续新增的表（如 bim_reviews）由各自的增量迁移创建，
# 不要加进这个清单——否则新库跑 0001 时会把新表建出来，随后 0002 再建会冲突。
_BASELINE_TABLES = frozenset({
    "users", "qa_sessions", "bid_reviews", "purchase_orders", "approval_records",
    "negotiation_sessions", "knowledge_pending_queue", "knowledge_chunks",
    "material_prices", "llm_calls",
})


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in METADATA.sorted_tables:
        if table.name == "alembic_version" or table.name not in _BASELINE_TABLES:
            continue
        if table.name not in set(inspector.get_table_names()):
            table.create(bind, checkfirst=True)
            inspector = sa.inspect(bind)   # 刷新缓存（后续表可能引用外键/索引）
            continue
        # 表已存在：只补缺失列（不动既有约束/默认值，避免误伤存量数据）；
        # 主键/唯一列无法经 ALTER 安全补齐（基线场景不应出现缺失）
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing_cols or col.primary_key or col.unique:
                continue
            op.add_column(table.name, col.copy())


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    for table in reversed(METADATA.sorted_tables):
        if table.name in existing and table.name in _BASELINE_TABLES:
            table.drop(bind, checkfirst=True)
