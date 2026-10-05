"""add project merge target

Revision ID: b8d31f2972a6
Revises: 71849af998f2
Create Date: 2026-08-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d31f2972a6"
down_revision: str | None = "71849af998f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("merged_into_project_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_projects_merged_into_project_id_projects",
            "projects",
            ["merged_into_project_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_projects_merged_into_project_id", ["merged_into_project_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_index("ix_projects_merged_into_project_id")
        batch_op.drop_constraint(
            "fk_projects_merged_into_project_id_projects", type_="foreignkey"
        )
        batch_op.drop_column("merged_into_project_id")
