from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import CandidateFact, CandidateProfile, Job
from jobbot.eligibility.service import (
    get_application_eligibility,
    list_application_eligibility,
    materialize_application_eligibility,
)
from jobbot.preparation.service import prepare_job_for_candidate
from jobbot.scoring.service import score_job_for_candidate


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def seed_candidate_and_jobs(session):
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
    jobs = [
        Job(
            canonical_url="https://example.com/jobs/1",
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
        ),
        Job(
            canonical_url="https://example.com/jobs/2",
            title="Backend Engineer",
            title_normalized="backend engineer",
            location_raw="Remote",
            location_normalized="remote",
            requirements_structured={
                "required_skills": ["python", "sql", "aws"],
                "seniority_signals": ["senior"],
                "required_years_experience": 5,
            },
            status="enriched",
        ),
    ]
    session.add_all(jobs)
    session.commit()
    return jobs


def test_materialize_application_eligibility_persists_snapshot(tmp_path: Path):
    session = make_session()
    jobs = seed_candidate_and_jobs(session)
    score_job_for_candidate(session, jobs[0].id, "alex-doe")
    prepare_job_for_candidate(session, job_id=jobs[0].id, candidate_profile_slug="alex-doe", output_dir=tmp_path)
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    row = materialize_application_eligibility(
        session,
        job_id=jobs[0].id,
        candidate_profile_slug="alex-doe",
    )

    assert row.job_id == jobs[0].id
    assert row.readiness_state == "ready_to_apply"
    assert row.ready is True
    assert row.prepared_summary["preparation_state"] == "ready"


def test_list_application_eligibility_can_return_ready_only(tmp_path: Path):
    session = make_session()
    jobs = seed_candidate_and_jobs(session)
    score_job_for_candidate(session, jobs[0].id, "alex-doe")
    score_job_for_candidate(session, jobs[1].id, "alex-doe")
    prepare_job_for_candidate(session, job_id=jobs[0].id, candidate_profile_slug="alex-doe", output_dir=tmp_path)
    review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    review.status = "approved"
    document = session.query(models.GeneratedDocument).one()
    document.review_status = "approved"
    session.commit()

    materialize_application_eligibility(session, job_id=jobs[0].id, candidate_profile_slug="alex-doe")
    materialize_application_eligibility(session, job_id=jobs[1].id, candidate_profile_slug="alex-doe")

    rows = list_application_eligibility(session, candidate_profile_slug="alex-doe", ready_only=True, limit=10)
    detail = get_application_eligibility(session, job_id=jobs[0].id, candidate_profile_slug="alex-doe")

    assert len(rows) == 1
    assert rows[0].readiness_state == "ready_to_apply"
    assert detail is not None
    assert detail.ready is True
