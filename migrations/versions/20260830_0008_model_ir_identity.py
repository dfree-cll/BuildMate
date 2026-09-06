"""Include parser version in reproducible Model IR identity."""

from typing import Sequence, Union

from alembic import op

revision: str = "0008_model_ir_identity"
down_revision: Union[str, None] = "0007_postgres_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("model_ir_versions") as batch:
        batch.drop_constraint("uq_model_ir_reproducible_version", type_="unique")
        batch.create_unique_constraint(
            "uq_model_ir_reproducible_version",
            ["tenant_id", "project_id", "source_sha256", "pipeline_version", "parser_version"],
        )


def downgrade() -> None:
    with op.batch_alter_table("model_ir_versions") as batch:
        batch.drop_constraint("uq_model_ir_reproducible_version", type_="unique")
        batch.create_unique_constraint(
            "uq_model_ir_reproducible_version",
            ["tenant_id", "project_id", "source_sha256", "pipeline_version"],
        )
