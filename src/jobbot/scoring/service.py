"""Deterministic scoring service built on enriched requirements."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db.models import CandidateFact, CandidateProfile, Job, JobScore, utcnow
from jobbot.models.enums import ApplicationState
from jobbot.scoring.schemas import JobScoreRead, JobScoreResult


def score_job_for_candidate(session: Session, job_id: int, candidate_profile_slug: str) -> JobScore:
    """Compute and persist a deterministic fit score for a candidate/job pair."""

    job = session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise ValueError(f"Unknown job id: {job_id}")

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError(f"Unknown candidate profile slug: {candidate_profile_slug}")

    facts = list(
        session.scalars(
            select(CandidateFact).where(CandidateFact.candidate_profile_id == candidate.id)
        ).all()
    )
    result = _score(job, candidate, facts)

    score_row = session.scalar(
        select(JobScore).where(
            JobScore.job_id == job.id,
            JobScore.candidate_profile_id == candidate.id,
        )
    )
    if score_row is None:
        score_row = JobScore(
            job_id=job.id,
            candidate_profile_id=candidate.id,
            overall_score=result.overall_score,
            score_json=result.model_dump(),
        )
        session.add(score_row)
    else:
        score_row.overall_score = result.overall_score
        score_row.score_json = result.model_dump()
        score_row.updated_at = utcnow()

    job.status = ApplicationState.SCORED.value
    session.commit()
    session.refresh(score_row)
    return score_row


def get_job_score_for_candidate(
    session: Session,
    job_id: int,
    candidate_profile_slug: str,
) -> JobScoreRead | None:
    """Return a persisted score for a candidate/job pair if it exists."""

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
    return JobScoreRead(
        job_id=score.job_id,
        candidate_profile_slug=slug,
        overall_score=score.overall_score,
        score_json=score.score_json,
    )


def _score(job: Job, candidate: CandidateProfile, facts: list[CandidateFact]) -> JobScoreResult:
    requirements = job.requirements_structured or {}
    required_skills = list(requirements.get("required_skills") or [])
    seniority_signals = list(requirements.get("seniority_signals") or [])
    years_required = requirements.get("required_years_experience")

    candidate_text = " ".join(
        [
            candidate.name,
            *(fact.content for fact in facts),
            *[str(value) for value in candidate.target_preferences.values()],
        ]
    ).lower()

    matched_skills = [skill for skill in required_skills if skill.lower() in candidate_text]
    missing_skills = [skill for skill in required_skills if skill not in matched_skills]
    skill_score = 1.0 if not required_skills else len(matched_skills) / max(len(required_skills), 1)

    location_prefs = _extract_location_preferences(candidate)
    location_raw = " ".join(filter(None, [job.location_raw, job.location_normalized])).lower()
    matched_location_preferences = [
        pref for pref in location_prefs if pref.lower() in location_raw
    ]
    location_score = 1.0 if not location_prefs else (1.0 if matched_location_preferences else 0.25)

    seniority_matches = [signal for signal in seniority_signals if signal.lower() in candidate_text]
    seniority_score = 1.0 if not seniority_signals else len(seniority_matches) / max(len(seniority_signals), 1)

    blocking_reasons = []
    if required_skills and not matched_skills:
        blocking_reasons.append("no_required_skills_matched")
    elif required_skills and len(matched_skills) / max(len(required_skills), 1) < 0.34:
        blocking_reasons.append("insufficient_required_skill_match")
    if location_prefs and not matched_location_preferences and "remote" not in location_raw:
        blocking_reasons.append("location_preference_mismatch")
    if seniority_signals and not seniority_matches:
        blocking_reasons.append("seniority_signal_mismatch")
    if years_required is not None and not _candidate_mentions_years_experience(candidate_text, years_required):
        blocking_reasons.append("experience_years_unverified")

    blocked = len(blocking_reasons) > 0
    confidence_score = _compute_confidence_score(
        requirements=requirements,
        required_skills=required_skills,
        matched_skills=matched_skills,
        blocking_reasons=blocking_reasons,
    )

    overall_score = round((skill_score * 0.6) + (location_score * 0.25) + (seniority_score * 0.15), 4)

    explanations = [
        f"Matched {len(matched_skills)} of {len(required_skills)} required skills."
        if required_skills
        else "No explicit required skills were extracted.",
        "Location aligns with candidate preferences."
        if matched_location_preferences or not location_prefs
        else "Location does not clearly match candidate preferences.",
        "Seniority signals align with candidate profile."
        if seniority_matches or not seniority_signals
        else "Seniority signals are only partially supported by the candidate profile.",
    ]
    if blocked:
        explanations.append(f"Blocking reasons: {', '.join(blocking_reasons)}.")
    explanations.append(f"Confidence score: {confidence_score}.")

    return JobScoreResult(
        overall_score=overall_score,
        skill_score=round(skill_score, 4),
        location_score=round(location_score, 4),
        seniority_score=round(seniority_score, 4),
        matched_skills=matched_skills,
        missing_skills=missing_skills,
        matched_location_preferences=matched_location_preferences,
        seniority_matches=seniority_matches,
        confidence_score=confidence_score,
        blocked=blocked,
        blocking_reasons=blocking_reasons,
        explanations=explanations,
    )


def _extract_location_preferences(candidate: CandidateProfile) -> list[str]:
    """Extract simple location preferences from the candidate target preferences."""

    prefs = candidate.target_preferences or {}
    values = []
    for key in ("locations", "location_preferences", "preferred_locations"):
        raw = prefs.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if item)
        elif raw:
            values.append(str(raw))
    remote_pref = prefs.get("remote")
    if remote_pref is True:
        values.append("remote")
    return values


def _candidate_mentions_years_experience(candidate_text: str, years_required: int) -> bool:
    """Check whether the candidate profile text supports the required years threshold."""

    for years in range(years_required, years_required + 10):
        if f"{years} years" in candidate_text or f"{years}+ years" in candidate_text:
            return True
    return False


def _compute_confidence_score(
    *,
    requirements: dict,
    required_skills: list[str],
    matched_skills: list[str],
    blocking_reasons: list[str],
) -> float:
    """Compute a simple confidence score for the deterministic scoring pass."""

    confidence = 0.55
    if requirements:
        confidence += 0.15
    if required_skills:
        confidence += min(len(matched_skills) / max(len(required_skills), 1), 1.0) * 0.2
    if not blocking_reasons:
        confidence += 0.1
    else:
        confidence -= min(len(blocking_reasons) * 0.1, 0.3)
    return round(max(0.0, min(confidence, 1.0)), 4)
