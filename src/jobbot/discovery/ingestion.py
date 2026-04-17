"""Persistence bridge from discovery contracts into the database."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from jobbot.db.models import Company, Job, JobSource
from jobbot.discovery.contracts import CanonicalJob, DiscoveryBatch
from jobbot.discovery.normalization import (
    canonicalize_job_url,
    normalize_company_name,
    normalize_job_title,
    normalize_location,
)


@dataclass(slots=True)
class JobIngestionCounters:
    inserted: int = 0
    updated: int = 0
    duplicate: int = 0
    source_attached: int = 0


def ingest_discovery_batch(session: Session, batch: DiscoveryBatch) -> JobIngestionCounters:
    """Persist a discovery batch into jobs and job_sources."""

    counters = JobIngestionCounters()

    for item in batch.jobs:
        outcome = ingest_canonical_job(session, item)
        if outcome == "inserted":
            counters.inserted += 1
        elif outcome == "updated":
            counters.updated += 1
        elif outcome == "duplicate":
            counters.duplicate += 1
        elif outcome == "source_attached":
            counters.source_attached += 1

    session.commit()
    return counters


def ingest_canonical_job(session: Session, item: CanonicalJob) -> str:
    """Insert or attach a discovered job using layered deduplication."""

    canonical_url = canonicalize_job_url(str(item.canonical_url))
    application_url = (
        canonicalize_job_url(str(item.application_url))
        if item.application_url is not None
        else None
    )

    company = _resolve_company(session, item.company_name, item.company_domain)

    job = session.scalar(select(Job).where(Job.canonical_url == canonical_url))
    if job is not None:
        _attach_source(session, job.id, item)
        _refresh_job_fields(job, item, company.id, canonical_url, application_url)
        return "duplicate"

    if item.external_job_id:
        job = session.scalar(
            select(Job).where(
                and_(
                    Job.source == item.source.value,
                    Job.external_job_id == item.external_job_id,
                )
            )
        )
        if job is not None:
            _attach_source(session, job.id, item)
            _refresh_job_fields(job, item, company.id, canonical_url, application_url)
            return "source_attached"

    fingerprint = _fingerprint_values(item.company_name, item.title, item.location_normalized)
    job = _find_by_fingerprint(session, *fingerprint)
    if job is not None:
        _attach_source(session, job.id, item)
        _refresh_job_fields(job, item, company.id, canonical_url, application_url)
        return "source_attached"

    job = Job(
        company_id=company.id,
        source=item.source.value,
        source_type=item.source_type,
        external_job_id=item.external_job_id,
        canonical_url=canonical_url,
        title=item.title,
        title_normalized=normalize_job_title(item.title),
        location_raw=item.location_raw,
        location_normalized=item.location_normalized,
        remote_type=item.remote_type,
        employment_type=item.employment_type,
        seniority=item.seniority,
        salary_text=item.salary_text,
        application_url=application_url,
        ats_vendor=item.ats_vendor,
        discovered_at=item.discovered_at,
        last_seen_at=item.discovered_at,
    )
    session.add(job)
    session.flush()
    _attach_source(session, job.id, item)
    return "inserted"


def _resolve_company(session: Session, company_name: str, company_domain: str | None) -> Company:
    normalized_name = normalize_company_name(company_name)
    company = None

    if company_domain:
        company = session.scalar(select(Company).where(Company.domain == company_domain.lower()))

    if company is None:
        company = session.scalar(select(Company).where(Company.name == normalized_name))

    if company is None:
        company = Company(name=normalized_name, domain=company_domain.lower() if company_domain else None)
        session.add(company)
        session.flush()

    return company


def _fingerprint_values(
    company_name: str,
    title: str,
    location_normalized: str | None,
) -> tuple[str, str, str | None]:
    return (
        normalize_company_name(company_name),
        normalize_job_title(title),
        normalize_location(location_normalized),
    )


def _find_by_fingerprint(
    session: Session,
    normalized_company_name: str,
    normalized_title: str,
    normalized_location: str | None,
) -> Job | None:
    query = (
        select(Job)
        .join(Company, Job.company_id == Company.id)
        .where(
            and_(
                Company.name == normalized_company_name,
                Job.title_normalized == normalized_title,
                or_(
                    Job.location_normalized == normalized_location,
                    and_(Job.location_normalized.is_(None), normalized_location is None),
                ),
            )
        )
    )
    return session.scalar(query)


def _attach_source(session: Session, job_id: int, item: CanonicalJob) -> None:
    source_url = canonicalize_job_url(str(item.canonical_url))
    source_row = session.scalar(
        select(JobSource).where(
            and_(
                JobSource.source_type == item.source_type,
                JobSource.source_url == source_url,
            )
        )
    )
    if source_row is None:
        session.add(
            JobSource(
                job_id=job_id,
                source_type=item.source_type,
                source_url=source_url,
                source_external_id=item.external_job_id,
                metadata_json=dict(item.metadata),
                first_seen_at=item.discovered_at,
                last_seen_at=item.discovered_at,
            )
        )
    else:
        source_row.job_id = job_id
        source_row.source_external_id = item.external_job_id or source_row.source_external_id
        source_row.metadata_json = dict(item.metadata)
        source_row.last_seen_at = item.discovered_at


def _refresh_job_fields(
    job: Job,
    item: CanonicalJob,
    company_id: int,
    canonical_url: str,
    application_url: str | None,
) -> None:
    job.company_id = company_id
    job.source = item.source.value or job.source
    job.source_type = item.source_type or job.source_type
    job.external_job_id = item.external_job_id or job.external_job_id
    job.canonical_url = canonical_url
    job.title = _prefer_text(job.title, item.title)
    job.title_normalized = _prefer_text(job.title_normalized, normalize_job_title(item.title))
    job.location_raw = _prefer_text(job.location_raw, item.location_raw)
    job.location_normalized = _prefer_text(job.location_normalized, item.location_normalized)
    job.remote_type = _prefer_text(job.remote_type, item.remote_type)
    job.employment_type = _prefer_text(job.employment_type, item.employment_type)
    job.seniority = _prefer_text(job.seniority, item.seniority)
    job.salary_text = _prefer_text(job.salary_text, item.salary_text)
    job.application_url = application_url or job.application_url
    job.ats_vendor = item.ats_vendor or job.ats_vendor
    job.last_seen_at = item.discovered_at


def _prefer_text(existing: str | None, new_value: str | None) -> str | None:
    """Prefer non-empty new text, otherwise preserve the existing value."""

    if new_value is None:
        return existing
    candidate = new_value.strip()
    if not candidate:
        return existing
    return candidate
