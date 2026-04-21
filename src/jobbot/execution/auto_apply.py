"""Durable auto-apply queue orchestration service."""

from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from jobbot.browser.service import get_browser_profile_policy
from jobbot.config import get_settings
from jobbot.db.models import AutoApplyQueueItem, AutoApplyQueueRunnerLease, CandidateProfile, Job, ReviewQueueItem, utcnow
from jobbot.eligibility.service import materialize_application_eligibility
from jobbot.execution.schemas import (
    AutoApplyPreflightCheckRead,
    AutoApplyPreflightRead,
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
from jobbot.models.enums import AutoApplyQueueStatus, ReviewStatus, TruthTier

_PAUSED_BY_OPERATOR = "paused_by_operator"
_CANCELLED_BY_OPERATOR = "cancelled_by_operator"

_QUEUED_AGE_WARNING_SECONDS = 30 * 60
_QUEUED_AGE_CRITICAL_SECONDS = 2 * 60 * 60
_RETRY_AGE_WARNING_SECONDS = 15 * 60
_RETRY_AGE_CRITICAL_SECONDS = 60 * 60
_RECENT_FAILURE_RATE_WARNING = 0.30
_RECENT_FAILURE_RATE_CRITICAL = 0.60
_RECENT_FAILURE_MIN_SAMPLE = 2
_RECENT_WINDOW_SECONDS = 60 * 60
_RUNNING_STALE_WARNING = 1
_RUNNING_STALE_CRITICAL = 3
_SELECTOR_PROBE_WINDOW_DEFAULT = 20
_SELECTOR_PROBE_MIN_SAMPLE_DEFAULT = 4
_SELECTOR_PROBE_FAILURE_RATE_CRITICAL_DEFAULT = 0.50
_SELECTOR_PROBE_FAILURE_RATE_WARNING_DEFAULT = 0.30
_SELECTOR_PROBE_FAILURE_CODES = {
    "guarded_submit_probe_failed",
    "guarded_submit_interaction_failed",
    "draft_target_not_opened",
}
_ADMISSION_SAMPLE_SIZE_DEFAULT = 5
_CANARY_VENDOR_ALLOWLIST_DEFAULT = "greenhouse,lever,workday"
_OUTCOME_SUBMITTED_VERIFIED = "submitted_verified"
_OUTCOME_SUBMITTED_UNVERIFIED = "submitted_unverified"
_OUTCOME_BLOCKED_ACTIONABLE = "blocked_actionable"
_OUTCOME_BLOCKED_NON_ACTIONABLE = "blocked_non_actionable"
_UNVERIFIED_SUBMIT_FAILURE_CODES = {
    "submitted_unverified_confirmation_missing",
    "submitted_unverified_submit_signal_missing",
    "guarded_submit_simulation_not_allowed_in_auto_apply",
}


class QueueRunnerAlreadyActiveError(ValueError):
    """Raised when another queue runner lease is active for a candidate."""

    def __init__(
        self,
        *,
        lease_expires_at: datetime | None,
        remaining_seconds: int | None,
        owner_host: str | None,
        owner_pid: int | None,
    ) -> None:
        super().__init__("queue_runner_already_active")
        self.lease_expires_at = lease_expires_at
        self.remaining_seconds = remaining_seconds
        self.owner_host = owner_host
        self.owner_pid = owner_pid


class AutoApplyPreflightBlockedError(ValueError):
    """Raised when auto-apply preflight checks fail before queue execution."""

    def __init__(self, preflight: AutoApplyPreflightRead) -> None:
        super().__init__("auto_apply_preflight_failed")
        self.preflight = preflight


def _resolve_preflight_selector_thresholds() -> dict[str, float | int]:
    """Resolve selector-health thresholds from settings with safe defaults."""

    settings = get_settings()
    window_size = int(
        max(1, getattr(settings, "auto_apply_selector_probe_window", _SELECTOR_PROBE_WINDOW_DEFAULT))
    )
    min_sample = int(
        max(1, getattr(settings, "auto_apply_selector_probe_min_sample", _SELECTOR_PROBE_MIN_SAMPLE_DEFAULT))
    )
    warning_threshold = float(
        getattr(
            settings,
            "auto_apply_selector_probe_failure_rate_warning",
            _SELECTOR_PROBE_FAILURE_RATE_WARNING_DEFAULT,
        )
    )
    critical_threshold = float(
        getattr(
            settings,
            "auto_apply_selector_probe_failure_rate_critical",
            _SELECTOR_PROBE_FAILURE_RATE_CRITICAL_DEFAULT,
        )
    )
    warning_threshold = min(max(warning_threshold, 0.0), 1.0)
    critical_threshold = min(max(critical_threshold, 0.0), 1.0)
    if critical_threshold < warning_threshold:
        critical_threshold = warning_threshold
    return {
        "window_size": window_size,
        "min_sample": min_sample,
        "warning_threshold": warning_threshold,
        "critical_threshold": critical_threshold,
    }


def is_playwright_runtime_available() -> tuple[bool, str | None]:
    """Check whether Playwright runtime can be imported by the current process."""

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, f"{exc.__class__.__name__}: {exc}"
    return True, None


def evaluate_auto_apply_preflight(
    session: Session,
    *,
    candidate_profile_slug: str,
    browser_profile_key: str | None,
) -> AutoApplyPreflightRead:
    """Evaluate deterministic prerequisites before auto-apply queue drains."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    now = utcnow()
    checks: list[AutoApplyPreflightCheckRead] = []
    blocked_reason_codes: list[str] = []
    settings = get_settings()
    selector_thresholds = _resolve_preflight_selector_thresholds()
    selector_window_size = int(selector_thresholds["window_size"])
    selector_min_sample = int(selector_thresholds["min_sample"])
    selector_warning_threshold = float(selector_thresholds["warning_threshold"])
    selector_critical_threshold = float(selector_thresholds["critical_threshold"])

    playwright_available, playwright_error = is_playwright_runtime_available()
    playwright_check_status = "ok" if playwright_available else "failed"
    playwright_check = AutoApplyPreflightCheckRead(
        check_key="playwright_runtime",
        status=playwright_check_status,
        blocking=not playwright_available,
        reason_code=None if playwright_available else "playwright_runtime_unavailable",
        summary=(
            "Playwright runtime import succeeded"
            if playwright_available
            else "Playwright runtime import failed"
        ),
        details={
            "playwright_available": playwright_available,
            "import_error": playwright_error,
        },
        recommended_actions=(
            []
            if playwright_available
            else [
                "Install Playwright dependencies and browser binaries before running unattended drains",
            ]
        ),
    )
    checks.append(playwright_check)
    if playwright_check.blocking and playwright_check.reason_code is not None:
        blocked_reason_codes.append(playwright_check.reason_code)

    normalized_browser_profile_key = (browser_profile_key or "").strip()
    if not normalized_browser_profile_key:
        browser_check = AutoApplyPreflightCheckRead(
            check_key="browser_profile_health",
            status="failed",
            blocking=True,
            reason_code="browser_profile_required_for_auto_apply_preflight",
            summary="Application browser profile key is required for guarded queue drains",
            details={
                "browser_profile_key": None,
            },
            recommended_actions=[
                "Provide --browser-profile-key (CLI) or browser_profile_key query/form input before queue drains",
            ],
        )
    else:
        try:
            policy = get_browser_profile_policy(session, normalized_browser_profile_key)
            browser_healthy = bool(policy.allow_application)
            browser_check = AutoApplyPreflightCheckRead(
                check_key="browser_profile_health",
                status="ok" if browser_healthy else "failed",
                blocking=not browser_healthy,
                reason_code=None if browser_healthy else "browser_profile_not_ready_for_application",
                summary=(
                    "Browser profile is healthy for application automation"
                    if browser_healthy
                    else "Browser profile is blocked for application automation"
                ),
                details={
                    "browser_profile_key": normalized_browser_profile_key,
                    "session_health": policy.session_health.value,
                    "requires_reauth": policy.requires_reauth,
                    "recommended_action": policy.recommended_action,
                    "reasons": list(policy.reasons),
                },
                recommended_actions=(
                    []
                    if browser_healthy
                    else [
                        "Run browser profile reauthentication and health probe before queue drain",
                    ]
                ),
            )
        except ValueError:
            browser_check = AutoApplyPreflightCheckRead(
                check_key="browser_profile_health",
                status="failed",
                blocking=True,
                reason_code="browser_profile_not_found",
                summary="Browser profile key does not exist",
                details={
                    "browser_profile_key": normalized_browser_profile_key,
                },
                recommended_actions=[
                    "Register or select an existing application browser profile",
                ],
            )
    checks.append(browser_check)
    if browser_check.blocking and browser_check.reason_code is not None:
        blocked_reason_codes.append(browser_check.reason_code)

    recent_rows = session.scalars(
        select(AutoApplyQueueItem)
        .where(
            AutoApplyQueueItem.candidate_profile_id == candidate.id,
            AutoApplyQueueItem.status.in_([
                AutoApplyQueueStatus.SUCCEEDED,
                AutoApplyQueueStatus.FAILED,
            ]),
            AutoApplyQueueItem.finished_at.is_not(None),
        )
        .order_by(
            AutoApplyQueueItem.finished_at.desc(),
            AutoApplyQueueItem.id.desc(),
        )
        .limit(selector_window_size)
    ).all()
    completed_count = len(recent_rows)
    selector_failure_codes = [
        str(row.last_error_code or "")
        for row in recent_rows
        if str(row.last_error_code or "") in _SELECTOR_PROBE_FAILURE_CODES
    ]
    selector_failure_count = len(selector_failure_codes)
    selector_failure_rate = (
        (selector_failure_count / float(completed_count))
        if completed_count > 0
        else None
    )

    selector_status = "ok"
    selector_blocking = False
    selector_reason_code: str | None = None
    selector_summary = "Recent selector probe health is acceptable"
    if completed_count < selector_min_sample:
        selector_status = "warning"
        selector_summary = "Not enough recent queue completions to establish selector-probe baseline"
    elif (
        selector_failure_rate is not None
        and selector_failure_rate >= selector_critical_threshold
    ):
        selector_status = "failed"
        selector_blocking = True
        selector_reason_code = "selector_probe_health_degraded"
        selector_summary = "Recent selector-related failure rate is above critical threshold"
    elif (
        selector_failure_rate is not None
        and selector_failure_rate >= selector_warning_threshold
    ):
        selector_status = "warning"
        selector_summary = "Recent selector-related failure rate is elevated"

    selector_check = AutoApplyPreflightCheckRead(
        check_key="selector_probe_health",
        status=selector_status,
        blocking=selector_blocking,
        reason_code=selector_reason_code,
        summary=selector_summary,
        details={
            "window_size": selector_window_size,
            "completed_count": completed_count,
            "selector_failure_count": selector_failure_count,
            "selector_failure_rate": selector_failure_rate,
            "selector_failure_codes": selector_failure_codes,
            "critical_threshold": selector_critical_threshold,
            "warning_threshold": selector_warning_threshold,
            "min_sample": selector_min_sample,
        },
        recommended_actions=(
            [
                "Run remediation-template requeue and inspect latest submit probe artifacts before unattended drains",
            ]
            if selector_blocking
            else []
        ),
    )
    checks.append(selector_check)
    if selector_check.blocking and selector_check.reason_code is not None:
        blocked_reason_codes.append(selector_check.reason_code)

    admission_sample_size = int(
        max(
            1,
            getattr(get_settings(), "auto_apply_admission_sample_size", _ADMISSION_SAMPLE_SIZE_DEFAULT),
        )
    )
    queued_rows = session.scalars(
        select(AutoApplyQueueItem)
        .where(
            AutoApplyQueueItem.candidate_profile_id == candidate.id,
            AutoApplyQueueItem.status == AutoApplyQueueStatus.QUEUED,
            or_(
                AutoApplyQueueItem.last_error_code.is_(None),
                AutoApplyQueueItem.last_error_code != _PAUSED_BY_OPERATOR,
            ),
        )
        .order_by(
            AutoApplyQueueItem.priority.desc(),
            AutoApplyQueueItem.created_at.asc(),
            AutoApplyQueueItem.id.asc(),
        )
        .limit(admission_sample_size)
    ).all()

    blocked_jobs: dict[str, list[str]] = {}
    for row in queued_rows:
        allow_job, reason_codes, _ = evaluate_auto_apply_job_admission(
            session,
            candidate_profile_slug=candidate_profile_slug,
            job_id=row.job_id,
        )
        if allow_job:
            continue
        blocked_jobs[str(row.job_id)] = reason_codes

    admission_check_status = "ok"
    admission_check_blocking = False
    admission_check_reason_code: str | None = None
    admission_check_summary = "Queued jobs satisfy auto-apply admission policy"
    if blocked_jobs:
        admission_check_status = "failed"
        admission_check_blocking = True
        admission_check_reason_code = "auto_apply_admission_blocked_jobs"
        admission_check_summary = "One or more queued jobs violate auto-apply admission policy"
    elif not queued_rows:
        admission_check_status = "warning"
        admission_check_summary = "No queued jobs available for admission-policy sampling"

    admission_check = AutoApplyPreflightCheckRead(
        check_key="queue_admission_policy",
        status=admission_check_status,
        blocking=admission_check_blocking,
        reason_code=admission_check_reason_code,
        summary=admission_check_summary,
        details={
            "sample_size": admission_sample_size,
            "sampled_queue_ids": [row.id for row in queued_rows],
            "sampled_job_ids": [row.job_id for row in queued_rows],
            "blocked_jobs": blocked_jobs,
        },
        recommended_actions=(
            [
                "Re-prepare blocked jobs, approve pending documents, and requeue only admitted jobs",
            ]
            if blocked_jobs
            else []
        ),
    )
    checks.append(admission_check)
    if admission_check.blocking and admission_check.reason_code is not None:
        blocked_reason_codes.append(admission_check.reason_code)

    canary_settings = _resolve_canary_limits_settings()
    one_hour_ago = now - timedelta(hours=1)
    one_day_ago = now - timedelta(days=1)
    verified_submit_count_1h = len(
        session.scalars(
            select(AutoApplyQueueItem).where(
                AutoApplyQueueItem.candidate_profile_id == candidate.id,
                AutoApplyQueueItem.status == AutoApplyQueueStatus.SUCCEEDED,
                AutoApplyQueueItem.last_error_code == _OUTCOME_SUBMITTED_VERIFIED,
                AutoApplyQueueItem.finished_at.is_not(None),
                AutoApplyQueueItem.finished_at >= one_hour_ago,
            )
        ).all()
    )
    verified_submit_count_24h = len(
        session.scalars(
            select(AutoApplyQueueItem).where(
                AutoApplyQueueItem.candidate_profile_id == candidate.id,
                AutoApplyQueueItem.status == AutoApplyQueueStatus.SUCCEEDED,
                AutoApplyQueueItem.last_error_code == _OUTCOME_SUBMITTED_VERIFIED,
                AutoApplyQueueItem.finished_at.is_not(None),
                AutoApplyQueueItem.finished_at >= one_day_ago,
            )
        ).all()
    )
    canary_check_status = "ok"
    canary_check_blocking = False
    canary_check_reason_code: str | None = None
    canary_check_summary = "Canary verified-submit budget available"
    if verified_submit_count_1h >= int(canary_settings["max_verified_per_hour"]):
        canary_check_status = "failed"
        canary_check_blocking = True
        canary_check_reason_code = "auto_apply_canary_hourly_limit_reached"
        canary_check_summary = "Hourly canary verified-submit limit reached"
    elif verified_submit_count_24h >= int(canary_settings["max_verified_per_day"]):
        canary_check_status = "failed"
        canary_check_blocking = True
        canary_check_reason_code = "auto_apply_canary_daily_limit_reached"
        canary_check_summary = "Daily canary verified-submit limit reached"

    canary_check = AutoApplyPreflightCheckRead(
        check_key="canary_submit_budget",
        status=canary_check_status,
        blocking=canary_check_blocking,
        reason_code=canary_check_reason_code,
        summary=canary_check_summary,
        details={
            "verified_submit_count_1h": verified_submit_count_1h,
            "verified_submit_limit_1h": int(canary_settings["max_verified_per_hour"]),
            "verified_submit_count_24h": verified_submit_count_24h,
            "verified_submit_limit_24h": int(canary_settings["max_verified_per_day"]),
        },
        recommended_actions=(
            [
                "Pause unattended queue drains until canary budget window resets",
            ]
            if canary_check_blocking
            else []
        ),
    )
    checks.append(canary_check)
    if canary_check.blocking and canary_check.reason_code is not None:
        blocked_reason_codes.append(canary_check.reason_code)

    effective_config_check = AutoApplyPreflightCheckRead(
        check_key="effective_configuration",
        status="ok",
        blocking=False,
        reason_code=None,
        summary="Effective preflight thresholds and admission/canary knobs",
        details={
            "selector_probe_window": selector_window_size,
            "selector_probe_min_sample": selector_min_sample,
            "selector_probe_failure_rate_warning": selector_warning_threshold,
            "selector_probe_failure_rate_critical": selector_critical_threshold,
            "admission_sample_size": admission_sample_size,
            "admission_enforce_on_enqueue": bool(
                getattr(settings, "auto_apply_admission_enforce_on_enqueue", True)
            ),
            "admission_min_confidence_score": float(
                getattr(settings, "auto_apply_min_confidence_score", 0.55)
            ),
            "admission_require_review_approved": bool(
                getattr(settings, "auto_apply_require_review_approved", True)
            ),
            "canary_max_verified_per_hour": int(canary_settings["max_verified_per_hour"]),
            "canary_max_verified_per_day": int(canary_settings["max_verified_per_day"]),
            "canary_vendor_allowlist": sorted(str(token) for token in canary_settings["vendor_allowlist"]),
        },
        recommended_actions=[],
    )
    checks.append(effective_config_check)

    drift_check = _build_preflight_configuration_drift_check(
        settings=settings,
        selector_window_size=selector_window_size,
        selector_min_sample=selector_min_sample,
        selector_warning_threshold=selector_warning_threshold,
        selector_critical_threshold=selector_critical_threshold,
        admission_sample_size=admission_sample_size,
        canary_settings=canary_settings,
    )
    checks.append(drift_check)

    return AutoApplyPreflightRead(
        candidate_profile_slug=candidate_profile_slug,
        browser_profile_key=normalized_browser_profile_key or None,
        evaluated_at=now,
        allow_run=len(blocked_reason_codes) == 0,
        blocked_reason_codes=blocked_reason_codes,
        checks=checks,
    )


def _build_preflight_configuration_drift_check(
    *,
    settings,
    selector_window_size: int,
    selector_min_sample: int,
    selector_warning_threshold: float,
    selector_critical_threshold: float,
    admission_sample_size: int,
    canary_settings: dict[str, object],
) -> AutoApplyPreflightCheckRead:
    """Build a non-blocking warning check when effective knobs diverge from defaults."""

    default_allowlist = {
        token.strip().lower()
        for token in str(_CANARY_VENDOR_ALLOWLIST_DEFAULT).split(",")
        if token.strip()
    }
    effective_allowlist = set(canary_settings["vendor_allowlist"])
    defaults = {
        "selector_probe_window": int(_SELECTOR_PROBE_WINDOW_DEFAULT),
        "selector_probe_min_sample": int(_SELECTOR_PROBE_MIN_SAMPLE_DEFAULT),
        "selector_probe_failure_rate_warning": float(_SELECTOR_PROBE_FAILURE_RATE_WARNING_DEFAULT),
        "selector_probe_failure_rate_critical": float(_SELECTOR_PROBE_FAILURE_RATE_CRITICAL_DEFAULT),
        "admission_sample_size": int(_ADMISSION_SAMPLE_SIZE_DEFAULT),
        "admission_enforce_on_enqueue": True,
        "admission_min_confidence_score": 0.55,
        "admission_require_review_approved": True,
        "canary_max_verified_per_hour": 5,
        "canary_max_verified_per_day": 20,
        "canary_vendor_allowlist": sorted(default_allowlist),
    }
    effective = {
        "selector_probe_window": int(selector_window_size),
        "selector_probe_min_sample": int(selector_min_sample),
        "selector_probe_failure_rate_warning": float(selector_warning_threshold),
        "selector_probe_failure_rate_critical": float(selector_critical_threshold),
        "admission_sample_size": int(admission_sample_size),
        "admission_enforce_on_enqueue": bool(
            getattr(settings, "auto_apply_admission_enforce_on_enqueue", True)
        ),
        "admission_min_confidence_score": float(
            getattr(settings, "auto_apply_min_confidence_score", 0.55)
        ),
        "admission_require_review_approved": bool(
            getattr(settings, "auto_apply_require_review_approved", True)
        ),
        "canary_max_verified_per_hour": int(canary_settings["max_verified_per_hour"]),
        "canary_max_verified_per_day": int(canary_settings["max_verified_per_day"]),
        "canary_vendor_allowlist": sorted(str(token) for token in effective_allowlist),
    }

    drift_keys = [key for key in defaults if effective[key] != defaults[key]]
    if not drift_keys:
        return AutoApplyPreflightCheckRead(
            check_key="configuration_drift",
            status="ok",
            blocking=False,
            reason_code=None,
            summary="Effective preflight configuration matches conservative defaults",
            details={
                "drift_keys": [],
                "defaults": defaults,
                "effective": effective,
            },
            recommended_actions=[],
        )

    return AutoApplyPreflightCheckRead(
        check_key="configuration_drift",
        status="warning",
        blocking=False,
        reason_code="preflight_configuration_drift_detected",
        summary="Effective preflight configuration diverges from conservative defaults",
        details={
            "drift_keys": drift_keys,
            "defaults": defaults,
            "effective": effective,
        },
        recommended_actions=[
            "Review and document non-default preflight knobs before unattended auto-apply runs",
        ],
    )


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
    blocked_job_ids: list[int] = []
    blocked_reasons: dict[str, list[str]] = {}
    touched_items: list[AutoApplyQueueItem] = []
    deduped_job_ids = list(dict.fromkeys(job_ids))
    now = utcnow()
    enforce_admission_on_enqueue = bool(
        getattr(get_settings(), "auto_apply_admission_enforce_on_enqueue", True)
    )

    for job_id in deduped_job_ids:
        if enforce_admission_on_enqueue:
            allow_job, reason_codes, _ = evaluate_auto_apply_job_admission(
                session,
                candidate_profile_slug=candidate_profile_slug,
                job_id=job_id,
            )
            if not allow_job:
                blocked_job_ids.append(job_id)
                blocked_reasons[str(job_id)] = reason_codes
                skipped_count += 1
                continue

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
        blocked_job_ids=blocked_job_ids,
        blocked_reasons=blocked_reasons,
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
    verified_submit_count_1h = 0
    verified_submit_count_24h = 0
    unverified_submit_count_24h = 0
    one_hour_ago = now - timedelta(hours=1)
    one_day_ago = now - timedelta(days=1)
    failure_code_counts: dict[str, int] = {}
    failure_code_queue_ids: dict[str, list[int]] = {}
    blocker_counts_24h: dict[str, int] = {}

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
            if row.last_error_code == _OUTCOME_SUBMITTED_VERIFIED and row.finished_at is not None:
                if _to_utc_naive(row.finished_at) >= _to_utc_naive(one_hour_ago):
                    verified_submit_count_1h += 1
                if _to_utc_naive(row.finished_at) >= _to_utc_naive(one_day_ago):
                    verified_submit_count_24h += 1
        elif row.status == AutoApplyQueueStatus.FAILED:
            failed_count += 1
            if row.last_error_code in _UNVERIFIED_SUBMIT_FAILURE_CODES and row.finished_at is not None and _to_utc_naive(row.finished_at) >= _to_utc_naive(one_day_ago):
                unverified_submit_count_24h += 1
            if row.last_error_code and row.finished_at is not None and _to_utc_naive(row.finished_at) >= _to_utc_naive(one_day_ago):
                blocker_counts_24h[row.last_error_code] = blocker_counts_24h.get(row.last_error_code, 0) + 1
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
    runner_lease_owner_host: str | None = None
    runner_lease_owner_pid: int | None = None
    if (
        runner_lease is not None
        and runner_lease.lease_token
        and runner_lease.lease_expires_at is not None
        and _is_after_now(runner_lease.lease_expires_at, now)
    ):
        runner_lease_active = True
        runner_lease_expires_at = runner_lease.lease_expires_at
        runner_lease_remaining_seconds = _seconds_until(runner_lease.lease_expires_at, now)
        runner_lease_owner_host = runner_lease.lease_owner_host
        runner_lease_owner_pid = runner_lease.lease_owner_pid

    recent_failure_rate_1h = None
    if recent_completed_count_1h > 0:
        recent_failure_rate_1h = recent_failed_count_1h / float(recent_completed_count_1h)

    verified_submit_rate_24h = None
    unverified_submit_ratio_24h = None
    submit_outcome_total_24h = verified_submit_count_24h + unverified_submit_count_24h
    if submit_outcome_total_24h > 0:
        verified_submit_rate_24h = verified_submit_count_24h / float(submit_outcome_total_24h)
        unverified_submit_ratio_24h = unverified_submit_count_24h / float(submit_outcome_total_24h)

    top_blocker_code_24h: str | None = None
    top_blocker_count_24h = 0
    if blocker_counts_24h:
        top_blocker_code_24h = min(
            blocker_counts_24h,
            key=lambda key: (-blocker_counts_24h[key], key),
        )
        top_blocker_count_24h = blocker_counts_24h[top_blocker_code_24h]

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

    slo_status = "ok"
    slo_alerts: list[str] = []
    slo_recommended_actions: list[str] = []
    if oldest_queued_at is not None:
        queued_age = _seconds_since(oldest_queued_at, now) or 0
        if queued_age >= _QUEUED_AGE_CRITICAL_SECONDS:
            slo_status = _elevate_slo_status(slo_status, "critical")
            slo_alerts.append(f"queued_backlog_age_critical:{queued_age}s")
            slo_recommended_actions.append("Investigate processing blockage and run queue-control triage")
        elif queued_age >= _QUEUED_AGE_WARNING_SECONDS:
            slo_status = _elevate_slo_status(slo_status, "warning")
            slo_alerts.append(f"queued_backlog_age_warning:{queued_age}s")

    if oldest_retry_scheduled_at is not None:
        retry_age = _seconds_since(oldest_retry_scheduled_at, now) or 0
        if retry_age >= _RETRY_AGE_CRITICAL_SECONDS:
            slo_status = _elevate_slo_status(slo_status, "critical")
            slo_alerts.append(f"retry_backlog_age_critical:{retry_age}s")
            slo_recommended_actions.append("Requeue top failed items and inspect top failure code")
        elif retry_age >= _RETRY_AGE_WARNING_SECONDS:
            slo_status = _elevate_slo_status(slo_status, "warning")
            slo_alerts.append(f"retry_backlog_age_warning:{retry_age}s")

    if stale_running_count >= _RUNNING_STALE_CRITICAL:
        slo_status = _elevate_slo_status(slo_status, "critical")
        slo_alerts.append(f"stale_running_critical:{stale_running_count}")
        slo_recommended_actions.append("Review runner lease diagnostics and reclaim stuck items")
    elif stale_running_count >= _RUNNING_STALE_WARNING:
        slo_status = _elevate_slo_status(slo_status, "warning")
        slo_alerts.append(f"stale_running_warning:{stale_running_count}")

    if recent_failure_rate_1h is not None and recent_completed_count_1h >= _RECENT_FAILURE_MIN_SAMPLE:
        if recent_failure_rate_1h >= _RECENT_FAILURE_RATE_CRITICAL:
            slo_status = _elevate_slo_status(slo_status, "critical")
            slo_alerts.append(f"recent_failure_rate_critical:{recent_failure_rate_1h:.2f}")
            slo_recommended_actions.append("Prioritize remediation template actions before next auto-apply run")
        elif recent_failure_rate_1h >= _RECENT_FAILURE_RATE_WARNING:
            slo_status = _elevate_slo_status(slo_status, "warning")
            slo_alerts.append(f"recent_failure_rate_warning:{recent_failure_rate_1h:.2f}")

    if runner_lease_active and (runner_lease_remaining_seconds or 0) > 10 * 60:
        slo_status = _elevate_slo_status(slo_status, "warning")
        slo_alerts.append(f"runner_lease_active_warning:{runner_lease_remaining_seconds}s")

    summary_delta_marker = _build_auto_apply_summary_delta_marker(
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
        recent_completed_count_1h=recent_completed_count_1h,
        recent_failure_rate_1h=recent_failure_rate_1h,
        verified_submit_count_1h=verified_submit_count_1h,
        verified_submit_count_24h=verified_submit_count_24h,
        unverified_submit_count_24h=unverified_submit_count_24h,
        verified_submit_rate_24h=verified_submit_rate_24h,
        unverified_submit_ratio_24h=unverified_submit_ratio_24h,
        blocker_counts_24h=blocker_counts_24h,
        top_failure_code=top_failure_code,
        top_failure_count=top_failure_count,
        slo_status=slo_status,
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
        summary_generated_at=now,
        recent_window_seconds=_RECENT_WINDOW_SECONDS,
        recent_window_started_at=one_hour_ago,
        recent_window_ended_at=now,
        next_attempt_at=next_attempt_at,
        oldest_queued_age_seconds=_seconds_since(oldest_queued_at, now),
        oldest_retry_scheduled_age_seconds=_seconds_since(oldest_retry_scheduled_at, now),
        recent_completed_count_1h=recent_completed_count_1h,
        recent_failure_rate_1h=recent_failure_rate_1h,
        verified_submit_count_1h=verified_submit_count_1h,
        verified_submit_count_24h=verified_submit_count_24h,
        unverified_submit_count_24h=unverified_submit_count_24h,
        verified_submit_rate_24h=verified_submit_rate_24h,
        unverified_submit_ratio_24h=unverified_submit_ratio_24h,
        summary_delta_marker=summary_delta_marker,
        blocker_counts_24h=blocker_counts_24h,
        top_blocker_code_24h=top_blocker_code_24h,
        top_blocker_count_24h=top_blocker_count_24h,
        runner_lease_active=runner_lease_active,
        runner_lease_expires_at=runner_lease_expires_at,
        runner_lease_remaining_seconds=runner_lease_remaining_seconds,
        runner_lease_owner_host=runner_lease_owner_host,
        runner_lease_owner_pid=runner_lease_owner_pid,
        top_failure_code=top_failure_code,
        top_failure_count=top_failure_count,
        top_failure_queue_ids=top_failure_queue_ids,
        recommended_remediation_action=recommended_remediation_action,
        recommended_requeue_route=recommended_requeue_route,
        recommended_cli_command=recommended_cli_command,
        slo_status=slo_status,
        slo_alerts=slo_alerts,
        slo_recommended_actions=list(dict.fromkeys(slo_recommended_actions)),
    )


def list_auto_apply_queue_summaries(
    session: Session,
    *,
    limit: int = 50,
    include_empty: bool = False,
    cursor: str | None = None,
) -> list[AutoApplyQueueSummaryRead]:
    """List candidate-scoped queue summaries for operations tooling."""

    normalized_cursor = (cursor or "").strip() or None

    if include_empty:
        stmt = select(CandidateProfile.slug)
        if normalized_cursor is not None:
            stmt = stmt.where(CandidateProfile.slug > normalized_cursor)
        candidate_slugs = session.scalars(
            stmt.order_by(CandidateProfile.slug.asc()).limit(limit)
        ).all()
    else:
        stmt = (
            select(CandidateProfile.slug)
            .join(
                AutoApplyQueueItem,
                AutoApplyQueueItem.candidate_profile_id == CandidateProfile.id,
            )
            .distinct()
        )
        if normalized_cursor is not None:
            stmt = stmt.where(CandidateProfile.slug > normalized_cursor)
        candidate_slugs = session.scalars(
            stmt.order_by(CandidateProfile.slug.asc()).limit(limit)
        ).all()

    summaries: list[AutoApplyQueueSummaryRead] = []
    for candidate_slug in candidate_slugs:
        summaries.append(
            get_auto_apply_queue_summary(
                session,
                candidate_profile_slug=candidate_slug,
            )
        )
    return summaries


def requeue_failed_auto_apply_items(
    session: Session,
    *,
    candidate_profile_slug: str,
    queue_ids: list[int] | None = None,
    limit: int = 100,
    actionable_only: bool = False,
    cooldown_seconds: int | None = None,
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
    effective_cooldown_seconds = cooldown_seconds
    if effective_cooldown_seconds is None:
        effective_cooldown_seconds = int(
            max(0, getattr(get_settings(), "auto_apply_requeue_actionable_cooldown_seconds", 120))
        )
    else:
        effective_cooldown_seconds = max(0, int(effective_cooldown_seconds))
    requeued_count = 0
    skipped_count = 0
    touched: list[AutoApplyQueueItem] = []

    for row in rows:
        touched.append(row)
        if row.status != AutoApplyQueueStatus.FAILED:
            skipped_count += 1
            continue
        if actionable_only:
            error_code = str(row.last_error_code or "")
            if not _is_retryable_error(error_code):
                skipped_count += 1
                continue
            if row.finished_at is not None and effective_cooldown_seconds > 0:
                age_seconds = _seconds_since(row.finished_at, now) or 0
                if age_seconds < effective_cooldown_seconds:
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
    preflight_required: bool = True,
) -> AutoApplyQueueRunRead:
    """Drain a bounded number of queued items for one candidate."""

    if lease_seconds < 30:
        raise ValueError("invalid_lease_seconds")

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    if preflight_required:
        preflight = evaluate_auto_apply_preflight(
            session,
            candidate_profile_slug=candidate_profile_slug,
            browser_profile_key=browser_profile_key,
        )
        if not preflight.allow_run:
            raise AutoApplyPreflightBlockedError(preflight)

    runner_lease_token = _acquire_runner_lease(
        session,
        candidate_id=candidate.id,
        lease_seconds=lease_seconds,
    )

    now = utcnow()
    canary_settings = _resolve_canary_limits_settings()
    _assert_canary_budget_available(
        session,
        candidate_id=candidate.id,
        now=now,
        hourly_limit=canary_settings["max_verified_per_hour"],
        daily_limit=canary_settings["max_verified_per_day"],
    )
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
                _assert_canary_job_allowed(
                    session,
                    job_id=item.job_id,
                    vendor_allowlist=canary_settings["vendor_allowlist"],
                )
                allow_job, reason_codes, _ = evaluate_auto_apply_job_admission(
                    session,
                    candidate_profile_slug=candidate_profile_slug,
                    job_id=item.job_id,
                )
                if not allow_job:
                    admission_reason = reason_codes[0] if reason_codes else "policy_rejected"
                    raise ValueError(f"auto_apply_admission_blocked:{admission_reason}")

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
                submit_outcome = _classify_submit_outcome(attempt_detail)
                if submit_outcome["outcome"] == _OUTCOME_SUBMITTED_UNVERIFIED:
                    _queue_unverified_submit_review(
                        session,
                        attempt_id=submitted.attempt_id,
                        reason_code=str(submit_outcome["reason_code"]),
                        details=submit_outcome,
                    )
                    raise ValueError(str(submit_outcome["reason_code"]))

                completed_at = utcnow()
                item.status = AutoApplyQueueStatus.SUCCEEDED
                item.source_attempt_id = submitted.attempt_id
                item.last_error_code = _OUTCOME_SUBMITTED_VERIFIED
                item.last_error_message = _OUTCOME_SUBMITTED_VERIFIED
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
                failure_outcome = _classify_failure_outcome(error_code)
                item.attempt_count += 1
                item.last_error_code = error_code
                item.last_error_message = failure_outcome
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
    owner_host = socket.gethostname()
    owner_pid = os.getpid()

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
                owner_host=lease.lease_owner_host,
                owner_pid=lease.lease_owner_pid,
            )
        lease.lease_token = lease_token
        lease.lease_expires_at = lease_expires_at
        lease.lease_owner_host = owner_host
        lease.lease_owner_pid = owner_pid
        lease.updated_at = now
        session.commit()
        return lease_token

    lease = AutoApplyQueueRunnerLease(
        candidate_profile_id=candidate_id,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        lease_owner_host=owner_host,
        lease_owner_pid=owner_pid,
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
                owner_host=lease.lease_owner_host,
                owner_pid=lease.lease_owner_pid,
            )
        if lease is None:
            raise
        lease.lease_token = lease_token
        lease.lease_expires_at = lease_expires_at
        lease.lease_owner_host = owner_host
        lease.lease_owner_pid = owner_pid
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
    lease.lease_owner_host = None
    lease.lease_owner_pid = None
    lease.updated_at = utcnow()
    session.commit()


def evaluate_auto_apply_job_admission(
    session: Session,
    *,
    candidate_profile_slug: str,
    job_id: int,
) -> tuple[bool, list[str], dict[str, object]]:
    """Evaluate deterministic admission policy for one candidate/job auto-apply run."""

    try:
        eligibility = materialize_application_eligibility(
            session,
            job_id=job_id,
            candidate_profile_slug=candidate_profile_slug,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"job_not_found", "candidate_profile_not_found"}:
            return False, [detail], {
                "job_id": job_id,
                "candidate_profile_slug": candidate_profile_slug,
                "readiness_state": "unknown",
                "ready": False,
                "confidence_score": None,
                "confidence_threshold": float(getattr(get_settings(), "auto_apply_min_confidence_score", 0.55)),
                "score_blocked": False,
                "all_documents_approved": False,
                "document_count": 0,
                "reasons": [detail],
            }
        raise
    settings = get_settings()
    confidence_threshold = float(
        max(0.0, min(1.0, getattr(settings, "auto_apply_min_confidence_score", 0.55)))
    )
    require_approved_docs = bool(
        getattr(settings, "auto_apply_require_review_approved", True)
    )

    score_summary = eligibility.score_summary or {}
    prepared_summary = eligibility.prepared_summary or {}
    reason_codes: list[str] = []

    if not eligibility.ready or eligibility.readiness_state != "ready_to_apply":
        reason_codes.append("readiness_state_not_ready_to_apply")

    if bool(score_summary.get("blocked")):
        reason_codes.append("score_blocked")

    confidence_value = score_summary.get("confidence_score")
    confidence_float: float | None = None
    if confidence_value is None:
        reason_codes.append("score_confidence_missing")
    else:
        try:
            confidence_float = float(confidence_value)
        except (TypeError, ValueError):
            reason_codes.append("score_confidence_invalid")
        else:
            if confidence_float < confidence_threshold:
                reason_codes.append("score_confidence_below_threshold")

    if require_approved_docs and not bool(prepared_summary.get("all_documents_approved")):
        reason_codes.append("prepared_documents_not_approved")
    if int(prepared_summary.get("document_count") or 0) <= 0:
        reason_codes.append("prepared_documents_missing")

    details: dict[str, object] = {
        "job_id": job_id,
        "candidate_profile_slug": candidate_profile_slug,
        "readiness_state": eligibility.readiness_state,
        "ready": eligibility.ready,
        "confidence_score": confidence_float,
        "confidence_threshold": confidence_threshold,
        "score_blocked": bool(score_summary.get("blocked")),
        "all_documents_approved": bool(prepared_summary.get("all_documents_approved")),
        "document_count": int(prepared_summary.get("document_count") or 0),
        "reasons": list(eligibility.reasons or []),
    }
    return len(reason_codes) == 0, reason_codes, details


def _is_retryable_error(error_code: str) -> bool:
    """Classify retryable orchestration errors for queue backoff."""

    retryable_errors = {
        "browser_profile_required_for_page_open",
        "browser_profile_not_ready_for_application",
        "browser_profile_not_found",
        "draft_execution_not_started",
        "guarded_submit_interaction_failed",
        "guarded_submit_probe_failed",
        "guarded_submit_simulation_not_allowed_in_auto_apply",
        "draft_target_not_opened",
    }
    if error_code.startswith("auto_apply_admission_blocked:"):
        return False
    if error_code.startswith("auto_apply_canary_"):
        return False
    if error_code in {
        "readiness_state_not_ready_to_apply",
        "score_blocked",
        "score_confidence_missing",
        "score_confidence_invalid",
        "score_confidence_below_threshold",
        "prepared_documents_not_approved",
        "prepared_documents_missing",
        "submitted_unverified_confirmation_missing",
        "submitted_unverified_submit_signal_missing",
    }:
        return False
    return error_code in retryable_errors


def _classify_failure_outcome(error_code: str) -> str:
    """Classify queue failure outcomes for deterministic remediation policy."""

    if _is_retryable_error(error_code):
        return _OUTCOME_BLOCKED_ACTIONABLE
    return _OUTCOME_BLOCKED_NON_ACTIONABLE


def _classify_submit_outcome(attempt_detail) -> dict[str, object]:
    """Classify guarded-submit outcome as verified or unverified using persisted diagnostics."""

    mode = str(getattr(attempt_detail, "submit_interaction_mode", "") or "").strip()
    status = str(getattr(attempt_detail, "submit_interaction_status", "") or "").strip().lower()
    clicked = getattr(attempt_detail, "submit_interaction_clicked", None)
    confirmation_count = getattr(attempt_detail, "submit_interaction_confirmation_count", None)
    confirmation_count = int(confirmation_count) if isinstance(confirmation_count, int) else 0

    if mode == "simulated_probe_fallback":
        return {
            "outcome": _OUTCOME_SUBMITTED_UNVERIFIED,
            "reason_code": "guarded_submit_simulation_not_allowed_in_auto_apply",
            "mode": mode,
            "status": status,
            "clicked": clicked,
            "confirmation_count": confirmation_count,
        }
    if confirmation_count <= 0:
        return {
            "outcome": _OUTCOME_SUBMITTED_UNVERIFIED,
            "reason_code": "submitted_unverified_confirmation_missing",
            "mode": mode,
            "status": status,
            "clicked": clicked,
            "confirmation_count": confirmation_count,
        }
    if mode == "probe_only" or not bool(clicked) or "failed" in status or "blocked" in status:
        return {
            "outcome": _OUTCOME_SUBMITTED_UNVERIFIED,
            "reason_code": "submitted_unverified_submit_signal_missing",
            "mode": mode,
            "status": status,
            "clicked": clicked,
            "confirmation_count": confirmation_count,
        }
    return {
        "outcome": _OUTCOME_SUBMITTED_VERIFIED,
        "reason_code": None,
        "mode": mode,
        "status": status,
        "clicked": clicked,
        "confirmation_count": confirmation_count,
    }


def _queue_unverified_submit_review(
    session: Session,
    *,
    attempt_id: int,
    reason_code: str,
    details: dict[str, object],
) -> None:
    """Queue manual review for unverified submits so they are never treated as success silently."""

    existing = session.scalar(
        select(ReviewQueueItem).where(
            ReviewQueueItem.entity_type == "application_attempt",
            ReviewQueueItem.entity_id == attempt_id,
            ReviewQueueItem.reason == "auto_apply_submit_unverified_review",
        )
    )
    now = utcnow()
    if existing is not None:
        existing.status = ReviewStatus.PENDING.value
        existing.truth_tier = TruthTier.INFERENCE
        existing.updated_at = now
        session.commit()
        return

    session.add(
        ReviewQueueItem(
            entity_type="application_attempt",
            entity_id=attempt_id,
            reason="auto_apply_submit_unverified_review",
            truth_tier=TruthTier.INFERENCE,
            confidence=0.5,
            status=ReviewStatus.PENDING.value,
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()
    session.commit()


def _resolve_canary_limits_settings() -> dict[str, object]:
    """Resolve canary-mode limits from settings with safe defaults."""

    settings = get_settings()
    allowlist_raw = str(getattr(settings, "auto_apply_canary_vendor_allowlist", _CANARY_VENDOR_ALLOWLIST_DEFAULT) or "")
    vendor_allowlist = {
        token.strip().lower()
        for token in allowlist_raw.split(",")
        if token.strip()
    }
    return {
        "max_verified_per_hour": int(max(1, getattr(settings, "auto_apply_canary_max_verified_per_hour", 5))),
        "max_verified_per_day": int(max(1, getattr(settings, "auto_apply_canary_max_verified_per_day", 20))),
        "vendor_allowlist": vendor_allowlist,
    }


def _assert_canary_budget_available(
    session: Session,
    *,
    candidate_id: int,
    now: datetime,
    hourly_limit: int,
    daily_limit: int,
) -> None:
    """Block queue runs when canary verified-submit budgets are exhausted."""

    one_hour_ago = now - timedelta(hours=1)
    one_day_ago = now - timedelta(days=1)
    verified_hourly = len(
        session.scalars(
            select(AutoApplyQueueItem).where(
                AutoApplyQueueItem.candidate_profile_id == candidate_id,
                AutoApplyQueueItem.status == AutoApplyQueueStatus.SUCCEEDED,
                AutoApplyQueueItem.last_error_code == _OUTCOME_SUBMITTED_VERIFIED,
                AutoApplyQueueItem.finished_at.is_not(None),
                AutoApplyQueueItem.finished_at >= one_hour_ago,
            )
        ).all()
    )
    if verified_hourly >= hourly_limit:
        raise ValueError("auto_apply_canary_hourly_limit_reached")

    verified_daily = len(
        session.scalars(
            select(AutoApplyQueueItem).where(
                AutoApplyQueueItem.candidate_profile_id == candidate_id,
                AutoApplyQueueItem.status == AutoApplyQueueStatus.SUCCEEDED,
                AutoApplyQueueItem.last_error_code == _OUTCOME_SUBMITTED_VERIFIED,
                AutoApplyQueueItem.finished_at.is_not(None),
                AutoApplyQueueItem.finished_at >= one_day_ago,
            )
        ).all()
    )
    if verified_daily >= daily_limit:
        raise ValueError("auto_apply_canary_daily_limit_reached")


def _assert_canary_job_allowed(
    session: Session,
    *,
    job_id: int,
    vendor_allowlist: set[str],
) -> None:
    """Block jobs outside canary vendor allowlist."""

    if not vendor_allowlist:
        return
    job = session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise ValueError("job_not_found")
    vendor = str(job.ats_vendor or "").strip().lower()
    if vendor and vendor not in vendor_allowlist:
        raise ValueError("auto_apply_canary_vendor_not_allowed")


def _build_auto_apply_summary_delta_marker(
    *,
    candidate_profile_slug: str,
    total_count: int,
    queued_count: int,
    paused_count: int,
    running_count: int,
    succeeded_count: int,
    failed_count: int,
    retry_scheduled_count: int,
    stale_running_count: int,
    next_attempt_at: datetime | None,
    recent_completed_count_1h: int,
    recent_failure_rate_1h: float | None,
    verified_submit_count_1h: int,
    verified_submit_count_24h: int,
    unverified_submit_count_24h: int,
    verified_submit_rate_24h: float | None,
    unverified_submit_ratio_24h: float | None,
    blocker_counts_24h: dict[str, int],
    top_failure_code: str | None,
    top_failure_count: int,
    slo_status: str,
) -> str:
    """Build deterministic per-candidate summary change marker for repeated polling scans."""

    payload = {
        "candidate_profile_slug": candidate_profile_slug,
        "total_count": total_count,
        "queued_count": queued_count,
        "paused_count": paused_count,
        "running_count": running_count,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "retry_scheduled_count": retry_scheduled_count,
        "stale_running_count": stale_running_count,
        "next_attempt_at": next_attempt_at.isoformat() if next_attempt_at is not None else None,
        "recent_completed_count_1h": recent_completed_count_1h,
        "recent_failure_rate_1h": recent_failure_rate_1h,
        "verified_submit_count_1h": verified_submit_count_1h,
        "verified_submit_count_24h": verified_submit_count_24h,
        "unverified_submit_count_24h": unverified_submit_count_24h,
        "verified_submit_rate_24h": verified_submit_rate_24h,
        "unverified_submit_ratio_24h": unverified_submit_ratio_24h,
        "blocker_counts_24h": dict(sorted(blocker_counts_24h.items())),
        "top_failure_code": top_failure_code,
        "top_failure_count": top_failure_count,
        "slo_status": slo_status,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"delta_{digest[:16]}"


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


def _elevate_slo_status(current: str, candidate: str) -> str:
    """Return the higher-severity status between current and candidate."""

    ranks = {"ok": 0, "warning": 1, "critical": 2}
    if ranks[candidate] > ranks[current]:
        return candidate
    return current
