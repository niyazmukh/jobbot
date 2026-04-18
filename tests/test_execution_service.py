from pathlib import Path
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import BrowserProfile, CandidateFact, CandidateProfile, Job
from jobbot.eligibility.service import materialize_application_eligibility
from jobbot.execution.service import (
    _selector_matches_html,
    _build_submit_remediation_guidance,
    _capture_target_page_html,
    bootstrap_draft_application_attempt,
    build_draft_field_plan,
    build_site_field_overlay,
    execute_guarded_submit,
    evaluate_submit_gate,
    get_execution_artifact_detail,
    get_execution_attempt_detail,
    get_execution_dashboard,
    get_execution_replay_bundle,
    list_execution_overview,
    list_draft_application_attempts,
    open_site_target_page,
    start_draft_execution_attempt,
)
from jobbot.models.enums import BrowserProfileType
from jobbot.preparation.service import prepare_job_for_candidate
from jobbot.scoring.service import score_job_for_candidate


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def seed_candidate_job_and_ready_snapshot(session, tmp_path: Path):
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
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
    job = Job(
        canonical_url="https://example.com/jobs/42",
        title="Senior Backend Engineer",
        title_normalized="senior backend engineer",
        location_raw="Remote",
        location_normalized="remote",
        requirements_structured={
            "required_skills": ["python", "sql", "aws"],
            "seniority_signals": ["senior"],
            "required_years_experience": 5,
        },
        status="enriched",
    )
    session.add(job)
    session.commit()
    score_job_for_candidate(session, job.id, "alex-doe")
    prepare_job_for_candidate(session, job_id=job.id, candidate_profile_slug="alex-doe", output_dir=tmp_path)
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()
    eligibility = materialize_application_eligibility(
        session,
        job_id=job.id,
        candidate_profile_slug="alex-doe",
    )
    return job.id, eligibility


def test_bootstrap_draft_application_attempt_creates_application_attempt_and_event(tmp_path: Path):
    session = make_session()
    job_id, eligibility = seed_candidate_job_and_ready_snapshot(session, tmp_path)

    result = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )

    application = session.query(models.Application).one()
    attempt = session.query(models.ApplicationAttempt).one()
    event = session.query(models.ApplicationEvent).one()

    assert result.job_id == job_id
    assert result.ready is True
    assert result.readiness_state == eligibility.readiness_state
    assert result.created_application is True
    assert result.attempt_result is None
    assert result.submit_confidence is None
    assert application.last_attempt_id == attempt.id
    assert attempt.mode.value == "draft"
    assert event.event_type == "draft_attempt_bootstrapped"


def test_bootstrap_draft_application_attempt_rejects_missing_or_not_ready_eligibility(tmp_path: Path):
    session = make_session()
    candidate = CandidateProfile(name="Alex Doe", slug="alex-doe")
    job = Job(
        canonical_url="https://example.com/jobs/13",
        title="Backend Engineer",
        title_normalized="backend engineer",
    )
    session.add_all([candidate, job])
    session.commit()

    try:
        bootstrap_draft_application_attempt(
            session,
            job_id=job.id,
            candidate_profile_slug="alex-doe",
        )
        assert False, "expected eligibility error"
    except ValueError as exc:
        assert str(exc) == "application_eligibility_not_found"

    not_ready_job = Job(
        canonical_url="https://example.com/jobs/99",
        title="Staff Engineer",
        title_normalized="staff engineer",
        location_raw="San Francisco, CA",
        location_normalized="san francisco bay area",
        requirements_structured={
            "required_skills": ["go", "distributed systems"],
            "seniority_signals": ["staff"],
            "required_years_experience": 10,
        },
        status="enriched",
    )
    session.add(not_ready_job)
    session.commit()
    score_job_for_candidate(session, not_ready_job.id, "alex-doe")
    materialize_application_eligibility(session, job_id=not_ready_job.id, candidate_profile_slug="alex-doe")

    try:
        bootstrap_draft_application_attempt(
            session,
            job_id=not_ready_job.id,
            candidate_profile_slug="alex-doe",
        )
        assert False, "expected not-ready error"
    except ValueError as exc:
        assert str(exc) == "application_not_ready_to_apply"


def test_bootstrap_draft_application_attempt_accepts_healthy_application_browser_profile(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    result = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )

    assert result.browser_profile_key == "apply-main"
    assert result.session_health == "healthy"


def test_list_draft_application_attempts_returns_bootstrapped_rows(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )

    rows = list_draft_application_attempts(
        session,
        candidate_profile_slug="alex-doe",
        limit=10,
    )

    assert len(rows) == 1
    assert rows[0].job_id == job_id
    assert rows[0].attempt_mode == "draft"
    assert rows[0].notes == "Bootstrapped from persisted eligibility snapshot."


def test_start_draft_execution_attempt_creates_startup_artifacts_and_event(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )

    startup = start_draft_execution_attempt(
        session,
        attempt_id=attempt.attempt_id,
    )

    artifacts = session.query(models.Artifact).filter(models.Artifact.attempt_id == attempt.attempt_id).all()
    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_execution_started",
    ).one()

    assert startup.attempt_id == attempt.attempt_id
    assert startup.job_id == job_id
    assert startup.prepared_document_count == 1
    assert startup.prepared_answer_count >= 2
    assert Path(startup.startup_dir).exists()
    assert len(startup.startup_artifact_ids) == len(artifacts)
    assert event.payload["target_url"].startswith("https://")
    assert any(artifact.artifact_type.value == "model_io" for artifact in artifacts)
    assert any(artifact.artifact_type.value == "answer_pack" for artifact in artifacts)
    context_path = Path(next(artifact.path for artifact in artifacts if artifact.artifact_type.value == "model_io"))
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    assert payload["candidate_profile_slug"] == "alex-doe"
    assert payload["readiness_state"] == "ready_to_apply"


def test_start_draft_execution_attempt_uses_live_target_capture_when_browser_profile_available(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html",
        lambda **kwargs: (
            "<html><body>startup-live-capture</body></html>",
            {"capture_method": "playwright", "status_code": 200, "final_url": kwargs["target_url"]},
        ),
    )

    startup = start_draft_execution_attempt(
        session,
        attempt_id=attempt.attempt_id,
    )

    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_execution_started",
    ).one()
    target_path = Path(startup.startup_dir) / "target_page.html"
    assert target_path.exists()
    assert "startup-live-capture" in target_path.read_text(encoding="utf-8")
    assert event.payload["target_capture"]["capture_method"] == "playwright"

    context_path = Path(startup.startup_dir) / "startup_context.json"
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    assert payload["target_capture"]["capture_method"] == "playwright"


