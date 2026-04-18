"""Durable auto-apply queue orchestration service."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db.models import AutoApplyQueueItem, CandidateProfile, utcnow
from jobbot.eligibility.service import materialize_application_eligibility
from jobbot.execution.schemas import AutoApplyEnqueueRead, AutoApplyQueueItemRead, AutoApplyQueueRunRead
from jobbot.execution.service import (
    bootstrap_draft_application_attempt,
    build_draft_field_plan,
    build_site_field_overlay,
    evaluate_submit_gate,
    execute_guarded_submit,
    get_execution_attempt_detail,
    open_site_target_page,
    start_draft_execution_attempt,
)
from jobbot.models.enums import AutoApplyQueueStatus


def enqueue_auto_apply_jobs(
    session: Session,
    *,
    candidate_profile_slug: str,
    job_ids: list[int],
    priority: int = 100,
    max_attempts: int = 3,
) -> AutoApplyEnqueueRead:
    """Enqueue candidate/job pairs for durable auto-apply execution."""

    if max_attempts < 1:
        raise ValueError("invalid_max_attempts")

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    queued_count = 0
    requeued_count = 0
    skipped_count = 0
    touched_items: list[AutoApplyQueueItem] = []
    deduped_job_ids = list(dict.fromkeys(job_ids))
    now = utcnow()

    for job_id in deduped_job_ids:
        item = session.scalar(
            select(AutoApplyQueueItem).where(
                AutoApplyQueueItem.candidate_profile_id == candidate.id,
                AutoApplyQueueItem.job_id == job_id,
            )
        )
        if item is None:
            item = AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=job_id,
                status=AutoApplyQueueStatus.QUEUED,
                priority=priority,
                max_attempts=max_attempts,
                created_at=now,
                updated_at=now,
            )
            session.add(item)
            session.flush()
            queued_count += 1
            touched_items.append(item)
            continue

        if item.status in {
            AutoApplyQueueStatus.QUEUED,
            AutoApplyQueueStatus.RUNNING,
            AutoApplyQueueStatus.SUCCEEDED,
        }:
            skipped_count += 1
            touched_items.append(item)
            continue

        item.status = AutoApplyQueueStatus.QUEUED
        item.priority = priority
        item.max_attempts = max_attempts
        item.next_attempt_at = now
        item.lease_token = None
        item.lease_expires_at = None
        item.last_error_code = None
        item.last_error_message = None
        item.finished_at = None
        item.updated_at = now
        requeued_count += 1
        touched_items.append(item)

    session.commit()
    for item in touched_items:
        session.refresh(item)

    return AutoApplyEnqueueRead(
        candidate_profile_slug=candidate_profile_slug,
        requested_job_ids=deduped_job_ids,
        queued_count=queued_count,
        requeued_count=requeued_count,
        skipped_count=skipped_count,
        items=[_to_queue_item_read(item=item, candidate_profile_slug=candidate_profile_slug) for item in touched_items],
    )


def list_auto_apply_queue_items(
    session: Session,
    *,
    candidate_profile_slug: str,
    limit: int = 100,
) -> list[AutoApplyQueueItemRead]:
    """List durable auto-apply queue rows for one candidate."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    rows = session.scalars(
        select(AutoApplyQueueItem)
        .where(AutoApplyQueueItem.candidate_profile_id == candidate.id)
        .order_by(
            AutoApplyQueueItem.priority.desc(),
            AutoApplyQueueItem.created_at.asc(),
            AutoApplyQueueItem.id.asc(),
        )
        .limit(limit)
    ).all()
    return [_to_queue_item_read(item=row, candidate_profile_slug=candidate_profile_slug) for row in rows]


