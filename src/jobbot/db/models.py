"""Initial ORM schema for Phase 0 foundation."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobbot.db.base import Base
from jobbot.models.enums import (
    AutoApplyQueueStatus,
    ApplicationMode,
    ApplicationState,
    ArtifactType,
    BrowserProfileType,
    ReviewStatus,
    SessionHealth,
    TruthTier,
)


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)
    source: Mapped[str | None] = mapped_column(String(100), index=True)
    source_type: Mapped[str | None] = mapped_column(String(100), index=True)
    external_job_id: Mapped[str | None] = mapped_column(String(255), index=True)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    title_normalized: Mapped[str] = mapped_column(String(255), index=True)
    location_raw: Mapped[str | None] = mapped_column(String(255))
    location_normalized: Mapped[str | None] = mapped_column(String(255), index=True)
    remote_type: Mapped[str | None] = mapped_column(String(50), index=True)
    employment_type: Mapped[str | None] = mapped_column(String(50))
    seniority: Mapped[str | None] = mapped_column(String(50), index=True)
    salary_text: Mapped[str | None] = mapped_column(String(255))
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(10))
    description_raw: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str | None] = mapped_column(Text)
    requirements_structured: Mapped[dict | None] = mapped_column(JSON)
    benefits_structured: Mapped[dict | None] = mapped_column(JSON)
    application_url: Mapped[str | None] = mapped_column(Text)
    ats_vendor: Mapped[str | None] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(
        String(50), default=ApplicationState.DISCOVERED.value, index=True
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    company: Mapped[Company | None] = relationship()


class JobSource(Base):
    __tablename__ = "job_sources"
    __table_args__ = (
        UniqueConstraint("source_type", "source_url", name="uq_job_sources_type_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(100), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    source_external_id: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobScore(Base):
    __tablename__ = "job_scores"
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_profile_id", name="uq_job_scores_job_candidate"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"), index=True)
    overall_score: Mapped[float] = mapped_column(Float)
    score_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    personal_details: Mapped[dict] = mapped_column(JSON, default=dict)
    target_preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    source_profile_data: Mapped[dict] = mapped_column(JSON, default=dict)
    banned_claims: Mapped[list] = mapped_column(JSON, default=list)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CandidateFact(Base):
    __tablename__ = "candidate_facts"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "fact_key", name="uq_candidate_facts_profile_fact_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"), index=True)
    fact_key: Mapped[str] = mapped_column(String(100), index=True)
    category: Mapped[str] = mapped_column(String(100), index=True)
    content: Mapped[str] = mapped_column(Text)
    structured_data: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BrowserProfile(Base):
    __tablename__ = "browser_profiles"
    __table_args__ = (
        UniqueConstraint("profile_key", name="uq_browser_profiles_profile_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int | None] = mapped_column(ForeignKey("candidate_profiles.id"), index=True)
    profile_key: Mapped[str] = mapped_column(String(100), index=True)
    profile_type: Mapped[BrowserProfileType] = mapped_column(Enum(BrowserProfileType), index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(Text)
    session_health: Mapped[str] = mapped_column(String(50), default=SessionHealth.LOGIN_REQUIRED.value, index=True)
    validation_details: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResumeVariant(Base):
    __tablename__ = "resume_variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    source_resume_path: Mapped[str | None] = mapped_column(Text)
    generated_document_id: Mapped[int | None] = mapped_column(ForeignKey("generated_documents.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GeneratedDocument(Base):
    __tablename__ = "generated_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    document_type: Mapped[str] = mapped_column(String(50), index=True)
    truth_tier_max: Mapped[TruthTier | None] = mapped_column(Enum(TruthTier))
    review_status: Mapped[str] = mapped_column(
        String(50), default=ReviewStatus.PENDING.value, index=True
    )
    content_path: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_profile_id", name="uq_applications_job_candidate"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"), index=True)
    current_state: Mapped[str] = mapped_column(
        String(50), default=ApplicationState.DISCOVERED.value, index=True
    )
    last_attempt_id: Mapped[int | None] = mapped_column(ForeignKey("application_attempts.id"))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApplicationAttempt(Base):
    __tablename__ = "application_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    mode: Mapped[ApplicationMode] = mapped_column(Enum(ApplicationMode), index=True)
    browser_profile_key: Mapped[str | None] = mapped_column(String(255), index=True)
    session_health: Mapped[str | None] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    result: Mapped[str | None] = mapped_column(String(50), index=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), index=True)
    submit_confidence: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("application_attempts.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_question_hash: Mapped[str] = mapped_column(String(128), index=True)
    normalized_question_text: Mapped[str] = mapped_column(Text)
    answer_text: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(50), index=True)
    approval_status: Mapped[str] = mapped_column(
        String(50), default=ReviewStatus.PENDING.value, index=True
    )
    truth_tier: Mapped[TruthTier | None] = mapped_column(Enum(TruthTier), index=True)
    extension_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    interview_prep_notes: Mapped[str | None] = mapped_column(Text)
    provenance_facts: Mapped[list] = mapped_column(JSON, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FieldMapping(Base):
    __tablename__ = "field_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("application_attempts.id"), index=True)
    field_key: Mapped[str] = mapped_column(String(100), index=True)
    raw_label: Mapped[str | None] = mapped_column(Text)
    raw_dom_signature: Mapped[str | None] = mapped_column(Text)
    inferred_type: Mapped[str | None] = mapped_column(String(100), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    answer_id: Mapped[int | None] = mapped_column(ForeignKey("answers.id"), index=True)
    truth_tier: Mapped[TruthTier | None] = mapped_column(Enum(TruthTier), index=True)
    chosen_answer: Mapped[str | None] = mapped_column(Text)
    answer_source: Mapped[str | None] = mapped_column(String(100))


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("application_attempts.id"), index=True)
    artifact_type: Mapped[ArtifactType] = mapped_column(Enum(ArtifactType), index=True)
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(128))
    retention_days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(String(50), index=True)
    model_provider: Mapped[str] = mapped_column(String(50), index=True)
    model_name: Mapped[str] = mapped_column(String(100), index=True)
    prompt_version: Mapped[str] = mapped_column(String(100), index=True)
    linked_entity_id: Mapped[int | None] = mapped_column(Integer)
    input_size: Mapped[int | None] = mapped_column(Integer)
    output_size: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ReviewQueueItem(Base):
    __tablename__ = "review_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(50), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    reason: Mapped[str] = mapped_column(String(100), index=True)
    truth_tier: Mapped[TruthTier | None] = mapped_column(Enum(TruthTier), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        String(50), default=ReviewStatus.PENDING.value, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApplicationEligibility(Base):
    __tablename__ = "application_eligibility"
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_profile_id", name="uq_application_eligibility_job_candidate"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"), index=True)
    readiness_state: Mapped[str] = mapped_column(String(50), index=True)
    ready: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    score_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    prepared_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    materialized_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AutoApplyQueueItem(Base):
    __tablename__ = "auto_apply_queue"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "job_id", name="uq_auto_apply_queue_candidate_job"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[AutoApplyQueueStatus] = mapped_column(
        Enum(AutoApplyQueueStatus),
        default=AutoApplyQueueStatus.QUEUED,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_token: Mapped[str | None] = mapped_column(String(100), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    source_attempt_id: Mapped[int | None] = mapped_column(ForeignKey("application_attempts.id"), index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), index=True)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