def test_start_draft_execution_attempt_uses_stub_capture_without_browser_profile(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )

    startup = start_draft_execution_attempt(
        session,
        attempt_id=attempt.attempt_id,
    )

    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_execution_started",
    ).one()
    target_path = Path(startup.startup_dir) / "target_page.html"
    assert target_path.exists()
    assert "Draft Execution Target" in target_path.read_text(encoding="utf-8")
    assert event.payload["target_capture"]["capture_method"] == "stub_startup"

def test_start_draft_execution_attempt_is_idempotent_once_started(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )

    first = start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    second = start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)

    artifacts = session.query(models.Artifact).filter(models.Artifact.attempt_id == attempt.attempt_id).all()
    assert first.event_id == second.event_id
    assert first.startup_artifact_ids == second.startup_artifact_ids
    assert len(artifacts) == len(first.startup_artifact_ids)


def test_build_draft_field_plan_creates_field_mappings_and_artifact(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)

    plan = build_draft_field_plan(
        session,
        attempt_id=attempt.attempt_id,
    )

    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_field_plan_created",
    ).one()

    assert plan.attempt_id == attempt.attempt_id
    assert plan.field_count == len(mappings)
    assert Path(plan.artifact_path).exists()
    assert any(entry.field_key == "resume_upload" for entry in plan.entries)
    assert any(entry.field_key == "why_this_role" for entry in plan.entries)
    assert any(entry.field_key == "email" for entry in plan.entries)
    assert event.payload["field_count"] == len(mappings)


def test_build_site_field_overlay_persists_greenhouse_selectors(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)

    overlay = build_site_field_overlay(session, attempt_id=attempt.attempt_id)

    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    email_mapping = next(mapping for mapping in mappings if mapping.field_key == "email")
    resume_mapping = next(mapping for mapping in mappings if mapping.field_key == "resume_upload")

    assert overlay.site_vendor == "greenhouse"
    assert overlay.entry_count == len(mappings)
    assert Path(overlay.artifact_path).exists()
    assert "input[name='email']" in email_mapping.raw_dom_signature
    assert "input[type='file'][name='resume']" in resume_mapping.raw_dom_signature
    assert any(entry.field_key == "why_this_role" and entry.manual_review_required for entry in overlay.entries)


def test_build_site_field_overlay_persists_lever_selectors(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "lever"
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)

    overlay = build_site_field_overlay(session, attempt_id=attempt.attempt_id)

    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    full_name_mapping = next(mapping for mapping in mappings if mapping.field_key == "full_name")
    email_mapping = next(mapping for mapping in mappings if mapping.field_key == "email")
    resume_mapping = next(mapping for mapping in mappings if mapping.field_key == "resume_upload")

    assert overlay.site_vendor == "lever"
    assert overlay.entry_count == len(mappings)
    assert Path(overlay.artifact_path).exists()
    assert "input[name='name']" in full_name_mapping.raw_dom_signature
    assert "input[name='email']" in email_mapping.raw_dom_signature
    assert "input[type='file'][name='resume']" in resume_mapping.raw_dom_signature


def test_open_site_target_page_creates_resolution_bundle_for_greenhouse(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)

    opened = open_site_target_page(session, attempt_id=attempt.attempt_id)

    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_target_opened",
    ).one()

    assert opened.site_vendor == "greenhouse"
    assert opened.browser_profile_key == "apply-main"
    assert opened.capture_method in {"playwright", "http_get", "stub_fallback"}
    assert opened.resolved_count > 0
    assert opened.unresolved_count >= 0
    assert any(entry.field_key == "email" and entry.resolution_status == "resolved" for entry in opened.entries)
    assert any(entry.field_key == "why_this_role" and entry.resolution_status == "manual_review" for entry in opened.entries)
    assert Path(next(artifact.path for artifact in session.query(models.Artifact).filter_by(id=opened.opened_page_artifact_id))).exists()
    assert event.payload["resolved_count"] == opened.resolved_count
    assert event.payload["target_capture"]["capture_method"] in {
        "playwright",
        "http_get",
        "stub_fallback",
    }
    persisted_attempt = session.query(models.ApplicationAttempt).filter_by(id=attempt.attempt_id).one()
    assert "Target opened via" in persisted_attempt.notes
    assert any("resolved_selector" in (mapping.raw_dom_signature or "") for mapping in mappings)


def test_open_site_target_page_persists_screenshot_artifact_for_playwright_capture(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)

    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html",
        lambda **kwargs: (
            "<html><body>playwright</body></html>",
            {"capture_method": "playwright", "status_code": 200, "final_url": kwargs["target_url"]},
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_screenshot_via_playwright",
        lambda **kwargs: b"\x89PNG\r\n\x1a\nplaywright-fake",
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_trace_via_playwright",
        lambda **kwargs: b"PK\x03\x04playwright-trace",
    )

    opened = open_site_target_page(session, attempt_id=attempt.attempt_id)

    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_target_opened",
    ).one()
    screenshot_id = opened.screenshot_artifact_id
    assert screenshot_id is not None
    screenshot_artifact = session.query(models.Artifact).filter_by(id=screenshot_id).one()
    assert screenshot_artifact.artifact_type.value == "screenshot"
    assert Path(screenshot_artifact.path).exists()
    assert event.payload["screenshot_artifact_id"] == screenshot_id
    trace_id = opened.trace_artifact_id
    assert trace_id is not None
    trace_artifact = session.query(models.Artifact).filter_by(id=trace_id).one()
    assert trace_artifact.artifact_type.value == "trace"
    assert Path(trace_artifact.path).exists()
    assert event.payload["trace_artifact_id"] == trace_id
    assert "trace_error" not in event.payload["target_capture"]


