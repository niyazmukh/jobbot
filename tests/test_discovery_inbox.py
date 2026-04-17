import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import CandidateFact, CandidateProfile
from jobbot.discovery.greenhouse.adapter import parse_greenhouse_board_payload
from jobbot.discovery.inbox import get_inbox_job_detail, get_ready_to_apply_job_detail, list_inbox_jobs, list_ready_to_apply_jobs
from jobbot.discovery.ingestion import ingest_discovery_batch
from jobbot.eligibility.service import materialize_application_eligibility
from jobbot.execution.service import (
    bootstrap_draft_application_attempt,
    build_draft_field_plan,
    build_site_field_overlay,
    evaluate_submit_gate,
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


def load_greenhouse_batch():
    payload = json.loads(
        Path("fixtures/discovery/greenhouse/board_jobs_sample.json").read_text(encoding="utf-8")
    )
    return parse_greenhouse_board_payload(
        company_name="Example Corp",
        board_url="https://boards.greenhouse.io/example",
        payload=payload,
    )


def test_list_inbox_jobs_returns_persisted_jobs():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    rows = list_inbox_jobs(session, limit=10)

    assert len(rows) == 2
    assert rows[0].company_name == "example corp"
    assert rows[0].source_count == 1


def test_list_inbox_jobs_filters_by_status():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    rows = list_inbox_jobs(session, limit=10, status="discovered")

    assert len(rows) == 2


def test_get_inbox_job_detail_returns_attached_sources():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    rows = list_inbox_jobs(session, limit=10)
    detail = get_inbox_job_detail(session, rows[0].job_id)

    assert detail is not None
    assert detail.job_id == rows[0].job_id
    assert len(detail.sources) == 1
    assert detail.sources[0].source_type == "ats_board"
    assert "board_url" in detail.sources[0].metadata_json


def test_list_inbox_jobs_filters_by_ats_vendor_and_remote_type():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    remote_rows = list_inbox_jobs(session, limit=10, ats_vendor="greenhouse", remote_type="remote")
    onsite_rows = list_inbox_jobs(session, limit=10, ats_vendor="greenhouse", remote_type="onsite")

    assert len(remote_rows) == 1
    assert len(onsite_rows) == 1


def test_list_inbox_jobs_supports_offset_and_sorting():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    rows = list_inbox_jobs(session, limit=1, offset=1, sort_by="title", descending=False)

    assert len(rows) == 1
    assert rows[0].title == "Senior Backend Engineer"


def test_inbox_reads_can_include_score_summary():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
    )
    session.add(candidate)
    session.flush()
    session.add(
        CandidateFact(
            candidate_profile_id=candidate.id,
            fact_key="skills-001",
            category="skills",
            content="Senior backend engineer with Python SQL AWS experience and 8 years experience",
        )
    )
    first_job = session.query(models.Job).order_by(models.Job.id).first()
    first_job.requirements_structured = {
        "required_skills": ["python", "sql", "aws"],
        "seniority_signals": ["senior"],
        "required_years_experience": 5,
    }
    session.commit()
    score_job_for_candidate(session, first_job.id, "alex-doe")

    rows = list_inbox_jobs(session, limit=10, candidate_profile_slug="alex-doe")
    detail = get_inbox_job_detail(session, first_job.id, candidate_profile_slug="alex-doe")

    scored_row = next(row for row in rows if row.job_id == first_job.id)
    assert scored_row.score_summary is not None
    assert scored_row.score_summary["candidate_profile_slug"] == "alex-doe"
    assert detail is not None
    assert detail.score_summary is not None
    assert detail.score_summary["blocked"] is False


def test_inbox_reads_can_include_prepared_summary(tmp_path: Path):
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())
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
    first_job = session.query(models.Job).order_by(models.Job.id).first()
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

    rows = list_inbox_jobs(session, limit=10, candidate_profile_slug="alex-doe")
    detail = get_inbox_job_detail(session, first_job.id, candidate_profile_slug="alex-doe")

    assert rows[0].prepared_summary is not None
    assert rows[0].prepared_summary["document_count"] == 1
    assert rows[0].prepared_summary["answer_count"] >= 2
    assert detail is not None
    assert detail.prepared_summary is not None
    assert detail.prepared_summary["pending_document_review"] is True
    assert detail.prepared_summary["preparation_state"] == "pending_review"
    assert detail.application_readiness is not None
    assert detail.application_readiness["state"] == "pending_review"


def test_list_inbox_jobs_can_filter_and_sort_by_preparation_state(tmp_path: Path):
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())
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
    jobs = session.query(models.Job).order_by(models.Job.id).all()
    for job in jobs:
        job.requirements_structured = {
            "required_skills": ["python", "sql", "aws"],
            "seniority_signals": ["senior"],
            "required_years_experience": 5,
        }
    session.commit()
    score_job_for_candidate(session, jobs[0].id, "alex-doe")
    prepare_job_for_candidate(
        session,
        job_id=jobs[0].id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )

    pending_rows = list_inbox_jobs(
        session,
        limit=10,
        candidate_profile_slug="alex-doe",
        preparation_state="pending_review",
    )
    sorted_rows = list_inbox_jobs(
        session,
        limit=10,
        candidate_profile_slug="alex-doe",
        sort_by="preparation_state",
        descending=True,
    )

    assert len(pending_rows) == 1
    assert pending_rows[0].prepared_summary["preparation_state"] == "pending_review"
    assert sorted_rows[0].job_id == jobs[0].id


