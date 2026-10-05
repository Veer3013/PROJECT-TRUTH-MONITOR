"""scope sources to endpoint URLs

Revision ID: c42e8f1a9d03
Revises: b8d31f2972a6
Create Date: 2026-08-03 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c42e8f1a9d03"
down_revision: str | None = "b8d31f2972a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch_op:
        batch_op.drop_constraint("uq_source_org_domain_name", type_="unique")
        batch_op.create_unique_constraint(
            "uq_source_org_domain_name_url",
            ["organization_id", "domain", "name", "base_url"],
        )


def downgrade() -> None:
    with op.batch_alter_table("sources") as batch_op:
        batch_op.drop_constraint("uq_source_org_domain_name_url", type_="unique")
        batch_op.create_unique_constraint(
            "uq_source_org_domain_name",
            ["organization_id", "domain", "name"],
        )