def test_open_site_target_page_keeps_flow_running_when_trace_capture_fails(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)

    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html",
        lambda **kwargs: (
            "<html><body>playwright</body></html>",
            {"capture_method": "playwright", "status_code": 200, "final_url": kwargs["target_url"]},
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_screenshot_via_playwright",
        lambda **kwargs: b"\x89PNG\r\n\x1a\nplaywright-fake",
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_trace_via_playwright",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("trace_capture_failed")),
    )

    opened = open_site_target_page(session, attempt_id=attempt.attempt_id)

    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_target_opened",
    ).one()
    assert opened.screenshot_artifact_id is not None
    assert opened.trace_artifact_id is None
    assert event.payload["screenshot_artifact_id"] == opened.screenshot_artifact_id
    assert event.payload["trace_artifact_id"] is None
    assert event.payload["target_capture"]["trace_error"] == "RuntimeError"


def test_evaluate_submit_gate_blocks_submit_when_manual_review_fields_remain(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)

    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)

    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_submit_gate_evaluated",
    ).one()

    assert gate.site_vendor == "greenhouse"
    assert gate.application_state == "review"
    assert gate.attempt_result == "blocked"
    assert gate.failure_code == "submit_gate_blocked"
    assert gate.allow_submit is False
    assert gate.confidence_score < 0.95
    assert "manual_review_required:why_this_role" in gate.stop_reasons
    assert set(gate.required_fields) == {"first_name", "last_name", "email", "resume_upload"}
    assert set(gate.resolved_required_fields) == {"first_name", "last_name", "email", "resume_upload"}
    assert event.payload["allow_submit"] is False
    persisted_attempt = session.query(models.ApplicationAttempt).filter_by(id=attempt.attempt_id).one()
    persisted_application = session.query(models.Application).filter_by(id=persisted_attempt.application_id).one()
    assert persisted_attempt.submit_confidence == gate.confidence_score
    assert persisted_attempt.result == "blocked"
    assert persisted_attempt.failure_code == "submit_gate_blocked"
    assert persisted_application.current_state == "review"

    listed = list_draft_application_attempts(
        session,
        candidate_profile_slug="alex-doe",
        limit=10,
    )
    assert listed[0].attempt_result == "blocked"
    assert listed[0].failure_code == "submit_gate_blocked"
    assert listed[0].submit_confidence == gate.confidence_score


def test_evaluate_submit_gate_supports_lever_and_tracks_required_fields(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "lever"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)

    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)

    assert gate.site_vendor == "lever"
    assert set(gate.required_fields) == {"full_name", "email", "resume_upload"}
    assert set(gate.resolved_required_fields) == {"full_name", "email", "resume_upload"}
    assert gate.failure_code == "submit_gate_blocked"
    assert "manual_review_required:why_this_role" in gate.stop_reasons


def test_execute_guarded_submit_succeeds_after_passing_submit_gate(tmp_path: Path, monkeypatch):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    for mapping in mappings:
        if mapping.field_key == "why_this_role":
            mapping.field_key = "prepared_answer_why_role"
        parsed = json.loads(mapping.raw_dom_signature or "{}")
        parsed["manual_review_required"] = False
        parsed["resolution_status"] = "resolved"
        if not parsed.get("resolved_selector"):
            parsed["resolved_selector"] = "input[name='autofill']"
        mapping.raw_dom_signature = json.dumps(parsed, ensure_ascii=True)
    session.commit()

    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
    assert gate.allow_submit is True

    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_screenshot_via_playwright",
        lambda **kwargs: b"\x89PNG\r\n\x1a\nguarded-submit-fake",
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_trace_via_playwright",
        lambda **kwargs: b"PK\x03\x04guarded-submit-fake",
    )

    submitted = execute_guarded_submit(session, attempt_id=attempt.attempt_id)
    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_submit_executed",
    ).one()
    persisted_attempt = session.query(models.ApplicationAttempt).filter_by(id=attempt.attempt_id).one()
    persisted_application = session.query(models.Application).filter_by(id=persisted_attempt.application_id).one()

    assert submitted.application_state == "applied"
    assert submitted.attempt_result == "success"
    assert submitted.failure_code is None
    assert submitted.allow_submit is True
    assert submitted.submission_mode == "greenhouse_guarded_submit"
    assert submitted.screenshot_artifact_id is not None
    assert submitted.trace_artifact_id is not None
    assert event.payload["screenshot_artifact_id"] == submitted.screenshot_artifact_id
    assert event.payload["trace_artifact_id"] == submitted.trace_artifact_id
    assert event.payload["submit_plan"]["site_vendor"] == "greenhouse"
    assert event.payload["submit_plan"]["submit_button_selectors"]
    assert event.payload["submit_probe"]["probe_available"] is True
    assert "matched_submit_selectors" in event.payload["submit_probe"]
    assert persisted_attempt.result == "success"
    assert persisted_attempt.failure_code is None
    assert persisted_attempt.ended_at is not None
    assert persisted_application.current_state == "applied"
    assert persisted_application.applied_at is not None

    overview_rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        limit=10,
    )
    assert len(overview_rows) == 1
    assert overview_rows[0].submit_interaction_mode in {
        "playwright",
        "simulated_probe_fallback",
    }
    assert overview_rows[0].submit_interaction_clicked is True
    assert overview_rows[0].submit_interaction_status is not None
    assert overview_rows[0].submit_interaction_confirmation_count is not None
    assert overview_rows[0].submit_troubleshoot_event_route is not None
    assert "#event-" in (overview_rows[0].submit_troubleshoot_event_route or "")
    assert overview_rows[0].submit_troubleshoot_artifact_route is not None
    assert "/execution/artifacts/" in (overview_rows[0].submit_troubleshoot_artifact_route or "")

    detail = get_execution_attempt_detail(session, attempt_id=attempt.attempt_id)
    assert detail.submit_interaction_mode in {
        "playwright",
        "simulated_probe_fallback",
    }
    assert detail.submit_interaction_clicked is True
    assert detail.submit_interaction_status is not None
    assert detail.submit_interaction_confirmation_count is not None
    assert detail.submit_troubleshoot_event_route is not None
    assert "#event-" in (detail.submit_troubleshoot_event_route or "")
    assert detail.submit_troubleshoot_artifact_route is not None
    assert "/execution/artifacts/" in (detail.submit_troubleshoot_artifact_route or "")


