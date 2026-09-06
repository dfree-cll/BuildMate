"""Add persisted review/build/chat slices and feedback operator."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.db.schema import METADATA

revision: str = "0006_workflows_chat"
down_revision: Union[str, None] = "0005_v2_domain_rag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("review_runs_v2", "model_build_runs", "chat_sessions_v2", "chat_messages_v2")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    for name in _TABLES:
        if name not in existing:
            METADATA.tables[name].create(bind, checkfirst=True)
    if "knowledge_feedback" in existing:
        columns = {item["name"] for item in inspector.get_columns("knowledge_feedback")}
        if "operator" not in columns:
            with op.batch_alter_table("knowledge_feedback") as batch:
                batch.add_column(sa.Column("operator", sa.Text(), nullable=True))
            op.execute("UPDATE knowledge_feedback SET operator=created_by WHERE operator IS NULL")
            with op.batch_alter_table("knowledge_feedback") as batch:
                batch.alter_column("operator", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    if "knowledge_feedback" in existing:
        columns = {item["name"] for item in inspector.get_columns("knowledge_feedback")}
        if "operator" in columns:
            with op.batch_alter_table("knowledge_feedback") as batch:
                batch.drop_column("operator")
    for name in reversed(_TABLES):
        if name in existing:
            op.drop_table(name)
