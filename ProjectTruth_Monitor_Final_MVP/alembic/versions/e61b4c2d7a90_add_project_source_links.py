"""add project source links

Revision ID: e61b4c2d7a90
Revises: c42e8f1a9d03
Create Date: 2026-08-03 00:15:00.000000
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "e61b4c2d7a90"
down_revision: str | None = "c42e8f1a9d03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_source_links",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("source_role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_role IN ('primary_registration', 'supporting')",
            name=op.f("ck_project_source_links_project_source_link_role_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_project_source_links_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_source_links_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_project_source_links_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_source_links")),
        sa.UniqueConstraint(
            "organization_id",
            "project_id",
            "source_id",
            name="uq_project_source_link",
        ),
    )
    op.create_index(
        "ix_project_source_links_org_project_active",
        "project_source_links",
        ["organization_id", "project_id", "is_active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_source_links_organization_id"),
        "project_source_links",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_source_links_project_id"),
        "project_source_links",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_source_links_source_id"),
        "project_source_links",
        ["source_id"],
        unique=False,
    )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT organization_id, id AS project_id, registration_source_id AS source_id,
                   'primary_registration' AS source_role
            FROM projects
            WHERE registration_source_id IS NOT NULL
            UNION ALL
            SELECT organization_id, project_id, source_id, 'supporting' AS source_role
            FROM monitoring_jobs
            WHERE project_id IS NOT NULL
            """
        )
    ).mappings()
    now = datetime.now(UTC)
    seen: set[tuple[str, str, str]] = set()
    link_table = sa.table(
        "project_source_links",
        sa.column("id", sa.String()),
        sa.column("organization_id", sa.String()),
        sa.column("project_id", sa.String()),
        sa.column("source_id", sa.String()),
        sa.column("source_role", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for row in rows:
        key = (row["organization_id"], row["project_id"], row["source_id"])
        if key in seen:
            continue
        seen.add(key)
        connection.execute(
            link_table.insert().values(
                id=str(uuid4()),
                organization_id=row["organization_id"],
                project_id=row["project_id"],
                source_id=row["source_id"],
                source_role=row["source_role"],
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_project_source_links_source_id"),
        table_name="project_source_links",
    )
    op.drop_index(
        op.f("ix_project_source_links_project_id"),
        table_name="project_source_links",
    )
    op.drop_index(
        op.f("ix_project_source_links_organization_id"),
        table_name="project_source_links",
    )
    op.drop_index(
        "ix_project_source_links_org_project_active",
        table_name="project_source_links",
    )
    op.drop_table("project_source_links")