def test_execute_guarded_submit_blocks_when_submit_gate_disallows(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
    assert gate.allow_submit is False

    try:
        execute_guarded_submit(session, attempt_id=attempt.attempt_id)
        assert False, "expected guarded submit block error"
    except ValueError as exc:
        assert str(exc) == "submit_gate_blocked"

    persisted_attempt = session.query(models.ApplicationAttempt).filter_by(id=attempt.attempt_id).one()
    persisted_application = session.query(models.Application).filter_by(id=persisted_attempt.application_id).one()
    assert persisted_attempt.result == "blocked"
    assert persisted_attempt.failure_code == "submit_gate_blocked"
    assert persisted_application.current_state == "review"


def test_execute_guarded_submit_blocks_when_submit_interaction_fails(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    for mapping in mappings:
        if mapping.field_key == "why_this_role":
            mapping.field_key = "prepared_answer_why_role"
        parsed = json.loads(mapping.raw_dom_signature or "{}")
        parsed["manual_review_required"] = False
        parsed["resolution_status"] = "resolved"
        if not parsed.get("resolved_selector"):
            parsed["resolved_selector"] = "input[name='autofill']"
        mapping.raw_dom_signature = json.dumps(parsed, ensure_ascii=True)
    session.commit()

    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
    assert gate.allow_submit is True

    monkeypatch.setattr(
        "jobbot.execution.service._execute_guarded_submit_interaction",
        lambda **kwargs: {
            "interaction_mode": "playwright",
            "attempted": True,
            "clicked": False,
            "clicked_selector": None,
            "final_url": kwargs["target_url"],
            "matched_confirmation_markers": [],
            "error": "selector_click_failed",
        },
    )

    try:
        execute_guarded_submit(session, attempt_id=attempt.attempt_id)
        assert False, "expected guarded submit interaction failure"
    except ValueError as exc:
        assert str(exc) == "guarded_submit_interaction_failed"

    persisted_attempt = session.query(models.ApplicationAttempt).filter_by(id=attempt.attempt_id).one()
    persisted_application = session.query(models.Application).filter_by(id=persisted_attempt.application_id).one()
    blocked_event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_submit_execution_blocked",
    ).order_by(models.ApplicationEvent.id.desc()).first()

    assert persisted_attempt.result == "blocked"
    assert persisted_attempt.failure_code == "guarded_submit_interaction_failed"
    assert "interaction_status=selector_click_failed" in (persisted_attempt.notes or "")
    assert persisted_application.current_state == "review"
    assert blocked_event is not None
    assert blocked_event.payload["submit_interaction"]["clicked"] is False
    assert blocked_event.payload["submit_interaction"]["error"] == "selector_click_failed"


def test_execute_guarded_submit_blocks_when_submit_selector_probe_fails(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html",
        lambda **kwargs: (
            "<html><body><div data-qa='application-review'>Review</div></body></html>",
            {
                "capture_method": "http_get",
                "status_code": 200,
                "final_url": kwargs["target_url"],
            },
        ),
    )
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    for mapping in mappings:
        if mapping.field_key == "why_this_role":
            mapping.field_key = "prepared_answer_why_role"
        parsed = json.loads(mapping.raw_dom_signature or "{}")
        parsed["manual_review_required"] = False
        parsed["resolution_status"] = "resolved"
        if not parsed.get("resolved_selector"):
            parsed["resolved_selector"] = "input[name='autofill']"
        mapping.raw_dom_signature = json.dumps(parsed, ensure_ascii=True)
    session.commit()
    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
    assert gate.allow_submit is True

    try:
        execute_guarded_submit(session, attempt_id=attempt.attempt_id)
        assert False, "expected guarded submit probe failure"
    except ValueError as exc:
        assert str(exc) == "guarded_submit_probe_failed"

    persisted_attempt = session.query(models.ApplicationAttempt).filter_by(id=attempt.attempt_id).one()
    persisted_application = session.query(models.Application).filter_by(id=persisted_attempt.application_id).one()
    blocked_event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_submit_execution_blocked",
    ).one()
    artifacts = session.query(models.Artifact).filter_by(attempt_id=attempt.attempt_id).all()
    probe_failed_artifact = next(
        artifact for artifact in artifacts if artifact.path.endswith("_guarded_submit_probe_failed.json")
    )
    probe_payload = json.loads(Path(probe_failed_artifact.path).read_text(encoding="utf-8"))

    assert persisted_attempt.result == "blocked"
    assert persisted_attempt.failure_code == "guarded_submit_probe_failed"
    assert "classification=page_changed_still_recognizable" in (persisted_attempt.notes or "")
    assert persisted_application.current_state == "review"
    assert blocked_event.payload["allow_submit"] is False
    assert blocked_event.payload["submit_probe"]["blocked_reason"] == "submit_selector_not_found"
    assert (
        blocked_event.payload["submit_probe"]["failure_classification"]
        == "page_changed_still_recognizable"
    )
    assert probe_payload["submit_probe"]["blocked_reason"] == "submit_selector_not_found"
    assert (
        probe_payload["submit_probe"]["failure_classification"]
        == "page_changed_still_recognizable"
    )

    overview_rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        limit=10,
    )
    assert len(overview_rows) == 1
    assert overview_rows[0].failure_code == "guarded_submit_probe_failed"
    assert overview_rows[0].failure_classification == "page_changed_still_recognizable"
    assert overview_rows[0].submit_remediation_message is not None
    assert "Inspect" in (overview_rows[0].submit_remediation_message or "")
    assert overview_rows[0].submit_remediation_primary_route is not None
    assert "/execution/artifacts/" in (overview_rows[0].submit_remediation_primary_route or "")
    assert overview_rows[0].submit_remediation_secondary_route is not None
    assert "/execution/replay/" in (overview_rows[0].submit_remediation_secondary_route or "")

    detail = get_execution_attempt_detail(session, attempt_id=attempt.attempt_id)
    assert detail.failure_code == "guarded_submit_probe_failed"
    assert detail.failure_classification == "page_changed_still_recognizable"
    assert detail.submit_remediation_message is not None
    assert "Inspect" in (detail.submit_remediation_message or "")
    assert detail.submit_remediation_primary_route is not None
    assert "/execution/artifacts/" in (detail.submit_remediation_primary_route or "")
    assert detail.submit_remediation_secondary_route is not None
    assert "/execution/replay/" in (detail.submit_remediation_secondary_route or "")


