"""Add the BuildMate v2 domain kernel and evidence-first RAG registry."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.db.schema import METADATA

revision: str = "0005_v2_domain_rag"
down_revision: Union[str, None] = "0004_bim_projects"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "tenants",
    "tenant_memberships",
    "projects",
    "artifacts",
    "workflow_runs",
    "workflow_steps",
    "task_events",
    "approvals",
    "review_findings",
    "model_ir_versions",
    "tool_calls",
    "knowledge_documents",
    "knowledge_ingest_jobs",
    "retrieval_runs",
    "knowledge_feedback",
    "outbox_events",
)

_CHUNK_COLUMNS = (
    sa.Column("project_id", sa.String(64), nullable=True),
    sa.Column("scope", sa.String(16), nullable=False, server_default="tenant"),
    sa.Column("metadata_json", sa.Text(), nullable=True),
    sa.Column("embedding_model", sa.String(128), nullable=True),
    sa.Column("page_no", sa.Integer(), nullable=True),
    sa.Column("element_refs", sa.Text(), nullable=True),
    sa.Column("token_count", sa.Integer(), nullable=True),
    sa.Column("chunk_version", sa.String(32), nullable=False, server_default="1"),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for name in _TABLES:
        if name not in existing_tables:
            METADATA.tables[name].create(bind, checkfirst=True)

    if "knowledge_chunks" in existing_tables:
        existing_columns = {
            column["name"] for column in inspector.get_columns("knowledge_chunks")
        }
        for column in _CHUNK_COLUMNS:
            if column.name not in existing_columns:
                op.add_column("knowledge_chunks", column.copy())


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "knowledge_chunks" in existing_tables:
        existing_columns = {
            column["name"] for column in inspector.get_columns("knowledge_chunks")
        }
        for column in reversed(_CHUNK_COLUMNS):
            if column.name in existing_columns:
                op.drop_column("knowledge_chunks", column.name)

    for name in reversed(_TABLES):
        if name in existing_tables:
            op.drop_table(name)
