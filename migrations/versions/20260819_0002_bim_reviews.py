"""bim_reviews: BIM 模型合规审查记录表

Revision ID: 0002_bim_reviews
Revises: 0001_baseline
Create Date: 2026-08-19

带存在性守卫：项目同时存在 METADATA.create_all 的建库途径（init_db.py /
测试夹具），表可能已被预建——守卫让 upgrade 在两种途径下都安全（幂等）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_bim_reviews"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("bim_reviews"):
        op.create_table(
            "bim_reviews",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="tenant_default"),
            sa.Column("user_id", sa.Text),
            sa.Column("file_name", sa.String(256), nullable=False),
            sa.Column("structured_data", sa.Text),
            sa.Column("issues", sa.Text),
            sa.Column("summary", sa.Text),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("error_msg", sa.Text),
            sa.Column("created_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP")),
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("bim_reviews"):
        op.drop_table("bim_reviews")