def test_execute_guarded_submit_probe_failure_classifies_authentication_session_issue(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html",
        lambda **kwargs: (
            "<html><body><div class='auth-required'>Session expired</div></body></html>",
            {
                "capture_method": "http_get",
                "status_code": 200,
                "final_url": kwargs["target_url"],
                "error": "session_expired_login_required",
            },
        ),
    )
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    for mapping in mappings:
        if mapping.field_key == "why_this_role":
            mapping.field_key = "prepared_answer_why_role"
        parsed = json.loads(mapping.raw_dom_signature or "{}")
        parsed["manual_review_required"] = False
        parsed["resolution_status"] = "resolved"
        if not parsed.get("resolved_selector"):
            parsed["resolved_selector"] = "input[name='autofill']"
        mapping.raw_dom_signature = json.dumps(parsed, ensure_ascii=True)
    session.commit()
    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
    assert gate.allow_submit is True

    try:
        execute_guarded_submit(session, attempt_id=attempt.attempt_id)
        assert False, "expected guarded submit probe failure"
    except ValueError as exc:
        assert str(exc) == "guarded_submit_probe_failed"

    blocked_event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_submit_execution_blocked",
    ).one()
    assert (
        blocked_event.payload["submit_probe"]["failure_classification"]
        == "authentication_session_issue"
    )

    detail = get_execution_attempt_detail(session, attempt_id=attempt.attempt_id)
    assert detail.failure_classification == "authentication_session_issue"
    assert detail.submit_remediation_message is not None
    assert "Re-authenticate" in (detail.submit_remediation_message or "")
    assert detail.submit_remediation_primary_route is not None
    assert "/execution/replay/" in (detail.submit_remediation_primary_route or "")


def test_build_submit_remediation_guidance_covers_known_classifications():
    base_kwargs = {
        "attempt_id": 11,
        "attempt_route": "/execution/attempts/11",
        "replay_route": "/execution/replay/11",
        "primary_action_route": "/execution/replay/11",
        "submit_troubleshoot_event_route": "/execution/attempts/11#event-22",
        "submit_troubleshoot_artifact_route": "/execution/artifacts/22",
    }
    cases = {
        "page_changed_still_recognizable": "Inspect",
        "unsupported_variant": "unsupported",
        "authentication_session_issue": "Re-authenticate",
        "browser_runtime_issue": "browser",
        "unknown_classification": "Review",
    }

    for classification, expected_keyword in cases.items():
        guidance = _build_submit_remediation_guidance(
            failure_code="guarded_submit_probe_failed",
            failure_classification=classification,
            **base_kwargs,
        )
        assert guidance["message"] is not None
        assert expected_keyword.lower() in str(guidance["message"]).lower()
        assert guidance["primary_route"] is not None


def test_execute_guarded_submit_is_idempotent_after_first_success(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    for mapping in mappings:
        if mapping.field_key == "why_this_role":
            mapping.field_key = "prepared_answer_why_role"
        parsed = json.loads(mapping.raw_dom_signature or "{}")
        parsed["manual_review_required"] = False
        parsed["resolution_status"] = "resolved"
        if not parsed.get("resolved_selector"):
            parsed["resolved_selector"] = "input[name='autofill']"
        mapping.raw_dom_signature = json.dumps(parsed, ensure_ascii=True)
    session.commit()
    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
    assert gate.allow_submit is True

    first = execute_guarded_submit(session, attempt_id=attempt.attempt_id)
    second = execute_guarded_submit(session, attempt_id=attempt.attempt_id)

    events = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_submit_executed",
    ).all()
    assert len(events) == 1
    assert second.event_id == first.event_id


def test_execute_guarded_submit_succeeds_for_lever_with_vendor_mode_and_plan(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "lever"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    for mapping in mappings:
        if mapping.field_key == "why_this_role":
            mapping.field_key = "prepared_answer_why_role"
        parsed = json.loads(mapping.raw_dom_signature or "{}")
        parsed["manual_review_required"] = False
        parsed["resolution_status"] = "resolved"
        if not parsed.get("resolved_selector"):
            parsed["resolved_selector"] = "input[name='autofill']"
        mapping.raw_dom_signature = json.dumps(parsed, ensure_ascii=True)
    session.commit()
    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
    assert gate.allow_submit is True

    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_screenshot_via_playwright",
        lambda **kwargs: b"\x89PNG\r\n\x1a\nlever-submit-fake",
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_trace_via_playwright",
        lambda **kwargs: b"PK\x03\x04lever-submit-trace",
    )

    submitted = execute_guarded_submit(session, attempt_id=attempt.attempt_id)
    event = session.query(models.ApplicationEvent).filter_by(
        attempt_id=attempt.attempt_id,
        event_type="draft_submit_executed",
    ).one()

    assert submitted.submission_mode == "lever_guarded_submit"
    assert event.payload["submission_mode"] == "lever_guarded_submit"
    assert event.payload["submit_plan"]["site_vendor"] == "lever"
    assert event.payload["submit_plan"]["submit_button_selectors"]
    assert event.payload["submit_probe"]["probe_available"] is True


def test_execute_guarded_submit_rejects_unsupported_site_even_with_gate_event(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "workday"
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    application = session.query(models.Application).filter_by(id=attempt.application_id).one()
    session.add(
        models.ApplicationEvent(
            application_id=application.id,
            attempt_id=attempt.attempt_id,
            event_type="draft_target_opened",
            message="Synthetic target-open event for unsupported-site guard test.",
            payload={"target_url": "https://example.com/workday/job/1"},
            created_at=models.utcnow(),
        )
    )
    session.add(
        models.ApplicationEvent(
            application_id=application.id,
            attempt_id=attempt.attempt_id,
            event_type="draft_submit_gate_evaluated",
            message="Synthetic gate event for unsupported-site guard test.",
            payload={"allow_submit": True, "confidence_score": 0.99},
            created_at=models.utcnow(),
        )
    )
    session.commit()

    try:
        execute_guarded_submit(session, attempt_id=attempt.attempt_id)
        assert False, "expected unsupported-site guarded-submit error"
    except ValueError as exc:
        assert str(exc) == "guarded_submit_not_supported_for_site"


def test_list_execution_overview_returns_blocked_attempts_with_job_context(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    evaluate_submit_gate(session, attempt_id=attempt.attempt_id)

    rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        blocked_only=False,
        limit=10,
    )
    blocked_rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        blocked_only=True,
        limit=10,
    )

    assert len(rows) == 1
    assert rows[0].job_id == job_id
    assert rows[0].job_title == "Senior Backend Engineer"
    assert rows[0].site_vendor == "greenhouse"
    assert rows[0].attempt_result == "blocked"
    assert rows[0].attempt_route == f"/execution/attempts/{attempt.attempt_id}"
    assert rows[0].replay_route == f"/execution/replay/{attempt.attempt_id}"
    assert rows[0].primary_action_route == f"/execution/replay/{attempt.attempt_id}"
    assert rows[0].primary_action_label == "Open replay bundle"
    assert rows[0].latest_artifact_route is not None
    assert rows[0].latest_artifact_label is not None
    assert rows[0].visual_evidence_route is not None
    assert rows[0].visual_evidence_label == "Open HTML"


