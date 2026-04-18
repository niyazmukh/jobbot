import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from jobbot.api.app import app, get_db_session
import jobbot.execution.service as execution_service
from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import CandidateFact, CandidateProfile
from jobbot.discovery.greenhouse.adapter import parse_greenhouse_board_payload
from jobbot.discovery.ingestion import ingest_discovery_batch
from jobbot.models.enums import BrowserProfileType
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


def test_list_jobs_endpoint_returns_inbox_rows():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/api/jobs?limit=10")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    assert payload[0]["company_name"] == "example corp"


def test_health_endpoint_returns_ok():
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_job_detail_endpoint_returns_sources():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        list_response = client.get("/api/jobs?limit=1")
        job_id = list_response.json()[0]["job_id"]
        response = client.get(f"/api/jobs/{job_id}")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == job_id
    assert len(payload["sources"]) == 1
    assert "board_url" in payload["sources"][0]["metadata_json"]


def test_list_jobs_endpoint_filters_by_remote_type():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/api/jobs?limit=10&ats_vendor=greenhouse&remote_type=remote")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["remote_type"] == "remote"


def test_list_jobs_endpoint_supports_offset_and_sorting():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/api/jobs?limit=1&offset=1&sort_by=title&descending=false")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["title"] == "Senior Backend Engineer"


