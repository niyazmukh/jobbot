import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from jobbot.api.app import app, get_db_session
from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import CandidateFact, CandidateProfile
from jobbot.discovery.greenhouse.adapter import parse_greenhouse_board_payload
from jobbot.discovery.ingestion import ingest_discovery_batch
from jobbot.execution.auto_apply import AutoApplyPreflightBlockedError
from jobbot.execution.schemas import AutoApplyPreflightCheckRead, AutoApplyPreflightRead
from jobbot.models.enums import AutoApplyQueueStatus, BrowserProfileType
from jobbot.preparation.service import prepare_job_for_candidate
from jobbot.scoring.service import score_job_for_candidate


def make_session():
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def load_greenhouse_batch():
    payload = json.loads(
        Path("fixtures/discovery/greenhouse/board_jobs_sample.json").read_text(encoding="utf-8")
    )
    return parse_greenhouse_board_payload(
        company_name="Example Corp",
        board_url="https://boards.greenhouse.io/example",
        payload=payload,
    )


@pytest.fixture(autouse=True)
def _playwright_runtime_available_by_default(monkeypatch):
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.is_playwright_runtime_available",
        lambda: (True, None),
    )


def _seed_ready_job(session, tmp_path: Path) -> int:
    ingest_discovery_batch(session, load_greenhouse_batch())
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={
            "email": "alex@example.com",
            "phone": "+1-555-0100",
            "location": "Remote",
            "linkedin_url": "https://www.linkedin.com/in/alex-doe",
        },
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/alex-doe/resume.pdf"},
    )
    session.add(candidate)
    session.flush()
    session.add_all(
        [
            CandidateFact(
                candidate_profile_id=candidate.id,
                fact_key="skills-001",
                category="skills",
                content="Senior backend engineer with Python SQL AWS experience and 8 years experience",
            ),
            CandidateFact(
                candidate_profile_id=candidate.id,
                fact_key="employment-002",
                category="employment",
                content="Led backend systems used by internal analytics teams.",
            ),
        ]
    )
    browser = models.BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    first_job = session.query(models.Job).order_by(models.Job.id).first()
    first_job.ats_vendor = "greenhouse"
    first_job.requirements_structured = {
        "required_skills": ["python", "sql", "aws"],
        "seniority_signals": ["senior"],
        "required_years_experience": 5,
    }
    session.commit()
    score_job_for_candidate(session, first_job.id, "alex-doe")
    prepare_job_for_candidate(
        session,
        job_id=first_job.id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()
    return first_job.id


def test_auto_apply_queue_api_enqueue_list_and_run(tmp_path, monkeypatch):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)

    monkeypatch.setattr(
        "jobbot.execution.auto_apply.evaluate_submit_gate",
        lambda *args, **kwargs: SimpleNamespace(allow_submit=True),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.execute_guarded_submit",
        lambda *args, **kwargs: SimpleNamespace(attempt_id=kwargs["attempt_id"]),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=1,
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=1,
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=1,
        ),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        enqueue_response = client.post(
            "/api/auto-apply/alex-doe/enqueue?priority=120&max_attempts=2",
            json={"job_ids": [job_id]},
        )
        list_response = client.get("/api/auto-apply/alex-doe/queue")
        run_response = client.post(
            "/api/auto-apply/alex-doe/run?browser_profile_key=apply-main&limit=5&lease_seconds=300"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert enqueue_response.status_code == 200
    assert enqueue_response.json()["queued_count"] == 1
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert run_response.status_code == 200
    assert run_response.json()["reclaimed_count"] == 0
    assert run_response.json()["processed_count"] == 1
    assert run_response.json()["succeeded_count"] == 1
    assert run_response.json()["items"][0]["status"] == "succeeded"


def test_auto_apply_queue_api_rejects_simulated_submit_fallback(tmp_path, monkeypatch):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)

    monkeypatch.setattr(
        "jobbot.execution.auto_apply.evaluate_submit_gate",
        lambda *args, **kwargs: SimpleNamespace(allow_submit=True),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.execute_guarded_submit",
        lambda *args, **kwargs: SimpleNamespace(attempt_id=kwargs["attempt_id"]),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=1,
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=1,
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=1,
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(submit_interaction_mode="simulated_probe_fallback"),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        enqueue_response = client.post(
            "/api/auto-apply/alex-doe/enqueue?priority=120&max_attempts=1",
            json={"job_ids": [job_id]},
        )
        run_response = client.post(
            "/api/auto-apply/alex-doe/run?browser_profile_key=apply-main&limit=5&lease_seconds=300"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert enqueue_response.status_code == 200
    assert run_response.status_code == 200
    assert run_response.json()["reclaimed_count"] == 0
    assert run_response.json()["processed_count"] == 1
    assert run_response.json()["succeeded_count"] == 0
    assert run_response.json()["failed_count"] == 1
    assert run_response.json()["items"][0]["status"] == "failed"
    assert (
        run_response.json()["items"][0]["last_error_code"]
        == "guarded_submit_simulation_not_allowed_in_auto_apply"
    )


def test_auto_apply_queue_api_routes_unverified_submit_to_review(tmp_path, monkeypatch):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)

    monkeypatch.setattr(
        "jobbot.execution.auto_apply.evaluate_submit_gate",
        lambda *args, **kwargs: SimpleNamespace(allow_submit=True),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.execute_guarded_submit",
        lambda *args, **kwargs: SimpleNamespace(attempt_id=kwargs["attempt_id"]),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=1,
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=0,
        ),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        enqueue_response = client.post(
            "/api/auto-apply/alex-doe/enqueue?priority=120&max_attempts=1",
            json={"job_ids": [job_id]},
        )
        run_response = client.post(
            "/api/auto-apply/alex-doe/run?browser_profile_key=apply-main&limit=5&lease_seconds=300"
        )
        review = session.query(models.ReviewQueueItem).filter_by(
            entity_type="application_attempt",
            reason="auto_apply_submit_unverified_review",
        ).one()
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert enqueue_response.status_code == 200
    assert run_response.status_code == 200
    assert run_response.json()["failed_count"] == 1
    assert run_response.json()["items"][0]["last_error_code"] == "submitted_unverified_confirmation_missing"
    assert review.status == "pending"


def test_auto_apply_run_api_canary_budget_conflict(tmp_path, monkeypatch):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()
    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status=AutoApplyQueueStatus.SUCCEEDED,
            priority=120,
            attempt_count=1,
            max_attempts=3,
            last_error_code="submitted_verified",
            last_error_message="submitted_verified",
            finished_at=now - timedelta(minutes=5),
            created_at=now - timedelta(minutes=30),
            updated_at=now - timedelta(minutes=5),
        )
    )
    session.commit()

    class _CanarySettings:
        auto_apply_selector_probe_window = 20
        auto_apply_selector_probe_min_sample = 4
        auto_apply_selector_probe_failure_rate_warning = 0.30
        auto_apply_selector_probe_failure_rate_critical = 0.50
        auto_apply_admission_sample_size = 5
        auto_apply_admission_enforce_on_enqueue = True
        auto_apply_min_confidence_score = 0.55
        auto_apply_require_review_approved = True
        auto_apply_canary_max_verified_per_hour = 1
        auto_apply_canary_max_verified_per_day = 100
        auto_apply_canary_vendor_allowlist = "greenhouse,lever,workday"

    monkeypatch.setattr("jobbot.execution.auto_apply.get_settings", lambda: _CanarySettings())

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        run_response = client.post(
            "/api/auto-apply/alex-doe/run?browser_profile_key=apply-main&limit=5&lease_seconds=300&preflight_required=false"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert run_response.status_code == 409
    assert run_response.json()["detail"] == "auto_apply_canary_hourly_limit_reached"


def test_auto_apply_queue_api_reclaims_stale_running_lease(tmp_path, monkeypatch):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)

    monkeypatch.setattr(
        "jobbot.execution.auto_apply.evaluate_submit_gate",
        lambda *args, **kwargs: SimpleNamespace(allow_submit=True),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.execute_guarded_submit",
        lambda *args, **kwargs: SimpleNamespace(attempt_id=kwargs["attempt_id"]),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=1,
        ),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        enqueue_response = client.post(
            "/api/auto-apply/alex-doe/enqueue?priority=120&max_attempts=2",
            json={"job_ids": [job_id]},
        )
        queue_id = enqueue_response.json()["items"][0]["queue_id"]
        row = session.query(models.AutoApplyQueueItem).filter_by(id=queue_id).one()
        row.status = AutoApplyQueueStatus.RUNNING
        row.lease_token = "stale-lease"
        row.lease_expires_at = None
        session.commit()

        run_response = client.post(
            "/api/auto-apply/alex-doe/run?browser_profile_key=apply-main&limit=5&lease_seconds=300"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert run_response.status_code == 200
    assert run_response.json()["reclaimed_count"] == 1
    assert run_response.json()["processed_count"] == 1
    assert run_response.json()["succeeded_count"] == 1
    assert run_response.json()["items"][0]["status"] == "succeeded"


def test_auto_apply_queue_summary_api_reports_counts_and_stale_running(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    now = models.utcnow()

    session.add_all(
        [
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=job_id,
                status="queued",
                priority=120,
                attempt_count=1,
                max_attempts=3,
                next_attempt_at=now + timedelta(minutes=3),
                created_at=now,
                updated_at=now,
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=second_job.id,
                status="running",
                priority=100,
                attempt_count=1,
                max_attempts=3,
                lease_token="stale-lease",
                lease_expires_at=now - timedelta(minutes=1),
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        summary_response = client.get("/api/auto-apply/alex-doe/summary")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert summary_response.status_code == 200
    payload = summary_response.json()
    assert payload["candidate_profile_slug"] == "alex-doe"
    assert payload["total_count"] == 2
    assert payload["queued_count"] == 1
    assert payload["running_count"] == 1
    assert payload["retry_scheduled_count"] == 1
    assert payload["stale_running_count"] == 1
    assert payload["summary_generated_at"] is not None
    assert payload["recent_window_seconds"] == 3600
    assert payload["recent_window_started_at"] is not None
    assert payload["recent_window_ended_at"] is not None
    assert payload["next_attempt_at"] is not None
    assert payload["oldest_queued_age_seconds"] is not None
    assert payload["oldest_queued_age_seconds"] >= 0
    assert payload["oldest_retry_scheduled_age_seconds"] is not None
    assert payload["oldest_retry_scheduled_age_seconds"] >= 0
    assert payload["recent_completed_count_1h"] == 0
    assert payload["recent_failure_rate_1h"] is None
    assert payload["verified_submit_count_1h"] == 0
    assert payload["verified_submit_count_24h"] == 0
    assert payload["unverified_submit_count_24h"] == 0
    assert payload["summary_delta_marker"] is not None
    assert payload["summary_delta_marker"].startswith("delta_")
    assert payload["runner_lease_active"] is False
    assert payload["runner_lease_expires_at"] is None
    assert payload["runner_lease_remaining_seconds"] is None
    assert payload["slo_status"] == "warning"
    assert payload["slo_alerts"]
    assert any("stale_running_warning" in alert for alert in payload["slo_alerts"])


def test_auto_apply_queue_summary_api_reports_active_runner_lease_diagnostics(tmp_path):
    session = make_session()
    _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()
    session.add(
        models.AutoApplyQueueRunnerLease(
            candidate_profile_id=candidate.id,
            lease_token="runner-active",
            lease_expires_at=now + timedelta(minutes=4),
            lease_owner_host="test-worker-host",
            lease_owner_pid=32100,
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        summary_response = client.get("/api/auto-apply/alex-doe/summary")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert summary_response.status_code == 200
    payload = summary_response.json()
    assert payload["runner_lease_active"] is True
    assert payload["runner_lease_expires_at"] is not None
    assert payload["runner_lease_remaining_seconds"] is not None
    assert payload["runner_lease_remaining_seconds"] > 0
    assert payload["runner_lease_owner_host"] == "test-worker-host"
    assert payload["runner_lease_owner_pid"] == 32100
    assert payload["summary_generated_at"] is not None
    assert payload["recent_window_seconds"] == 3600
    assert payload["recent_window_started_at"] is not None
    assert payload["recent_window_ended_at"] is not None
    assert payload["top_failure_code"] is None
    assert payload["recommended_remediation_action"] is None
    assert payload["recommended_requeue_route"] is None
    assert payload["recommended_cli_command"] is None
    assert payload["slo_status"] == "ok"
    assert payload["slo_alerts"] == []


def test_auto_apply_queue_summary_api_reports_recent_failure_rate_window(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()
    jobs = session.query(models.Job).order_by(models.Job.id.asc()).all()
    other_jobs = [row for row in jobs if row.id != job_id]
    second_job = other_jobs[0]
    if len(other_jobs) > 1:
        third_job = other_jobs[1]
    else:
        extra_job = models.Job(
            canonical_url="https://jobs.example.com/telemetry-window-test",
            title="Telemetry Window Test",
            title_normalized="telemetry window test",
            status="discovered",
            discovered_at=now,
            last_seen_at=now,
        )
        session.add(extra_job)
        session.commit()
        third_job = extra_job

    session.add_all(
        [
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=job_id,
                status="failed",
                priority=120,
                attempt_count=2,
                max_attempts=3,
                last_error_code="submit_gate_blocked",
                last_error_message="submit_gate_blocked",
                finished_at=now - timedelta(minutes=10),
                created_at=now - timedelta(minutes=20),
                updated_at=now - timedelta(minutes=10),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=second_job.id,
                status="succeeded",
                priority=100,
                attempt_count=1,
                max_attempts=3,
                finished_at=now - timedelta(minutes=25),
                created_at=now - timedelta(minutes=30),
                updated_at=now - timedelta(minutes=25),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=third_job.id,
                status="failed",
                priority=90,
                attempt_count=3,
                max_attempts=3,
                last_error_code="guarded_submit_probe_failed",
                last_error_message="guarded_submit_probe_failed",
                finished_at=now - timedelta(hours=3),
                created_at=now - timedelta(hours=4),
                updated_at=now - timedelta(hours=3),
            ),
        ]
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        summary_response = client.get("/api/auto-apply/alex-doe/summary")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert summary_response.status_code == 200
    payload = summary_response.json()
    assert payload["summary_generated_at"] is not None
    assert payload["recent_window_seconds"] == 3600
    assert payload["recent_window_started_at"] is not None
    assert payload["recent_window_ended_at"] is not None
    assert payload["recent_completed_count_1h"] == 2
    assert payload["recent_failure_rate_1h"] == 0.5
    assert payload["top_failure_code"] == "submit_gate_blocked"
    assert payload["top_failure_count"] == 1
    assert payload["top_failure_queue_ids"]
    assert payload["recommended_remediation_action"] == "selective_retry_requeue"
    assert payload["recommended_requeue_route"] == "/api/auto-apply/alex-doe/requeue-failed"
    assert "requeue-auto-apply-failed --candidate-profile alex-doe" in payload["recommended_cli_command"]
    assert payload["slo_status"] == "warning"
    assert payload["slo_alerts"]
    assert any("recent_failure_rate_warning" in alert for alert in payload["slo_alerts"])


def test_auto_apply_queue_summary_api_reports_functional_kpi_rates_and_blocker_trend(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    jobs = session.query(models.Job).order_by(models.Job.id.asc()).all()
    now = models.utcnow()

    other_jobs = [row for row in jobs if row.id != job_id]
    if len(other_jobs) < 2:
        for index in range(2 - len(other_jobs)):
            synthetic = models.Job(
                source="test",
                source_type="fixture",
                canonical_url=f"https://example.invalid/functional-kpi-{index}",
                title=f"Functional KPI {index}",
                title_normalized=f"functional kpi {index}",
                location_normalized="Remote",
                ats_vendor="greenhouse",
            )
            session.add(synthetic)
            session.flush()
            other_jobs.append(synthetic)
    second_job_id = other_jobs[0].id
    third_job_id = other_jobs[1].id

    session.add_all(
        [
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=job_id,
                status="succeeded",
                priority=120,
                attempt_count=1,
                max_attempts=3,
                last_error_code="submitted_verified",
                last_error_message="submitted_verified",
                finished_at=now - timedelta(minutes=20),
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(minutes=20),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=second_job_id,
                status="failed",
                priority=100,
                attempt_count=1,
                max_attempts=3,
                last_error_code="submitted_unverified_confirmation_missing",
                last_error_message="submitted_unverified_confirmation_missing",
                finished_at=now - timedelta(minutes=10),
                created_at=now - timedelta(hours=1),
                updated_at=now - timedelta(minutes=10),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=third_job_id,
                status="failed",
                priority=90,
                attempt_count=2,
                max_attempts=3,
                last_error_code="submit_gate_blocked",
                last_error_message="submit_gate_blocked",
                finished_at=now - timedelta(minutes=5),
                created_at=now - timedelta(hours=1),
                updated_at=now - timedelta(minutes=5),
            ),
        ]
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        summary_response = client.get("/api/auto-apply/alex-doe/summary")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert summary_response.status_code == 200
    payload = summary_response.json()
    assert payload["verified_submit_count_24h"] == 1
    assert payload["unverified_submit_count_24h"] == 1
    assert payload["verified_submit_rate_24h"] == 0.5
    assert payload["unverified_submit_ratio_24h"] == 0.5
    assert payload["blocker_counts_24h"]["submit_gate_blocked"] == 1
    assert payload["top_blocker_code_24h"] == "submit_gate_blocked"
    assert payload["top_blocker_count_24h"] == 1


def test_auto_apply_queue_summary_api_delta_marker_changes_with_summary_state(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=12),
            updated_at=now - timedelta(minutes=12),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first = client.get("/api/auto-apply/alex-doe/summary")
        assert first.status_code == 200
        first_marker = first.json()["summary_delta_marker"]
        assert first_marker is not None

        row = session.query(models.AutoApplyQueueItem).filter_by(job_id=job_id).one()
        row.status = "failed"
        row.last_error_code = "submit_gate_blocked"
        row.last_error_message = "submit_gate_blocked"
        row.finished_at = models.utcnow()
        row.updated_at = models.utcnow()
        session.commit()

        second = client.get("/api/auto-apply/alex-doe/summary")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert second.status_code == 200
    second_marker = second.json()["summary_delta_marker"]
    assert second_marker is not None
    assert second_marker != first_marker


def test_auto_apply_queue_summary_api_reports_critical_slo_status(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    jobs = session.query(models.Job).order_by(models.Job.id.asc()).all()
    now = models.utcnow()

    extra_jobs: list[models.Job] = []
    for index in range(3):
        extra_job = models.Job(
            canonical_url=f"https://jobs.example.com/critical-slo-{index}",
            title=f"Critical SLO {index}",
            title_normalized=f"critical slo {index}",
            status="discovered",
            discovered_at=now,
            last_seen_at=now,
        )
        session.add(extra_job)
        extra_jobs.append(extra_job)
    session.commit()

    target_jobs = [row for row in jobs if row.id != job_id][:1] + extra_jobs
    session.add_all(
        [
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=job_id,
                status="queued",
                priority=120,
                attempt_count=0,
                max_attempts=3,
                created_at=now - timedelta(hours=3),
                updated_at=now - timedelta(hours=3),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=target_jobs[0].id,
                status="queued",
                priority=110,
                attempt_count=2,
                max_attempts=3,
                next_attempt_at=now + timedelta(minutes=5),
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=2),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=target_jobs[1].id,
                status="failed",
                priority=100,
                attempt_count=3,
                max_attempts=3,
                last_error_code="submit_gate_blocked",
                last_error_message="submit_gate_blocked",
                finished_at=now - timedelta(minutes=20),
                created_at=now - timedelta(minutes=30),
                updated_at=now - timedelta(minutes=20),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=target_jobs[2].id,
                status="failed",
                priority=90,
                attempt_count=3,
                max_attempts=3,
                last_error_code="submit_gate_blocked",
                last_error_message="submit_gate_blocked",
                finished_at=now - timedelta(minutes=15),
                created_at=now - timedelta(minutes=25),
                updated_at=now - timedelta(minutes=15),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=target_jobs[3].id,
                status="failed",
                priority=80,
                attempt_count=3,
                max_attempts=3,
                last_error_code="submit_gate_blocked",
                last_error_message="submit_gate_blocked",
                finished_at=now - timedelta(minutes=10),
                created_at=now - timedelta(minutes=20),
                updated_at=now - timedelta(minutes=10),
            ),
        ]
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        summary_response = client.get("/api/auto-apply/alex-doe/summary")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert summary_response.status_code == 200
    payload = summary_response.json()
    assert payload["slo_status"] == "critical"
    assert payload["slo_alerts"]
    assert any("queued_backlog_age_critical" in alert for alert in payload["slo_alerts"])
    assert any("retry_backlog_age_critical" in alert for alert in payload["slo_alerts"])
    assert any("recent_failure_rate_critical" in alert for alert in payload["slo_alerts"])


def test_auto_apply_queue_api_requeue_failed_items(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    now = models.utcnow()

    failed_row = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=job_id,
        status="failed",
        priority=120,
        attempt_count=2,
        max_attempts=3,
        last_error_code="submit_gate_blocked",
        last_error_message="submit_gate_blocked",
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    succeeded_row = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=second_job.id,
        status="succeeded",
        priority=100,
        attempt_count=1,
        max_attempts=3,
        created_at=now,
        updated_at=now,
    )
    session.add_all([failed_row, succeeded_row])
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/auto-apply/alex-doe/requeue-failed",
            json={"queue_ids": [failed_row.id, succeeded_row.id]},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["requeued_count"] == 1
    assert payload["skipped_count"] == 1
    item_by_id = {row["queue_id"]: row for row in payload["items"]}
    assert item_by_id[failed_row.id]["status"] == "queued"
    assert item_by_id[failed_row.id]["last_error_code"] is None
    assert item_by_id[succeeded_row.id]["status"] == "succeeded"


def test_auto_apply_queue_api_requeue_failed_default_scope(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    now = models.utcnow()

    failed_row = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=job_id,
        status="failed",
        priority=120,
        attempt_count=2,
        max_attempts=3,
        last_error_code="guarded_submit_probe_failed",
        last_error_message="guarded_submit_probe_failed",
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    queued_row = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=second_job.id,
        status="queued",
        priority=100,
        attempt_count=0,
        max_attempts=3,
        created_at=now,
        updated_at=now,
    )
    session.add_all([failed_row, queued_row])
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.post("/api/auto-apply/alex-doe/requeue-failed")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["requeued_count"] == 1
    assert payload["skipped_count"] == 1


def test_auto_apply_queue_api_requeue_failed_targeted_ignores_limit_and_reports_missing(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    now = models.utcnow()

    first_failed = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=job_id,
        status="failed",
        priority=120,
        attempt_count=2,
        max_attempts=3,
        last_error_code="submit_gate_blocked",
        last_error_message="submit_gate_blocked",
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    second_failed = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=second_job.id,
        status="failed",
        priority=100,
        attempt_count=1,
        max_attempts=3,
        last_error_code="guarded_submit_probe_failed",
        last_error_message="guarded_submit_probe_failed",
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add_all([first_failed, second_failed])
    session.commit()

    missing_queue_id = max(first_failed.id, second_failed.id) + 999

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/auto-apply/alex-doe/requeue-failed?limit=1",
            json={"queue_ids": [first_failed.id, second_failed.id, missing_queue_id]},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["requeued_count"] == 2
    assert payload["skipped_count"] == 0
    assert payload["missing_queue_ids"] == [missing_queue_id]
    item_by_id = {row["queue_id"]: row for row in payload["items"]}
    assert item_by_id[first_failed.id]["status"] == "queued"
    assert item_by_id[second_failed.id]["status"] == "queued"


def test_auto_apply_queue_api_requeue_failed_actionable_only_with_cooldown(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    third_job = session.query(models.Job).filter(~models.Job.id.in_([job_id, second_job.id])).order_by(models.Job.id.asc()).first()
    if third_job is None:
        synthetic = models.Job(
            source="test",
            source_type="fixture",
            canonical_url="https://example.invalid/synthetic-actionable-only",
            title="Synthetic Actionable Recovery Role",
            title_normalized="synthetic actionable recovery role",
            location_normalized="Remote",
            ats_vendor="greenhouse",
        )
        session.add(synthetic)
        session.flush()
        third_job = synthetic
    now = models.utcnow()

    actionable_ready = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=job_id,
        status="failed",
        priority=120,
        attempt_count=2,
        max_attempts=3,
        last_error_code="guarded_submit_probe_failed",
        last_error_message="guarded_submit_probe_failed",
        finished_at=now - timedelta(minutes=5),
        created_at=now,
        updated_at=now,
    )
    actionable_cooldown = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=second_job.id,
        status="failed",
        priority=100,
        attempt_count=1,
        max_attempts=3,
        last_error_code="guarded_submit_interaction_failed",
        last_error_message="guarded_submit_interaction_failed",
        finished_at=now - timedelta(seconds=30),
        created_at=now,
        updated_at=now,
    )
    non_actionable = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=third_job.id,
        status="failed",
        priority=90,
        attempt_count=1,
        max_attempts=3,
        last_error_code="score_blocked",
        last_error_message="score_blocked",
        finished_at=now - timedelta(minutes=10),
        created_at=now,
        updated_at=now,
    )
    session.add_all([actionable_ready, actionable_cooldown, non_actionable])
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/auto-apply/alex-doe/requeue-failed?actionable_only=true&cooldown_seconds=120"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["requeued_count"] == 1
    assert payload["skipped_count"] == 2
    item_by_id = {row["queue_id"]: row for row in payload["items"]}
    assert item_by_id[actionable_ready.id]["status"] == "queued"
    assert item_by_id[actionable_cooldown.id]["status"] == "failed"
    assert item_by_id[non_actionable.id]["status"] == "failed"


def test_auto_apply_queue_api_control_pause_resume_cancel(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    now = models.utcnow()

    active_row = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=job_id,
        status="queued",
        priority=120,
        attempt_count=0,
        max_attempts=3,
        created_at=now,
        updated_at=now,
    )
    retry_row = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=second_job.id,
        status="queued",
        priority=100,
        attempt_count=1,
        max_attempts=3,
        next_attempt_at=now + timedelta(minutes=10),
        created_at=now,
        updated_at=now,
    )
    session.add_all([active_row, retry_row])
    session.commit()

    missing_queue_id = max(active_row.id, retry_row.id) + 999

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        pause_response = client.post(
            "/api/auto-apply/alex-doe/queue-control?operation=pause&limit=1",
            json={"queue_ids": [active_row.id, retry_row.id, missing_queue_id]},
        )
        resume_response = client.post(
            "/api/auto-apply/alex-doe/queue-control?operation=resume",
            json={"queue_ids": [active_row.id]},
        )
        cancel_response = client.post(
            "/api/auto-apply/alex-doe/queue-control?operation=cancel",
            json={"queue_ids": [active_row.id]},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert pause_response.status_code == 200
    pause_payload = pause_response.json()
    assert pause_payload["operation"] == "pause"
    assert pause_payload["updated_count"] == 2
    assert pause_payload["skipped_count"] == 0
    assert pause_payload["missing_queue_ids"] == [missing_queue_id]

    assert resume_response.status_code == 200
    resume_payload = resume_response.json()
    assert resume_payload["operation"] == "resume"
    assert resume_payload["updated_count"] == 1
    assert resume_payload["skipped_count"] == 0

    assert cancel_response.status_code == 200
    cancel_payload = cancel_response.json()
    assert cancel_payload["operation"] == "cancel"
    assert cancel_payload["updated_count"] == 1
    assert cancel_payload["skipped_count"] == 0
    item_by_id = {row["queue_id"]: row for row in cancel_payload["items"]}
    assert item_by_id[active_row.id]["status"] == "failed"
    assert item_by_id[active_row.id]["last_error_code"] == "cancelled_by_operator"


def test_auto_apply_queue_api_run_skips_paused_items(tmp_path, monkeypatch):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    paused_row = models.AutoApplyQueueItem(
        candidate_profile_id=candidate.id,
        job_id=job_id,
        status="queued",
        priority=120,
        attempt_count=0,
        max_attempts=3,
        last_error_code="paused_by_operator",
        last_error_message="paused_by_operator",
        created_at=now,
        updated_at=now,
    )
    session.add(paused_row)
    session.commit()

    monkeypatch.setattr(
        "jobbot.execution.auto_apply.evaluate_submit_gate",
        lambda *args, **kwargs: SimpleNamespace(allow_submit=True),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.execute_guarded_submit",
        lambda *args, **kwargs: SimpleNamespace(attempt_id=kwargs["attempt_id"]),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        run_response = client.post(
            "/api/auto-apply/alex-doe/run?browser_profile_key=apply-main&limit=5&lease_seconds=300"
        )
        summary_response = client.get("/api/auto-apply/alex-doe/summary")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert run_payload["processed_count"] == 0
    assert run_payload["succeeded_count"] == 0
    assert run_payload["failed_count"] == 0

    assert summary_response.status_code == 200
    summary_payload = summary_response.json()
    assert summary_payload["queued_count"] == 0
    assert summary_payload["paused_count"] == 1


def test_auto_apply_queue_api_run_returns_conflict_when_runner_lease_active(tmp_path):
    session = make_session()
    _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()
    active_lease = models.AutoApplyQueueRunnerLease(
        candidate_profile_id=candidate.id,
        lease_token="runner-active",
        lease_expires_at=now + timedelta(minutes=5),
        lease_owner_host="conflict-worker-host",
        lease_owner_pid=45601,
        created_at=now,
        updated_at=now,
    )
    session.add(active_lease)
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/auto-apply/alex-doe/run?browser_profile_key=apply-main&limit=5&lease_seconds=300"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "queue_runner_already_active"
    assert detail["runner_lease_expires_at"] is not None
    assert detail["runner_lease_remaining_seconds"] is not None
    assert detail["runner_lease_remaining_seconds"] > 0
    assert detail["runner_lease_owner_host"] == "conflict-worker-host"
    assert detail["runner_lease_owner_pid"] == 45601


def test_auto_apply_queue_api_run_reuses_stale_runner_lease(tmp_path, monkeypatch):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()
    stale_lease = models.AutoApplyQueueRunnerLease(
        candidate_profile_id=candidate.id,
        lease_token="runner-stale",
        lease_expires_at=now - timedelta(minutes=1),
        lease_owner_host="stale-worker-host",
        lease_owner_pid=47611,
        created_at=now,
        updated_at=now,
    )
    session.add(stale_lease)
    session.commit()

    monkeypatch.setattr(
        "jobbot.execution.auto_apply.evaluate_submit_gate",
        lambda *args, **kwargs: SimpleNamespace(allow_submit=True),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.execute_guarded_submit",
        lambda *args, **kwargs: SimpleNamespace(attempt_id=kwargs["attempt_id"]),
    )
    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_execution_attempt_detail",
        lambda *args, **kwargs: SimpleNamespace(
            submit_interaction_mode="playwright",
            submit_interaction_status="succeeded",
            submit_interaction_clicked=True,
            submit_interaction_confirmation_count=1,
        ),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        enqueue_response = client.post(
            "/api/auto-apply/alex-doe/enqueue?priority=120&max_attempts=2",
            json={"job_ids": [job_id]},
        )
        run_response = client.post(
            "/api/auto-apply/alex-doe/run?browser_profile_key=apply-main&limit=5&lease_seconds=300"
        )
        lease_row = session.query(models.AutoApplyQueueRunnerLease).filter_by(candidate_profile_id=candidate.id).one()
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert enqueue_response.status_code == 200
    assert run_response.status_code == 200
    assert run_response.json()["processed_count"] == 1
    assert run_response.json()["succeeded_count"] == 1
    assert lease_row.lease_token is None
    assert lease_row.lease_expires_at is None
    assert lease_row.lease_owner_host is None
    assert lease_row.lease_owner_pid is None


def test_auto_apply_queue_api_list_respects_warning_slo_filter(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=40),
            updated_at=now - timedelta(minutes=40),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        warning_response = client.get("/api/auto-apply/alex-doe/queue?queue_slo_filter=warning")
        critical_response = client.get("/api/auto-apply/alex-doe/queue?queue_slo_filter=critical")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert warning_response.status_code == 200
    warning_payload = warning_response.json()
    assert len(warning_payload) == 1
    assert warning_payload[0]["status"] == "queued"

    assert critical_response.status_code == 200
    assert critical_response.json() == []


def test_auto_apply_queue_api_list_respects_critical_slo_filter(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(hours=3),
            updated_at=now - timedelta(hours=3),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        critical_response = client.get("/api/auto-apply/alex-doe/queue?queue_slo_filter=critical")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert critical_response.status_code == 200
    payload = critical_response.json()
    assert len(payload) == 1
    assert payload[0]["status"] == "queued"


def test_auto_apply_queue_summaries_api_lists_fleet_rows_with_slo_filter(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    alex = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=alex.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=45),
            updated_at=now - timedelta(minutes=45),
        )
    )

    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    blake = CandidateProfile(
        name="Blake Doe",
        slug="blake-doe",
        personal_details={"email": "blake@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/blake-doe/resume.pdf"},
    )
    session.add(blake)
    session.flush()
    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=blake.id,
            job_id=second_job.id,
            status="queued",
            priority=110,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(hours=3),
            updated_at=now - timedelta(hours=3),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        all_response = client.get("/api/auto-apply/summaries?queue_slo_filter=all")
        warning_response = client.get("/api/auto-apply/summaries?queue_slo_filter=warning")
        critical_response = client.get("/api/auto-apply/summaries?queue_slo_filter=critical")
        candidate_sorted_response = client.get(
            "/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert all_response.status_code == 200
    all_payload = all_response.json()
    assert [row["candidate_profile_slug"] for row in all_payload] == ["blake-doe", "alex-doe"]

    assert warning_response.status_code == 200
    warning_payload = warning_response.json()
    assert [row["candidate_profile_slug"] for row in warning_payload] == ["blake-doe", "alex-doe"]

    assert candidate_sorted_response.status_code == 200
    candidate_sorted_payload = candidate_sorted_response.json()
    assert [row["candidate_profile_slug"] for row in candidate_sorted_payload] == ["alex-doe", "blake-doe"]

    assert critical_response.status_code == 200
    critical_payload = critical_response.json()
    assert len(critical_payload) == 1
    assert critical_payload[0]["candidate_profile_slug"] == "blake-doe"
    assert critical_payload[0]["slo_status"] == "critical"


def test_auto_apply_queue_summaries_export_api_returns_csv(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    alex = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=alex.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=50),
            updated_at=now - timedelta(minutes=50),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/api/auto-apply/summaries/export?queue_slo_filter=warning")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "candidate_profile_slug,slo_status,total_count" in response.text
    assert "summary_generated_at,recent_window_seconds,recent_window_started_at,recent_window_ended_at" in response.text
    assert "alex-doe,warning" in response.text


def test_auto_apply_queue_summaries_export_api_honors_severity_sorting(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    alex = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=alex.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=45),
            updated_at=now - timedelta(minutes=45),
        )
    )

    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    blake = CandidateProfile(
        name="Blake Doe",
        slug="blake-doe",
        personal_details={"email": "blake@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/blake-doe/resume.pdf"},
    )
    session.add(blake)
    session.flush()
    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=blake.id,
            job_id=second_job.id,
            status="queued",
            priority=110,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(hours=3),
            updated_at=now - timedelta(hours=3),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/api/auto-apply/summaries/export?queue_slo_filter=all")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert lines[0].startswith("candidate_profile_slug,slo_status")
    assert lines[1].startswith("blake-doe,critical")
    assert lines[2].startswith("alex-doe,warning")


def test_auto_apply_queue_summaries_api_supports_candidate_cursor_pagination(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    alex = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=alex.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=45),
            updated_at=now - timedelta(minutes=45),
        )
    )

    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    for slug in ("blake-doe", "casey-doe"):
        profile = CandidateProfile(
            name=slug.replace("-", " ").title(),
            slug=slug,
            personal_details={"email": f"{slug}@example.com"},
            target_preferences={"preferred_locations": ["Remote"], "remote": True},
            source_profile_data={"resume_path": f"/profiles/{slug}/resume.pdf"},
        )
        session.add(profile)
        session.flush()
        session.add(
            models.AutoApplyQueueItem(
                candidate_profile_id=profile.id,
                job_id=second_job.id,
                status="queued",
                priority=110,
                attempt_count=0,
                max_attempts=3,
                created_at=now - timedelta(minutes=35),
                updated_at=now - timedelta(minutes=35),
            )
        )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first_page = client.get(
            "/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc&limit=2"
        )
        next_cursor = first_page.headers.get("X-Next-Cursor")
        second_page = client.get(
            f"/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc&limit=2&cursor={next_cursor}"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert [row["candidate_profile_slug"] for row in first_payload] == ["alex-doe", "blake-doe"]
    assert next_cursor == "blake-doe"

    assert second_page.status_code == 200
    second_payload = second_page.json()
    assert [row["candidate_profile_slug"] for row in second_payload] == ["casey-doe"]


def test_auto_apply_queue_summaries_export_api_supports_candidate_cursor_pagination(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    alex = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=alex.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=45),
            updated_at=now - timedelta(minutes=45),
        )
    )

    second_job = session.query(models.Job).filter(models.Job.id != job_id).order_by(models.Job.id.asc()).first()
    for slug in ("blake-doe", "casey-doe"):
        profile = CandidateProfile(
            name=slug.replace("-", " ").title(),
            slug=slug,
            personal_details={"email": f"{slug}@example.com"},
            target_preferences={"preferred_locations": ["Remote"], "remote": True},
            source_profile_data={"resume_path": f"/profiles/{slug}/resume.pdf"},
        )
        session.add(profile)
        session.flush()
        session.add(
            models.AutoApplyQueueItem(
                candidate_profile_id=profile.id,
                job_id=second_job.id,
                status="queued",
                priority=110,
                attempt_count=0,
                max_attempts=3,
                created_at=now - timedelta(minutes=35),
                updated_at=now - timedelta(minutes=35),
            )
        )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first_page = client.get(
            "/api/auto-apply/summaries/export?queue_slo_filter=all&sort_by=candidate_asc&limit=2"
        )
        next_cursor = first_page.headers.get("X-Next-Cursor")
        second_page = client.get(
            f"/api/auto-apply/summaries/export?queue_slo_filter=all&sort_by=candidate_asc&limit=2&cursor={next_cursor}"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert first_page.status_code == 200
    first_lines = [line for line in first_page.text.splitlines() if line.strip()]
    assert first_lines[0].startswith("candidate_profile_slug,slo_status")
    assert first_lines[1].startswith("alex-doe,")
    assert first_lines[2].startswith("blake-doe,")
    assert next_cursor == "blake-doe"

    assert second_page.status_code == 200
    second_lines = [line for line in second_page.text.splitlines() if line.strip()]
    assert second_lines[0].startswith("candidate_profile_slug,slo_status")
    assert second_lines[1].startswith("casey-doe,")


def test_auto_apply_queue_summaries_export_api_rejects_cursor_without_candidate_sort(tmp_path):
    session = make_session()
    _seed_ready_job(session, tmp_path)

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(
            "/api/auto-apply/summaries/export?queue_slo_filter=all&sort_by=severity_desc&cursor=alex-doe"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 400
    assert response.json()["detail"] == "cursor_requires_candidate_asc_sort"


def test_auto_apply_queue_summaries_api_rejects_cursor_without_candidate_sort(tmp_path):
    session = make_session()
    _seed_ready_job(session, tmp_path)

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(
            "/api/auto-apply/summaries?queue_slo_filter=all&sort_by=severity_desc&cursor=alex-doe"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 400
    assert response.json()["detail"] == "cursor_requires_candidate_asc_sort"


def test_auto_apply_queue_summaries_api_emits_snapshot_lineage_id_header(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=40),
            updated_at=now - timedelta(minutes=40),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first = client.get("/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc")
        second = client.get("/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert first.status_code == 200
    assert second.status_code == 200
    snapshot_1 = first.headers.get("X-Snapshot-Lineage-Id")
    snapshot_2 = second.headers.get("X-Snapshot-Lineage-Id")
    generated_at = first.headers.get("X-Snapshot-Generated-At")
    max_age_seconds = first.headers.get("X-Snapshot-Max-Age-Seconds")
    assert snapshot_1 is not None
    assert snapshot_1.startswith("snapshot_")
    assert snapshot_1 == snapshot_2
    assert generated_at is not None
    datetime.fromisoformat(generated_at)
    assert max_age_seconds is not None
    assert int(max_age_seconds) >= 0


def test_auto_apply_queue_summaries_api_returns_304_when_snapshot_matches(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=30),
            updated_at=now - timedelta(minutes=30),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first = client.get("/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc")
        snapshot = first.headers.get("X-Snapshot-Lineage-Id")
        assert snapshot is not None

        second = client.get(
            "/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc",
            headers={"If-None-Match": f'"{snapshot}"'},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert first.status_code == 200
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers.get("X-Snapshot-Lineage-Id") == snapshot
    assert second.headers.get("ETag") == f'"{snapshot}"'


def test_auto_apply_queue_summaries_api_returns_200_when_snapshot_changes(tmp_path):
    session = make_session()
    first_job_id = _seed_ready_job(session, tmp_path)
    first_job = session.query(models.Job).filter_by(id=first_job_id).one()
    second_job = (
        session.query(models.Job)
        .filter(models.Job.id != first_job.id)
        .order_by(models.Job.id.asc())
        .first()
    )
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=first_job.id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=30),
            updated_at=now - timedelta(minutes=30),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first = client.get("/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc")
        first_snapshot = first.headers.get("X-Snapshot-Lineage-Id")
        assert first_snapshot is not None

        session.add(
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=second_job.id,
                status="queued",
                priority=110,
                attempt_count=0,
                max_attempts=3,
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=10),
            )
        )
        session.commit()

        second = client.get(
            "/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc",
            headers={"If-None-Match": first_snapshot},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert first.status_code == 200
    assert second.status_code == 200
    second_snapshot = second.headers.get("X-Snapshot-Lineage-Id")
    assert second_snapshot is not None
    assert second_snapshot != first_snapshot
    assert second.headers.get("ETag") == f'"{second_snapshot}"'


def test_auto_apply_queue_summaries_export_api_returns_304_when_snapshot_matches(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=40),
            updated_at=now - timedelta(minutes=40),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first = client.get("/api/auto-apply/summaries/export?queue_slo_filter=all")
        snapshot = first.headers.get("X-Snapshot-Lineage-Id")
        assert snapshot is not None

        second = client.get(
            "/api/auto-apply/summaries/export?queue_slo_filter=all",
            headers={"x-snapshot-lineage-id": snapshot},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert first.status_code == 200
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers.get("X-Snapshot-Lineage-Id") == snapshot
    assert second.headers.get("ETag") == f'"{snapshot}"'


def test_auto_apply_queue_summaries_export_api_emits_snapshot_lineage_id_header(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=50),
            updated_at=now - timedelta(minutes=50),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/api/auto-apply/summaries/export?queue_slo_filter=all")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    snapshot = response.headers.get("X-Snapshot-Lineage-Id")
    generated_at = response.headers.get("X-Snapshot-Generated-At")
    max_age_seconds = response.headers.get("X-Snapshot-Max-Age-Seconds")
    assert snapshot is not None
    assert snapshot.startswith("snapshot_")
    assert generated_at is not None
    datetime.fromisoformat(generated_at)
    assert max_age_seconds is not None
    assert int(max_age_seconds) >= 0


def test_auto_apply_queue_summaries_api_emits_changed_candidate_hints_with_prior_markers(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    alex = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    second_job = (
        session.query(models.Job)
        .filter(models.Job.id != job_id)
        .order_by(models.Job.id.asc())
        .first()
    )
    blake = models.CandidateProfile(
        name="Blake Doe",
        slug="blake-doe",
        personal_details={"email": "blake@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/blake-doe/resume.pdf"},
    )
    session.add(blake)
    session.flush()

    session.add_all(
        [
            models.AutoApplyQueueItem(
                candidate_profile_id=alex.id,
                job_id=job_id,
                status="queued",
                priority=120,
                attempt_count=0,
                max_attempts=3,
                created_at=now - timedelta(minutes=20),
                updated_at=now - timedelta(minutes=20),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=blake.id,
                job_id=second_job.id,
                status="queued",
                priority=110,
                attempt_count=0,
                max_attempts=3,
                created_at=now - timedelta(minutes=15),
                updated_at=now - timedelta(minutes=15),
            ),
        ]
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first = client.get("/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc")
        first_items = first.json()
        markers = {
            row["candidate_profile_slug"]: row["summary_delta_marker"]
            for row in first_items
        }
        markers["blake-doe"] = "delta_outdated_marker"

        second = client.get(
            "/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc",
            headers={"x-prior-summary-markers": json.dumps(markers)},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers.get("X-Changed-Candidate-Count") == "1"
    assert second.headers.get("X-Changed-Candidates") == "blake-doe"
    assert second.headers.get("X-Changed-Candidates-Returned") == "1"
    assert second.headers.get("X-Changed-Candidates-Truncated") == "false"


def test_auto_apply_queue_summaries_export_api_emits_changed_candidate_hints_with_prior_markers(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()

    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status="queued",
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=20),
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        summary_response = client.get("/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc")
        marker = summary_response.json()[0]["summary_delta_marker"]
        export_response = client.get(
            "/api/auto-apply/summaries/export?queue_slo_filter=all",
            headers={"x-prior-summary-markers": json.dumps({"alex-doe": marker})},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert export_response.status_code == 200
    assert export_response.headers.get("X-Changed-Candidate-Count") == "0"
    assert export_response.headers.get("X-Changed-Candidates") == ""
    assert export_response.headers.get("X-Changed-Candidates-Returned") == "0"
    assert export_response.headers.get("X-Changed-Candidates-Truncated") == "false"


def test_auto_apply_queue_summaries_api_truncates_changed_candidate_header_list(tmp_path):
    session = make_session()
    _seed_ready_job(session, tmp_path)
    now = models.utcnow()

    created_slugs = ["alex-doe"]
    for index in range(30):
        slug = f"delta-candidate-{index:02d}"
        session.add(
            models.CandidateProfile(
                name=f"Delta Candidate {index}",
                slug=slug,
                personal_details={"email": f"{slug}@example.com"},
                target_preferences={"preferred_locations": ["Remote"], "remote": True},
                source_profile_data={"resume_path": f"/profiles/{slug}/resume.pdf"},
                created_at=now,
                updated_at=now,
            )
        )
        created_slugs.append(slug)
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(
            "/api/auto-apply/summaries?queue_slo_filter=all&sort_by=candidate_asc&include_empty=true&limit=200",
            headers={"x-prior-summary-markers": json.dumps({"seed": "delta_seed"})},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    changed_count = int(response.headers.get("X-Changed-Candidate-Count") or "0")
    returned_count = int(response.headers.get("X-Changed-Candidates-Returned") or "0")
    changed_candidates = [
        token
        for token in str(response.headers.get("X-Changed-Candidates") or "").split(",")
        if token
    ]
    assert changed_count == len(created_slugs)
    assert returned_count == len(changed_candidates)
    assert returned_count == 25
    assert response.headers.get("X-Changed-Candidates-Truncated") == "true"


def test_auto_apply_continuous_worker_api_start_status_stop_and_list(tmp_path, monkeypatch):
    session = make_session()
    _seed_ready_job(session, tmp_path)

    class _DummySessionContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "jobbot.execution.worker_runtime.run_auto_apply_queue",
        lambda *args, **kwargs: SimpleNamespace(
            processed_count=1,
            succeeded_count=1,
            failed_count=0,
            retried_count=0,
            reclaimed_count=0,
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.worker_runtime.SessionLocal",
        lambda: _DummySessionContext(),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        start_response = client.post(
            "/api/auto-apply/alex-doe/worker/start"
            "?browser_profile_key=apply-main&limit=5&lease_seconds=300&poll_seconds=1&max_cycles=1"
        )
        assert start_response.status_code == 200
        start_payload = start_response.json()
        assert start_payload["candidate_profile_slug"] == "alex-doe"

        status_payload = None
        for _ in range(20):
            status_response = client.get("/api/auto-apply/alex-doe/worker/status")
            assert status_response.status_code == 200
            status_payload = status_response.json()
            if not status_payload["active"] and status_payload["cycles_completed"] >= 1:
                break
            time.sleep(0.05)

        assert status_payload is not None
        assert status_payload["cycles_completed"] >= 1
        assert status_payload["total_processed_count"] >= 1
        assert status_payload["last_heartbeat_at"] is not None

        list_response = client.get("/api/auto-apply/workers")
        assert list_response.status_code == 200
        list_payload = list_response.json()
        assert any(row["candidate_profile_slug"] == "alex-doe" for row in list_payload)

        stop_response = client.post("/api/auto-apply/alex-doe/worker/stop?join_timeout_seconds=1")
        assert stop_response.status_code == 200
        stop_payload = stop_response.json()
        assert stop_payload["active"] is False
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_auto_apply_continuous_worker_api_rejects_second_start_when_active(tmp_path, monkeypatch):
    session = make_session()
    _seed_ready_job(session, tmp_path)

    class _DummySessionContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "jobbot.execution.worker_runtime.run_auto_apply_queue",
        lambda *args, **kwargs: SimpleNamespace(
            processed_count=0,
            succeeded_count=0,
            failed_count=0,
            retried_count=0,
            reclaimed_count=0,
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.worker_runtime.SessionLocal",
        lambda: _DummySessionContext(),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first_start = client.post(
            "/api/auto-apply/alex-doe/worker/start"
            "?browser_profile_key=apply-main&limit=5&lease_seconds=300&poll_seconds=300&max_cycles=100"
        )
        second_start = client.post(
            "/api/auto-apply/alex-doe/worker/start"
            "?browser_profile_key=apply-main&limit=5&lease_seconds=300&poll_seconds=300&max_cycles=100"
        )
        stop_response = client.post("/api/auto-apply/alex-doe/worker/stop?join_timeout_seconds=1")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert first_start.status_code == 200
    assert second_start.status_code == 409
    assert second_start.json()["detail"] == "continuous_worker_already_active"
    assert stop_response.status_code == 200


def test_auto_apply_preflight_endpoint_reports_blocked_without_browser_profile(tmp_path):
    session = make_session()
    _seed_ready_job(session, tmp_path)

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/api/auto-apply/alex-doe/preflight")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["allow_run"] is False
    assert payload["blocked_reason_codes"] == ["browser_profile_required_for_auto_apply_preflight"]
    check_map = {row["check_key"]: row for row in payload["checks"]}
    assert check_map["playwright_runtime"]["status"] == "ok"
    assert check_map["browser_profile_health"]["status"] == "failed"
    assert check_map["configuration_drift"]["status"] == "ok"
    assert check_map["configuration_drift"]["details"]["drift_keys"] == []


def test_auto_apply_enqueue_blocks_jobs_failing_admission_policy(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "pending"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        enqueue_response = client.post(
            "/api/auto-apply/alex-doe/enqueue?priority=120&max_attempts=2",
            json={"job_ids": [job_id]},
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert enqueue_response.status_code == 200
    payload = enqueue_response.json()
    assert payload["queued_count"] == 0
    assert payload["skipped_count"] == 1
    assert payload["blocked_job_ids"] == [job_id]
    assert "prepared_documents_not_approved" in payload["blocked_reasons"][str(job_id)]


def test_auto_apply_preflight_blocks_when_queued_jobs_fail_admission_policy(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    now = models.utcnow()
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "pending"
    session.add(
        models.AutoApplyQueueItem(
            candidate_profile_id=candidate.id,
            job_id=job_id,
            status=AutoApplyQueueStatus.QUEUED,
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/api/auto-apply/alex-doe/preflight?browser_profile_key=apply-main")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["allow_run"] is False
    assert "auto_apply_admission_blocked_jobs" in payload["blocked_reason_codes"]
    check_map = {row["check_key"]: row for row in payload["checks"]}
    assert check_map["queue_admission_policy"]["status"] == "failed"
    assert str(job_id) in check_map["queue_admission_policy"]["details"]["blocked_jobs"]


def test_auto_apply_run_api_blocks_when_preflight_fails(tmp_path):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        enqueue_response = client.post(
            "/api/auto-apply/alex-doe/enqueue?priority=120&max_attempts=2",
            json={"job_ids": [job_id]},
        )
        run_response = client.post("/api/auto-apply/alex-doe/run?limit=5&lease_seconds=300")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert enqueue_response.status_code == 200
    assert run_response.status_code == 409
    detail = run_response.json()["detail"]
    assert detail["code"] == "auto_apply_preflight_failed"
    assert detail["preflight"]["allow_run"] is False
    assert "browser_profile_required_for_auto_apply_preflight" in detail["preflight"]["blocked_reason_codes"]


def test_auto_apply_worker_start_api_blocks_when_preflight_fails(tmp_path):
    session = make_session()
    _seed_ready_job(session, tmp_path)

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        start_response = client.post(
            "/api/auto-apply/alex-doe/worker/start?limit=5&lease_seconds=300&poll_seconds=30"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert start_response.status_code == 409
    detail = start_response.json()["detail"]
    assert detail["code"] == "auto_apply_preflight_failed"
    assert detail["preflight"]["allow_run"] is False
    assert "browser_profile_required_for_auto_apply_preflight" in detail["preflight"]["blocked_reason_codes"]


def test_auto_apply_continuous_worker_status_surfaces_runtime_preflight_block_diagnostics(
    tmp_path, monkeypatch
):
    session = make_session()
    _seed_ready_job(session, tmp_path)

    class _DummySessionContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    preflight = AutoApplyPreflightRead(
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
        evaluated_at=datetime.now(timezone.utc),
        allow_run=False,
        blocked_reason_codes=["selector_probe_health_degraded", "auto_apply_canary_daily_limit_reached"],
        checks=[
            AutoApplyPreflightCheckRead(
                check_key="selector_probe_health",
                status="failed",
                blocking=True,
                reason_code="selector_probe_health_degraded",
                summary="Selector probe health is degraded",
                details={"failure_rate": 0.8},
                recommended_actions=["Reduce selector instability before resuming unattended drains"],
            )
        ],
    )

    monkeypatch.setattr(
        "jobbot.execution.worker_runtime.run_auto_apply_queue",
        lambda *args, **kwargs: (_ for _ in ()).throw(AutoApplyPreflightBlockedError(preflight)),
    )
    monkeypatch.setattr(
        "jobbot.execution.worker_runtime.SessionLocal",
        lambda: _DummySessionContext(),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        start_response = client.post(
            "/api/auto-apply/alex-doe/worker/start"
            "?browser_profile_key=apply-main&limit=5&lease_seconds=300&poll_seconds=1&max_cycles=1"
        )
        assert start_response.status_code == 200

        status_payload = None
        for _ in range(20):
            status_response = client.get("/api/auto-apply/alex-doe/worker/status")
            assert status_response.status_code == 200
            status_payload = status_response.json()
            if not status_payload["active"] and status_payload["cycles_completed"] >= 1:
                break
            time.sleep(0.05)
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert status_payload is not None
    assert status_payload["last_error_code"] == "auto_apply_preflight_failed"
    assert status_payload["last_preflight_blocked_reason_codes"] == [
        "selector_probe_health_degraded",
        "auto_apply_canary_daily_limit_reached",
    ]
    assert status_payload["last_preflight_blocked_count"] == 2


def test_auto_apply_preflight_endpoint_honors_selector_threshold_overrides(tmp_path, monkeypatch):
    session = make_session()
    job_id = _seed_ready_job(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    alt_jobs = (
        session.query(models.Job)
        .filter(models.Job.id != job_id)
        .order_by(models.Job.id.asc())
        .limit(5)
        .all()
    )
    if len(alt_jobs) < 2:
        for index in range(2 - len(alt_jobs)):
            synthetic = models.Job(
                source="test",
                source_type="fixture",
                canonical_url=f"https://example.invalid/synthetic-{index}",
                title=f"Synthetic Role {index}",
                title_normalized=f"synthetic role {index}",
                location_normalized="Remote",
                ats_vendor="greenhouse",
            )
            session.add(synthetic)
            session.flush()
            alt_jobs.append(synthetic)
    second_job_id = alt_jobs[0].id
    third_job_id = alt_jobs[1].id
    now = models.utcnow()

    session.add_all(
        [
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=job_id,
                status=AutoApplyQueueStatus.FAILED,
                attempt_count=1,
                max_attempts=3,
                finished_at=now - timedelta(minutes=5),
                last_error_code="guarded_submit_probe_failed",
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=5),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=second_job_id,
                status=AutoApplyQueueStatus.SUCCEEDED,
                attempt_count=1,
                max_attempts=3,
                finished_at=now - timedelta(minutes=4),
                created_at=now - timedelta(minutes=9),
                updated_at=now - timedelta(minutes=4),
            ),
            models.AutoApplyQueueItem(
                candidate_profile_id=candidate.id,
                job_id=third_job_id,
                status=AutoApplyQueueStatus.SUCCEEDED,
                attempt_count=1,
                max_attempts=3,
                finished_at=now - timedelta(minutes=3),
                created_at=now - timedelta(minutes=8),
                updated_at=now - timedelta(minutes=3),
            ),
        ]
    )
    session.commit()

    monkeypatch.setattr(
        "jobbot.execution.auto_apply.get_settings",
        lambda: SimpleNamespace(
            auto_apply_selector_probe_window=10,
            auto_apply_selector_probe_min_sample=2,
            auto_apply_selector_probe_failure_rate_warning=0.2,
            auto_apply_selector_probe_failure_rate_critical=0.3,
        ),
    )

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(
            "/api/auto-apply/alex-doe/preflight?browser_profile_key=apply-main"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["allow_run"] is False
    assert "selector_probe_health_degraded" in payload["blocked_reason_codes"]
    check_map = {row["check_key"]: row for row in payload["checks"]}
    selector_check = check_map["selector_probe_health"]
    assert selector_check["status"] == "failed"
    assert selector_check["blocking"] is True
    assert selector_check["details"]["min_sample"] == 2
    assert selector_check["details"]["critical_threshold"] == 0.3
    config_check = check_map["effective_configuration"]
    assert config_check["status"] == "ok"
    assert config_check["details"]["selector_probe_min_sample"] == 2
    assert config_check["details"]["selector_probe_failure_rate_critical"] == 0.3
    drift_check = check_map["configuration_drift"]
    assert drift_check["status"] == "warning"
    assert drift_check["blocking"] is False
    assert drift_check["reason_code"] == "preflight_configuration_drift_detected"
    assert "selector_probe_window" in drift_check["details"]["drift_keys"]
    assert "selector_probe_min_sample" in drift_check["details"]["drift_keys"]
