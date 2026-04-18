import json
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
    assert run_response.json()["processed_count"] == 1
    assert run_response.json()["succeeded_count"] == 1
    assert run_response.json()["items"][0]["status"] == "succeeded"
