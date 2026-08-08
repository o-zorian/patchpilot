"""Create M3 artifact metadata table.

Revision ID: 20260808_0003
Revises: 20260807_0002
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0003"
down_revision: str | None = "20260807_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=1000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "kind", name="uq_artifacts_run_kind"),
    )
    op.create_index(
        "ix_artifacts_run_id_created_at",
        "artifacts",
        ["run_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_run_id_created_at", table_name="artifacts")
    op.drop_table("artifacts")
