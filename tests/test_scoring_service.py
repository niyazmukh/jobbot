from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import CandidateFact, CandidateProfile, Job
from jobbot.scoring.service import score_job_for_candidate


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_score_job_for_candidate_persists_explainable_score():
    session = make_session()
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        target_preferences={"preferred_locations": ["Toronto"], "remote": True},
    )
    session.add(candidate)
    session.flush()
    session.add(
        CandidateFact(
            candidate_profile_id=candidate.id,
            fact_key="skills-001",
            category="skills",
            content="Python SQL AWS Docker Senior engineer",
        )
    )
    job = Job(
        canonical_url="https://example.com/jobs/42",
        title="Senior Data Engineer",
        title_normalized="senior data engineer",
        location_raw="Toronto, ON",
        location_normalized="toronto, ontario",
        requirements_structured={
            "required_skills": ["python", "sql", "aws"],
            "seniority_signals": ["senior"],
        },
        status="enriched",
    )
    session.add(job)
    session.commit()

    score_row = score_job_for_candidate(session, job.id, "alex-doe")

    assert score_row.overall_score > 0.9
    assert score_row.score_json["skill_score"] == 1.0
    assert score_row.score_json["location_score"] == 1.0
    assert score_row.score_json["confidence_score"] > 0.7
    assert score_row.score_json["blocked"] is False
    assert "Matched 3 of 3 required skills." in score_row.score_json["explanations"][0]


def test_score_job_for_candidate_blocks_on_clear_mismatch():
    session = make_session()
    candidate = CandidateProfile(
        name="Jordan Smith",
        slug="jordan-smith",
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
    job = Job(
        canonical_url="https://example.com/jobs/84",
        title="Principal Machine Learning Engineer",
        title_normalized="principal machine learning engineer",
        location_raw="San Francisco, CA",
        location_normalized="san francisco bay area",
        requirements_structured={
            "required_skills": ["python", "machine learning", "aws"],
            "seniority_signals": ["principal"],
            "required_years_experience": 8,
        },
        status="enriched",
    )
    session.add(job)
    session.commit()

    score_row = score_job_for_candidate(session, job.id, "jordan-smith")

    assert score_row.score_json["blocked"] is True
    assert "no_required_skills_matched" in score_row.score_json["blocking_reasons"]
    assert "location_preference_mismatch" in score_row.score_json["blocking_reasons"]
    assert score_row.score_json["confidence_score"] < 0.5
