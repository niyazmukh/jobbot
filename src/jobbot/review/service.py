"""Manual review queue services."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db.models import Answer, CandidateProfile, GeneratedDocument, Job, JobScore, ReviewQueueItem, utcnow
from jobbot.models.enums import ReviewStatus, TruthTier
from jobbot.review.schemas import ReviewQueueRead


def queue_score_review(
    session: Session,
    *,
    job_id: int,
    candidate_profile_slug: str,
    reason: str | None = None,
) -> ReviewQueueRead:
    """Create or refresh a review queue item for a persisted score."""

    row = session.execute(
        select(JobScore, CandidateProfile, Job)
        .join(CandidateProfile, CandidateProfile.id == JobScore.candidate_profile_id)
        .join(Job, Job.id == JobScore.job_id)
        .where(
            JobScore.job_id == job_id,
            CandidateProfile.slug == candidate_profile_slug,
        )
    ).first()
    if row is None:
        raise ValueError("job_score_not_found")

    score, candidate, job = row
    payload = score.score_json or {}
    review_reason = reason or _default_review_reason(payload)

    queue_item = session.scalar(
        select(ReviewQueueItem).where(
            ReviewQueueItem.entity_type == "job_score",
            ReviewQueueItem.entity_id == score.id,
        )
    )
    now = utcnow()
    if queue_item is None:
        queue_item = ReviewQueueItem(
            entity_type="job_score",
            entity_id=score.id,
            reason=review_reason,
            truth_tier=TruthTier.INFERENCE,
            confidence=payload.get("confidence_score"),
            status=ReviewStatus.PENDING.value,
        )
        session.add(queue_item)
    else:
        queue_item.reason = review_reason
        queue_item.truth_tier = TruthTier.INFERENCE
        queue_item.confidence = payload.get("confidence_score")
        queue_item.updated_at = now
        if queue_item.status not in {ReviewStatus.APPROVED.value, ReviewStatus.REJECTED.value}:
            queue_item.status = ReviewStatus.PENDING.value

    session.commit()
    session.refresh(queue_item)
    return _to_review_read(queue_item, score=score, candidate_slug=candidate.slug, job=job)


def list_review_queue(
    session: Session,
    *,
    status: str | None = None,
    entity_type: str | None = None,
    limit: int = 50,
) -> list[ReviewQueueRead]:
    """Return review queue items with lightweight context."""

    stmt = (
        select(ReviewQueueItem, JobScore, CandidateProfile, Job)
        .outerjoin(
            JobScore,
            (ReviewQueueItem.entity_type == "job_score") & (ReviewQueueItem.entity_id == JobScore.id),
        )
        .outerjoin(CandidateProfile, CandidateProfile.id == JobScore.candidate_profile_id)
        .outerjoin(Job, Job.id == JobScore.job_id)
        .order_by(ReviewQueueItem.created_at.desc(), ReviewQueueItem.id.desc())
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(ReviewQueueItem.status == status)
    if entity_type is not None:
        stmt = stmt.where(ReviewQueueItem.entity_type == entity_type)

    rows = session.execute(stmt).all()
    return [
        _to_review_read(item, score=score, candidate_slug=(candidate.slug if candidate else None), job=job)
        for item, score, candidate, job in rows
    ]


def set_review_status(
    session: Session,
    *,
    review_id: int,
    status: ReviewStatus,
) -> ReviewQueueRead:
    """Update the status of a review queue item."""

    item = session.scalar(select(ReviewQueueItem).where(ReviewQueueItem.id == review_id))
    if item is None:
        raise ValueError("review_item_not_found")

    item.status = status.value
    item.updated_at = utcnow()
    _apply_review_status_writeback(session, item, status)
    session.commit()
    session.refresh(item)

    score = None
    candidate_slug = None
    job = None
    if item.entity_type == "job_score":
        row = session.execute(
            select(JobScore, CandidateProfile, Job)
            .join(CandidateProfile, CandidateProfile.id == JobScore.candidate_profile_id)
            .join(Job, Job.id == JobScore.job_id)
            .where(JobScore.id == item.entity_id)
        ).first()
        if row is not None:
            score, candidate, job = row
            candidate_slug = candidate.slug

    return _to_review_read(item, score=score, candidate_slug=candidate_slug, job=job)


def _default_review_reason(payload: dict) -> str:
    """Choose a default review reason from deterministic score output."""

    blocking_reasons = list(payload.get("blocking_reasons") or [])
    if blocking_reasons:
        return blocking_reasons[0]
    if payload.get("confidence_score") is not None and payload["confidence_score"] < 0.7:
        return "low_score_confidence"
    return "manual_score_review"


def _apply_review_status_writeback(
    session: Session,
    item: ReviewQueueItem,
    status: ReviewStatus,
) -> None:
    """Propagate review decisions into the governed entity when supported."""

    if item.entity_type == "generated_document":
        document = session.scalar(select(GeneratedDocument).where(GeneratedDocument.id == item.entity_id))
        if document is not None:
            document.review_status = status.value
    elif item.entity_type == "answer":
        answer = session.scalar(select(Answer).where(Answer.id == item.entity_id))
        if answer is not None:
            answer.approval_status = status.value
            answer.extension_approved = status == ReviewStatus.APPROVED


def _to_review_read(
    item: ReviewQueueItem,
    *,
    score: JobScore | None,
    candidate_slug: str | None,
    job: Job | None,
) -> ReviewQueueRead:
    """Build a review read model with optional score/job context."""

    context = None
    if item.entity_type == "job_score" and score is not None:
        payload = score.score_json or {}
        context = {
            "job_id": score.job_id,
            "candidate_profile_slug": candidate_slug,
            "overall_score": score.overall_score,
            "confidence_score": payload.get("confidence_score"),
            "blocked": payload.get("blocked"),
            "blocking_reasons": payload.get("blocking_reasons", []),
            "job_title": job.title if job else None,
        }

    return ReviewQueueRead(
        id=item.id,
        entity_type=item.entity_type,
        entity_id=item.entity_id,
        reason=item.reason,
        truth_tier=item.truth_tier.value if item.truth_tier else None,
        confidence=item.confidence,
        status=item.status,
        created_at=item.created_at,
        updated_at=item.updated_at,
        context=context,
    )
