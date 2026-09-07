"""Rename the review role and demo login without rewriting audit history."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010_reviewer_role"
down_revision: Union[str, None] = "0009_wall_pipeline_options"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table("users"):
        reviewer_exists = bind.execute(sa.text(
            "SELECT 1 FROM users WHERE username = 'reviewer01'"
        )).first()
        if reviewer_exists is None:
            bind.execute(sa.text(
                "UPDATE users SET username = 'reviewer01', "
                "email = 'reviewer01@buildmate.local', role = 'reviewer', "
                "token_version = token_version + 1 WHERE username = 'teacher01'"
            ))
        bind.execute(sa.text(
            "UPDATE users SET role = 'reviewer', token_version = token_version + 1 "
            "WHERE role = 'teacher'"
        ))
        # A pre-existing reviewer account wins the canonical login. Any remaining
        # legacy demo login is disabled so the old ID cannot authenticate.
        bind.execute(sa.text(
            "UPDATE users SET is_active = false, token_version = token_version + 1 "
            "WHERE username = 'teacher01'"
        ))
    if _has_table("tenant_memberships"):
        bind.execute(sa.text(
            "UPDATE tenant_memberships SET role = 'reviewer', version = version + 1 "
            "WHERE role = 'teacher'"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table("tenant_memberships"):
        bind.execute(sa.text(
            "UPDATE tenant_memberships SET role = 'teacher', version = version + 1 "
            "WHERE role = 'reviewer'"
        ))
    if _has_table("users"):
        teacher_exists = bind.execute(sa.text(
            "SELECT 1 FROM users WHERE username = 'teacher01'"
        )).first()
        if teacher_exists is None:
            bind.execute(sa.text(
                "UPDATE users SET username = 'teacher01', "
                "email = 'teacher01@buildmate.local', role = 'teacher', "
                "token_version = token_version + 1 WHERE username = 'reviewer01'"
            ))
        bind.execute(sa.text(
            "UPDATE users SET role = 'teacher', token_version = token_version + 1 "
            "WHERE role = 'reviewer'"
        ))
