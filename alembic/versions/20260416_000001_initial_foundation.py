"""Initial foundation schema."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260416_000001"
down_revision = None
branch_labels = None
depends_on = None


application_mode = sa.Enum("draft", "guarded_submit", "assist", name="applicationmode")
browser_profile_type = sa.Enum("discovery", "application", name="browserprofiletype")
artifact_type = sa.Enum(
    "screenshot",
    "trace",
    "html_snapshot",
    "generated_document",
    "model_io",
    "answer_pack",
    name="artifacttype",
)
truth_tier = sa.Enum("observed", "inference", "extension", name="truthtier")


def upgrade() -> None:
    for enum in (
        application_mode,
        browser_profile_type,
        artifact_type,
        truth_tier,
    ):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_companies_name", "companies", ["name"])
    op.create_index("ix_companies_domain", "companies", ["domain"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("source_type", sa.String(length=100), nullable=True),
        sa.Column("external_job_id", sa.String(length=255), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("title_normalized", sa.String(length=255), nullable=False),
        sa.Column("location_raw", sa.String(length=255), nullable=True),
        sa.Column("location_normalized", sa.String(length=255), nullable=True),
        sa.Column("remote_type", sa.String(length=50), nullable=True),
        sa.Column("employment_type", sa.String(length=50), nullable=True),
        sa.Column("seniority", sa.String(length=50), nullable=True),
        sa.Column("salary_text", sa.String(length=255), nullable=True),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("description_raw", sa.Text(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column("requirements_structured", sa.JSON(), nullable=True),
        sa.Column("benefits_structured", sa.JSON(), nullable=True),
        sa.Column("application_url", sa.Text(), nullable=True),
        sa.Column("ats_vendor", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.UniqueConstraint("canonical_url"),
    )
    for index in (
        "company_id",
        "source",
        "source_type",
        "external_job_id",
        "title",
        "title_normalized",
        "location_normalized",
        "remote_type",
        "seniority",
        "ats_vendor",
        "status",
        "discovered_at",
        "last_seen_at",
    ):
        op.create_index(f"ix_jobs_{index}", "jobs", [index])

    op.create_table(
        "job_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_external_id", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.UniqueConstraint("source_type", "source_url", name="uq_job_sources_type_url"),
    )
    op.create_index("ix_job_sources_job_id", "job_sources", ["job_id"])
    op.create_index("ix_job_sources_source_type", "job_sources", ["source_type"])

    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("personal_details", sa.JSON(), nullable=False),
        sa.Column("target_preferences", sa.JSON(), nullable=False),
        sa.Column("source_profile_data", sa.JSON(), nullable=False),
        sa.Column("banned_claims", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_candidate_profiles_name", "candidate_profiles", ["name"])

    op.create_table(
        "job_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("score_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["candidate_profile_id"], ["candidate_profiles.id"]),
        sa.UniqueConstraint("job_id", "candidate_profile_id", name="uq_job_scores_job_candidate"),
    )
    op.create_index("ix_job_scores_job_id", "job_scores", ["job_id"])
    op.create_index("ix_job_scores_candidate_profile_id", "job_scores", ["candidate_profile_id"])
    op.create_index("ix_job_scores_created_at", "job_scores", ["created_at"])
    op.create_index("ix_job_scores_updated_at", "job_scores", ["updated_at"])

    op.create_table(
        "candidate_facts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("fact_key", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured_data", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_profile_id"], ["candidate_profiles.id"]),
        sa.UniqueConstraint("candidate_profile_id", "fact_key", name="uq_candidate_facts_profile_fact_key"),
    )
    op.create_index("ix_candidate_facts_candidate_profile_id", "candidate_facts", ["candidate_profile_id"])
    op.create_index("ix_candidate_facts_fact_key", "candidate_facts", ["fact_key"])
    op.create_index("ix_candidate_facts_category", "candidate_facts", ["category"])

    op.create_table(
        "browser_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=True),
        sa.Column("profile_key", sa.String(length=100), nullable=False),
        sa.Column("profile_type", browser_profile_type, nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("session_health", sa.String(length=50), nullable=False),
        sa.Column("validation_details", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_profile_id"], ["candidate_profiles.id"]),
        sa.UniqueConstraint("profile_key", name="uq_browser_profiles_profile_key"),
    )
    op.create_index("ix_browser_profiles_candidate_profile_id", "browser_profiles", ["candidate_profile_id"])
    op.create_index("ix_browser_profiles_profile_key", "browser_profiles", ["profile_key"])
    op.create_index("ix_browser_profiles_profile_type", "browser_profiles", ["profile_type"])
    op.create_index("ix_browser_profiles_session_health", "browser_profiles", ["session_health"])
    op.create_index("ix_browser_profiles_last_validated_at", "browser_profiles", ["last_validated_at"])
    op.create_index("ix_browser_profiles_last_used_at", "browser_profiles", ["last_used_at"])

    op.create_table(
        "generated_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("document_type", sa.String(length=50), nullable=False),
        sa.Column("truth_tier_max", truth_tier, nullable=True),
        sa.Column("review_status", sa.String(length=50), nullable=False),
        sa.Column("content_path", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_profile_id"], ["candidate_profiles.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
    )
    op.create_index("ix_generated_documents_candidate_profile_id", "generated_documents", ["candidate_profile_id"])
    op.create_index("ix_generated_documents_job_id", "generated_documents", ["job_id"])
    op.create_index("ix_generated_documents_document_type", "generated_documents", ["document_type"])
    op.create_index("ix_generated_documents_review_status", "generated_documents", ["review_status"])

    op.create_table(
        "resume_variants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_resume_path", sa.Text(), nullable=True),
        sa.Column("generated_document_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_profile_id"], ["candidate_profiles.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["generated_document_id"], ["generated_documents.id"]),
    )
    op.create_index("ix_resume_variants_candidate_profile_id", "resume_variants", ["candidate_profile_id"])
    op.create_index("ix_resume_variants_job_id", "resume_variants", ["job_id"])

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("current_state", sa.String(length=50), nullable=False),
        sa.Column("last_attempt_id", sa.Integer(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_profile_id"], ["candidate_profiles.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.UniqueConstraint("job_id", "candidate_profile_id", name="uq_applications_job_candidate"),
    )
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index("ix_applications_candidate_profile_id", "applications", ["candidate_profile_id"])
    op.create_index("ix_applications_current_state", "applications", ["current_state"])
    op.create_index("ix_applications_applied_at", "applications", ["applied_at"])

    op.create_table(
        "application_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("mode", application_mode, nullable=False),
        sa.Column("browser_profile_key", sa.String(length=255), nullable=True),
        sa.Column("session_health", sa.String(length=50), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.String(length=50), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("submit_confidence", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
    )
    for index in ("application_id", "mode", "browser_profile_key", "started_at", "result", "failure_code"):
        op.create_index(f"ix_application_attempts_{index}", "application_attempts", [index])

    op.create_foreign_key(
        "fk_applications_last_attempt_id",
        "applications",
        "application_attempts",
        ["last_attempt_id"],
        ["id"],
    )

    op.create_table(
        "application_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["application_attempts.id"]),
    )
    for index in ("application_id", "attempt_id", "event_type", "created_at"):
        op.create_index(f"ix_application_events_{index}", "application_events", [index])

    op.create_table(
        "answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("canonical_question_hash", sa.String(length=128), nullable=False),
        sa.Column("normalized_question_text", sa.Text(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("approval_status", sa.String(length=50), nullable=False),
        sa.Column("truth_tier", truth_tier, nullable=True),
        sa.Column("extension_approved", sa.Boolean(), nullable=False),
        sa.Column("interview_prep_notes", sa.Text(), nullable=True),
        sa.Column("provenance_facts", sa.JSON(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for index in ("canonical_question_hash", "source_type", "approval_status", "truth_tier"):
        op.create_index(f"ix_answers_{index}", "answers", [index])

    op.create_table(
        "field_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("field_key", sa.String(length=100), nullable=False),
        sa.Column("raw_label", sa.Text(), nullable=True),
        sa.Column("raw_dom_signature", sa.Text(), nullable=True),
        sa.Column("inferred_type", sa.String(length=100), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("answer_id", sa.Integer(), nullable=True),
        sa.Column("truth_tier", truth_tier, nullable=True),
        sa.Column("chosen_answer", sa.Text(), nullable=True),
        sa.Column("answer_source", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["application_attempts.id"]),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"]),
    )
    for index in ("attempt_id", "field_key", "inferred_type", "answer_id", "truth_tier"):
        op.create_index(f"ix_field_mappings_{index}", "field_mappings", [index])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("attempt_id", sa.Integer(), nullable=True),
        sa.Column("artifact_type", artifact_type, nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["application_attempts.id"]),
    )
    for index in ("attempt_id", "artifact_type", "created_at"):
        op.create_index(f"ix_artifacts_{index}", "artifacts", [index])

    op.create_table(
        "model_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stage", sa.String(length=50), nullable=False),
        sa.Column("model_provider", sa.String(length=50), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("linked_entity_id", sa.Integer(), nullable=True),
        sa.Column("input_size", sa.Integer(), nullable=True),
        sa.Column("output_size", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for index in (
        "stage",
        "model_provider",
        "model_name",
        "prompt_version",
        "created_at",
    ):
        op.create_index(f"ix_model_calls_{index}", "model_calls", [index])

    op.create_table(
        "review_queue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("truth_tier", truth_tier, nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    for index in ("entity_type", "entity_id", "reason", "truth_tier", "status", "created_at"):
        op.create_index(f"ix_review_queue_{index}", "review_queue", [index])

    op.create_table(
        "application_eligibility",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("readiness_state", sa.String(length=50), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("score_summary", sa.JSON(), nullable=False),
        sa.Column("prepared_summary", sa.JSON(), nullable=False),
        sa.Column("materialized_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["candidate_profile_id"], ["candidate_profiles.id"]),
        sa.UniqueConstraint("job_id", "candidate_profile_id", name="uq_application_eligibility_job_candidate"),
    )
    for index in (
        "job_id",
        "candidate_profile_id",
        "readiness_state",
        "ready",
        "materialized_at",
        "updated_at",
    ):
        op.create_index(f"ix_application_eligibility_{index}", "application_eligibility", [index])


def downgrade() -> None:
    op.drop_table("application_eligibility")
    op.drop_table("review_queue")
    op.drop_table("model_calls")
    op.drop_table("artifacts")
    op.drop_table("field_mappings")
    op.drop_table("answers")
    op.drop_table("application_events")
    op.drop_constraint("fk_applications_last_attempt_id", "applications", type_="foreignkey")
    op.drop_table("application_attempts")
    op.drop_table("applications")
    op.drop_table("resume_variants")
    op.drop_table("generated_documents")
    op.drop_table("browser_profiles")
    op.drop_table("candidate_facts")
    op.drop_table("job_scores")
    op.drop_table("candidate_profiles")
    op.drop_table("job_sources")
    op.drop_table("jobs")
    op.drop_table("companies")

    for enum in (
        truth_tier,
        artifact_type,
        browser_profile_type,
        application_mode,
    ):
        enum.drop(op.get_bind(), checkfirst=True)
