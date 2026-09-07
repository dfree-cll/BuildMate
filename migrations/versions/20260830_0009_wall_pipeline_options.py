"""Persist workflow options required by configurable wall pipelines."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009_wall_pipeline_options"
down_revision: Union[str, None] = "0008_model_ir_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("workflow_runs")}
    if "options" not in columns:
        op.add_column(
            "workflow_runs",
            sa.Column("options", sa.Text(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("workflow_runs")}
    if "options" in columns:
        op.drop_column("workflow_runs", "options")
