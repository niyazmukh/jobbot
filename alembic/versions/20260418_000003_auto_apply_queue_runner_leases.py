"""Add candidate-scoped auto-apply queue runner lease table."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260418_000003"
down_revision = "20260418_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auto_apply_queue_runner_leases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_profile_id"], ["candidate_profiles.id"]),
        sa.UniqueConstraint(
            "candidate_profile_id",
            name="uq_auto_apply_queue_runner_leases_candidate",
        ),
    )

    for index in (
        "candidate_profile_id",
        "lease_token",
        "lease_expires_at",
        "created_at",
        "updated_at",
    ):
        op.create_index(
            f"ix_auto_apply_queue_runner_leases_{index}",
            "auto_apply_queue_runner_leases",
            [index],
        )


def downgrade() -> None:
    for index in (
        "updated_at",
        "created_at",
        "lease_expires_at",
        "lease_token",
        "candidate_profile_id",
    ):
        op.drop_index(
            f"ix_auto_apply_queue_runner_leases_{index}",
            table_name="auto_apply_queue_runner_leases",
        )

    op.drop_table("auto_apply_queue_runner_leases")
