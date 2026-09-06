"""Private agent memory, with database-enforced tenant/user isolation on PostgreSQL."""
from alembic import op
import sqlalchemy as sa

revision = "0011_agent_memory"
down_revision = "0010_reviewer_role"
branch_labels = None
depends_on = None


def _owner_columns():
    return [sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("project_id", sa.String(64)),
            sa.Column("created_by", sa.String(128), nullable=False)]


def _create_table(name, *items):
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(name):
        actual = {col["name"] for col in inspector.get_columns(name)}
        required = {item.name for item in items if isinstance(item, sa.Column)}
        if not required <= actual:
            raise RuntimeError(f"{name} exists with an incompatible schema")
        return  # Test/bootstrap create_all may already have installed this exact schema.
    op.create_table(name, *items)


def upgrade():
    _create_table("agent_memory_sessions",
        sa.Column("id", sa.String(64), primary_key=True), *_owner_columns(),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default="新会话"),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("preferences", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"))
    _create_table("agent_memory_turns",
        sa.Column("id", sa.String(64), primary_key=True), *_owner_columns(),
        sa.Column("memory_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("user_text", sa.Text, nullable=False), sa.Column("answer", sa.Text, nullable=False),
        sa.Column("result", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("memory_id", "seq", name="uq_agent_memory_turn_seq"))
    op.create_index("idx_agent_memory_scope", "agent_memory_sessions",
                    ["tenant_id", "project_id", "created_by", "agent", "updated_at"], if_not_exists=True)
    op.create_index("idx_agent_memory_turns", "agent_memory_turns", ["memory_id", "seq"], if_not_exists=True)
    if op.get_bind().dialect.name == "postgresql":
        for table in ("agent_memory_sessions", "agent_memory_turns"):
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            predicate = ("tenant_id = NULLIF(current_setting('app.tenant_id', true), '') AND "
                         "created_by = NULLIF(current_setting('app.user_id', true), '')")
            op.execute(f'CREATE POLICY "{table}_owner" ON "{table}" USING ({predicate}) WITH CHECK ({predicate})')


def downgrade():
    op.drop_table("agent_memory_turns")
    op.drop_table("agent_memory_sessions")
