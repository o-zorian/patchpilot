"""Add M5 sandbox execution metadata.

Revision ID: 20260808_0005
Revises: 20260808_0004
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0005"
down_revision: str | None = "20260808_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("sandbox_mode", sa.String(length=32)))
    op.add_column("runs", sa.Column("sandbox_image", sa.String(length=255)))


def downgrade() -> None:
    op.drop_column("runs", "sandbox_image")
    op.drop_column("runs", "sandbox_mode")
