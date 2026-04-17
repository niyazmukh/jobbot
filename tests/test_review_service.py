from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import Answer, CandidateFact, CandidateProfile, GeneratedDocument, Job
from jobbot.models.enums import ReviewStatus
from jobbot.preparation.service import prepare_job_for_candidate
from jobbot.review.service import list_review_queue, queue_score_review, set_review_status
from jobbot.scoring.service import score_job_for_candidate


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def seed_scored_job(session):
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
    score_job_for_candidate(session, job.id, "jordan-smith")
    return job.id


def test_queue_score_review_creates_review_item_with_context():
    session = make_session()
    job_id = seed_scored_job(session)

    review = queue_score_review(session, job_id=job_id, candidate_profile_slug="jordan-smith")

    assert review.entity_type == "job_score"
    assert review.status == ReviewStatus.PENDING.value
    assert review.context["job_id"] == job_id
    assert review.context["candidate_profile_slug"] == "jordan-smith"
    assert review.context["blocked"] is True


def test_set_review_status_updates_existing_item():
    session = make_session()
    job_id = seed_scored_job(session)
    review = queue_score_review(session, job_id=job_id, candidate_profile_slug="jordan-smith")

    updated = set_review_status(session, review_id=review.id, status=ReviewStatus.APPROVED)

    assert updated.id == review.id
    assert updated.status == ReviewStatus.APPROVED.value


def test_list_review_queue_can_filter_by_status():
    session = make_session()
    job_id = seed_scored_job(session)
    review = queue_score_review(session, job_id=job_id, candidate_profile_slug="jordan-smith")
    set_review_status(session, review_id=review.id, status=ReviewStatus.REJECTED)

    reviews = list_review_queue(session, status=ReviewStatus.REJECTED.value, limit=10)

    assert len(reviews) == 1
    assert reviews[0].status == ReviewStatus.REJECTED.value


def test_set_review_status_writes_back_to_prepared_entities(tmp_path):
    session = make_session()
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
                content="Built Python data platforms with AWS and SQL pipelines.",
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
        title="Senior Data Engineer",
        title_normalized="senior data engineer",
        location_raw="Remote - Canada",
        location_normalized="remote canada",
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

    document_review = session.query(models.ReviewQueueItem).filter_by(entity_type="generated_document").one()
    answer_review = session.query(models.ReviewQueueItem).filter_by(entity_type="answer").first()

    updated_document_review = set_review_status(
        session,
        review_id=document_review.id,
        status=ReviewStatus.APPROVED,
    )
    updated_answer_review = set_review_status(
        session,
        review_id=answer_review.id,
        status=ReviewStatus.REJECTED,
    )

    document = session.query(GeneratedDocument).one()
    answer = session.query(Answer).filter(Answer.id == answer_review.entity_id).one()

    assert updated_document_review.status == ReviewStatus.APPROVED.value
    assert updated_answer_review.status == ReviewStatus.REJECTED.value
    assert document.review_status == ReviewStatus.APPROVED.value
    assert answer.approval_status == ReviewStatus.REJECTED.value
    assert answer.extension_approved is False
