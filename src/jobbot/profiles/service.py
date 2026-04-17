"""Candidate profile import service."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db.models import CandidateFact, CandidateProfile
from jobbot.profiles.schemas import CandidateFactInput, CandidateProfileImport


def slugify(value: str) -> str:
    """Create a filesystem- and URL-friendly slug."""

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "candidate"


def _next_available_slug(session: Session, base_slug: str) -> str:
    """Find a unique slug for the candidate profile table."""

    candidate = base_slug
    suffix = 2
    while session.scalar(select(CandidateProfile.id).where(CandidateProfile.slug == candidate)) is not None:
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def _normalize_fact_key(fact: CandidateFactInput, index: int) -> str:
    """Produce a stable fact key when one is not supplied."""

    if fact.fact_key:
        return fact.fact_key
    base = slugify(fact.category)[:40] or "fact"
    return f"{base}-{index:03d}"


def import_candidate_profile(
    session: Session,
    payload: CandidateProfileImport,
    *,
    replace_existing: bool = False,
) -> CandidateProfile:
    """Insert or replace a candidate profile and its authoritative facts."""

    slug = payload.slug or slugify(payload.name)
    existing = session.scalar(select(CandidateProfile).where(CandidateProfile.slug == slug))

    if existing is not None and not replace_existing:
        raise ValueError(f"Candidate profile slug already exists: {slug}")

    if existing is None:
        slug = _next_available_slug(session, slug)
        profile = CandidateProfile(
            name=payload.name,
            slug=slug,
            personal_details=payload.personal_details,
            target_preferences=payload.target_preferences,
            source_profile_data=payload.source_profile_data,
            banned_claims=payload.banned_claims,
        )
        session.add(profile)
        session.flush()
    else:
        profile = existing
        profile.name = payload.name
        profile.personal_details = payload.personal_details
        profile.target_preferences = payload.target_preferences
        profile.source_profile_data = payload.source_profile_data
        profile.banned_claims = payload.banned_claims
        session.query(CandidateFact).filter(
            CandidateFact.candidate_profile_id == profile.id
        ).delete()
        session.flush()

    for index, fact in enumerate(payload.facts, start=1):
        session.add(
            CandidateFact(
                candidate_profile_id=profile.id,
                fact_key=_normalize_fact_key(fact, index),
                category=fact.category,
                content=fact.content,
                structured_data=fact.structured_data,
                confidence=fact.confidence,
            )
        )

    session.commit()
    session.refresh(profile)
    return profile
