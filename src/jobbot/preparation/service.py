"""Deterministic preparation service for generated documents and answer packs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.config import get_settings
from jobbot.db.models import (
    Answer,
    CandidateFact,
    CandidateProfile,
    GeneratedDocument,
    Job,
    JobScore,
    ResumeVariant,
    ReviewQueueItem,
    utcnow,
)
from jobbot.models.enums import ApplicationState, ReviewStatus, TruthTier
from jobbot.model_calls import allow_non_essential_model_call
from jobbot.preparation.schemas import PreparedAnswerPlan, PreparedClaim, PreparedJobSummary


def prepare_job_for_candidate(
    session: Session,
    *,
    job_id: int,
    candidate_profile_slug: str,
    output_dir: Path | None = None,
) -> PreparedJobSummary:
    """Create deterministic preparation records for a candidate/job pair."""

    job = session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise ValueError(f"Unknown job id: {job_id}")

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError(f"Unknown candidate profile slug: {candidate_profile_slug}")

    score = session.scalar(
        select(JobScore).where(
            JobScore.job_id == job.id,
            JobScore.candidate_profile_id == candidate.id,
        )
    )
    if score is None:
        raise ValueError("job_score_not_found")

    facts = list(
        session.scalars(
            select(CandidateFact)
            .where(CandidateFact.candidate_profile_id == candidate.id)
            .order_by(CandidateFact.id)
        ).all()
    )
    claims = _build_resume_claims(job, score.score_json or {}, facts)
    settings = get_settings()
    extension_calls_allowed = True
    if bool((candidate.target_preferences or {}).get("enable_tier3_extensions")):
        extension_calls_allowed = allow_non_essential_model_call(
            session,
            stage="preparation_extension_answer",
            linked_entity_id=job.id,
            daily_budget_usd=settings.model_call_daily_budget_usd,
            weekly_budget_usd=settings.model_call_weekly_budget_usd,
        )
    answers = _build_answer_pack(
        job,
        candidate,
        score.score_json or {},
        facts,
        extension_calls_allowed=extension_calls_allowed,
    )

    base_dir = output_dir or (get_settings().artifacts_dir / "generated" / candidate.slug / str(job.id))
    base_dir.mkdir(parents=True, exist_ok=True)

    resume_path = base_dir / "resume_variant_v1.md"
    resume_text = _render_resume_markdown(job, candidate, claims, score.score_json or {})
    resume_path.write_text(resume_text, encoding="utf-8")

    document = GeneratedDocument(
        candidate_profile_id=candidate.id,
        job_id=job.id,
        document_type="resume_markdown",
        truth_tier_max=_truth_tier_max(claims),
        review_status=_review_status_for_claims(claims),
        content_path=str(resume_path),
        metadata_json={
            "generation_method": "deterministic_prepare_v1",
            "claims": [claim.model_dump() for claim in claims],
            "score_summary": {
                "overall_score": score.overall_score,
                "confidence_score": (score.score_json or {}).get("confidence_score"),
                "blocked": (score.score_json or {}).get("blocked"),
                "blocking_reasons": (score.score_json or {}).get("blocking_reasons", []),
            },
        },
    )
    session.add(document)
    session.flush()

    resume_variant = ResumeVariant(
        candidate_profile_id=candidate.id,
        job_id=job.id,
        name=f"{candidate.name} | {job.title}",
        source_resume_path=_extract_source_resume_path(candidate),
        generated_document_id=document.id,
    )
    session.add(resume_variant)
    session.flush()

    answer_ids: list[int] = []
    queued_review_ids: list[int] = []
    for plan in answers:
        answer = _upsert_answer(
            session,
            plan,
            source_type=_answer_source_type(job.id, candidate.id),
        )
        answer_ids.append(answer.id)
        if plan.truth_tier == TruthTier.INFERENCE.value:
            queued = _queue_review_item(
                session,
                entity_type="answer",
                entity_id=answer.id,
                reason="tier2_first_use_answer_review",
                truth_tier=TruthTier.INFERENCE,
                confidence=0.75,
            )
            queued_review_ids.append(queued.id)
        elif plan.truth_tier == TruthTier.EXTENSION.value:
            queued = _queue_review_item(
                session,
                entity_type="answer",
                entity_id=answer.id,
                reason="tier3_first_use_answer_review",
                truth_tier=TruthTier.EXTENSION,
                confidence=0.55,
            )
            queued_review_ids.append(queued.id)

    if any(claim.truth_tier == TruthTier.INFERENCE.value for claim in claims):
        queued = _queue_review_item(
            session,
            entity_type="generated_document",
            entity_id=document.id,
            reason="tier2_first_use_document_review",
            truth_tier=TruthTier.INFERENCE,
            confidence=(score.score_json or {}).get("confidence_score"),
        )
        queued_review_ids.append(queued.id)

    job.status = ApplicationState.PREPARED.value
    job.last_seen_at = utcnow()
    session.commit()
    session.refresh(document)
    session.refresh(resume_variant)

    return PreparedJobSummary(
        job_id=job.id,
        candidate_profile_slug=candidate.slug,
        resume_variant_id=resume_variant.id,
        generated_document_ids=[document.id],
        answer_ids=answer_ids,
        queued_review_ids=queued_review_ids,
    )


def _build_resume_claims(
    job: Job,
    score_json: dict,
    facts: list[CandidateFact],
) -> list[PreparedClaim]:
    """Create a compact deterministic claim set for a resume variant."""

    matched_skills = {skill.lower() for skill in score_json.get("matched_skills", [])}
    prioritized_facts = []
    fallback_facts = []
    for fact in facts:
        haystack = f"{fact.category} {fact.content}".lower()
        if any(skill in haystack for skill in matched_skills):
            prioritized_facts.append(fact)
        else:
            fallback_facts.append(fact)

    chosen = (prioritized_facts + fallback_facts)[:3]
    claims = [
        PreparedClaim(
            text=fact.content,
            truth_tier=TruthTier.OBSERVED.value,
            provenance_facts=[fact.fact_key],
        )
        for fact in chosen
    ]

    if matched_skills:
        inferred_text = (
            f"Aligned with {job.title} requirements through evidence of "
            f"{', '.join(sorted(matched_skills))}."
        )
        inference_fact_keys = [fact.fact_key for fact in chosen[:2]]
        claims.append(
            PreparedClaim(
                text=inferred_text,
                truth_tier=TruthTier.INFERENCE.value,
                provenance_facts=inference_fact_keys,
            )
        )

    return claims


def _build_answer_pack(
    job: Job,
    candidate: CandidateProfile,
    score_json: dict,
    facts: list[CandidateFact],
    *,
    extension_calls_allowed: bool = True,
) -> list[PreparedAnswerPlan]:
    """Create a deterministic baseline answer pack."""

    top_fact_keys = [fact.fact_key for fact in facts[:2]]
    top_fact_text = " ".join(fact.content for fact in facts[:2]).strip()
    matched_skills = score_json.get("matched_skills", [])
    blocking_reasons = score_json.get("blocking_reasons", [])

    why_role = PreparedAnswerPlan(
        question="Why are you interested in this role?",
        answer_text=(
            f"I am interested in this {job.title} opportunity because it aligns with my background in "
            f"{', '.join(matched_skills) if matched_skills else 'relevant technical work'} and fits the direction "
            f"of work I am targeting."
        ),
        truth_tier=TruthTier.INFERENCE.value,
        provenance_facts=top_fact_keys,
        interview_prep_notes="Keep the explanation tied to concrete past work and avoid unsupported company-specific claims.",
    )
    relevant_skills = PreparedAnswerPlan(
        question="What relevant skills do you bring to this role?",
        answer_text=top_fact_text or candidate.name,
        truth_tier=TruthTier.OBSERVED.value,
        provenance_facts=top_fact_keys,
        interview_prep_notes="Use exact past examples from the linked candidate facts.",
    )

    plans = [why_role, relevant_skills]
    if blocking_reasons:
        plans.append(
            PreparedAnswerPlan(
                question="What should you clarify before applying?",
                answer_text=(
                    "Before applying, I would review the fit gaps currently flagged: "
                    + ", ".join(blocking_reasons)
                    + "."
                ),
                truth_tier=TruthTier.INFERENCE.value,
                provenance_facts=top_fact_keys,
                interview_prep_notes="Treat this as internal prep guidance rather than application copy.",
            )
        )
    if bool((candidate.target_preferences or {}).get("enable_tier3_extensions")) and extension_calls_allowed:
        plans.append(
            PreparedAnswerPlan(
                question="What impact plan would you propose for the first 90 days?",
                answer_text=(
                    "Based on the role scope and my prior work, I would start with discovery, "
                    "identify key delivery risks, and propose a phased execution plan with measurable milestones."
                ),
                truth_tier=TruthTier.EXTENSION.value,
                provenance_facts=top_fact_keys,
                interview_prep_notes=(
                    "Treat this as a hypothesis to discuss, not a factual claim about internal company priorities."
                ),
            )
        )

    return plans


def _render_resume_markdown(
    job: Job,
    candidate: CandidateProfile,
    claims: list[PreparedClaim],
    score_json: dict,
) -> str:
    """Render a deterministic markdown resume variant."""

    summary = "\n".join(f"- {claim.text} [{claim.truth_tier}]" for claim in claims)
    score_line = json.dumps(
        {
            "overall_score": score_json.get("overall_score"),
            "confidence_score": score_json.get("confidence_score"),
            "blocked": score_json.get("blocked"),
        },
        ensure_ascii=True,
    )
    return (
        f"# Resume Variant\n\n"
        f"Candidate: {candidate.name}\n"
        f"Target role: {job.title}\n"
        f"Company: {job.company.name if job.company else 'Unknown company'}\n\n"
        f"## Selected Evidence\n{summary}\n\n"
        f"## Score Summary\n{score_line}\n"
    )


def _truth_tier_max(claims: list[PreparedClaim]) -> TruthTier:
    """Return the highest truth tier found in the claim set."""

    if any(claim.truth_tier == TruthTier.EXTENSION.value for claim in claims):
        return TruthTier.EXTENSION
    if any(claim.truth_tier == TruthTier.INFERENCE.value for claim in claims):
        return TruthTier.INFERENCE
    return TruthTier.OBSERVED


def _review_status_for_claims(claims: list[PreparedClaim]) -> str:
    """Choose the initial review status for a generated document."""

    if any(claim.truth_tier == TruthTier.EXTENSION.value for claim in claims):
        return ReviewStatus.PENDING.value
    if any(claim.truth_tier == TruthTier.INFERENCE.value for claim in claims):
        return ReviewStatus.PENDING.value
    return ReviewStatus.APPROVED.value


def _extract_source_resume_path(candidate: CandidateProfile) -> str | None:
    """Extract a likely source resume path from profile source data."""

    source_data = candidate.source_profile_data or {}
    for key in ("resume_path", "source_resume_path", "resume_file"):
        value = source_data.get(key)
        if value:
            return str(value)
    return None


def _question_hash(question: str) -> str:
    """Create a stable hash for a normalized question."""

    normalized = " ".join(question.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _answer_source_type(job_id: int, candidate_id: int) -> str:
    """Build a stable answer source key for a candidate/job preparation set."""

    return f"deterministic_prepare_v1:job:{job_id}:candidate:{candidate_id}"


def _upsert_answer(session: Session, plan: PreparedAnswerPlan, *, source_type: str) -> Answer:
    """Reuse an existing identical answer when possible, otherwise insert a new one."""

    needs_review = plan.truth_tier in {
        TruthTier.INFERENCE.value,
        TruthTier.EXTENSION.value,
    }
    question_hash = _question_hash(plan.question)
    answer = session.scalar(
        select(Answer).where(
            Answer.canonical_question_hash == question_hash,
            Answer.answer_text == plan.answer_text,
            Answer.source_type == source_type,
        )
    )
    if answer is None:
        answer = Answer(
            canonical_question_hash=question_hash,
            normalized_question_text=plan.question,
            answer_text=plan.answer_text,
            source_type=source_type,
            approval_status=(
                ReviewStatus.PENDING.value
                if needs_review
                else ReviewStatus.APPROVED.value
            ),
            truth_tier=TruthTier(plan.truth_tier),
            extension_approved=False,
            interview_prep_notes=plan.interview_prep_notes,
            provenance_facts=plan.provenance_facts,
        )
        session.add(answer)
        session.flush()
        return answer

    answer.interview_prep_notes = plan.interview_prep_notes
    answer.provenance_facts = plan.provenance_facts
    answer.last_used_at = utcnow()
    session.flush()
    return answer


def _queue_review_item(
    session: Session,
    *,
    entity_type: str,
    entity_id: int,
    reason: str,
    truth_tier: TruthTier,
    confidence: float | None,
) -> ReviewQueueItem:
    """Create or refresh a manual review queue item."""

    item = session.scalar(
        select(ReviewQueueItem).where(
            ReviewQueueItem.entity_type == entity_type,
            ReviewQueueItem.entity_id == entity_id,
        )
    )
    if item is None:
        item = ReviewQueueItem(
            entity_type=entity_type,
            entity_id=entity_id,
            reason=reason,
            truth_tier=truth_tier,
            confidence=confidence,
            status=ReviewStatus.PENDING.value,
        )
        session.add(item)
        session.flush()
        return item

    item.reason = reason
    item.truth_tier = truth_tier
    item.confidence = confidence
    item.updated_at = utcnow()
    if item.status not in {ReviewStatus.APPROVED.value, ReviewStatus.REJECTED.value}:
        item.status = ReviewStatus.PENDING.value
    session.flush()
    return item
