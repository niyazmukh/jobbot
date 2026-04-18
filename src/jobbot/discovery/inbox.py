"""Inbox-style read models for persisted discovered jobs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobbot.db.models import (
    Answer,
    Application,
    ApplicationAttempt,
    ApplicationEvent,
    CandidateProfile,
    Company,
    GeneratedDocument,
    Job,
    JobScore,
    JobSource,
    ResumeVariant,
)


class InboxJobRow(BaseModel):
    """Read model for a deduplicated job inbox row."""

    model_config = ConfigDict(extra="forbid")

    job_id: int
    company_name: str | None = None
    title: str
    location_normalized: str | None = None
    remote_type: str | None = None
    status: str
    ats_vendor: str | None = None
    source_count: int
    canonical_url: str
    last_seen_at: datetime
    score_summary: dict | None = None
    prepared_summary: dict | None = None
    execution_summary: dict | None = None
    application_readiness: dict | None = None


class InboxJobSourceRow(BaseModel):
    """Read model for a single source attached to a job."""

    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_url: str
    source_external_id: str | None = None
    metadata_json: dict
    first_seen_at: datetime
    last_seen_at: datetime


class InboxJobDetail(BaseModel):
    """Read model for a single inbox job and its provenance."""

    model_config = ConfigDict(extra="forbid")

    job_id: int
    company_name: str | None = None
    title: str
    location_raw: str | None = None
    location_normalized: str | None = None
    remote_type: str | None = None
    employment_type: str | None = None
    status: str
    ats_vendor: str | None = None
    canonical_url: str
    application_url: str | None = None
    discovered_at: datetime
    last_seen_at: datetime
    score_summary: dict | None = None
    prepared_summary: dict | None = None
    execution_summary: dict | None = None
    application_readiness: dict | None = None
    sources: list[InboxJobSourceRow]


def list_inbox_jobs(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    candidate_profile_slug: str | None = None,
    status: str | None = None,
    ats_vendor: str | None = None,
    remote_type: str | None = None,
    preparation_state: str | None = None,
    application_readiness: str | None = None,
    execution_state: str | None = None,
    sort_by: str = "last_seen_at",
    descending: bool = True,
) -> list[InboxJobRow]:
    """Return the latest discovered jobs for the local inbox."""

    sort_column = _resolve_sort_column(sort_by)
    order_clause = None if sort_column is None else (sort_column.desc() if descending else sort_column.asc())

    stmt = (
        select(
            Job.id,
            Company.name,
            Job.title,
            Job.location_normalized,
            Job.remote_type,
            Job.status,
            Job.ats_vendor,
            func.count(JobSource.id),
            Job.canonical_url,
            Job.last_seen_at,
        )
        .select_from(Job)
        .outerjoin(Company, Job.company_id == Company.id)
        .outerjoin(JobSource, JobSource.job_id == Job.id)
        .group_by(
            Job.id,
            Company.name,
            Job.title,
            Job.location_normalized,
            Job.remote_type,
            Job.status,
            Job.ats_vendor,
            Job.canonical_url,
            Job.last_seen_at,
        )
    )
    if order_clause is not None:
        stmt = stmt.order_by(order_clause, Job.id.desc())

    if status is not None:
        stmt = stmt.where(Job.status == status)
    if ats_vendor is not None:
        stmt = stmt.where(Job.ats_vendor == ats_vendor)
    if remote_type is not None:
        stmt = stmt.where(Job.remote_type == remote_type)

    rows = session.execute(stmt).all()
    inbox_rows = [
        _build_inbox_job_row(
            session,
            row=row,
            candidate_profile_slug=candidate_profile_slug,
        )
        for row in rows
    ]
    if preparation_state is not None:
        inbox_rows = [
            row for row in inbox_rows if _matches_preparation_state(row.prepared_summary, preparation_state)
        ]
    if application_readiness is not None:
        inbox_rows = [
            row for row in inbox_rows if _matches_application_readiness(row.application_readiness, application_readiness)
        ]
    if execution_state is not None:
        inbox_rows = [
            row for row in inbox_rows if _matches_execution_state(row.execution_summary, execution_state)
        ]
    if sort_by == "preparation_state":
        inbox_rows.sort(
            key=lambda row: (_preparation_sort_rank(row.prepared_summary), row.job_id),
            reverse=descending,
        )
    elif sort_by == "application_readiness":
        inbox_rows.sort(
            key=lambda row: (_application_readiness_sort_rank(row.application_readiness), row.job_id),
            reverse=descending,
        )
    elif sort_by == "execution_state":
        inbox_rows.sort(
            key=lambda row: (_execution_sort_rank(row.execution_summary), row.job_id),
            reverse=descending,
        )
    else:
        inbox_rows = inbox_rows[offset : offset + limit]
        return inbox_rows

    return inbox_rows[offset : offset + limit]


def list_ready_to_apply_jobs(
    session: Session,
    *,
    candidate_profile_slug: str,
    limit: int = 20,
    offset: int = 0,
) -> list[InboxJobRow]:
    """Return jobs that are currently ready to apply for a candidate."""

    return list_inbox_jobs(
        session,
        limit=limit,
        offset=offset,
        candidate_profile_slug=candidate_profile_slug,
        application_readiness="ready_to_apply",
        sort_by="application_readiness",
        descending=True,
    )


def _resolve_sort_column(sort_by: str):
    """Resolve supported inbox sort fields."""

    mapping = {
        "last_seen_at": Job.last_seen_at,
        "discovered_at": Job.discovered_at,
        "title": Job.title,
        "title_normalized": Job.title_normalized,
        "company_name": Company.name,
        "preparation_state": None,
        "application_readiness": None,
        "execution_state": None,
    }
    return mapping.get(sort_by, Job.last_seen_at)


def get_inbox_job_detail(
    session: Session,
    job_id: int,
    candidate_profile_slug: str | None = None,
) -> InboxJobDetail | None:
    """Return a single persisted job with attached source provenance."""

    job = session.execute(
        select(Job, Company.name)
        .outerjoin(Company, Job.company_id == Company.id)
        .where(Job.id == job_id)
    ).first()

    if job is None:
        return None

    job_row, company_name = job
    source_rows = session.scalars(
        select(JobSource).where(JobSource.job_id == job_id).order_by(JobSource.first_seen_at, JobSource.id)
    ).all()
    score_summary = _build_score_summary(session, job_row.id, candidate_profile_slug)
    prepared_summary = _build_prepared_summary(session, job_row.id, candidate_profile_slug)
    execution_summary = _build_execution_summary(session, job_row.id, candidate_profile_slug)

    return InboxJobDetail(
        job_id=job_row.id,
        company_name=company_name,
        title=job_row.title,
        location_raw=job_row.location_raw,
        location_normalized=job_row.location_normalized,
        remote_type=job_row.remote_type,
        employment_type=job_row.employment_type,
        status=job_row.status,
        ats_vendor=job_row.ats_vendor,
        canonical_url=job_row.canonical_url,
        application_url=job_row.application_url,
        discovered_at=job_row.discovered_at,
        last_seen_at=job_row.last_seen_at,
        score_summary=score_summary,
        prepared_summary=prepared_summary,
        execution_summary=execution_summary,
        application_readiness=_build_application_readiness(
            score_summary,
            prepared_summary,
            execution_summary,
        ),
        sources=[
            InboxJobSourceRow(
                source_type=source.source_type,
                source_url=source.source_url,
                source_external_id=source.source_external_id,
                metadata_json=source.metadata_json,
                first_seen_at=source.first_seen_at,
                last_seen_at=source.last_seen_at,
            )
            for source in source_rows
        ],
    )


def get_ready_to_apply_job_detail(
    session: Session,
    *,
    job_id: int,
    candidate_profile_slug: str,
) -> InboxJobDetail | None:
    """Return job detail only when the candidate/job pair is ready to apply."""

    detail = get_inbox_job_detail(
        session,
        job_id,
        candidate_profile_slug=candidate_profile_slug,
    )
    if detail is None:
        return None
    if detail.application_readiness is None:
        return None
    if detail.application_readiness.get("state") != "ready_to_apply":
        return None
    return detail


def _build_inbox_job_row(
    session: Session,
    *,
    row,
    candidate_profile_slug: str | None,
) -> InboxJobRow:
    """Build an inbox row with derived score, preparation, and readiness summaries."""

    score_summary = _build_score_summary(session, row[0], candidate_profile_slug)
    prepared_summary = _build_prepared_summary(session, row[0], candidate_profile_slug)
    execution_summary = _build_execution_summary(session, row[0], candidate_profile_slug)
    return InboxJobRow(
        job_id=row[0],
        company_name=row[1],
        title=row[2],
        location_normalized=row[3],
        remote_type=row[4],
        status=row[5],
        ats_vendor=row[6],
        source_count=row[7],
        canonical_url=row[8],
        last_seen_at=row[9],
        score_summary=score_summary,
        prepared_summary=prepared_summary,
        execution_summary=execution_summary,
        application_readiness=_build_application_readiness(
            score_summary,
            prepared_summary,
            execution_summary,
        ),
    )


def _build_score_summary(
    session: Session,
    job_id: int,
    candidate_profile_slug: str | None,
) -> dict | None:
    """Load a compact score summary for inbox reads when a candidate is specified."""

    if candidate_profile_slug is None:
        return None

    row = session.execute(
        select(JobScore, CandidateProfile.slug)
        .join(CandidateProfile, CandidateProfile.id == JobScore.candidate_profile_id)
        .where(
            JobScore.job_id == job_id,
            CandidateProfile.slug == candidate_profile_slug,
        )
    ).first()
    if row is None:
        return None

    score, slug = row
    payload = score.score_json or {}
    return {
        "candidate_profile_slug": slug,
        "overall_score": score.overall_score,
        "confidence_score": payload.get("confidence_score"),
        "blocked": payload.get("blocked"),
        "blocking_reasons": payload.get("blocking_reasons", []),
    }


def _build_prepared_summary(
    session: Session,
    job_id: int,
    candidate_profile_slug: str | None,
) -> dict | None:
    """Load a compact preparation summary for inbox reads when a candidate is specified."""

    if candidate_profile_slug is None:
        return None

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        return None

    document_rows = session.execute(
        select(GeneratedDocument, ResumeVariant)
        .outerjoin(ResumeVariant, ResumeVariant.generated_document_id == GeneratedDocument.id)
        .where(
            GeneratedDocument.job_id == job_id,
            GeneratedDocument.candidate_profile_id == candidate.id,
        )
        .order_by(GeneratedDocument.created_at, GeneratedDocument.id)
    ).all()
    if not document_rows:
        return None

    answer_source_type = f"deterministic_prepare_v1:job:{job_id}:candidate:{candidate.id}"
    answers = session.scalars(
        select(Answer).where(Answer.source_type == answer_source_type)
    ).all()

    document_statuses = [document.review_status for document, _ in document_rows]
    return {
        "candidate_profile_slug": candidate.slug,
        "document_count": len(document_rows),
        "answer_count": len(answers),
        "resume_variant_id": next((variant.id for _, variant in document_rows if variant is not None), None),
        "document_review_statuses": document_statuses,
        "all_documents_approved": bool(document_statuses) and all(status == "approved" for status in document_statuses),
        "pending_document_review": any(status == "pending" for status in document_statuses),
        "preparation_state": _derive_preparation_state(document_statuses),
    }


def _build_execution_summary(
    session: Session,
    job_id: int,
    candidate_profile_slug: str | None,
) -> dict | None:
    """Load the latest execution attempt summary for inbox reads when a candidate is specified."""

    if candidate_profile_slug is None:
        return None

    row = session.execute(
        select(Application, ApplicationAttempt, CandidateProfile.slug)
        .join(CandidateProfile, CandidateProfile.id == Application.candidate_profile_id)
        .outerjoin(ApplicationAttempt, ApplicationAttempt.id == Application.last_attempt_id)
        .where(
            Application.job_id == job_id,
            CandidateProfile.slug == candidate_profile_slug,
        )
    ).first()
    if row is None:
        return None

    application, attempt, slug = row
    if application is None:
        return None
    failure_classification: str | None = None
    if attempt is not None:
        latest_event = session.execute(
            select(ApplicationEvent)
            .where(ApplicationEvent.attempt_id == attempt.id)
            .order_by(ApplicationEvent.created_at.desc(), ApplicationEvent.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest_event is not None:
            payload = latest_event.payload or {}
            submit_probe = payload.get("submit_probe") or {}
            value = submit_probe.get("failure_classification")
            if isinstance(value, str) and value.strip():
                failure_classification = value.strip()

    return {
        "candidate_profile_slug": slug,
        "application_state": application.current_state,
        "attempt_id": (attempt.id if attempt is not None else None),
        "attempt_mode": (attempt.mode.value if attempt is not None else None),
        "attempt_result": (attempt.result if attempt is not None else None),
        "failure_code": (attempt.failure_code if attempt is not None else None),
        "failure_classification": failure_classification,
        "submit_confidence": (attempt.submit_confidence if attempt is not None else None),
        "notes": (attempt.notes if attempt is not None else None),
    }


def _derive_preparation_state(document_statuses: list[str]) -> str:
    """Reduce document review statuses into one operator-facing preparation state."""

    if not document_statuses:
        return "not_prepared"
    if all(status == "approved" for status in document_statuses):
        return "ready"
    if any(status == "pending" for status in document_statuses):
        return "pending_review"
    if any(status == "rejected" for status in document_statuses):
        return "needs_revision"
    return "prepared"


def _matches_preparation_state(prepared_summary: dict | None, preparation_state: str) -> bool:
    """Return whether a prepared summary matches the requested operator state."""

    normalized = preparation_state.strip().lower()
    if prepared_summary is None:
        return normalized == "not_prepared"
    return prepared_summary.get("preparation_state") == normalized


def _preparation_sort_rank(prepared_summary: dict | None) -> int:
    """Rank preparation state for operator sorting."""

    state = "not_prepared" if prepared_summary is None else prepared_summary.get("preparation_state")
    order = {
        "not_prepared": 0,
        "prepared": 1,
        "pending_review": 2,
        "needs_revision": 3,
        "ready": 4,
    }
    return order.get(state or "not_prepared", 0)


def _build_application_readiness(
    score_summary: dict | None,
    prepared_summary: dict | None,
    execution_summary: dict | None,
) -> dict | None:
    """Reduce score and preparation signals into one execution-facing readiness state."""

    if score_summary is None and prepared_summary is None and execution_summary is None:
        return None

    reasons: list[str] = []
    state = "needs_scoring"
    if score_summary is None:
        reasons.append("score_missing")
    elif score_summary.get("blocked"):
        state = "blocked"
        reasons.extend(list(score_summary.get("blocking_reasons", [])))
    elif prepared_summary is None:
        state = "needs_preparation"
        reasons.append("prepared_outputs_missing")
    else:
        prep_state = prepared_summary.get("preparation_state")
        if prep_state == "ready":
            state = "ready_to_apply"
        elif prep_state == "pending_review":
            state = "pending_review"
            reasons.append("prepared_documents_pending_review")
        elif prep_state == "needs_revision":
            state = "needs_revision"
            reasons.append("prepared_documents_rejected")
        else:
            state = "needs_preparation"
            reasons.append("prepared_outputs_incomplete")

    if execution_summary is not None:
        attempt_result = execution_summary.get("attempt_result")
        failure_code = execution_summary.get("failure_code")
        application_state = execution_summary.get("application_state")
        if attempt_result == "blocked":
            state = "execution_blocked"
            if failure_code:
                reasons.append(failure_code)
        elif application_state == "applied":
            state = "applied"

    return {
        "state": state,
        "ready": state == "ready_to_apply",
        "reasons": reasons,
    }


def _matches_application_readiness(
    application_readiness: dict | None,
    requested_state: str,
) -> bool:
    """Return whether a readiness summary matches the requested operator state."""

    normalized = requested_state.strip().lower()
    if application_readiness is None:
        return normalized == "needs_scoring"
    return application_readiness.get("state") == normalized


def _application_readiness_sort_rank(application_readiness: dict | None) -> int:
    """Rank execution-facing readiness states for operator sorting."""

    state = "needs_scoring" if application_readiness is None else application_readiness.get("state")
    order = {
        "needs_scoring": 0,
        "blocked": 1,
        "needs_preparation": 2,
        "pending_review": 3,
        "needs_revision": 4,
        "ready_to_apply": 5,
        "execution_blocked": 6,
        "applied": 7,
    }
    return order.get(state or "needs_scoring", 0)


def _matches_execution_state(
    execution_summary: dict | None,
    requested_state: str,
) -> bool:
    """Return whether an execution summary matches the requested operator state."""

    normalized = requested_state.strip().lower()
    if execution_summary is None:
        return normalized == "no_attempt"

    attempt_result = str(execution_summary.get("attempt_result") or "").lower()
    application_state = str(execution_summary.get("application_state") or "").lower()
    failure_code = str(execution_summary.get("failure_code") or "").lower()

    if normalized == "blocked":
        return attempt_result == "blocked"
    if normalized == "applied":
        return application_state == "applied"
    if normalized == "pending":
        return attempt_result == ""
    if normalized == "no_attempt":
        return execution_summary.get("attempt_id") is None
    return (
        attempt_result == normalized
        or application_state == normalized
        or failure_code == normalized
    )


def _execution_sort_rank(execution_summary: dict | None) -> int:
    """Rank execution states for operator sorting."""

    if execution_summary is None or execution_summary.get("attempt_id") is None:
        return 0

    attempt_result = execution_summary.get("attempt_result")
    application_state = execution_summary.get("application_state")
    if application_state == "applied":
        return 4
    if attempt_result == "blocked":
        return 3
    if attempt_result == "failed":
        return 2
    if attempt_result == "success":
        return 5
    return 1
