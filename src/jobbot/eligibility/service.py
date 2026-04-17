"""Persisted execution-eligibility materialization services."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db.models import ApplicationEligibility, CandidateProfile, utcnow
from jobbot.discovery.inbox import get_inbox_job_detail
from jobbot.eligibility.schemas import ApplicationEligibilityRead


def materialize_application_eligibility(
    session: Session,
    *,
    job_id: int,
    candidate_profile_slug: str,
) -> ApplicationEligibilityRead:
    """Persist the current candidate/job readiness state into a DB-backed snapshot."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    detail = get_inbox_job_detail(
        session,
        job_id,
        candidate_profile_slug=candidate_profile_slug,
    )
    if detail is None:
        raise ValueError("job_not_found")

    readiness = detail.application_readiness or {
        "state": "needs_scoring",
        "ready": False,
        "reasons": ["readiness_not_computed"],
    }
    now = utcnow()
    row = session.scalar(
        select(ApplicationEligibility).where(
            ApplicationEligibility.job_id == job_id,
            ApplicationEligibility.candidate_profile_id == candidate.id,
        )
    )
    if row is None:
        row = ApplicationEligibility(
            job_id=job_id,
            candidate_profile_id=candidate.id,
            readiness_state=str(readiness.get("state")),
            ready=bool(readiness.get("ready")),
            reasons=list(readiness.get("reasons") or []),
            score_summary=detail.score_summary or {},
            prepared_summary=detail.prepared_summary or {},
            materialized_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.readiness_state = str(readiness.get("state"))
        row.ready = bool(readiness.get("ready"))
        row.reasons = list(readiness.get("reasons") or [])
        row.score_summary = detail.score_summary or {}
        row.prepared_summary = detail.prepared_summary or {}
        row.materialized_at = now
        row.updated_at = now

    session.commit()
    session.refresh(row)
    return _to_read(row, candidate.slug)


def list_application_eligibility(
    session: Session,
    *,
    candidate_profile_slug: str,
    ready_only: bool = False,
    limit: int = 50,
) -> list[ApplicationEligibilityRead]:
    """Return persisted eligibility snapshots for one candidate."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    stmt = (
        select(ApplicationEligibility)
        .where(ApplicationEligibility.candidate_profile_id == candidate.id)
        .order_by(ApplicationEligibility.ready.desc(), ApplicationEligibility.updated_at.desc())
        .limit(limit)
    )
    if ready_only:
        stmt = stmt.where(ApplicationEligibility.ready.is_(True))

    rows = session.scalars(stmt).all()
    return [_to_read(row, candidate.slug) for row in rows]


def get_application_eligibility(
    session: Session,
    *,
    job_id: int,
    candidate_profile_slug: str,
) -> ApplicationEligibilityRead | None:
    """Return one persisted eligibility snapshot if present."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        return None

    row = session.scalar(
        select(ApplicationEligibility).where(
            ApplicationEligibility.job_id == job_id,
            ApplicationEligibility.candidate_profile_id == candidate.id,
        )
    )
    if row is None:
        return None
    return _to_read(row, candidate.slug)


def _to_read(row: ApplicationEligibility, candidate_profile_slug: str) -> ApplicationEligibilityRead:
    """Convert ORM eligibility row into a read model."""

    return ApplicationEligibilityRead(
        job_id=row.job_id,
        candidate_profile_slug=candidate_profile_slug,
        readiness_state=row.readiness_state,
        ready=row.ready,
        reasons=list(row.reasons or []),
        score_summary=row.score_summary or {},
        prepared_summary=row.prepared_summary or {},
        materialized_at=row.materialized_at,
        updated_at=row.updated_at,
    )