def test_list_execution_overview_and_dashboard_support_failure_and_confidence_filters(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    blocked_attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=blocked_attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=blocked_attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=blocked_attempt.attempt_id)
    open_site_target_page(session, attempt_id=blocked_attempt.attempt_id)
    gate = evaluate_submit_gate(session, attempt_id=blocked_attempt.attempt_id)

    _ = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )

    filtered_rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        failure_code="submit_gate_blocked",
        max_submit_confidence=gate.confidence_score + 0.01,
        limit=10,
    )
    assert len(filtered_rows) == 1
    assert filtered_rows[0].attempt_id == blocked_attempt.attempt_id
    assert filtered_rows[0].failure_code == "submit_gate_blocked"
    assert filtered_rows[0].failure_classification == "unknown_classification"
    assert filtered_rows[0].submit_confidence == gate.confidence_score

    classification_rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        failure_classification="unknown_classification",
        limit=10,
    )
    assert len(classification_rows) == 1
    assert classification_rows[0].attempt_id == blocked_attempt.attempt_id

    empty_rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        failure_code="submit_gate_blocked",
        max_submit_confidence=max(gate.confidence_score - 0.2, 0.0),
        limit=10,
    )
    assert empty_rows == []

    dashboard = get_execution_dashboard(
        session,
        candidate_profile_slug="alex-doe",
        failure_code="submit_gate_blocked",
        max_submit_confidence=gate.confidence_score + 0.01,
        limit=10,
    )
    assert dashboard.total_attempts == 1
    assert dashboard.blocked_attempts == 1
    assert dashboard.manual_review_blocked_attempts == 0
    assert dashboard.pending_attempts == 0
    assert dashboard.blocked_failure_counts == {"submit_gate_blocked": 1}
    assert dashboard.blocked_failure_classification_counts == {"unknown_classification": 1}
    assert dashboard.recent_attempts[0].attempt_id == blocked_attempt.attempt_id
    assert any("failure_code=submit_gate_blocked" in action for action in dashboard.recommended_actions)


def test_execution_overview_and_dashboard_support_manual_review_only_filter(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    blocked_attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=blocked_attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=blocked_attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=blocked_attempt.attempt_id)
    open_site_target_page(session, attempt_id=blocked_attempt.attempt_id)
    evaluate_submit_gate(session, attempt_id=blocked_attempt.attempt_id)

    manual_attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    persisted_manual = session.query(models.ApplicationAttempt).filter_by(id=manual_attempt.attempt_id).one()
    persisted_manual.result = "blocked"
    persisted_manual.failure_code = "manual_review_required:unresolved_required"
    session.commit()

    manual_rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        manual_review_only=True,
        limit=10,
    )
    assert len(manual_rows) == 1
    assert manual_rows[0].attempt_id == manual_attempt.attempt_id
    assert manual_rows[0].failure_code == "manual_review_required:unresolved_required"

    dashboard = get_execution_dashboard(
        session,
        candidate_profile_slug="alex-doe",
        manual_review_only=True,
        limit=10,
    )
    assert dashboard.total_attempts == 1
    assert dashboard.blocked_attempts == 1
    assert dashboard.manual_review_blocked_attempts == 1
    assert dashboard.blocked_failure_counts == {"manual_review_required:unresolved_required": 1}
    assert dashboard.blocked_failure_classification_counts == {"unknown_classification": 1}
    assert dashboard.recent_attempts[0].attempt_id == manual_attempt.attempt_id
    assert any("manual-review-required failures only" in action for action in dashboard.recommended_actions)


def test_execution_overview_supports_submit_confidence_sort_and_invalid_sort(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    blocked_attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=blocked_attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=blocked_attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=blocked_attempt.attempt_id)
    open_site_target_page(session, attempt_id=blocked_attempt.attempt_id)
    gate = evaluate_submit_gate(session, attempt_id=blocked_attempt.attempt_id)
    assert gate.confidence_score is not None

    pending_attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )

    rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        sort_by="submit_confidence",
        descending=False,
        limit=10,
    )
    assert len(rows) == 2
    assert rows[0].attempt_id == blocked_attempt.attempt_id
    assert rows[1].attempt_id == pending_attempt.attempt_id
    assert rows[0].submit_confidence == gate.confidence_score
    assert rows[1].submit_confidence is None

    try:
        _ = list_execution_overview(
            session,
            candidate_profile_slug="alex-doe",
            sort_by="not_a_real_sort_key",
            limit=10,
        )
    except ValueError as exc:
        assert str(exc) == "invalid_execution_overview_sort"
    else:
        raise AssertionError("invalid sort key should raise ValueError")


