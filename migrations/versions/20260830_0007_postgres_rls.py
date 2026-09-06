"""Enable PostgreSQL tenant RLS for v2 business tables."""

from typing import Sequence, Union

from alembic import op

revision: str = "0007_postgres_rls"
down_revision: Union[str, None] = "0006_workflows_chat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TENANT_TABLES = (
    "tenant_memberships", "projects", "artifacts", "workflow_runs",
    "workflow_steps", "task_events", "approvals", "review_findings",
    "review_runs_v2", "model_ir_versions", "model_build_runs", "tool_calls",
    "knowledge_documents", "knowledge_chunks", "knowledge_ingest_jobs",
    "retrieval_runs", "knowledge_feedback", "chat_sessions_v2",
    "chat_messages_v2", "outbox_events",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TENANT_TABLES:
        policy = f"{table}_tenant_isolation"
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"')
        op.execute(
            f'CREATE POLICY "{policy}" ON "{table}" '
            "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in reversed(_TENANT_TABLES):
        policy = f"{table}_tenant_isolation"
        op.execute(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
