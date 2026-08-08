"""Add M4 ownership, idempotency, worker, and cancellation state.

Revision ID: 20260808_0004
Revises: 20260808_0003
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0004"
down_revision: str | None = "20260808_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("owner_id", sa.String(length=128), server_default="local", nullable=False),
    )
    op.add_column("runs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("claimed_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("worker_id", sa.String(length=255)))
    op.create_index("ix_tasks_owner_id", "tasks", ["owner_id"], unique=False)
    op.create_index(
        "uq_runs_idempotency_scope",
        "runs",
        ["task_id", "strategy", "model", "idempotency_key"],
        unique=True,
    )
    op.create_index("ix_runs_worker_id", "runs", ["worker_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_worker_id", table_name="runs")
    op.drop_index("uq_runs_idempotency_scope", table_name="runs")
    op.drop_index("ix_tasks_owner_id", table_name="tasks")
    op.drop_column("runs", "worker_id")
    op.drop_column("runs", "heartbeat_at")
    op.drop_column("runs", "claimed_at")
    op.drop_column("runs", "cancel_requested_at")
    op.drop_column("tasks", "owner_id")