def test_get_execution_artifact_detail_returns_safe_json_preview(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    artifact = next(
        item
        for item in session.query(models.Artifact)
        .filter_by(attempt_id=attempt.attempt_id)
        .order_by(models.Artifact.id)
        .all()
        if item.artifact_type.value == "model_io"
    )

    detail = get_execution_artifact_detail(session, artifact_id=artifact.id)

    assert detail.artifact_id == artifact.id
    assert detail.attempt_id == attempt.attempt_id
    assert detail.exists is True
    assert detail.raw_route == f"/execution/artifacts/{artifact.id}/raw"
    assert detail.launch_route == f"/execution/artifacts/{artifact.id}/launch"
    assert detail.launch_label == "Open text"
    assert detail.launch_target == "open_text"
    assert detail.preview_kind == "json"
    assert detail.preview_text is not None
    assert "candidate_profile_slug" in detail.preview_text
    assert detail.preview_truncated is False


def test_get_execution_artifact_detail_suppresses_binary_preview(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )
    screenshot_path = tmp_path / "capture.png"
    screenshot_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    artifact = models.Artifact(
        attempt_id=attempt.attempt_id,
        artifact_type=models.ArtifactType.SCREENSHOT,
        path=str(screenshot_path),
        size_bytes=screenshot_path.stat().st_size,
    )
    session.add(artifact)
    session.commit()

    detail = get_execution_artifact_detail(session, artifact_id=artifact.id)

    assert detail.preview_kind == "binary_image"
    assert detail.preview_text is None
    assert detail.preview_truncated is False
    assert detail.launch_route == f"/execution/artifacts/{artifact.id}/launch"
    assert detail.launch_label == "View image"
    assert detail.launch_target == "inspect_image"


def test_get_execution_attempt_detail_returns_events_and_artifacts(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    evaluate_submit_gate(session, attempt_id=attempt.attempt_id)

    detail = get_execution_attempt_detail(session, attempt_id=attempt.attempt_id)

    assert detail.attempt_id == attempt.attempt_id
    assert detail.attempt_result == "blocked"
    assert detail.failure_code == "submit_gate_blocked"
    assert len(detail.events) >= 6
    assert detail.events[-1].event_type == "draft_submit_gate_evaluated"
    assert detail.events[-1].artifact_routes
    assert all("/execution/artifacts/" in route for route in detail.events[-1].artifact_routes)
    assert len(detail.artifacts) >= 6
    assert any(artifact.artifact_type == "html_snapshot" for artifact in detail.artifacts)
    html_artifact = next(artifact for artifact in detail.artifacts if artifact.artifact_type == "html_snapshot")
    assert html_artifact.inspect_route == f"/execution/artifacts/{html_artifact.artifact_id}"
    assert html_artifact.raw_route == f"/execution/artifacts/{html_artifact.artifact_id}/raw"
    assert html_artifact.launch_route == f"/execution/artifacts/{html_artifact.artifact_id}/launch"
    assert html_artifact.launch_label == "Open HTML"
    assert html_artifact.launch_target == "open_html"


def test_get_execution_replay_bundle_returns_replay_assets_and_actions(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    evaluate_submit_gate(session, attempt_id=attempt.attempt_id)

    replay = get_execution_replay_bundle(session, attempt_id=attempt.attempt_id)

    assert replay.attempt_id == attempt.attempt_id
    assert replay.attempt_result == "blocked"
    assert replay.latest_event_type == "draft_submit_gate_evaluated"
    assert replay.startup_dir is not None
    assert replay.target_url is not None
    assert any(asset.label == "startup_context" and asset.exists for asset in replay.assets)
    assert any(asset.label == "submit_gate" and asset.artifact_id is not None for asset in replay.assets)
    assert any(
        asset.label == "startup_context"
        and asset.inspect_route == f"/execution/artifacts/{asset.artifact_id}"
        and asset.raw_route == f"/execution/artifacts/{asset.artifact_id}/raw"
        and asset.launch_route == f"/execution/artifacts/{asset.artifact_id}/launch"
        and asset.launch_label == "Open text"
        and asset.launch_target == "open_text"
        and asset.openable_locally
        and asset.open_hint == "open_text"
        for asset in replay.assets
    )
    assert any("Resolve manual-review" in action for action in replay.recommended_actions)


def test_get_execution_replay_bundle_exposes_playwright_trace_asset_launch_metadata(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html",
        lambda **kwargs: (
            "<html><body>playwright</body></html>",
            {"capture_method": "playwright", "status_code": 200, "final_url": kwargs["target_url"]},
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_screenshot_via_playwright",
        lambda **kwargs: b"\x89PNG\r\n\x1a\nplaywright-fake",
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_trace_via_playwright",
        lambda **kwargs: b"PK\x03\x04playwright-trace",
    )
    open_site_target_page(session, attempt_id=attempt.attempt_id)

    replay = get_execution_replay_bundle(session, attempt_id=attempt.attempt_id)
    trace_asset = next(asset for asset in replay.assets if asset.label == "opened_target_trace")

    assert trace_asset.exists
    assert trace_asset.artifact_type == "trace"
    assert trace_asset.openable_locally
    assert trace_asset.open_hint == "open_trace"
    assert trace_asset.launch_label == "Download trace"
    assert trace_asset.launch_target == "download_trace"
    assert trace_asset.launch_route is not None


def test_get_execution_replay_bundle_includes_guarded_submit_assets_after_success(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    for mapping in mappings:
        if mapping.field_key == "why_this_role":
            mapping.field_key = "prepared_answer_why_role"
        parsed = json.loads(mapping.raw_dom_signature or "{}")
        parsed["manual_review_required"] = False
        parsed["resolution_status"] = "resolved"
        if not parsed.get("resolved_selector"):
            parsed["resolved_selector"] = "input[name='autofill']"
        mapping.raw_dom_signature = json.dumps(parsed, ensure_ascii=True)
    session.commit()
    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
    assert gate.allow_submit is True

    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_screenshot_via_playwright",
        lambda **kwargs: b"\x89PNG\r\n\x1a\nguarded-submit-fake",
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_trace_via_playwright",
        lambda **kwargs: b"PK\x03\x04guarded-submit-trace",
    )
    execute_guarded_submit(session, attempt_id=attempt.attempt_id)

    replay = get_execution_replay_bundle(session, attempt_id=attempt.attempt_id)

    assert replay.attempt_result == "success"
    assert replay.latest_event_type == "draft_submit_executed"
    assert any(asset.label == "guarded_submit" and asset.artifact_id is not None for asset in replay.assets)
    assert any(
        asset.label == "guarded_submit_screenshot"
        and asset.artifact_type == "screenshot"
        and asset.launch_label == "View image"
        and asset.launch_target == "inspect_image"
        for asset in replay.assets
    )
    assert any(
        asset.label == "guarded_submit_trace"
        and asset.artifact_type == "trace"
        and asset.launch_label == "Download trace"
        and asset.launch_target == "download_trace"
        for asset in replay.assets
    )
    assert any("Inspect guarded_submit artifacts" in action for action in replay.recommended_actions)


def test_list_execution_overview_prefers_latest_visual_evidence_after_guarded_submit(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt.attempt_id).all()
    for mapping in mappings:
        if mapping.field_key == "why_this_role":
            mapping.field_key = "prepared_answer_why_role"
        parsed = json.loads(mapping.raw_dom_signature or "{}")
        parsed["manual_review_required"] = False
        parsed["resolution_status"] = "resolved"
        if not parsed.get("resolved_selector"):
            parsed["resolved_selector"] = "input[name='autofill']"
        mapping.raw_dom_signature = json.dumps(parsed, ensure_ascii=True)
    session.commit()
    gate = evaluate_submit_gate(session, attempt_id=attempt.attempt_id)
    assert gate.allow_submit is True

    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_screenshot_via_playwright",
        lambda **kwargs: b"\x89PNG\r\n\x1a\nguarded-submit-fake",
    )
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_trace_via_playwright",
        lambda **kwargs: b"PK\x03\x04guarded-submit-trace",
    )
    execute_guarded_submit(session, attempt_id=attempt.attempt_id)

    rows = list_execution_overview(
        session,
        candidate_profile_slug="alex-doe",
        limit=10,
    )

    assert len(rows) == 1
    assert rows[0].attempt_result == "success"
    assert rows[0].latest_event_type == "draft_submit_executed"
    assert rows[0].visual_evidence_label == "Download trace"


def test_get_execution_dashboard_returns_summary_counts(tmp_path: Path):
    session = make_session()
    job_id, _ = seed_candidate_job_and_ready_snapshot(session, tmp_path)
    candidate = session.query(models.CandidateProfile).filter_by(slug="alex-doe").one()
    candidate.personal_details = {
        "email": "alex@example.com",
        "phone": "+1-555-0100",
        "location": "Remote",
        "linkedin_url": "https://www.linkedin.com/in/alex-doe",
    }
    job = session.query(models.Job).filter_by(id=job_id).one()
    job.ats_vendor = "greenhouse"
    browser = BrowserProfile(
        profile_key="apply-main",
        profile_type=BrowserProfileType.APPLICATION,
        display_name="Apply Main",
        storage_path="/profiles/apply-main",
        session_health="healthy",
        validation_details={"reasons": ["session_healthy"]},
    )
    session.add(browser)
    session.commit()

    blocked_attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=blocked_attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=blocked_attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=blocked_attempt.attempt_id)
    open_site_target_page(session, attempt_id=blocked_attempt.attempt_id)
    evaluate_submit_gate(session, attempt_id=blocked_attempt.attempt_id)

    pending_attempt = bootstrap_draft_application_attempt(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )

    dashboard = get_execution_dashboard(
        session,
        candidate_profile_slug="alex-doe",
        limit=10,
    )

    assert dashboard.candidate_profile_slug == "alex-doe"
    assert dashboard.total_attempts == 2
    assert dashboard.blocked_attempts == 1
    assert dashboard.manual_review_blocked_attempts == 0
    assert dashboard.pending_attempts == 1
    assert dashboard.review_state_attempts == 1
    assert dashboard.replay_ready_attempts == 1
    assert dashboard.blocked_failure_counts == {"submit_gate_blocked": 1}
    assert dashboard.blocked_failure_classification_counts == {"unknown_classification": 1}
    assert dashboard.blocked_recent_attempts[0].attempt_id == blocked_attempt.attempt_id
    assert any(row.attempt_id == pending_attempt.attempt_id for row in dashboard.recent_attempts)
    assert any("Resolve blocked guarded attempts" in action for action in dashboard.recommended_actions)


def test_capture_target_page_html_uses_http_get_when_available(monkeypatch):
    class FakeResponse:
        status = 200
        url = "https://example.com/jobs/42"
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self, _max_bytes):
            return b"<html><body>ok</body></html>"

    monkeypatch.setattr(
        "jobbot.execution.service.urlopen",
        lambda request, timeout=0: FakeResponse(),
    )

    html, metadata = _capture_target_page_html(
        target_url="https://example.com/jobs/42",
        job_title="Senior Backend Engineer",
        browser_profile_key="apply-main",
        candidate_profile_slug="alex-doe",
    )

    assert "<body>ok</body>" in html
    assert metadata["capture_method"] == "http_get"
    assert metadata["status_code"] == 200


