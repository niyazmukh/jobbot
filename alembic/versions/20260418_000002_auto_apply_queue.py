"""Add durable auto-apply queue table."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260418_000002"
down_revision = "20260416_000001"
branch_labels = None
depends_on = None


auto_apply_queue_status = sa.Enum(
    "queued",
    "running",
    "succeeded",
    "failed",
    name="autoapplyqueuestatus",
)


def upgrade() -> None:
    auto_apply_queue_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "auto_apply_queue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("status", auto_apply_queue_status, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("source_attempt_id", sa.Integer(), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_profile_id"], ["candidate_profiles.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["source_attempt_id"], ["application_attempts.id"]),
        sa.UniqueConstraint("candidate_profile_id", "job_id", name="uq_auto_apply_queue_candidate_job"),
    )

    for index in (
        "candidate_profile_id",
        "job_id",
        "status",
        "priority",
        "lease_token",
        "lease_expires_at",
        "next_attempt_at",
        "source_attempt_id",
        "last_error_code",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    ):
        op.create_index(f"ix_auto_apply_queue_{index}", "auto_apply_queue", [index])


def downgrade() -> None:
    for index in (
        "updated_at",
        "created_at",
        "finished_at",
        "started_at",
        "last_error_code",
        "source_attempt_id",
        "next_attempt_at",
        "lease_expires_at",
        "lease_token",
        "priority",
        "status",
        "job_id",
        "candidate_profile_id",
    ):
        op.drop_index(f"ix_auto_apply_queue_{index}", table_name="auto_apply_queue")

    op.drop_table("auto_apply_queue")
    auto_apply_queue_status.drop(op.get_bind(), checkfirst=True)
