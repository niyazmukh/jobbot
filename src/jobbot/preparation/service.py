"""Deterministic preparation service for generated documents and answer packs."""

from __future__ import annotations

import hashlib
import json
import re
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
from jobbot.preparation.llm_cv_writer import build_iterative_llm_resume, llm_provider_ready


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
    resume_generation_method = "deterministic_prepare_v1"
    resume_generation_metadata: dict[str, object] = {}
    llm_writer_enabled = bool(getattr(settings, "llm_cv_writer_enabled", False))
    provider_ready = llm_provider_ready()
    use_llm_cv_writer = llm_writer_enabled and provider_ready
    if use_llm_cv_writer and allow_non_essential_model_call(
        session,
        stage="preparation_cv_writer",
        linked_entity_id=job.id,
        daily_budget_usd=settings.model_call_daily_budget_usd,
        weekly_budget_usd=settings.model_call_weekly_budget_usd,
    ):
        try:
            llm_result = build_iterative_llm_resume(
                session,
                job=job,
                candidate=candidate,
                facts=facts,
                score_json=score.score_json or {},
            )
            resume_text = llm_result.markdown
            resume_generation_method = "iterative_llm_cv_writer_v1"
            resume_generation_metadata = llm_result.metadata
        except Exception as exc:  # pragma: no cover - provider/network dependent
            resume_text = _render_resume_markdown(
                job,
                candidate,
                facts,
                claims,
                score.score_json or {},
            )
            resume_generation_metadata = {
                "llm_writer_fallback_reason": str(exc),
            }
    else:
        resume_text = _render_resume_markdown(
            job,
            candidate,
            facts,
            claims,
            score.score_json or {},
        )
        if llm_writer_enabled and not provider_ready:
            resume_generation_metadata = {
                "llm_writer_fallback_reason": "llm_provider_not_ready",
            }
    resume_path.write_text(resume_text, encoding="utf-8")

    document = GeneratedDocument(
        candidate_profile_id=candidate.id,
        job_id=job.id,
        document_type="resume_markdown",
        truth_tier_max=_truth_tier_max(claims),
        review_status=_review_status_for_claims(claims),
        content_path=str(resume_path),
        metadata_json={
            "generation_method": resume_generation_method,
            "generation_metadata": resume_generation_metadata,
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
    facts: list[CandidateFact],
    claims: list[PreparedClaim],
    score_json: dict,
) -> str:
    """Render a deterministic resume markdown with explicit structure and job alignment."""

    personal_details = candidate.personal_details or {}
    email = str(personal_details.get("email") or "")
    phone = str(personal_details.get("phone") or "")
    linkedin_url = str(personal_details.get("linkedin_url") or "")
    location = str(personal_details.get("location") or "")

    target_company = job.company.name if job.company else "Unknown company"
    requirement_tokens = _job_requirement_tokens(job)
    ranked_facts = _rank_facts_for_job(facts, requirement_tokens)

    summary_points = [
        row.content
        for row in ranked_facts
        if row.category in {"achievement", "experience", "employment"}
    ][:3]
    if not summary_points:
        summary_points = [claim.text for claim in claims][:3]

    experience_rows = [row for row in facts if row.category in {"experience", "employment"}]
    experience_rows = _sort_experience_rows_desc(experience_rows)
    achievement_rows = [row for row in ranked_facts if row.category == "achievement"]
    experience_block = _render_experience_sections(
        experience_rows=experience_rows,
        ranked_achievements=achievement_rows,
    )

    transferable_value_rows = [
        row.content
        for row in ranked_facts
        if row.category in {"achievement", "skill", "skills", "experience", "employment"}
    ][:6]
    transferable_value_block = "\n".join(f"- {row}" for row in transferable_value_rows) or "- Not enough ranked evidence"

    education_rows = [row.content for row in facts if row.category == "education"]
    education_rows = _sort_education_rows_desc(education_rows)
    education_block = "\n".join(f"- {row}" for row in education_rows[:6]) or "- Education details unavailable"

    skill_rows = [row.content for row in facts if row.category in {"skill", "skills"}]
    skill_block = "\n".join(f"- {row}" for row in skill_rows[:12]) or "- Skills details unavailable"

    matched_skills = score_json.get("matched_skills") or []
    missing_skills = score_json.get("missing_skills") or []
    blocking_reasons = score_json.get("blocking_reasons") or []
    seniority_matches = score_json.get("seniority_matches") or []

    requirement_lines: list[str] = []
    required_years = (job.requirements_structured or {}).get("required_years_experience")
    if required_years is not None:
        requirement_lines.append(f"- Required years (parsed): {required_years}")
    if matched_skills:
        requirement_lines.append(f"- Matched skills: {', '.join(matched_skills)}")
    if missing_skills:
        requirement_lines.append(f"- Missing skills to evidence: {', '.join(missing_skills)}")
    if seniority_matches:
        requirement_lines.append(f"- Seniority alignment: {', '.join(seniority_matches)}")
    requirement_lines.extend(_build_requirement_coverage_lines(job, ranked_facts))
    if not requirement_lines:
        requirement_lines.append("- Requirement parser returned limited structured signals; alignment inferred from role text and evidence.")

    gap_lines = [f"- {reason}" for reason in blocking_reasons] or ["- No blocking gap flags from deterministic scorer"]

    summary_block = "\n".join(f"- {text}" for text in summary_points)
    score_line = json.dumps(
        {
            "overall_score": score_json.get("overall_score"),
            "confidence_score": score_json.get("confidence_score"),
            "blocked": score_json.get("blocked"),
            "blocking_reasons": blocking_reasons,
        },
        ensure_ascii=True,
    )

    return (
        f"# {candidate.name}\n\n"
        f"## Contact Information\n"
        f"- Email: {email or 'Not provided'}\n"
        f"- Phone: {phone or 'Not provided'}\n"
        f"- LinkedIn: {linkedin_url or 'Not provided'}\n"
        f"- Location: {location or 'Not provided'}\n\n"
        f"## Target Role\n"
        f"- Role: {job.title}\n"
        f"- Company: {target_company}\n"
        f"- Job URL: {job.canonical_url}\n\n"
        f"## Professional Summary\n{summary_block}\n\n"
        f"## Professional Experience\n{experience_block}\n\n"
        f"## Role-Relevant Value Evidence\n{transferable_value_block}\n\n"
        f"## Education\n{education_block}\n\n"
        f"## Skills\n{skill_block}\n\n"
        f"## Requirements Alignment\n"
        f"{chr(10).join(requirement_lines)}\n\n"
        f"## Gap Notes\n"
        f"{chr(10).join(gap_lines)}\n\n"
        f"## Score Summary\n{score_line}\n"
    )


def _job_requirement_tokens(job: Job) -> set[str]:
    """Build normalized role tokens from job title, structured requirements, and description."""

    raw_parts: list[str] = [job.title or "", job.description_text or ""]
    structured = job.requirements_structured or {}
    for key in (
        "required_skills",
        "preferred_skills",
        "seniority_signals",
        "domain_signals",
        "workplace_signals",
    ):
        value = structured.get(key)
        if isinstance(value, list):
            raw_parts.extend(str(item) for item in value)

    text = " ".join(raw_parts).lower()
    tokens = {token for token in re.findall(r"[a-z0-9]{3,}", text)}
    stopwords = {
        "with",
        "from",
        "that",
        "this",
        "your",
        "their",
        "will",
        "have",
        "years",
        "year",
        "into",
        "about",
        "across",
        "through",
        "using",
    }
    return {token for token in tokens if token not in stopwords}


def _rank_facts_for_job(facts: list[CandidateFact], requirement_tokens: set[str]) -> list[CandidateFact]:
    """Rank candidate facts by keyword overlap against job tokens."""

    if not requirement_tokens:
        return list(facts)

    def score(fact: CandidateFact) -> tuple[int, int]:
        haystack = f"{fact.category} {fact.content}".lower()
        overlap = sum(1 for token in requirement_tokens if token in haystack)
        return overlap, len(fact.content)

    return sorted(facts, key=score, reverse=True)


def _render_experience_sections(
    *,
    experience_rows: list[CandidateFact],
    ranked_achievements: list[CandidateFact],
) -> str:
    """Render experience in best-practice blocks grouped by role/employer with bullets."""

    if not experience_rows:
        return "- Experience details unavailable"

    lines: list[str] = []
    for index, row in enumerate(experience_rows[:6]):
        role, employer, period = _parse_experience_entry(row.content)
        heading = role if not employer else f"{role} — {employer}"
        lines.append(f"### {heading}")
        if period:
            lines.append(f"*{period}*")

        start = index * 2
        mapped = ranked_achievements[start : start + 2]
        if mapped:
            for item in mapped:
                lines.append(f"- {item.content}")
        else:
            lines.append(f"- {row.content}")
        lines.append("")

    return "\n".join(lines).strip()


def _sort_experience_rows_desc(rows: list[CandidateFact]) -> list[CandidateFact]:
    """Sort experience rows from newest to oldest using parsed period years."""

    def key(row: CandidateFact) -> tuple[int, int]:
        _, _, period = _parse_experience_entry(row.content)
        return _period_sort_key(period if period else row.content)

    return sorted(rows, key=key, reverse=True)


def _sort_education_rows_desc(rows: list[str]) -> list[str]:
    """Sort education rows from newest to oldest by detected graduation year."""

    return sorted(rows, key=_education_year_sort_key, reverse=True)


def _period_sort_key(text: str) -> tuple[int, int]:
    """Return (end_year, start_year) for chronological sorting."""

    normalized = text.lower()
    years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", normalized)]
    start_year = years[0] if years else 0
    end_year = years[-1] if years else 0
    if any(token in normalized for token in ("present", "current", "now")):
        end_year = 9999
    return end_year, start_year


def _education_year_sort_key(text: str) -> int:
    """Return the most relevant year in an education entry for sorting."""

    years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", text)]
    if not years:
        return 0
    return max(years)


def _parse_experience_entry(content: str) -> tuple[str, str, str]:
    """Parse 'Role at Employer (Period)' text into structured components."""

    text = " ".join(content.strip().split())
    match = re.match(r"^(?P<role>.+?)\s+at\s+(?P<employer>.+?)\s*\((?P<period>.+)\)$", text, flags=re.IGNORECASE)
    if match:
        return (
            match.group("role").strip(),
            match.group("employer").strip(),
            match.group("period").strip(),
        )

    return text, "", ""


def _build_requirement_coverage_lines(job: Job, ranked_facts: list[CandidateFact]) -> list[str]:
    """Build explicit requirement-coverage notes using ranked evidence snippets."""

    requirements = []
    structured = job.requirements_structured or {}
    required_skills = structured.get("required_skills") if isinstance(structured.get("required_skills"), list) else []
    requirements.extend([str(skill) for skill in required_skills[:4]])

    description = (job.description_text or "").lower()
    if "sales cycle" in description:
        requirements.append("end-to-end sales cycle ownership")
    if "negotiat" in description:
        requirements.append("complex commercial negotiation")
    if "executive" in description:
        requirements.append("executive stakeholder relationship management")

    if not requirements:
        return []

    evidence_rows = [row.content for row in ranked_facts[:8]]
    lines: list[str] = []
    for requirement in requirements[:6]:
        token = requirement.lower()
        matched = next((row for row in evidence_rows if any(part in row.lower() for part in token.split()[:2])), None)
        if matched:
            lines.append(f"- Requirement coverage ({requirement}): {matched}")
        else:
            lines.append(f"- Requirement coverage ({requirement}): partial evidence; discuss transferable examples in interview.")

    return lines


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