def test_selector_matches_html_detects_attribute_and_id_signatures():
    html = """
    <html><body>
      <button id="submit_app" type="submit" data-qa="submit-application" class="postings-btn--large">Apply</button>
      <div class="application-review"></div>
    </body></html>
    """
    assert _selector_matches_html("button[type='submit']", html)
    assert _selector_matches_html("button#submit_app", html)
    assert _selector_matches_html("button[data-qa='submit-application']", html)
    assert _selector_matches_html(".application-review", html)
    assert not _selector_matches_html("button[data-qa='not-there']", html)


def test_capture_target_page_html_falls_back_to_stub_on_error(monkeypatch):
    monkeypatch.setattr(
        "jobbot.execution.service.urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(OSError("offline")),
    )

    html, metadata = _capture_target_page_html(
        target_url="https://example.com/jobs/42",
        job_title="Senior Backend Engineer",
        browser_profile_key="apply-main",
        candidate_profile_slug="alex-doe",
    )

    assert "Opened target URL" in html
    assert metadata["capture_method"] == "stub_fallback"
    assert metadata["error"].startswith("os_error:")


def test_capture_target_page_html_prefers_playwright_capture_when_available(monkeypatch):
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html_via_playwright",
        lambda **kwargs: (
            "<html><body>playwright</body></html>",
            {
                "capture_method": "playwright",
                "status_code": 200,
                "final_url": kwargs["target_url"],
            },
        ),
    )
    monkeypatch.setattr(
        "jobbot.execution.service.urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(AssertionError("http should not be used")),
    )

    html, metadata = _capture_target_page_html(
        target_url="https://example.com/jobs/42",
        job_title="Senior Backend Engineer",
        browser_profile_key="apply-main",
        candidate_profile_slug="alex-doe",
    )

    assert "<body>playwright</body>" in html
    assert metadata["capture_method"] == "playwright"
    assert metadata["status_code"] == 200


def test_capture_target_page_html_falls_back_to_http_after_playwright_error(monkeypatch):
    class FakeResponse:
        status = 200
        url = "https://example.com/jobs/42"
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self, _max_bytes):
            return b"<html><body>http-fallback</body></html>"

    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html_via_playwright",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("playwright_not_available")),
    )
    monkeypatch.setattr(
        "jobbot.execution.service.urlopen",
        lambda request, timeout=0: FakeResponse(),
    )

    html, metadata = _capture_target_page_html(
        target_url="https://example.com/jobs/42",
        job_title="Senior Backend Engineer",
        browser_profile_key="apply-main",
        candidate_profile_slug="alex-doe",
    )

    assert "<body>http-fallback</body>" in html
    assert metadata["capture_method"] == "http_get"
    assert metadata.get("playwright_error") == "RuntimeError"
