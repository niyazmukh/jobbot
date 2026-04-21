"""In-process supervised continuous worker runtime for auto-apply queues."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Event, Lock, Thread

from jobbot.db import SessionLocal
from jobbot.db.models import utcnow
from jobbot.execution.auto_apply import (
    AutoApplyPreflightBlockedError,
    QueueRunnerAlreadyActiveError,
    run_auto_apply_queue,
)


@dataclass
class _AutoApplyContinuousWorkerState:
    candidate_profile_slug: str
    browser_profile_key: str | None
    limit: int
    lease_seconds: int
    poll_seconds: int
    max_cycles: int | None
    started_at: datetime
    stop_event: Event
    thread: Thread
    stopped_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    cycles_completed: int = 0
    total_processed_count: int = 0
    total_succeeded_count: int = 0
    total_failed_count: int = 0
    total_retried_count: int = 0
    total_reclaimed_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_preflight_blocked_reason_codes: list[str] | None = None
    last_preflight_blocked_count: int = 0


_WORKER_STATES: dict[str, _AutoApplyContinuousWorkerState] = {}
_WORKER_LOCK = Lock()


def start_auto_apply_continuous_worker(
    *,
    candidate_profile_slug: str,
    browser_profile_key: str | None,
    limit: int,
    lease_seconds: int,
    poll_seconds: int,
    max_cycles: int | None,
) -> dict:
    """Start one candidate-scoped continuous worker or raise when already active."""

    with _WORKER_LOCK:
        current = _WORKER_STATES.get(candidate_profile_slug)
        if current is not None and _is_state_active(current):
            raise ValueError("continuous_worker_already_active")

        stop_event = Event()
        started_at = utcnow()
        state = _AutoApplyContinuousWorkerState(
            candidate_profile_slug=candidate_profile_slug,
            browser_profile_key=browser_profile_key,
            limit=limit,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
            max_cycles=max_cycles,
            started_at=started_at,
            stop_event=stop_event,
            thread=Thread(target=lambda: None),
        )
        thread = Thread(
            target=_worker_loop,
            args=(candidate_profile_slug,),
            name=f"auto-apply-worker-{candidate_profile_slug}",
            daemon=True,
        )
        state.thread = thread
        _WORKER_STATES[candidate_profile_slug] = state
        thread.start()
        return _state_to_payload(state)


def stop_auto_apply_continuous_worker(
    *,
    candidate_profile_slug: str,
    join_timeout_seconds: int = 2,
) -> dict:
    """Stop one candidate-scoped continuous worker and return current status."""

    with _WORKER_LOCK:
        state = _WORKER_STATES.get(candidate_profile_slug)
        if state is None:
            return _inactive_payload(candidate_profile_slug=candidate_profile_slug)
        state.stop_event.set()
        thread = state.thread

    thread.join(timeout=join_timeout_seconds)

    with _WORKER_LOCK:
        state = _WORKER_STATES.get(candidate_profile_slug)
        if state is None:
            return _inactive_payload(candidate_profile_slug=candidate_profile_slug)
        if not state.thread.is_alive() and state.stopped_at is None:
            state.stopped_at = utcnow()
        return _state_to_payload(state)


def get_auto_apply_continuous_worker_status(*, candidate_profile_slug: str) -> dict:
    """Return status for one candidate-scoped continuous worker."""

    with _WORKER_LOCK:
        state = _WORKER_STATES.get(candidate_profile_slug)
        if state is None:
            return _inactive_payload(candidate_profile_slug=candidate_profile_slug)
        if not state.thread.is_alive() and state.stopped_at is None:
            state.stopped_at = utcnow()
        return _state_to_payload(state)


def list_auto_apply_continuous_worker_statuses(*, limit: int = 200) -> list[dict]:
    """Return candidate-scoped continuous worker statuses sorted by candidate slug."""

    with _WORKER_LOCK:
        rows = sorted(_WORKER_STATES.values(), key=lambda state: state.candidate_profile_slug)
        payloads = []
        for state in rows[:limit]:
            if not state.thread.is_alive() and state.stopped_at is None:
                state.stopped_at = utcnow()
            payloads.append(_state_to_payload(state))
        return payloads


def _worker_loop(candidate_profile_slug: str) -> None:
    while True:
        with _WORKER_LOCK:
            state = _WORKER_STATES.get(candidate_profile_slug)
            if state is None:
                return
            if state.stop_event.is_set():
                state.stopped_at = state.stopped_at or utcnow()
                return
            state.last_heartbeat_at = utcnow()
            state.last_run_started_at = utcnow()
            limit = state.limit
            lease_seconds = state.lease_seconds
            browser_profile_key = state.browser_profile_key
            max_cycles = state.max_cycles
            poll_seconds = state.poll_seconds

        try:
            with SessionLocal() as session:
                batch = run_auto_apply_queue(
                    session,
                    candidate_profile_slug=candidate_profile_slug,
                    browser_profile_key=browser_profile_key,
                    limit=limit,
                    lease_seconds=lease_seconds,
                )
            with _WORKER_LOCK:
                state = _WORKER_STATES.get(candidate_profile_slug)
                if state is None:
                    return
                state.total_processed_count += batch.processed_count
                state.total_succeeded_count += batch.succeeded_count
                state.total_failed_count += batch.failed_count
                state.total_retried_count += batch.retried_count
                state.total_reclaimed_count += batch.reclaimed_count
                state.last_error_code = None
                state.last_error_message = None
                state.last_preflight_blocked_reason_codes = []
                state.last_preflight_blocked_count = 0
        except AutoApplyPreflightBlockedError as exc:
            with _WORKER_LOCK:
                state = _WORKER_STATES.get(candidate_profile_slug)
                if state is None:
                    return
                state.last_error_code = "auto_apply_preflight_failed"
                state.last_error_message = (
                    "blocked_reason_codes="
                    + ",".join(exc.preflight.blocked_reason_codes or [])
                )
                state.last_preflight_blocked_reason_codes = list(exc.preflight.blocked_reason_codes)
                state.last_preflight_blocked_count = len(exc.preflight.blocked_reason_codes)
        except QueueRunnerAlreadyActiveError as exc:
            with _WORKER_LOCK:
                state = _WORKER_STATES.get(candidate_profile_slug)
                if state is None:
                    return
                state.last_error_code = "queue_runner_already_active"
                state.last_error_message = (
                    f"runner_lease_remaining_seconds={exc.remaining_seconds},"
                    f"runner_lease_owner_host={exc.owner_host},"
                    f"runner_lease_owner_pid={exc.owner_pid}"
                )
                state.last_preflight_blocked_reason_codes = []
                state.last_preflight_blocked_count = 0
        except ValueError as exc:
            with _WORKER_LOCK:
                state = _WORKER_STATES.get(candidate_profile_slug)
                if state is None:
                    return
                state.last_error_code = str(exc) or "continuous_worker_run_failed"
                state.last_error_message = str(exc) or "continuous_worker_run_failed"
                state.last_preflight_blocked_reason_codes = []
                state.last_preflight_blocked_count = 0
                if str(exc) == "candidate_profile_not_found":
                    state.stop_event.set()
        except Exception as exc:  # pragma: no cover - defensive runtime fallback
            with _WORKER_LOCK:
                state = _WORKER_STATES.get(candidate_profile_slug)
                if state is None:
                    return
                state.last_error_code = exc.__class__.__name__
                state.last_error_message = str(exc)
                state.last_preflight_blocked_reason_codes = []
                state.last_preflight_blocked_count = 0
        finally:
            with _WORKER_LOCK:
                state = _WORKER_STATES.get(candidate_profile_slug)
                if state is not None:
                    state.last_run_finished_at = utcnow()
                    state.last_heartbeat_at = state.last_run_finished_at
                    state.cycles_completed += 1
                    if max_cycles is not None and state.cycles_completed >= max_cycles:
                        state.stop_event.set()

        with _WORKER_LOCK:
            state = _WORKER_STATES.get(candidate_profile_slug)
            if state is None:
                return
            if state.stop_event.is_set():
                state.stopped_at = state.stopped_at or utcnow()
                return
        if state.stop_event.wait(poll_seconds):
            with _WORKER_LOCK:
                refreshed = _WORKER_STATES.get(candidate_profile_slug)
                if refreshed is not None:
                    refreshed.stopped_at = refreshed.stopped_at or utcnow()
            return


def _is_state_active(state: _AutoApplyContinuousWorkerState) -> bool:
    return state.thread.is_alive() and not state.stop_event.is_set()


def _inactive_payload(*, candidate_profile_slug: str) -> dict:
    return {
        "candidate_profile_slug": candidate_profile_slug,
        "active": False,
        "browser_profile_key": None,
        "limit": 0,
        "lease_seconds": 0,
        "poll_seconds": 0,
        "max_cycles": None,
        "started_at": None,
        "stopped_at": None,
        "last_heartbeat_at": None,
        "last_run_started_at": None,
        "last_run_finished_at": None,
        "cycles_completed": 0,
        "total_processed_count": 0,
        "total_succeeded_count": 0,
        "total_failed_count": 0,
        "total_retried_count": 0,
        "total_reclaimed_count": 0,
        "last_error_code": None,
        "last_error_message": None,
        "last_preflight_blocked_reason_codes": [],
        "last_preflight_blocked_count": 0,
    }


def _state_to_payload(state: _AutoApplyContinuousWorkerState) -> dict:
    return {
        "candidate_profile_slug": state.candidate_profile_slug,
        "active": _is_state_active(state),
        "browser_profile_key": state.browser_profile_key,
        "limit": state.limit,
        "lease_seconds": state.lease_seconds,
        "poll_seconds": state.poll_seconds,
        "max_cycles": state.max_cycles,
        "started_at": state.started_at,
        "stopped_at": state.stopped_at,
        "last_heartbeat_at": state.last_heartbeat_at,
        "last_run_started_at": state.last_run_started_at,
        "last_run_finished_at": state.last_run_finished_at,
        "cycles_completed": state.cycles_completed,
        "total_processed_count": state.total_processed_count,
        "total_succeeded_count": state.total_succeeded_count,
        "total_failed_count": state.total_failed_count,
        "total_retried_count": state.total_retried_count,
        "total_reclaimed_count": state.total_reclaimed_count,
        "last_error_code": state.last_error_code,
        "last_error_message": state.last_error_message,
        "last_preflight_blocked_reason_codes": list(state.last_preflight_blocked_reason_codes or []),
        "last_preflight_blocked_count": state.last_preflight_blocked_count,
    }
