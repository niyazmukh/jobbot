"""Add owner diagnostics to queue runner leases."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260418_000004"
down_revision = "20260418_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "auto_apply_queue_runner_leases",
        sa.Column("lease_owner_host", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "auto_apply_queue_runner_leases",
        sa.Column("lease_owner_pid", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_auto_apply_queue_runner_leases_lease_owner_host",
        "auto_apply_queue_runner_leases",
        ["lease_owner_host"],
    )
    op.create_index(
        "ix_auto_apply_queue_runner_leases_lease_owner_pid",
        "auto_apply_queue_runner_leases",
        ["lease_owner_pid"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auto_apply_queue_runner_leases_lease_owner_pid",
        table_name="auto_apply_queue_runner_leases",
    )
    op.drop_index(
        "ix_auto_apply_queue_runner_leases_lease_owner_host",
        table_name="auto_apply_queue_runner_leases",
    )
    op.drop_column("auto_apply_queue_runner_leases", "lease_owner_pid")
    op.drop_column("auto_apply_queue_runner_leases", "lease_owner_host")
