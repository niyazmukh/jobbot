import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

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
    assert payload["next_attempt_at"] is not None
    assert payload["oldest_queued_age_seconds"] is not None
    assert payload["oldest_queued_age_seconds"] >= 0
    assert payload["oldest_retry_scheduled_age_seconds"] is not None
    assert payload["oldest_retry_scheduled_age_seconds"] >= 0
    assert payload["recent_completed_count_1h"] == 0
    assert payload["recent_failure_rate_1h"] is None
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