def test_list_inbox_jobs_can_filter_and_sort_by_application_readiness(tmp_path: Path):
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())
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
    jobs = session.query(models.Job).order_by(models.Job.id).all()
    for job in jobs:
        job.requirements_structured = {
            "required_skills": ["python", "sql", "aws"],
            "seniority_signals": ["senior"],
            "required_years_experience": 5,
        }
    session.commit()
    score_job_for_candidate(session, jobs[0].id, "alex-doe")
    score_job_for_candidate(session, jobs[1].id, "alex-doe")
    prepare_job_for_candidate(
        session,
        job_id=jobs[0].id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )

    pending_rows = list_inbox_jobs(
        session,
        limit=10,
        candidate_profile_slug="alex-doe",
        application_readiness="pending_review",
    )
    sorted_rows = list_inbox_jobs(
        session,
        limit=10,
        candidate_profile_slug="alex-doe",
        sort_by="application_readiness",
        descending=True,
    )

    assert len(pending_rows) == 1
    assert pending_rows[0].application_readiness["state"] == "pending_review"
    assert sorted_rows[0].application_readiness["state"] == "pending_review"


def test_ready_to_apply_helpers_return_only_ready_jobs(tmp_path: Path):
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())
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
    jobs = session.query(models.Job).order_by(models.Job.id).all()
    for job in jobs:
        job.requirements_structured = {
            "required_skills": ["python", "sql", "aws"],
            "seniority_signals": ["senior"],
            "required_years_experience": 5,
        }
    session.commit()
    score_job_for_candidate(session, jobs[0].id, "alex-doe")
    score_job_for_candidate(session, jobs[1].id, "alex-doe")
    prepare_job_for_candidate(
        session,
        job_id=jobs[0].id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    generated_document = session.query(models.GeneratedDocument).one()
    generated_document.review_status = "approved"
    session.commit()

    ready_rows = list_ready_to_apply_jobs(session, candidate_profile_slug="alex-doe", limit=10)
    ready_detail = get_ready_to_apply_job_detail(
        session,
        job_id=jobs[0].id,
        candidate_profile_slug="alex-doe",
    )
    missing_detail = get_ready_to_apply_job_detail(
        session,
        job_id=jobs[1].id,
        candidate_profile_slug="alex-doe",
    )

    assert len(ready_rows) == 1
    assert ready_rows[0].application_readiness["state"] == "ready_to_apply"
    assert ready_detail is not None
    assert ready_detail.application_readiness["state"] == "ready_to_apply"
    assert missing_detail is None


def test_inbox_reads_surface_blocked_execution_summary_and_readiness(tmp_path: Path):
    session = make_session()
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
    first_job.requirements_structured = {
        "required_skills": ["python", "sql", "aws"],
        "seniority_signals": ["senior"],
        "required_years_experience": 5,
    }
    first_job.ats_vendor = "greenhouse"
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
    generated_document = session.query(models.GeneratedDocument).one()
    generated_document.review_status = "approved"
    session.commit()

    materialize_application_eligibility(session, job_id=first_job.id, candidate_profile_slug="alex-doe")
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=first_job.id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    evaluate_submit_gate(session, attempt_id=attempt.attempt_id)

    rows = list_inbox_jobs(session, limit=10, candidate_profile_slug="alex-doe")
    detail = get_inbox_job_detail(session, first_job.id, candidate_profile_slug="alex-doe")

    assert rows[0].execution_summary is not None
    assert rows[0].execution_summary["attempt_result"] == "blocked"
    assert rows[0].execution_summary["failure_code"] == "submit_gate_blocked"
    assert rows[0].application_readiness["state"] == "execution_blocked"
    assert "submit_gate_blocked" in rows[0].application_readiness["reasons"]
    assert detail is not None
    assert detail.execution_summary is not None
    assert detail.execution_summary["application_state"] == "review"
    assert detail.application_readiness["state"] == "execution_blocked"


def test_list_inbox_jobs_can_filter_and_sort_by_execution_state(tmp_path: Path):
    session = make_session()
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
    jobs = session.query(models.Job).order_by(models.Job.id).all()
    for job in jobs:
        job.requirements_structured = {
            "required_skills": ["python", "sql", "aws"],
            "seniority_signals": ["senior"],
            "required_years_experience": 5,
        }
    jobs[0].ats_vendor = "greenhouse"
    session.commit()
    score_job_for_candidate(session, jobs[0].id, "alex-doe")
    score_job_for_candidate(session, jobs[1].id, "alex-doe")
    prepare_job_for_candidate(
        session,
        job_id=jobs[0].id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    generated_document = session.query(models.GeneratedDocument).one()
    generated_document.review_status = "approved"
    session.commit()

    materialize_application_eligibility(session, job_id=jobs[0].id, candidate_profile_slug="alex-doe")
    attempt = bootstrap_draft_application_attempt(
        session,
        job_id=jobs[0].id,
        candidate_profile_slug="alex-doe",
        browser_profile_key="apply-main",
    )
    start_draft_execution_attempt(session, attempt_id=attempt.attempt_id)
    build_draft_field_plan(session, attempt_id=attempt.attempt_id)
    build_site_field_overlay(session, attempt_id=attempt.attempt_id)
    open_site_target_page(session, attempt_id=attempt.attempt_id)
    evaluate_submit_gate(session, attempt_id=attempt.attempt_id)

    blocked_rows = list_inbox_jobs(
        session,
        limit=10,
        candidate_profile_slug="alex-doe",
        execution_state="blocked",
    )
    sorted_rows = list_inbox_jobs(
        session,
        limit=10,
        candidate_profile_slug="alex-doe",
        sort_by="execution_state",
        descending=True,
    )

    assert len(blocked_rows) == 1
    assert blocked_rows[0].execution_summary["attempt_result"] == "blocked"
    assert sorted_rows[0].job_id == jobs[0].id
