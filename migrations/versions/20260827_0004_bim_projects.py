"""Persist project-level BIM models and their immutable level ledger."""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.db.schema import METADATA

revision: str = "0004_bim_projects"
down_revision: Union[str, None] = "0003_bim_learning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("bim_projects", "bim_project_levels")


def upgrade() -> None:
    bind = op.get_bind()
    for name in _TABLES:
        METADATA.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for name in reversed(_TABLES):
        if name in existing:
            op.drop_table(name)
