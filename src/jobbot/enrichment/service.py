"""Deterministic enrichment service for discovered jobs."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db.models import Job, JobSource, utcnow
from jobbot.enrichment.schemas import EnrichedRequirements
from jobbot.models.enums import ApplicationState


SKILL_PATTERNS = [
    "python",
    "java",
    "javascript",
    "typescript",
    "sql",
    "aws",
    "gcp",
    "azure",
    "docker",
    "kubernetes",
    "spark",
    "airflow",
    "machine learning",
    "deep learning",
    "llm",
    "graphql",
    "react",
]

EDUCATION_PATTERNS = [
    "bachelor",
    "master",
    "phd",
    "computer science",
    "engineering",
]

SENIORITY_PATTERNS = [
    "senior",
    "staff",
    "principal",
    "lead",
    "manager",
]


def enrich_job(session: Session, job_id: int) -> Job:
    """Deterministically enrich a discovered job with structured requirements."""

    job = session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise ValueError(f"Unknown job id: {job_id}")

    source_rows = list(
        session.scalars(select(JobSource).where(JobSource.job_id == job.id)).all()
    )
    source_text = job.description_text or job.description_raw or ""
    requirements = extract_requirements_from_job(job, source_rows, source_text)
    job.requirements_structured = requirements.model_dump()
    job.status = ApplicationState.ENRICHED.value
    job.last_seen_at = utcnow()
    session.commit()
    session.refresh(job)
    return job


def extract_requirements_from_text(text: str) -> EnrichedRequirements:
    """Extract a baseline requirements structure using deterministic rules."""

    content = text.lower()
    required_skills = [skill for skill in SKILL_PATTERNS if skill in content]
    education_signals = [signal for signal in EDUCATION_PATTERNS if signal in content]
    seniority_signals = [signal for signal in SENIORITY_PATTERNS if signal in content]

    years_matches = re.findall(r"(\d+)\+?\s+years", content)
    years_required = max((int(match) for match in years_matches), default=None)

    preferred_skills = []
    for skill in required_skills:
        if re.search(rf"(preferred|nice to have)[^.\n]*{re.escape(skill)}", content):
            preferred_skills.append(skill)

    required_skills = [skill for skill in required_skills if skill not in preferred_skills]

    return EnrichedRequirements(
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        required_years_experience=years_required,
        seniority_signals=seniority_signals,
        education_signals=education_signals,
    )


def extract_requirements_from_job(
    job: Job,
    source_rows: list[JobSource],
    text: str,
) -> EnrichedRequirements:
    """Merge known source metadata with text fallback into one requirement set."""

    base = extract_requirements_from_text(text)
    domain_signals: list[str] = []
    workplace_signals: list[str] = []
    source_attributes: dict = {}

    if job.employment_type:
        source_attributes["employment_type"] = job.employment_type
    if job.remote_type:
        workplace_signals.append(job.remote_type)
    if job.seniority:
        base.seniority_signals = _merge_unique(base.seniority_signals, [job.seniority.lower()])

    for source in source_rows:
        metadata = source.metadata_json or {}
        if not metadata:
            continue
        _merge_source_attributes(source_attributes, metadata)
        vendor_domain = _extract_domain_signals(job.ats_vendor, metadata)
        vendor_workplace = _extract_workplace_signals(job.ats_vendor, metadata)
        domain_signals = _merge_unique(domain_signals, vendor_domain)
        workplace_signals = _merge_unique(workplace_signals, vendor_workplace)

        employment_candidate = _extract_employment_type(job.ats_vendor, metadata)
        if employment_candidate and "employment_type" not in source_attributes:
            source_attributes["employment_type"] = employment_candidate

    return EnrichedRequirements(
        required_skills=base.required_skills,
        preferred_skills=base.preferred_skills,
        required_years_experience=base.required_years_experience,
        seniority_signals=base.seniority_signals,
        education_signals=base.education_signals,
        domain_signals=domain_signals,
        workplace_signals=workplace_signals,
        source_attributes=source_attributes,
        extraction_method="known_source_then_text_rules",
    )


def _merge_source_attributes(target: dict, metadata: dict) -> None:
    """Persist a compact subset of known-source attributes."""

    for key in (
        "department",
        "team",
        "teams",
        "sub_teams",
        "workplace_type",
        "work_location_option",
        "location_flexibility",
        "commitment",
        "bullet_fields",
    ):
        value = metadata.get(key)
        if value and key not in target:
            target[key] = value


def _extract_domain_signals(ats_vendor: str | None, metadata: dict) -> list[str]:
    """Extract domain/team/department signals from known-source metadata."""

    signals: list[str] = []
    for key in ("department", "team"):
        value = metadata.get(key)
        if value:
            signals.append(str(value).lower())
    for key in ("teams", "sub_teams"):
        raw = metadata.get(key)
        if isinstance(raw, list):
            signals.extend(str(item).lower() for item in raw if item)
    if ats_vendor == "workday":
        for field in metadata.get("bullet_fields") or []:
            label = str(field.get("label", "")).strip().lower()
            if label in {"job family", "job category", "organization"} and field.get("text"):
                signals.append(str(field["text"]).lower())
    return _merge_unique([], signals)


def _extract_workplace_signals(ats_vendor: str | None, metadata: dict) -> list[str]:
    """Extract workplace arrangement signals from known-source metadata."""

    signals: list[str] = []
    for key in ("workplace_type", "work_location_option", "location_flexibility"):
        value = metadata.get(key)
        if value:
            signals.append(str(value).lower())
    if ats_vendor == "workday":
        for field in metadata.get("bullet_fields") or []:
            label = str(field.get("label", "")).strip().lower()
            if label in {"locations", "work shift", "time type"} and field.get("text"):
                signals.append(str(field["text"]).lower())
    return _merge_unique([], signals)


def _extract_employment_type(ats_vendor: str | None, metadata: dict) -> str | None:
    """Extract employment type from known-source metadata."""

    if metadata.get("commitment"):
        return str(metadata["commitment"])
    if ats_vendor == "workday":
        for field in metadata.get("bullet_fields") or []:
            label = str(field.get("label", "")).strip().lower()
            if label == "time type" and field.get("text"):
                return str(field["text"])
    return None


def _merge_unique(existing: list[str], incoming: list[str]) -> list[str]:
    """Merge string lists while preserving order and uniqueness."""

    merged = list(existing)
    seen = {value for value in existing}
    for value in incoming:
        if not value or value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged
