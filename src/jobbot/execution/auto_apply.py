"""Durable auto-apply queue orchestration service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db.models import AutoApplyQueueItem, AutoApplyQueueRunnerLease, CandidateProfile, utcnow
from jobbot.eligibility.service import materialize_application_eligibility
from jobbot.execution.schemas import (
    AutoApplyQueueControlRead,
    AutoApplyEnqueueRead,
    AutoApplyQueueItemRead,
    AutoApplyQueueRequeueRead,
    AutoApplyQueueRunRead,
    AutoApplyQueueSummaryRead,
)
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

_PAUSED_BY_OPERATOR = "paused_by_operator"
_CANCELLED_BY_OPERATOR = "cancelled_by_operator"


class QueueRunnerAlreadyActiveError(ValueError):
    """Raised when another queue runner lease is active for a candidate."""

    def __init__(
        self,
        *,
        lease_expires_at: datetime | None,
        remaining_seconds: int | None,
    ) -> None:
        super().__init__("queue_runner_already_active")
        self.lease_expires_at = lease_expires_at
        self.remaining_seconds = remaining_seconds


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


def get_auto_apply_queue_summary(
    session: Session,
    *,
    candidate_profile_slug: str,
) -> AutoApplyQueueSummaryRead:
    """Return candidate-scoped auto-apply queue summary for monitoring."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    now = utcnow()
    rows = session.scalars(
        select(AutoApplyQueueItem).where(
            AutoApplyQueueItem.candidate_profile_id == candidate.id,
        )
    ).all()
    runner_lease = session.scalar(
        select(AutoApplyQueueRunnerLease).where(
            AutoApplyQueueRunnerLease.candidate_profile_id == candidate.id,
        )
    )

    queued_count = 0
    paused_count = 0
    running_count = 0
    succeeded_count = 0
    failed_count = 0
    retry_scheduled_count = 0
    stale_running_count = 0
    next_attempt_at: datetime | None = None
    oldest_queued_at: datetime | None = None
    oldest_retry_scheduled_at: datetime | None = None
    recent_completed_count_1h = 0
    recent_failed_count_1h = 0
    one_hour_ago = now - timedelta(hours=1)
    failure_code_counts: dict[str, int] = {}
    failure_code_queue_ids: dict[str, list[int]] = {}

    for row in rows:
        if row.status == AutoApplyQueueStatus.QUEUED:
            if row.last_error_code == _PAUSED_BY_OPERATOR:
                paused_count += 1
            else:
                queued_count += 1
                if oldest_queued_at is None or _to_utc_naive(row.created_at) < _to_utc_naive(oldest_queued_at):
                    oldest_queued_at = row.created_at
            if (
                row.last_error_code != _PAUSED_BY_OPERATOR
                and row.next_attempt_at is not None
                and _is_after_now(row.next_attempt_at, now)
            ):
                retry_scheduled_count += 1
                if oldest_retry_scheduled_at is None or _to_utc_naive(row.updated_at) < _to_utc_naive(oldest_retry_scheduled_at):
                    oldest_retry_scheduled_at = row.updated_at
        elif row.status == AutoApplyQueueStatus.RUNNING:
            running_count += 1
            if row.lease_expires_at is None or not _is_after_now(row.lease_expires_at, now):
                stale_running_count += 1
        elif row.status == AutoApplyQueueStatus.SUCCEEDED:
            succeeded_count += 1
        elif row.status == AutoApplyQueueStatus.FAILED:
            failed_count += 1
            if row.last_error_code and row.last_error_code != _CANCELLED_BY_OPERATOR:
                failure_code_counts[row.last_error_code] = failure_code_counts.get(row.last_error_code, 0) + 1
                failure_code_queue_ids.setdefault(row.last_error_code, []).append(row.id)

        if (
            row.status in {AutoApplyQueueStatus.SUCCEEDED, AutoApplyQueueStatus.FAILED}
            and row.finished_at is not None
            and _to_utc_naive(row.finished_at) >= _to_utc_naive(one_hour_ago)
        ):
            recent_completed_count_1h += 1
            if row.status == AutoApplyQueueStatus.FAILED:
                recent_failed_count_1h += 1

        if row.next_attempt_at is not None:
            if next_attempt_at is None or row.next_attempt_at < next_attempt_at:
                next_attempt_at = row.next_attempt_at

    runner_lease_active = False
    runner_lease_expires_at: datetime | None = None
    runner_lease_remaining_seconds: int | None = None
    if (
        runner_lease is not None
        and runner_lease.lease_token
        and runner_lease.lease_expires_at is not None
        and _is_after_now(runner_lease.lease_expires_at, now)
    ):
        runner_lease_active = True
        runner_lease_expires_at = runner_lease.lease_expires_at
        runner_lease_remaining_seconds = _seconds_until(runner_lease.lease_expires_at, now)

    recent_failure_rate_1h = None
    if recent_completed_count_1h > 0:
        recent_failure_rate_1h = recent_failed_count_1h / float(recent_completed_count_1h)

    top_failure_code: str | None = None
    top_failure_count = 0
    top_failure_queue_ids: list[int] = []
    if failure_code_counts:
        top_failure_code = max(failure_code_counts, key=lambda key: (failure_code_counts[key], key))
        top_failure_count = failure_code_counts[top_failure_code]
        top_failure_queue_ids = failure_code_queue_ids.get(top_failure_code, [])

    recommended_remediation_action: str | None = None
    recommended_requeue_route: str | None = None
    recommended_cli_command: str | None = None
    if top_failure_code is not None:
        recommended_remediation_action = _recommended_queue_remediation_action(top_failure_code)
        recommended_requeue_route = f"/api/auto-apply/{candidate_profile_slug}/requeue-failed"
        queue_id_flags = " ".join([f"--queue-id {queue_id}" for queue_id in top_failure_queue_ids[:10]])
        if recommended_remediation_action == "reauth_then_requeue":
            recommended_cli_command = (
                "reauth-browser-profile --profile-key <application-profile-key>; "
                f"requeue-auto-apply-failed --candidate-profile {candidate_profile_slug} {queue_id_flags}".strip()
            )
        elif queue_id_flags:
            recommended_cli_command = (
                f"requeue-auto-apply-failed --candidate-profile {candidate_profile_slug} {queue_id_flags}"
            )
        else:
            recommended_cli_command = (
                f"requeue-auto-apply-failed --candidate-profile {candidate_profile_slug}"
            )

    return AutoApplyQueueSummaryRead(
        candidate_profile_slug=candidate_profile_slug,
        total_count=len(rows),
        queued_count=queued_count,
        paused_count=paused_count,
        running_count=running_count,
        succeeded_count=succeeded_count,
        failed_count=failed_count,
        retry_scheduled_count=retry_scheduled_count,
        stale_running_count=stale_running_count,
        next_attempt_at=next_attempt_at,
        oldest_queued_age_seconds=_seconds_since(oldest_queued_at, now),
        oldest_retry_scheduled_age_seconds=_seconds_since(oldest_retry_scheduled_at, now),
        recent_completed_count_1h=recent_completed_count_1h,
        recent_failure_rate_1h=recent_failure_rate_1h,
        runner_lease_active=runner_lease_active,
        runner_lease_expires_at=runner_lease_expires_at,
        runner_lease_remaining_seconds=runner_lease_remaining_seconds,
        top_failure_code=top_failure_code,
        top_failure_count=top_failure_count,
        top_failure_queue_ids=top_failure_queue_ids,
        recommended_remediation_action=recommended_remediation_action,
        recommended_requeue_route=recommended_requeue_route,
        recommended_cli_command=recommended_cli_command,
    )


def requeue_failed_auto_apply_items(
    session: Session,
    *,
    candidate_profile_slug: str,
    queue_ids: list[int] | None = None,
    limit: int = 100,
) -> AutoApplyQueueRequeueRead:
    """Requeue failed auto-apply items for one candidate with optional targeting."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    requested_queue_ids = list(dict.fromkeys(queue_ids or []))
    stmt = select(AutoApplyQueueItem).where(
        AutoApplyQueueItem.candidate_profile_id == candidate.id,
    )
    if requested_queue_ids:
        stmt = stmt.where(AutoApplyQueueItem.id.in_(requested_queue_ids))
    ordered_stmt = stmt.order_by(AutoApplyQueueItem.created_at.asc(), AutoApplyQueueItem.id.asc())
    if requested_queue_ids:
        rows = session.scalars(ordered_stmt).all()
    else:
        rows = session.scalars(ordered_stmt.limit(limit)).all()

    found_ids = {row.id for row in rows}
    missing_queue_ids = [queue_id for queue_id in requested_queue_ids if queue_id not in found_ids]

    now = utcnow()
    requeued_count = 0
    skipped_count = 0
    touched: list[AutoApplyQueueItem] = []

    for row in rows:
        touched.append(row)
        if row.status != AutoApplyQueueStatus.FAILED:
            skipped_count += 1
            continue
        row.status = AutoApplyQueueStatus.QUEUED
        row.next_attempt_at = now
        row.lease_token = None
        row.lease_expires_at = None
        row.last_error_code = None
        row.last_error_message = None
        row.finished_at = None
        row.updated_at = now
        requeued_count += 1

    if touched:
        session.commit()
        for row in touched:
            session.refresh(row)

    return AutoApplyQueueRequeueRead(
        candidate_profile_slug=candidate_profile_slug,
        requested_queue_ids=requested_queue_ids,
        missing_queue_ids=missing_queue_ids,
        requeued_count=requeued_count,
        skipped_count=skipped_count,
        items=[_to_queue_item_read(item=row, candidate_profile_slug=candidate_profile_slug) for row in touched],
    )


def control_auto_apply_queue_items(
    session: Session,
    *,
    candidate_profile_slug: str,
    operation: str,
    queue_ids: list[int] | None = None,
    limit: int = 100,
) -> AutoApplyQueueControlRead:
    """Pause, resume, or cancel queued auto-apply items for one candidate."""

    normalized_operation = operation.strip().lower()
    if normalized_operation not in {"pause", "resume", "cancel"}:
        raise ValueError("invalid_queue_operation")

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    requested_queue_ids = list(dict.fromkeys(queue_ids or []))
    stmt = select(AutoApplyQueueItem).where(
        AutoApplyQueueItem.candidate_profile_id == candidate.id,
    )
    if requested_queue_ids:
        stmt = stmt.where(AutoApplyQueueItem.id.in_(requested_queue_ids))
    else:
        stmt = stmt.where(AutoApplyQueueItem.status == AutoApplyQueueStatus.QUEUED)

    ordered_stmt = stmt.order_by(
        AutoApplyQueueItem.priority.desc(),
        AutoApplyQueueItem.created_at.asc(),
        AutoApplyQueueItem.id.asc(),
    )
    if requested_queue_ids:
        rows = session.scalars(ordered_stmt).all()
    else:
        rows = session.scalars(ordered_stmt.limit(limit)).all()

    found_ids = {row.id for row in rows}
    missing_queue_ids = [queue_id for queue_id in requested_queue_ids if queue_id not in found_ids]

    now = utcnow()
    updated_count = 0
    skipped_count = 0
    touched: list[AutoApplyQueueItem] = []

    for row in rows:
        touched.append(row)
        if normalized_operation == "pause":
            if row.status != AutoApplyQueueStatus.QUEUED or row.last_error_code == _PAUSED_BY_OPERATOR:
                skipped_count += 1
                continue
            row.next_attempt_at = None
            row.lease_token = None
            row.lease_expires_at = None
            row.last_error_code = _PAUSED_BY_OPERATOR
            row.last_error_message = _PAUSED_BY_OPERATOR
            row.finished_at = None
            row.updated_at = now
            updated_count += 1
            continue

        if normalized_operation == "resume":
            if row.status != AutoApplyQueueStatus.QUEUED or row.last_error_code != _PAUSED_BY_OPERATOR:
                skipped_count += 1
                continue
            row.next_attempt_at = now
            row.last_error_code = None
            row.last_error_message = None
            row.updated_at = now
            updated_count += 1
            continue

        if row.status != AutoApplyQueueStatus.QUEUED:
            skipped_count += 1
            continue
        row.status = AutoApplyQueueStatus.FAILED
        row.next_attempt_at = None
        row.lease_token = None
        row.lease_expires_at = None
        row.last_error_code = _CANCELLED_BY_OPERATOR
        row.last_error_message = _CANCELLED_BY_OPERATOR
        row.finished_at = now
        row.updated_at = now
        updated_count += 1

    if touched:
        session.commit()
        for row in touched:
            session.refresh(row)

    return AutoApplyQueueControlRead(
        candidate_profile_slug=candidate_profile_slug,
        operation=normalized_operation,
        requested_queue_ids=requested_queue_ids,
        missing_queue_ids=missing_queue_ids,
        updated_count=updated_count,
        skipped_count=skipped_count,
        items=[_to_queue_item_read(item=row, candidate_profile_slug=candidate_profile_slug) for row in touched],
    )


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

    runner_lease_token = _acquire_runner_lease(
        session,
        candidate_id=candidate.id,
        lease_seconds=lease_seconds,
    )

    now = utcnow()
    try:
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
            if row.status == AutoApplyQueueStatus.QUEUED and row.last_error_code == _PAUSED_BY_OPERATOR:
                continue
            if row.next_attempt_at is not None and _is_after_now(row.next_attempt_at, now):
                continue
            if (
                row.status == AutoApplyQueueStatus.RUNNING
                and row.lease_expires_at is not None
                and _is_after_now(row.lease_expires_at, now)
            ):
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
    finally:
        _release_runner_lease(
            session,
            candidate_id=candidate.id,
            lease_token=runner_lease_token,
        )


def _acquire_runner_lease(
    session: Session,
    *,
    candidate_id: int,
    lease_seconds: int,
) -> str:
    """Acquire candidate-scoped queue runner lease or raise if already active."""

    now = utcnow()
    lease_token = f"runner-{uuid4().hex}"
    lease_expires_at = now + timedelta(seconds=lease_seconds)

    lease = session.scalar(
        select(AutoApplyQueueRunnerLease).where(
            AutoApplyQueueRunnerLease.candidate_profile_id == candidate_id,
        )
    )
    if lease is not None:
        if (
            lease.lease_token
            and lease.lease_expires_at is not None
            and _is_after_now(lease.lease_expires_at, now)
        ):
            raise QueueRunnerAlreadyActiveError(
                lease_expires_at=lease.lease_expires_at,
                remaining_seconds=_seconds_until(lease.lease_expires_at, now),
            )
        lease.lease_token = lease_token
        lease.lease_expires_at = lease_expires_at
        lease.updated_at = now
        session.commit()
        return lease_token

    lease = AutoApplyQueueRunnerLease(
        candidate_profile_id=candidate_id,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        created_at=now,
        updated_at=now,
    )
    session.add(lease)
    try:
        session.commit()
        return lease_token
    except IntegrityError:
        session.rollback()
        lease = session.scalar(
            select(AutoApplyQueueRunnerLease).where(
                AutoApplyQueueRunnerLease.candidate_profile_id == candidate_id,
            )
        )
        if (
            lease is not None
            and lease.lease_token
            and lease.lease_expires_at is not None
            and _is_after_now(lease.lease_expires_at, now)
        ):
            raise QueueRunnerAlreadyActiveError(
                lease_expires_at=lease.lease_expires_at,
                remaining_seconds=_seconds_until(lease.lease_expires_at, now),
            )
        if lease is None:
            raise
        lease.lease_token = lease_token
        lease.lease_expires_at = lease_expires_at
        lease.updated_at = now
        session.commit()
        return lease_token


def _release_runner_lease(
    session: Session,
    *,
    candidate_id: int,
    lease_token: str,
) -> None:
    """Release candidate-scoped queue runner lease if owned by caller."""

    lease = session.scalar(
        select(AutoApplyQueueRunnerLease).where(
            AutoApplyQueueRunnerLease.candidate_profile_id == candidate_id,
        )
    )
    if lease is None or lease.lease_token != lease_token:
        return
    lease.lease_token = None
    lease.lease_expires_at = None
    lease.updated_at = utcnow()
    session.commit()


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
        lease_is_stale = row.lease_expires_at is None or not _is_after_now(row.lease_expires_at, now)
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


def _is_after_now(value: datetime, now: datetime) -> bool:
    """Return true when value is chronologically after now with tolerant timezone handling."""

    left = _to_utc_naive(value)
    right = _to_utc_naive(now)
    return left > right


def _to_utc_naive(value: datetime) -> datetime:
    """Normalize datetime to UTC naive for SQLite-compatible comparisons."""

    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _seconds_since(value: datetime | None, now: datetime) -> int | None:
    """Return whole seconds elapsed since value, clamped at zero."""

    if value is None:
        return None
    elapsed = (_to_utc_naive(now) - _to_utc_naive(value)).total_seconds()
    if elapsed < 0:
        return 0
    return int(elapsed)


def _seconds_until(value: datetime | None, now: datetime) -> int | None:
    """Return whole seconds until value, clamped at zero."""

    if value is None:
        return None
    remaining = (_to_utc_naive(value) - _to_utc_naive(now)).total_seconds()
    if remaining < 0:
        return 0
    return int(remaining)


def _recommended_queue_remediation_action(failure_code: str) -> str:
    """Map top queue failure code to operator remediation action template."""

    if failure_code in {
        "browser_profile_required_for_page_open",
        "browser_profile_not_ready_for_application",
        "browser_profile_not_found",
    }:
        return "reauth_then_requeue"
    if failure_code in {
        "guarded_submit_interaction_failed",
        "guarded_submit_probe_failed",
        "submit_gate_blocked",
        "draft_target_not_opened",
        "guarded_submit_simulation_not_allowed_in_auto_apply",
    }:
        return "selective_retry_requeue"
    return "requeue_failed_items"
