from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import Job, JobSource
from jobbot.enrichment.service import enrich_job, extract_requirements_from_job, extract_requirements_from_text


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_extract_requirements_from_text_finds_skills_and_years():
    result = extract_requirements_from_text(
        """
        We require 5+ years of Python, SQL, and AWS experience.
        Nice to have: Kubernetes and Airflow.
        Bachelor's degree in Computer Science required.
        """
    )

    assert result.required_years_experience == 5
    assert "python" in result.required_skills
    assert "sql" in result.required_skills
    assert "aws" in result.required_skills
    assert "kubernetes" in result.preferred_skills
    assert "airflow" in result.preferred_skills
    assert "bachelor" in result.education_signals


def test_enrich_job_updates_requirements_and_status():
    session = make_session()
    job = Job(
        canonical_url="https://example.com/jobs/1",
        title="Senior Platform Engineer",
        title_normalized="senior platform engineer",
        description_text=(
            "Senior role. Requires 7 years of Python and Docker experience. "
            "Preferred: Kubernetes. Master's degree preferred."
        ),
        status="discovered",
    )
    session.add(job)
    session.commit()

    enriched = enrich_job(session, job.id)

    assert enriched.status == "enriched"
    assert enriched.requirements_structured["required_years_experience"] == 7
    assert "python" in enriched.requirements_structured["required_skills"]
    assert "docker" in enriched.requirements_structured["required_skills"]


def test_extract_requirements_from_job_uses_known_source_metadata():
    session = make_session()
    job = Job(
        canonical_url="https://example.com/jobs/2",
        title="Staff Data Engineer",
        title_normalized="staff data engineer",
        ats_vendor="lever",
        remote_type="hybrid",
        employment_type="Full-time",
        status="discovered",
    )
    session.add(job)
    session.flush()
    source = JobSource(
        job_id=job.id,
        source_type="ats_board",
        source_url="https://jobs.example.com/2",
        metadata_json={
            "department": "Data Platform",
            "team": "Analytics",
            "workplace_type": "Hybrid",
            "commitment": "Full-time",
        },
    )
    session.add(source)
    session.commit()

    requirements = extract_requirements_from_job(job, [source], "")

    assert requirements.extraction_method == "known_source_then_text_rules"
    assert "data platform" in requirements.domain_signals
    assert "analytics" in requirements.domain_signals
    assert "hybrid" in requirements.workplace_signals
    assert requirements.source_attributes["employment_type"] == "Full-time"


def test_extract_requirements_from_job_uses_workday_bullet_fields():
    job = Job(
        canonical_url="https://example.com/jobs/3",
        title="Machine Learning Engineer",
        title_normalized="machine learning engineer",
        ats_vendor="workday",
        status="discovered",
    )
    source = JobSource(
        job_id=1,
        source_type="ats_board",
        source_url="https://jobs.example.com/3",
        metadata_json={
            "bullet_fields": [
                {"label": "Time Type", "text": "Full time"},
                {"label": "Organization", "text": "AI Platform"},
                {"label": "Locations", "text": "Remote - United States"},
            ]
        },
    )

    requirements = extract_requirements_from_job(job, [source], "")

    assert requirements.source_attributes["employment_type"] == "Full time"
    assert "ai platform" in requirements.domain_signals
    assert "remote - united states" in requirements.workplace_signals