def run_auto_apply_queue(
    session: Session,
    *,
    candidate_profile_slug: str,
    browser_profile_key: str | None,
    limit: int = 10,
    lease_seconds: int = 300,
) -> AutoApplyQueueRunRead:
    """Drain a bounded number of queued items for one candidate."""

    if lease_seconds < 30:
        raise ValueError("invalid_lease_seconds")

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    now = utcnow()
    reclaimed_count = _reclaim_stale_running_items(
        session,
        candidate_id=candidate.id,
        now=now,
    )
    rows = session.scalars(
        select(AutoApplyQueueItem)
        .where(
            AutoApplyQueueItem.candidate_profile_id == candidate.id,
            AutoApplyQueueItem.status.in_(
                [
                    AutoApplyQueueStatus.QUEUED,
                    AutoApplyQueueStatus.RUNNING,
                ]
            ),
        )
        .order_by(
            AutoApplyQueueItem.priority.desc(),
            AutoApplyQueueItem.created_at.asc(),
            AutoApplyQueueItem.id.asc(),
        )
        .limit(max(limit * 3, limit))
    ).all()

    candidates: list[AutoApplyQueueItem] = []
    for row in rows:
        if row.next_attempt_at is not None and row.next_attempt_at > now:
            continue
        if row.status == AutoApplyQueueStatus.RUNNING and row.lease_expires_at is not None and row.lease_expires_at > now:
            continue
        candidates.append(row)
        if len(candidates) >= limit:
            break

    processed_items: list[AutoApplyQueueItem] = []
    succeeded_count = 0
    failed_count = 0
    retried_count = 0

    for item in candidates:
        lease_token = f"lease-{uuid4().hex}"
        started_at = utcnow()
        item.status = AutoApplyQueueStatus.RUNNING
        item.lease_token = lease_token
        item.lease_expires_at = started_at + timedelta(seconds=lease_seconds)
        if item.started_at is None:
            item.started_at = started_at
        item.updated_at = started_at
        session.commit()

        try:
            readiness = materialize_application_eligibility(
                session,
                job_id=item.job_id,
                candidate_profile_slug=candidate_profile_slug,
            )
            if not readiness.ready or readiness.readiness_state != "ready_to_apply":
                raise ValueError("application_not_ready_to_apply")

            attempt = bootstrap_draft_application_attempt(
                session,
                job_id=item.job_id,
                candidate_profile_slug=candidate_profile_slug,
                browser_profile_key=browser_profile_key,
                reuse_existing_active_attempt=True,
            )
            start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
            build_draft_field_plan(session, attempt_id=attempt.attempt_id)
            build_site_field_overlay(session, attempt_id=attempt.attempt_id)
            open_site_target_page(session, attempt_id=attempt.attempt_id)
            gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
            if not gate.allow_submit:
                raise ValueError("submit_gate_blocked")
            submitted = execute_guarded_submit(session, attempt_id=attempt.attempt_id)
            attempt_detail = get_execution_attempt_detail(session, attempt_id=submitted.attempt_id)
            if attempt_detail.submit_interaction_mode == "simulated_probe_fallback":
                raise ValueError("guarded_submit_simulation_not_allowed_in_auto_apply")

            completed_at = utcnow()
            item.status = AutoApplyQueueStatus.SUCCEEDED
            item.source_attempt_id = submitted.attempt_id
            item.last_error_code = None
            item.last_error_message = None
            item.finished_at = completed_at
            item.lease_token = None
            item.lease_expires_at = None
            item.next_attempt_at = None
            item.updated_at = completed_at
            session.commit()
            session.refresh(item)
            processed_items.append(item)
            succeeded_count += 1
        except ValueError as exc:
            error_code = (str(exc) or "auto_apply_failed").strip() or "auto_apply_failed"
            item.attempt_count += 1
            item.last_error_code = error_code
            item.last_error_message = error_code
            item.lease_token = None
            item.lease_expires_at = None
            item.updated_at = utcnow()
            if _is_retryable_error(error_code) and item.attempt_count < item.max_attempts:
                backoff_seconds = min(2 ** item.attempt_count, 300)
                item.status = AutoApplyQueueStatus.QUEUED
                item.next_attempt_at = item.updated_at + timedelta(seconds=backoff_seconds)
                retried_count += 1
            else:
                item.status = AutoApplyQueueStatus.FAILED
                item.finished_at = item.updated_at
                failed_count += 1
            session.commit()
            session.refresh(item)
            processed_items.append(item)

    return AutoApplyQueueRunRead(
        candidate_profile_slug=candidate_profile_slug,
        requested_limit=limit,
        reclaimed_count=reclaimed_count,
        processed_count=len(processed_items),
        succeeded_count=succeeded_count,
        failed_count=failed_count,
        retried_count=retried_count,
        items=[
            _to_queue_item_read(item=item, candidate_profile_slug=candidate_profile_slug)
            for item in processed_items
        ],
    )


def _is_retryable_error(error_code: str) -> bool:
    """Classify retryable orchestration errors for queue backoff."""

    return error_code in {
        "browser_profile_required_for_page_open",
        "browser_profile_not_ready_for_application",
        "browser_profile_not_found",
        "draft_execution_not_started",
        "guarded_submit_interaction_failed",
        "guarded_submit_probe_failed",
        "guarded_submit_simulation_not_allowed_in_auto_apply",
        "draft_target_not_opened",
    }


def _reclaim_stale_running_items(
    session: Session,
    *,
    candidate_id: int,
    now,
) -> int:
    """Return stale RUNNING queue items to QUEUED for safe recovery."""

    rows = session.scalars(
        select(AutoApplyQueueItem).where(
            AutoApplyQueueItem.candidate_profile_id == candidate_id,
            AutoApplyQueueItem.status == AutoApplyQueueStatus.RUNNING,
        )
    ).all()
    reclaimed = 0
    for row in rows:
        lease_is_stale = row.lease_expires_at is None or row.lease_expires_at <= now
        if not lease_is_stale:
            continue
        row.status = AutoApplyQueueStatus.QUEUED
        row.lease_token = None
        row.lease_expires_at = None
        row.next_attempt_at = now
        row.last_error_code = "stale_lease_reclaimed"
        row.last_error_message = "running_lease_expired_or_missing"
        row.updated_at = now
        reclaimed += 1
    if reclaimed:
        session.commit()
    return reclaimed


def _to_queue_item_read(
    *,
    item: AutoApplyQueueItem,
    candidate_profile_slug: str,
) -> AutoApplyQueueItemRead:
    """Convert ORM queue row to typed read model."""

    return AutoApplyQueueItemRead(
        queue_id=item.id,
        candidate_profile_slug=candidate_profile_slug,
        job_id=item.job_id,
        status=item.status.value,
        priority=item.priority,
        attempt_count=item.attempt_count,
        max_attempts=item.max_attempts,
        source_attempt_id=item.source_attempt_id,
        last_error_code=item.last_error_code,
        last_error_message=item.last_error_message,
        next_attempt_at=item.next_attempt_at,
        lease_expires_at=item.lease_expires_at,
        started_at=item.started_at,
        finished_at=item.finished_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
