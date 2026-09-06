"""BIM learning loop: profiles, build runs, feedback and element mappings."""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.db.schema import METADATA

revision: str = "0003_bim_learning"
down_revision: Union[str, None] = "0002_bim_reviews"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("drawing_profiles", "build_runs", "extraction_feedback", "element_mappings")


def upgrade() -> None:
    bind = op.get_bind()
    for name in _TABLES:
        METADATA.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    for name in reversed(_TABLES):
        if name in existing:
            op.drop_table(name)
