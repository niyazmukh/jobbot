"""Read models for prepared documents and answer packs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db.models import Answer, CandidateProfile, GeneratedDocument, Job, ResumeVariant


class PreparedDocumentRead(BaseModel):
    """Read model for a generated document tied to a candidate/job pair."""

    model_config = ConfigDict(extra="forbid")

    generated_document_id: int
    resume_variant_id: int | None = None
    document_type: str
    truth_tier_max: str | None = None
    review_status: str
    content_path: str | None = None
    metadata_json: dict
    created_at: datetime


class PreparedAnswerRead(BaseModel):
    """Read model for a reusable prepared answer."""

    model_config = ConfigDict(extra="forbid")

    answer_id: int
    normalized_question_text: str
    answer_text: str
    approval_status: str
    truth_tier: str | None = None
    provenance_facts: list
    interview_prep_notes: str | None = None
    created_at: datetime


class PreparedJobRead(BaseModel):
    """Read model for all preparation outputs for a candidate/job pair."""

    model_config = ConfigDict(extra="forbid")

    job_id: int
    candidate_profile_slug: str
    job_title: str
    documents: list[PreparedDocumentRead]
    answers: list[PreparedAnswerRead]


def get_prepared_job_read(
    session: Session,
    *,
    job_id: int,
    candidate_profile_slug: str,
) -> PreparedJobRead | None:
    """Return persisted preparation outputs for a candidate/job pair."""

    job = session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
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
            GeneratedDocument.job_id == job.id,
            GeneratedDocument.candidate_profile_id == candidate.id,
        )
        .order_by(GeneratedDocument.created_at, GeneratedDocument.id)
    ).all()
    answer_rows = session.scalars(
        select(Answer)
        .where(Answer.source_type == f"deterministic_prepare_v1:job:{job.id}:candidate:{candidate.id}")
        .order_by(Answer.created_at, Answer.id)
    ).all()

    documents = [
        PreparedDocumentRead(
            generated_document_id=document.id,
            resume_variant_id=(variant.id if variant else None),
            document_type=document.document_type,
            truth_tier_max=(document.truth_tier_max.value if document.truth_tier_max else None),
            review_status=document.review_status,
            content_path=document.content_path,
            metadata_json=document.metadata_json,
            created_at=document.created_at,
        )
        for document, variant in document_rows
    ]
    answers = [
        PreparedAnswerRead(
            answer_id=answer.id,
            normalized_question_text=answer.normalized_question_text,
            answer_text=answer.answer_text,
            approval_status=answer.approval_status,
            truth_tier=(answer.truth_tier.value if answer.truth_tier else None),
            provenance_facts=list(answer.provenance_facts or []),
            interview_prep_notes=answer.interview_prep_notes,
            created_at=answer.created_at,
        )
        for answer in answer_rows
    ]
    return PreparedJobRead(
        job_id=job.id,
        candidate_profile_slug=candidate.slug,
        job_title=job.title,
        documents=documents,
        answers=answers,
    )