def test_job_score_endpoint_returns_persisted_score():
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

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(f"/api/jobs/{first_job.id}/scores/alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == first_job.id
    assert payload["candidate_profile_slug"] == "alex-doe"
    assert payload["score_json"]["blocked"] is False


def test_job_list_and_detail_endpoints_can_include_score_summary():
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

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        list_response = client.get("/api/jobs?candidate_profile_slug=alex-doe")
        detail_response = client.get(f"/api/jobs/{first_job.id}?candidate_profile_slug=alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert list_response.status_code == 200
    scored_row = next(item for item in list_response.json() if item["job_id"] == first_job.id)
    assert scored_row["score_summary"]["candidate_profile_slug"] == "alex-doe"
    assert detail_response.status_code == 200
    assert detail_response.json()["score_summary"]["blocked"] is False


def test_inbox_ui_routes_render_html():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        inbox_response = client.get("/inbox")
        detail_response = client.get("/inbox/jobs/1")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert inbox_response.status_code == 200
    assert "JobBot Inbox" in inbox_response.text
    assert detail_response.status_code == 200
    assert "Sources" in detail_response.text


def test_review_queue_api_and_ui_routes_work():
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        target_preferences={"preferred_locations": ["Toronto"], "remote": False},
    )
    session.add(candidate)
    session.flush()
    session.add(
        CandidateFact(
            candidate_profile_id=candidate.id,
            fact_key="skills-001",
            category="skills",
            content="Customer support specialist with Excel and CRM experience",
        )
    )
    first_job = session.query(models.Job).order_by(models.Job.id).first()
    first_job.requirements_structured = {
        "required_skills": ["python", "machine learning", "aws"],
        "seniority_signals": ["principal"],
        "required_years_experience": 8,
    }
    session.commit()
    score_job_for_candidate(session, first_job.id, "alex-doe")

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        create_response = client.post(f"/api/review-queue/jobs/{first_job.id}/scores/alex-doe")
        list_response = client.get("/api/review-queue")
        ui_response = client.get("/review-queue")
        status_response = client.post(f"/api/review-queue/{create_response.json()['id']}/status/approved")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert create_response.status_code == 200
    assert create_response.json()["entity_type"] == "job_score"
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert ui_response.status_code == 200
    assert "JobBot Review Queue" in ui_response.text
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "approved"


def test_review_status_api_rematerializes_eligibility_snapshot(tmp_path):
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
    document_review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        update_response = client.post(f"/api/review-queue/{document_review.id}/status/approved")
        eligibility_response = client.get(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert update_response.status_code == 200
    assert update_response.json()["status"] == "approved"
    assert eligibility_response.status_code == 200
    assert eligibility_response.json()["prepared_summary"]["preparation_state"] == "ready"


def test_prepared_job_endpoint_returns_persisted_outputs(tmp_path):
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
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

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(f"/api/jobs/{first_job.id}/prepared/alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == first_job.id
    assert payload["candidate_profile_slug"] == "alex-doe"
    assert len(payload["documents"]) == 1
    assert len(payload["answers"]) >= 2


def test_inbox_ui_surfaces_prepared_summary(tmp_path):
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

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        inbox_response = client.get("/inbox?candidate_profile_slug=alex-doe")
        detail_response = client.get(f"/inbox/jobs/{first_job.id}?candidate_profile_slug=alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert inbox_response.status_code == 200
    assert "Prepared docs: 1" in inbox_response.text
    assert detail_response.status_code == 200
    assert "Prepared Outputs" in detail_response.text


def test_api_jobs_can_filter_by_preparation_state(tmp_path):
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

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(
            "/api/jobs?candidate_profile_slug=alex-doe&preparation_state=pending_review&sort_by=preparation_state"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["prepared_summary"]["preparation_state"] == "pending_review"


def test_api_jobs_can_filter_by_application_readiness(tmp_path):
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

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(
            "/api/jobs?candidate_profile_slug=alex-doe&application_readiness=pending_review&sort_by=application_readiness"
        )
        detail_response = client.get(f"/inbox/jobs/{jobs[0].id}?candidate_profile_slug=alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["application_readiness"]["state"] == "pending_review"
    assert detail_response.status_code == 200
    assert "Application Readiness" in detail_response.text


def test_ready_to_apply_api_and_html_helpers_return_only_ready_jobs(tmp_path):
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

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        list_response = client.get("/api/jobs/ready-to-apply/alex-doe")
        detail_response = client.get(f"/api/jobs/{jobs[0].id}/ready-to-apply/alex-doe")
        html_response = client.get("/ready-to-apply/alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert list_response.status_code == 200
    payload = list_response.json()
    assert len(payload) == 1
    assert payload[0]["application_readiness"]["state"] == "ready_to_apply"
    assert detail_response.status_code == 200
    assert detail_response.json()["application_readiness"]["state"] == "ready_to_apply"
    assert html_response.status_code == 200
    assert "ready_to_apply" in html_response.text


def test_eligibility_api_endpoints_materialize_and_list(tmp_path):
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
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        materialize_response = client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        detail_response = client.get(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        list_response = client.get("/api/eligibility/alex-doe?ready_only=true")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert materialize_response.status_code == 200
    assert materialize_response.json()["readiness_state"] == "ready_to_apply"
    assert detail_response.status_code == 200
    assert detail_response.json()["ready"] is True
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1


def test_execution_api_bootstraps_and_lists_draft_attempts(tmp_path):
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
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        materialize_response = client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe"
        )
        list_response = client.get("/api/execution/draft-attempts/alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert materialize_response.status_code == 200
    assert bootstrap_response.status_code == 200
    assert bootstrap_response.json()["attempt_mode"] == "draft"
    assert bootstrap_response.json()["readiness_state"] == "ready_to_apply"
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert list_response.json()[0]["job_id"] == first_job.id


def test_execution_api_can_start_draft_attempt_and_return_startup_bundle(tmp_path):
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
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        materialize_response = client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        start_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert materialize_response.status_code == 200
    assert bootstrap_response.status_code == 200
    assert start_response.status_code == 200
    assert start_response.json()["attempt_id"] == attempt_id
    assert start_response.json()["prepared_document_count"] == 1
    assert len(start_response.json()["startup_artifact_ids"]) >= 3


def test_execution_api_can_build_field_plan_from_started_attempt(tmp_path):
    session = make_session()
    ingest_discovery_batch(session, load_greenhouse_batch())
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={
            "email": "alex@example.com",
            "phone": "+1-555-0100",
            "location": "Remote",
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
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        plan_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert bootstrap_response.status_code == 200
    assert plan_response.status_code == 200
    assert plan_response.json()["attempt_id"] == attempt_id
    assert plan_response.json()["field_count"] >= 4
    assert any(entry["field_key"] == "resume_upload" for entry in plan_response.json()["entries"])


def test_execution_api_can_build_greenhouse_site_overlay(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        overlay_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert bootstrap_response.status_code == 200
    assert overlay_response.status_code == 200
    assert overlay_response.json()["site_vendor"] == "greenhouse"
    assert overlay_response.json()["entry_count"] >= 4
    assert any(
        entry["field_key"] == "resume_upload" and "input[type='file'][name='resume']" in entry["selector_candidates"]
        for entry in overlay_response.json()["entries"]
    )


def test_execution_api_can_open_greenhouse_target_and_return_resolutions(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        open_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert bootstrap_response.status_code == 200
    assert open_response.status_code == 200
    assert open_response.json()["site_vendor"] == "greenhouse"
    assert open_response.json()["browser_profile_key"] == "apply-main"
    assert open_response.json()["capture_method"] in {"http_get", "stub_fallback"}
    assert open_response.json()["resolved_count"] > 0
    assert any(
        entry["field_key"] == "why_this_role" and entry["resolution_status"] == "manual_review"
        for entry in open_response.json()["entries"]
    )


def test_execution_api_can_evaluate_submit_gate_for_greenhouse(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        gate_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert bootstrap_response.status_code == 200
    assert gate_response.status_code == 200
    assert gate_response.json()["site_vendor"] == "greenhouse"
    assert gate_response.json()["application_state"] == "review"
    assert gate_response.json()["attempt_result"] == "blocked"
    assert gate_response.json()["failure_code"] == "submit_gate_blocked"
    assert gate_response.json()["allow_submit"] is False
    assert "manual_review_required:why_this_role" in gate_response.json()["stop_reasons"]


def test_execution_api_can_execute_guarded_submit_after_gate_passes(tmp_path, monkeypatch):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html",
        lambda **kwargs: (
            (
                "<html><body>"
                "<div data-qa='application-review'>Review</div>"
                "<button type='submit' data-qa='submit-application'>Submit</button>"
                "</body></html>"
            ),
            {
                "capture_method": "http_get",
                "status_code": 200,
                "final_url": kwargs["target_url"],
            },
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
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt_id).all()
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
        gate_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        submit_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/guarded-submit")
        overview_response = client.get("/api/execution/overview/alex-doe")
        attempt_detail_response = client.get(f"/api/execution/attempts/{attempt_id}")
        attempt_detail_html_response = client.get(f"/execution/attempts/{attempt_id}")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert gate_response.status_code == 200
    assert gate_response.json()["allow_submit"] is True
    assert submit_response.status_code == 200
    assert submit_response.json()["application_state"] == "applied"
    assert submit_response.json()["attempt_result"] == "success"
    assert submit_response.json()["failure_code"] is None
    assert submit_response.json()["allow_submit"] is True
    assert submit_response.json()["submission_mode"] == "greenhouse_guarded_submit"
    assert overview_response.status_code == 200
    assert len(overview_response.json()) == 1
    assert overview_response.json()[0]["submit_interaction_mode"] in {
        "playwright",
        "simulated_probe_fallback",
    }
    assert overview_response.json()[0]["submit_interaction_clicked"] is True
    assert overview_response.json()[0]["submit_interaction_status"] is not None
    assert overview_response.json()[0]["submit_troubleshoot_event_route"] is not None
    assert "#event-" in overview_response.json()[0]["submit_troubleshoot_event_route"]
    assert overview_response.json()[0]["submit_troubleshoot_artifact_route"] is not None
    assert attempt_detail_response.status_code == 200
    assert attempt_detail_response.json()["submit_interaction_mode"] in {
        "playwright",
        "simulated_probe_fallback",
    }
    assert attempt_detail_response.json()["submit_interaction_clicked"] is True
    assert attempt_detail_response.json()["submit_interaction_status"] is not None
    assert attempt_detail_response.json()["submit_troubleshoot_event_route"] is not None
    assert "#event-" in attempt_detail_response.json()["submit_troubleshoot_event_route"]
    assert attempt_detail_response.json()["submit_troubleshoot_artifact_route"] is not None
    assert attempt_detail_html_response.status_code == 200
    assert "Submit-Stage Diagnostics" in attempt_detail_html_response.text
    assert "Interaction mode" in attempt_detail_html_response.text
    assert "Submit Troubleshooting" in attempt_detail_html_response.text


def test_execution_api_guarded_submit_returns_conflict_when_submit_gate_blocked(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        gate_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        submit_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/guarded-submit")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert gate_response.status_code == 200
    assert gate_response.json()["allow_submit"] is False
    assert submit_response.status_code == 409
    assert submit_response.json()["detail"] == "submit_gate_blocked"


def test_execution_api_guarded_submit_returns_conflict_when_submit_selector_probe_fails(
    tmp_path, monkeypatch
):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

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

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt_id).all()
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
        gate_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        submit_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/guarded-submit")
        overview_response = client.get("/api/execution/overview/alex-doe")
        attempt_detail_response = client.get(f"/api/execution/attempts/{attempt_id}")
        inbox_list_response = client.get("/api/jobs?candidate_profile_slug=alex-doe")
        inbox_detail_response = client.get(
            f"/api/jobs/{first_job.id}?candidate_profile_slug=alex-doe"
        )
        remediation_response = client.post(
            f"/api/execution/draft-attempts/{attempt_id}/remediate-submit"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert gate_response.status_code == 200
    assert gate_response.json()["allow_submit"] is True
    assert submit_response.status_code == 409
    assert submit_response.json()["detail"] == "guarded_submit_probe_failed"
    assert overview_response.status_code == 200
    assert overview_response.json()[0]["failure_code"] == "guarded_submit_probe_failed"
    assert (
        overview_response.json()[0]["failure_classification"]
        == "page_changed_still_recognizable"
    )
    assert overview_response.json()[0]["submit_remediation_message"] is not None
    assert (
        "/execution/artifacts/"
        in (overview_response.json()[0]["submit_remediation_primary_route"] or "")
    )
    assert (
        "/execution/replay/"
        in (overview_response.json()[0]["submit_remediation_secondary_route"] or "")
    )
    assert attempt_detail_response.status_code == 200
    assert attempt_detail_response.json()["failure_code"] == "guarded_submit_probe_failed"
    assert (
        attempt_detail_response.json()["failure_classification"]
        == "page_changed_still_recognizable"
    )
    assert attempt_detail_response.json()["submit_remediation_message"] is not None
    assert (
        "/execution/artifacts/"
        in (attempt_detail_response.json()["submit_remediation_primary_route"] or "")
    )
    assert (
        "/execution/replay/"
        in (attempt_detail_response.json()["submit_remediation_secondary_route"] or "")
    )
    assert inbox_list_response.status_code == 200
    assert (
        inbox_list_response.json()[0]["execution_summary"]["failure_classification"]
        == "page_changed_still_recognizable"
    )
    assert inbox_detail_response.status_code == 200
    assert (
        inbox_detail_response.json()["execution_summary"]["failure_classification"]
        == "page_changed_still_recognizable"
    )

    assert remediation_response.status_code == 200
    remediation_payload = remediation_response.json()
    assert remediation_payload["source_attempt_id"] == attempt_id
    assert remediation_payload["attempt_id"] != attempt_id
    assert remediation_payload["remediation_action"] == "refresh_target_and_submit_gate"
    assert "bootstrap" in remediation_payload["executed_steps"]
    assert "open_target" in remediation_payload["executed_steps"]
    assert "submit_gate" in remediation_payload["executed_steps"]
    assert remediation_payload["stop_reason"] is None


def test_execution_api_guarded_submit_returns_conflict_when_submit_interaction_fails(
    tmp_path, monkeypatch
):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

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
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html",
        lambda **kwargs: (
            (
                "<html><body>"
                "<div data-qa='application-review'>Review</div>"
                "<button type='submit' data-qa='submit-application'>Submit</button>"
                "</body></html>"
            ),
            {
                "capture_method": "http_get",
                "status_code": 200,
                "final_url": kwargs["target_url"],
            },
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
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt_id).all()
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
        gate_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        submit_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/guarded-submit")
        attempt_detail_response = client.get(f"/api/execution/attempts/{attempt_id}")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert gate_response.status_code == 200
    assert gate_response.json()["allow_submit"] is True
    assert submit_response.status_code == 409
    assert submit_response.json()["detail"] == "guarded_submit_interaction_failed"
    assert attempt_detail_response.status_code == 200
    assert attempt_detail_response.json()["failure_code"] == "guarded_submit_interaction_failed"


def test_execution_api_guarded_submit_returns_not_found_for_unsupported_site(tmp_path):
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
    first_job = session.query(models.Job).order_by(models.Job.id).first()
    first_job.requirements_structured = {
        "required_skills": ["python", "sql", "aws"],
        "seniority_signals": ["senior"],
        "required_years_experience": 5,
    }
    first_job.ats_vendor = "workday"
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

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        application = session.query(models.Application).filter_by(job_id=first_job.id).one()
        session.add(
            models.ApplicationEvent(
                application_id=application.id,
                attempt_id=attempt_id,
                event_type="draft_target_opened",
                message="Synthetic target-open event for unsupported-site endpoint test.",
                payload={"target_url": "https://example.com/workday/job/1"},
                created_at=models.utcnow(),
            )
        )
        session.add(
            models.ApplicationEvent(
                application_id=application.id,
                attempt_id=attempt_id,
                event_type="draft_submit_gate_evaluated",
                message="Synthetic gate event for unsupported-site endpoint test.",
                payload={"allow_submit": True, "confidence_score": 0.99},
                created_at=models.utcnow(),
            )
        )
        session.commit()
        submit_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/guarded-submit")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert submit_response.status_code == 404
    assert submit_response.json()["detail"] == "guarded_submit_not_supported_for_site"


def test_inbox_api_and_html_surface_blocked_execution_summary(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        list_response = client.get("/api/jobs?candidate_profile_slug=alex-doe")
        detail_response = client.get(f"/api/jobs/{first_job.id}?candidate_profile_slug=alex-doe")
        html_response = client.get(f"/inbox/jobs/{first_job.id}?candidate_profile_slug=alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert list_response.status_code == 200
    assert list_response.json()[0]["execution_summary"]["attempt_result"] == "blocked"
    assert list_response.json()[0]["application_readiness"]["state"] == "execution_blocked"
    assert detail_response.status_code == 200
    assert detail_response.json()["execution_summary"]["failure_code"] == "submit_gate_blocked"
    assert html_response.status_code == 200
    assert "Execution Summary" in html_response.text
    assert "submit_gate_blocked" in html_response.text


def test_jobs_api_can_filter_and_sort_by_execution_state(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{jobs[0].id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{jobs[0].id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        filter_response = client.get("/api/jobs?candidate_profile_slug=alex-doe&execution_state=blocked")
        sort_response = client.get("/api/jobs?candidate_profile_slug=alex-doe&sort_by=execution_state&descending=true")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert filter_response.status_code == 200
    assert len(filter_response.json()) == 1
    assert filter_response.json()[0]["execution_summary"]["attempt_result"] == "blocked"
    assert sort_response.status_code == 200
    assert sort_response.json()[0]["job_id"] == jobs[0].id


def test_execution_overview_api_and_html_surface_blocked_attempts(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        api_response = client.get("/api/execution/overview/alex-doe?blocked_only=true")
        html_response = client.get("/execution/overview/alex-doe?blocked_only=true")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert api_response.status_code == 200
    assert len(api_response.json()) == 1
    assert api_response.json()[0]["attempt_result"] == "blocked"
    assert api_response.json()[0]["failure_code"] == "submit_gate_blocked"
    assert api_response.json()[0]["latest_event_type"] == "draft_submit_gate_evaluated"
    assert api_response.json()[0]["attempt_route"].endswith(
        f"/execution/attempts/{api_response.json()[0]['attempt_id']}"
    )
    assert api_response.json()[0]["replay_route"].endswith(
        f"/execution/replay/{api_response.json()[0]['attempt_id']}"
    )
    assert api_response.json()[0]["primary_action_label"] == "Open replay bundle"
    assert api_response.json()[0]["visual_evidence_route"] is not None
    assert api_response.json()[0]["visual_evidence_label"] == "Open HTML"
    assert api_response.json()[0]["artifact_count"] >= 6
    assert html_response.status_code == 200
    assert "Execution Overview" in html_response.text
    assert "submit_gate_blocked" in html_response.text
    assert "Latest stage: draft_submit_gate_evaluated" in html_response.text
    assert "Open HTML" in html_response.text


def test_execution_overview_and_dashboard_api_support_failure_and_confidence_filters(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        blocked_bootstrap = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        blocked_attempt_id = blocked_bootstrap.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/open-target")
        gate_response = client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/submit-gate")
        confidence = gate_response.json()["confidence_score"]
        client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )

        overview_response = client.get(
            "/api/execution/overview/alex-doe"
            f"?failure_code=submit_gate_blocked&max_submit_confidence={confidence + 0.01}"
        )
        classification_overview_response = client.get(
            "/api/execution/overview/alex-doe?failure_classification=unknown_classification"
        )
        dashboard_response = client.get(
            "/api/execution/dashboard/alex-doe"
            f"?failure_code=submit_gate_blocked&max_submit_confidence={confidence + 0.01}"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert overview_response.status_code == 200
    assert len(overview_response.json()) == 1
    assert overview_response.json()[0]["attempt_id"] == blocked_attempt_id
    assert overview_response.json()[0]["failure_code"] == "submit_gate_blocked"
    assert overview_response.json()[0]["failure_classification"] == "unknown_classification"
    assert overview_response.json()[0]["submit_confidence"] == confidence
    assert classification_overview_response.status_code == 200
    assert len(classification_overview_response.json()) == 1
    assert (
        classification_overview_response.json()[0]["failure_classification"]
        == "unknown_classification"
    )

    assert dashboard_response.status_code == 200
    payload = dashboard_response.json()
    assert payload["total_attempts"] == 1
    assert payload["blocked_attempts"] == 1
    assert payload["manual_review_blocked_attempts"] == 0
    assert payload["pending_attempts"] == 0
    assert payload["blocked_failure_counts"] == {"submit_gate_blocked": 1}
    assert payload["blocked_failure_classification_counts"] == {"unknown_classification": 1}
    assert payload["recent_attempts"][0]["attempt_id"] == blocked_attempt_id
    assert any("failure_code=submit_gate_blocked" in action for action in payload["recommended_actions"])


def test_execution_dashboard_bulk_remediation_api_scopes_by_failure_code(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        blocked_bootstrap = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        blocked_attempt_id = blocked_bootstrap.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/submit-gate")

        remediation_response = client.post(
            "/api/execution/dashboard/alex-doe/bulk-remediate-submit"
            "?failure_code=submit_gate_blocked&limit=5"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert remediation_response.status_code == 200
    payload = remediation_response.json()
    assert payload["candidate_profile_slug"] == "alex-doe"
    assert payload["requested_count"] == 1
    assert payload["targeted_attempt_ids"] == [blocked_attempt_id]
    assert payload["remediated_count"] == 1
    assert len(payload["results"]) == 1
    assert payload["results"][0]["source_attempt_id"] == blocked_attempt_id
    assert "submit_gate" in payload["results"][0]["executed_steps"]


def test_execution_dashboard_bulk_remediation_api_reports_partial_failures(tmp_path, monkeypatch):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")

        first_bootstrap = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        first_attempt_id = first_bootstrap.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{first_attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{first_attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{first_attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{first_attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{first_attempt_id}/submit-gate")

        second_bootstrap = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        second_attempt_id = second_bootstrap.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{second_attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{second_attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{second_attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{second_attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{second_attempt_id}/submit-gate")

        original_run = execution_service.run_submit_remediation_action

        def flaky_remediation(service_session, *, attempt_id: int):
            if attempt_id == first_attempt_id:
                raise ValueError("draft_field_plan_not_created")
            return original_run(service_session, attempt_id=attempt_id)

        monkeypatch.setattr(
            "jobbot.execution.service.run_submit_remediation_action",
            flaky_remediation,
        )

        remediation_response = client.post(
            "/api/execution/dashboard/alex-doe/bulk-remediate-submit"
            "?failure_code=submit_gate_blocked&limit=10"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert remediation_response.status_code == 200
    payload = remediation_response.json()
    assert payload["requested_count"] == 2
    assert payload["remediated_count"] == 1
    assert payload["failed_count"] == 1
    assert len(payload["results"]) == 1
    assert payload["results"][0]["source_attempt_id"] == second_attempt_id
    assert len(payload["failures"]) == 1
    assert payload["failures"][0]["source_attempt_id"] == first_attempt_id
    assert payload["failures"][0]["error_code"] == "draft_field_plan_not_created"


def test_execution_overview_and_dashboard_api_support_manual_review_only_filter(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        blocked_bootstrap = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        blocked_attempt_id = blocked_bootstrap.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/submit-gate")

        manual_bootstrap = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        manual_attempt_id = manual_bootstrap.json()["attempt_id"]
        manual_attempt = session.query(models.ApplicationAttempt).filter_by(id=manual_attempt_id).one()
        manual_attempt.result = "blocked"
        manual_attempt.failure_code = "manual_review_required:unresolved_required"
        session.commit()

        overview_response = client.get("/api/execution/overview/alex-doe?manual_review_only=true")
        dashboard_response = client.get("/api/execution/dashboard/alex-doe?manual_review_only=true")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert overview_response.status_code == 200
    assert len(overview_response.json()) == 1
    assert overview_response.json()[0]["attempt_id"] == manual_attempt_id
    assert overview_response.json()[0]["failure_code"] == "manual_review_required:unresolved_required"

    assert dashboard_response.status_code == 200
    payload = dashboard_response.json()
    assert payload["total_attempts"] == 1
    assert payload["blocked_attempts"] == 1
    assert payload["manual_review_blocked_attempts"] == 1
    assert payload["blocked_failure_counts"] == {"manual_review_required:unresolved_required": 1}
    assert payload["blocked_failure_classification_counts"] == {"unknown_classification": 1}
    assert payload["recent_attempts"][0]["attempt_id"] == manual_attempt_id
    assert any("manual-review-required failures only" in action for action in payload["recommended_actions"])


def test_execution_overview_api_supports_confidence_sort_and_invalid_sort(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        blocked_bootstrap = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        blocked_attempt_id = blocked_bootstrap.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/submit-gate")

        pending_bootstrap = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        pending_attempt_id = pending_bootstrap.json()["attempt_id"]

        sorted_response = client.get(
            "/api/execution/overview/alex-doe?sort_by=submit_confidence&descending=false"
        )
        invalid_sort_response = client.get("/api/execution/overview/alex-doe?sort_by=not_a_real_sort_key")
        invalid_sort_dashboard = client.get("/api/execution/dashboard/alex-doe?sort_by=not_a_real_sort_key")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert sorted_response.status_code == 200
    payload = sorted_response.json()
    assert len(payload) == 2
    assert payload[0]["attempt_id"] == blocked_attempt_id
    assert payload[1]["attempt_id"] == pending_attempt_id
    assert payload[0]["submit_confidence"] is not None
    assert payload[1]["submit_confidence"] is None

    assert invalid_sort_response.status_code == 400
    assert invalid_sort_response.json()["detail"] == "invalid_execution_overview_sort"
    assert invalid_sort_dashboard.status_code == 400
    assert invalid_sort_dashboard.json()["detail"] == "invalid_execution_overview_sort"


def test_execution_attempt_detail_api_and_html_surface_events_and_artifacts(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        api_response = client.get(f"/api/execution/attempts/{attempt_id}")
        html_response = client.get(f"/execution/attempts/{attempt_id}")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert api_response.status_code == 200
    assert api_response.json()["attempt_result"] == "blocked"
    assert api_response.json()["events"][-1]["event_type"] == "draft_submit_gate_evaluated"
    assert api_response.json()["events"][-1]["artifact_routes"]
    assert all(
        route.startswith("/execution/artifacts/")
        for route in api_response.json()["events"][-1]["artifact_routes"]
    )
    assert any(artifact["artifact_type"] == "html_snapshot" for artifact in api_response.json()["artifacts"])
    html_artifact = next(
        artifact for artifact in api_response.json()["artifacts"] if artifact["artifact_type"] == "html_snapshot"
    )
    assert html_artifact["inspect_route"].endswith(f"/execution/artifacts/{html_artifact['artifact_id']}")
    assert html_artifact["raw_route"].endswith(f"/execution/artifacts/{html_artifact['artifact_id']}/raw")
    assert html_artifact["launch_route"].endswith(f"/execution/artifacts/{html_artifact['artifact_id']}/launch")
    assert html_artifact["launch_label"] == "Open HTML"
    assert html_response.status_code == 200
    assert "Execution Events" in html_response.text
    assert "Execution Artifacts" in html_response.text
    assert "draft_submit_gate_evaluated" in html_response.text
    assert "/execution/artifacts/" in html_response.text
    assert "Open HTML" in html_response.text


def test_execution_artifact_detail_api_and_html_surface_safe_preview(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        attempt_response = client.get(f"/api/execution/attempts/{attempt_id}")
        artifact_id = next(
            artifact["artifact_id"]
            for artifact in attempt_response.json()["artifacts"]
            if artifact["artifact_type"] == "model_io"
        )
        api_response = client.get(f"/api/execution/artifacts/{artifact_id}")
        html_response = client.get(f"/execution/artifacts/{artifact_id}")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert api_response.status_code == 200
    assert api_response.json()["artifact_id"] == artifact_id
    assert api_response.json()["raw_route"] == f"/execution/artifacts/{artifact_id}/raw"
    assert api_response.json()["launch_route"] == f"/execution/artifacts/{artifact_id}/launch"
    assert api_response.json()["launch_label"] == "Open text"
    assert api_response.json()["launch_target"] == "open_text"
    assert api_response.json()["preview_kind"] == "json"
    assert "candidate_profile_slug" in api_response.json()["preview_text"]
    assert html_response.status_code == 200
    assert "Artifact #" in html_response.text
    assert "Preview" in html_response.text
    assert "candidate_profile_slug" in html_response.text


def test_execution_replay_bundle_api_and_html_surface_assets_and_actions(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        api_response = client.get(f"/api/execution/replay/{attempt_id}")
        html_response = client.get(f"/execution/replay/{attempt_id}")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert api_response.status_code == 200
    assert api_response.json()["attempt_id"] == attempt_id
    assert api_response.json()["attempt_result"] == "blocked"
    assert api_response.json()["latest_event_type"] == "draft_submit_gate_evaluated"
    assert any(asset["label"] == "submit_gate" for asset in api_response.json()["assets"])
    assert any(
        asset["label"] == "startup_context"
        and asset["inspect_route"] is not None
        and asset["raw_route"] is not None
        and asset["launch_route"] is not None
        and asset["launch_label"] == "Open text"
        and asset["launch_target"] == "open_text"
        and asset["openable_locally"] is True
        and asset["open_hint"] == "open_text"
        for asset in api_response.json()["assets"]
    )
    assert any("Resolve manual-review" in action for action in api_response.json()["recommended_actions"])
    assert html_response.status_code == 200
    assert "Replay Bundle" in html_response.text
    assert "Replay Assets" in html_response.text
    assert "Recommended Actions" in html_response.text
    assert "openable=True" in html_response.text


def test_execution_replay_bundle_api_surfaces_guarded_submit_assets_after_success(tmp_path, monkeypatch):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()
    monkeypatch.setattr(
        "jobbot.execution.service._capture_target_page_html",
        lambda **kwargs: (
            (
                "<html><body>"
                "<div data-qa='application-review'>Review</div>"
                "<button type='submit' data-qa='submit-application'>Submit</button>"
                "</body></html>"
            ),
            {
                "capture_method": "http_get",
                "status_code": 200,
                "final_url": kwargs["target_url"],
            },
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
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{attempt_id}/open-target")
        mappings = session.query(models.FieldMapping).filter_by(attempt_id=attempt_id).all()
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
        gate_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/submit-gate")
        monkeypatch.setattr(
            "jobbot.execution.service._capture_target_page_screenshot_via_playwright",
            lambda **kwargs: b"\x89PNG\r\n\x1a\nguarded-submit-fake",
        )
        monkeypatch.setattr(
            "jobbot.execution.service._capture_target_page_trace_via_playwright",
            lambda **kwargs: b"PK\x03\x04guarded-submit-trace",
        )
        submit_response = client.post(f"/api/execution/draft-attempts/{attempt_id}/guarded-submit")
        replay_response = client.get(f"/api/execution/replay/{attempt_id}")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert gate_response.status_code == 200
    assert gate_response.json()["allow_submit"] is True
    assert submit_response.status_code == 200
    assert replay_response.status_code == 200
    assert replay_response.json()["attempt_result"] == "success"
    assert replay_response.json()["latest_event_type"] == "draft_submit_executed"
    assert any(asset["label"] == "guarded_submit" for asset in replay_response.json()["assets"])
    assert any(asset["label"] == "guarded_submit_screenshot" for asset in replay_response.json()["assets"])
    assert any(asset["label"] == "guarded_submit_trace" for asset in replay_response.json()["assets"])


def test_execution_raw_artifact_and_replay_asset_routes_serve_persisted_files(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{attempt_id}/start")
        attempt_response = client.get(f"/api/execution/attempts/{attempt_id}")
        artifact_id = next(
            artifact["artifact_id"]
            for artifact in attempt_response.json()["artifacts"]
            if artifact["artifact_type"] == "model_io"
        )
        artifact_raw = client.get(f"/api/execution/artifacts/{artifact_id}/raw")
        artifact_launch = client.get(
            f"/api/execution/artifacts/{artifact_id}/launch",
            follow_redirects=False,
        )
        replay_raw = client.get(f"/api/execution/replay/{attempt_id}/assets/startup_context/raw")
        replay_launch = client.get(
            f"/api/execution/replay/{attempt_id}/assets/startup_context/launch",
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert artifact_raw.status_code == 200
    assert "candidate_profile_slug" in artifact_raw.text
    assert artifact_launch.status_code in {302, 307}
    assert artifact_launch.headers["location"].endswith(f"/execution/artifacts/{artifact_id}/raw")
    assert replay_raw.status_code == 200
    assert "candidate_profile_slug" in replay_raw.text
    assert replay_launch.status_code in {302, 307}
    assert replay_launch.headers["location"].endswith(f"/execution/artifacts/{artifact_id}/raw")


def test_execution_screenshot_launch_redirects_to_inspect_view(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        bootstrap_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        attempt_id = bootstrap_response.json()["attempt_id"]
        screenshot_path = tmp_path / "capture.png"
        screenshot_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        artifact = models.Artifact(
            attempt_id=attempt_id,
            artifact_type=models.ArtifactType.SCREENSHOT,
            path=str(screenshot_path),
            size_bytes=screenshot_path.stat().st_size,
        )
        session.add(artifact)
        session.commit()
        api_response = client.get(f"/api/execution/artifacts/{artifact.id}")
        html_response = client.get(f"/execution/artifacts/{artifact.id}")
        launch_response = client.get(
            f"/api/execution/artifacts/{artifact.id}/launch",
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert api_response.status_code == 200
    assert api_response.json()["launch_label"] == "View image"
    assert api_response.json()["launch_target"] == "inspect_image"
    assert html_response.status_code == 200
    assert "<img src=" in html_response.text
    assert launch_response.status_code in {302, 307}
    assert launch_response.headers["location"].endswith(f"/execution/artifacts/{artifact.id}")


def test_execution_dashboard_api_and_html_surface_summary_and_links(tmp_path):
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
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        client.post(f"/api/eligibility/jobs/{first_job.id}/alex-doe")
        blocked_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        blocked_attempt_id = blocked_response.json()["attempt_id"]
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/start")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/field-plan")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/site-overlay")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/open-target")
        client.post(f"/api/execution/draft-attempts/{blocked_attempt_id}/submit-gate")
        pending_response = client.post(
            f"/api/execution/draft-attempts/jobs/{first_job.id}/alex-doe?browser_profile_key=apply-main"
        )
        pending_attempt_id = pending_response.json()["attempt_id"]
        api_response = client.get("/api/execution/dashboard/alex-doe")
        html_response = client.get("/execution/dashboard/alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert api_response.status_code == 200
    payload = api_response.json()
    assert payload["candidate_profile_slug"] == "alex-doe"
    assert payload["total_attempts"] == 2
    assert payload["blocked_attempts"] == 1
    assert payload["manual_review_blocked_attempts"] == 0
    assert payload["pending_attempts"] == 1
    assert payload["review_state_attempts"] == 1
    assert payload["replay_ready_attempts"] == 1
    assert payload["blocked_failure_counts"] == {"submit_gate_blocked": 1}
    assert payload["blocked_failure_classification_counts"] == {"unknown_classification": 1}
    assert payload["blocked_recent_attempts"][0]["attempt_id"] == blocked_attempt_id
    assert payload["blocked_recent_attempts"][0]["visual_evidence_route"] is not None
    assert payload["blocked_recent_attempts"][0]["visual_evidence_label"] == "Open HTML"
    assert any(row["attempt_id"] == pending_attempt_id for row in payload["recent_attempts"])
    assert any("Resolve blocked guarded attempts" in action for action in payload["recommended_actions"])

    assert html_response.status_code == 200
    assert "Execution Dashboard" in html_response.text
    assert "Blocked Attempts" in html_response.text
    assert "Blocked Failure Breakdown" in html_response.text
    assert "Blocked Failure Classification Breakdown" in html_response.text
    assert "Recent Attempts" in html_response.text
    assert f"/execution/replay/{blocked_attempt_id}" in html_response.text
    assert f"/execution/attempts/{blocked_attempt_id}" in html_response.text
    assert "Open HTML" in html_response.text
    assert "Run blocked-only remediation" in html_response.text
    assert "Run manual-review remediation" in html_response.text
    assert "Run classification remediation" in html_response.text
    assert (
        "/execution/dashboard/alex-doe/bulk-remediate-submit?failure_code=submit_gate_blocked"
        in html_response.text
    )
    assert (
        "/execution/dashboard/alex-doe/bulk-remediate-submit?manual_review_only=true"
        in html_response.text
    )
    assert (
        "/execution/dashboard/alex-doe/bulk-remediate-submit?failure_classification=unknown_classification"
        in html_response.text
    )


def test_execution_dashboard_bulk_remediation_html_redirect_preserves_filters():
    session = make_session()
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={"email": "alex@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/alex-doe/resume.pdf"},
    )
    session.add(candidate)
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
            "/execution/dashboard/alex-doe/bulk-remediate-submit"
            "?failure_code=submit_gate_blocked&manual_review_only=true&limit=7",
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/execution/dashboard/alex-doe?failure_code=submit_gate_blocked&manual_review_only=true&limit=7&bulk_requested=0&bulk_remediated=0&bulk_failed=0"
    )


def test_execution_dashboard_html_surfaces_bulk_remediation_feedback():
    session = make_session()
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={"email": "alex@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/alex-doe/resume.pdf"},
    )
    session.add(candidate)
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
            "/execution/dashboard/alex-doe"
            "?bulk_requested=3&bulk_remediated=2&bulk_failed=1"
            "&bulk_first_failure_attempt=42&bulk_first_failure_code=draft_field_plan_not_created"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    assert "Bulk Remediation Result" in response.text
    assert "targeted=3 | remediated=2 | failed=1" in response.text
    assert "First failure: attempt #42 (draft_field_plan_not_created)" in response.text


def test_execution_dashboard_html_persists_bulk_remediation_history():
    session = make_session()
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={"email": "alex@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/alex-doe/resume.pdf"},
    )
    session.add(candidate)
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        first = client.post(
            "/execution/dashboard/alex-doe/bulk-remediate-submit"
            "?failure_code=submit_gate_blocked&limit=7",
            follow_redirects=False,
        )
        second = client.post(
            "/execution/dashboard/alex-doe/bulk-remediate-submit"
            "?manual_review_only=true&limit=3",
            follow_redirects=False,
        )
        response = client.get("/execution/dashboard/alex-doe")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert first.status_code == 303
    assert second.status_code == 303
    assert response.status_code == 200
    assert "Bulk Remediation History" in response.text
    assert response.text.count("targeted=0 | remediated=0 | failed=0") >= 2
    assert "failure_code=submit_gate_blocked" in response.text
    assert "manual_review_only=true" in response.text
    assert "Re-run scope" in response.text
    assert (
        "/execution/dashboard/alex-doe/bulk-remediate-submit?manual_review_only=true&amp;limit=3&amp;sort_by=started_at&amp;descending=true"
        in response.text
    )


def test_execution_dashboard_html_history_supports_sort_and_metadata():
    session = make_session()
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={"email": "alex@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={
            "resume_path": "/profiles/alex-doe/resume.pdf",
            "execution_dashboard_bulk_history": [
                {
                    "created_at": "2026-04-18T09:00:00+00:00",
                    "requested_count": 5,
                    "remediated_count": 2,
                    "failed_count": 3,
                    "manual_review_only": True,
                    "limit": 5,
                    "sort_by": "started_at",
                    "descending": True,
                    "first_failure_attempt_id": 101,
                    "first_failure_code": "draft_field_plan_not_created",
                },
                {
                    "created_at": "2026-04-18T10:00:00+00:00",
                    "requested_count": 4,
                    "remediated_count": 3,
                    "failed_count": 1,
                    "failure_code": "submit_gate_blocked",
                    "limit": 4,
                    "sort_by": "started_at",
                    "descending": True,
                    "first_failure_attempt_id": 77,
                    "first_failure_code": "browser_profile_not_ready_for_application",
                },
            ],
        },
    )
    session.add(candidate)
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/execution/dashboard/alex-doe?history_sort=failed_desc")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    assert "Bulk Remediation History" in response.text
    assert "Recorded: 2026-04-18T09:00:00+00:00" in response.text
    assert "First failure: attempt #101 (draft_field_plan_not_created)" in response.text
    assert "Sort history" in response.text
    assert "history_sort=failed_desc" in response.text
    assert "history_sort=newest" in response.text
    assert response.text.index("targeted=5 | remediated=2 | failed=3") < response.text.index(
        "targeted=4 | remediated=3 | failed=1"
    )


def test_execution_dashboard_history_rejects_invalid_sort():
    session = make_session()
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={"email": "alex@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/alex-doe/resume.pdf"},
    )
    session.add(candidate)
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get("/execution/dashboard/alex-doe?history_sort=not_valid")
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_execution_dashboard_history_sort"
