from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import (
    Answer,
    CandidateFact,
    CandidateProfile,
    GeneratedDocument,
    Job,
    ModelCall,
    ResumeVariant,
    ReviewQueueItem,
)
from jobbot.preparation.read_models import get_prepared_job_read
from jobbot.preparation.service import prepare_job_for_candidate
from jobbot.scoring.service import score_job_for_candidate


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def seed_scored_job(session, *, enable_tier3_extensions: bool = False):
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        target_preferences={
            "preferred_locations": ["Remote"],
            "remote": True,
            "enable_tier3_extensions": enable_tier3_extensions,
        },
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
    return job.id


def test_prepare_job_for_candidate_persists_documents_answers_and_review_items(tmp_path: Path):
    session = make_session()
    job_id = seed_scored_job(session)

    summary = prepare_job_for_candidate(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )

    document = session.query(GeneratedDocument).one()
    variant = session.query(ResumeVariant).one()
    answers = session.query(Answer).order_by(Answer.id).all()
    reviews = session.query(ReviewQueueItem).order_by(ReviewQueueItem.id).all()
    job = session.query(Job).filter(Job.id == job_id).one()

    assert summary.job_id == job_id
    assert summary.resume_variant_id == variant.id
    assert document.id in summary.generated_document_ids
    assert len(summary.answer_ids) == len(answers)
    assert variant.generated_document_id == document.id
    assert Path(document.content_path).exists()
    assert document.metadata_json["generation_method"] == "deterministic_prepare_v1"
    assert any(claim["truth_tier"] == "inference" for claim in document.metadata_json["claims"])
    assert any(answer.truth_tier.value == "observed" for answer in answers)
    assert any(answer.truth_tier.value == "inference" for answer in answers)
    assert any(review.entity_type == "generated_document" for review in reviews)
    assert any(review.entity_type == "answer" for review in reviews)
    assert job.status == "prepared"


def test_get_prepared_job_read_returns_scoped_documents_and_answers(tmp_path: Path):
    session = make_session()
    job_id = seed_scored_job(session)
    prepare_job_for_candidate(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )

    prepared = get_prepared_job_read(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
    )

    assert prepared is not None
    assert prepared.job_id == job_id
    assert prepared.candidate_profile_slug == "alex-doe"
    assert len(prepared.documents) == 1
    assert prepared.documents[0].resume_variant_id is not None
    assert len(prepared.answers) >= 2
    assert all(answer.provenance_facts for answer in prepared.answers)


def test_prepare_job_for_candidate_includes_extension_answer_when_enabled(tmp_path: Path):
    session = make_session()
    job_id = seed_scored_job(session, enable_tier3_extensions=True)

    prepare_job_for_candidate(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )

    answers = session.query(Answer).order_by(Answer.id).all()
    extension_answers = [answer for answer in answers if answer.truth_tier.value == "extension"]
    extension_review = (
        session.query(ReviewQueueItem)
        .filter_by(entity_type="answer", reason="tier3_first_use_answer_review")
        .one()
    )

    assert len(extension_answers) == 1
    assert extension_answers[0].approval_status == "pending"
    assert extension_answers[0].extension_approved is False
    assert extension_review.truth_tier.value == "extension"


def test_prepare_job_for_candidate_skips_extension_answer_when_budget_ceiling_exceeded(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id = seed_scored_job(session, enable_tier3_extensions=True)
    session.add(
        ModelCall(
            stage="scoring",
            model_provider="openai",
            model_name="gpt-5.4-mini",
            prompt_version="score_v1",
            estimated_cost=4.0,
        )
    )
    session.commit()

    class _BudgetSettings:
        model_call_daily_budget_usd = 1.0
        model_call_weekly_budget_usd = 10.0

    monkeypatch.setattr("jobbot.preparation.service.get_settings", lambda: _BudgetSettings())

    prepare_job_for_candidate(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )

    answers = session.query(Answer).order_by(Answer.id).all()
    extension_answers = [answer for answer in answers if answer.truth_tier.value == "extension"]
    extension_review_count = (
        session.query(ReviewQueueItem)
        .filter_by(entity_type="answer", reason="tier3_first_use_answer_review")
        .count()
    )
    blocked_calls = (
        session.query(ModelCall)
        .filter_by(
            model_provider="budget_guardrail",
            model_name="blocked_non_essential",
            stage="preparation_extension_answer",
        )
        .count()
    )

    assert extension_answers == []
    assert extension_review_count == 0
    assert blocked_calls == 1
