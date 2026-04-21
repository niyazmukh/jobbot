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
from jobbot.preparation.service import (
    _sort_education_rows_desc,
    _sort_experience_rows_desc,
    prepare_job_for_candidate,
)
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


def test_prepare_job_for_candidate_persists_documents_answers_and_review_items(tmp_path: Path, monkeypatch):
    session = make_session()
    job_id = seed_scored_job(session)

    monkeypatch.setattr("jobbot.preparation.service.llm_provider_ready", lambda: False)

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


def test_prepare_job_for_candidate_uses_iterative_llm_cv_writer_when_enabled(tmp_path: Path, monkeypatch):
    session = make_session()
    job_id = seed_scored_job(session)

    class _LlmSettings:
        model_call_daily_budget_usd = 10.0
        model_call_weekly_budget_usd = 50.0
        llm_cv_writer_enabled = True

    class _LlmResult:
        markdown = "# Alex Doe\n\n## Professional Summary\n- LLM generated resume content"
        metadata = {"provider": "gemini", "model_name": "gemini-3.0-flash"}

    monkeypatch.setattr("jobbot.preparation.service.get_settings", lambda: _LlmSettings())
    monkeypatch.setattr("jobbot.preparation.service.llm_provider_ready", lambda: True)
    monkeypatch.setattr(
        "jobbot.preparation.service.build_iterative_llm_resume",
        lambda *args, **kwargs: _LlmResult(),
    )

    summary = prepare_job_for_candidate(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )

    document = session.query(GeneratedDocument).filter(GeneratedDocument.id == summary.generated_document_ids[0]).one()
    content = Path(document.content_path).read_text(encoding="utf-8")
    assert "LLM generated resume content" in content
    assert document.metadata_json["generation_method"] == "iterative_llm_cv_writer_v1"
    assert document.metadata_json["generation_metadata"]["provider"] == "gemini"


def test_prepare_job_for_candidate_records_deterministic_fallback_when_llm_writer_enabled_but_provider_not_ready(
    tmp_path: Path, monkeypatch
):
    session = make_session()
    job_id = seed_scored_job(session)

    class _LlmSettings:
        model_call_daily_budget_usd = 10.0
        model_call_weekly_budget_usd = 50.0
        llm_cv_writer_enabled = True

    monkeypatch.setattr("jobbot.preparation.service.get_settings", lambda: _LlmSettings())
    monkeypatch.setattr("jobbot.preparation.service.llm_provider_ready", lambda: False)

    summary = prepare_job_for_candidate(
        session,
        job_id=job_id,
        candidate_profile_slug="alex-doe",
        output_dir=tmp_path,
    )

    document = (
        session.query(GeneratedDocument)
        .filter(GeneratedDocument.id == summary.generated_document_ids[0])
        .one()
    )

    assert document.metadata_json["generation_method"] == "deterministic_prepare_v1"
    assert document.metadata_json["generation_metadata"]["llm_writer_fallback_reason"] == (
        "llm_provider_not_ready"
    )


def test_sort_helpers_order_experience_and_education_descending():
    experience_rows = [
        CandidateFact(category="experience", content="Senior HRBP & Advisor to CEO at SEZ Alabuga (2016-2020)"),
        CandidateFact(category="experience", content="CHRO at Tattelecom JSC (2021-2023)"),
        CandidateFact(category="experience", content="Researcher at University of Haifa / Technion (2024-Present)"),
        CandidateFact(category="experience", content="Product Manager at Digitrade (2025-Present)"),
    ]
    sorted_experience = _sort_experience_rows_desc(experience_rows)
    sorted_experience_content = [row.content for row in sorted_experience]

    assert sorted_experience_content == [
        "Product Manager at Digitrade (2025-Present)",
        "Researcher at University of Haifa / Technion (2024-Present)",
        "CHRO at Tattelecom JSC (2021-2023)",
        "Senior HRBP & Advisor to CEO at SEZ Alabuga (2016-2020)",
    ]

    education_rows = [
        "MA in Linguistics and Pedagogy, Kazan Federal University, Russia (2011)",
        "MBA in Technology Management and Product Commercialization, Rochester Institute of Technology, USA (2016)",
        "MS in Human Services (research track), University of Haifa, Israel (2025)",
        "PhD in Behavioral Science, Technion - Israel Institute of Technology, Israel (2028 (Expected))",
    ]
    sorted_education = _sort_education_rows_desc(education_rows)

    assert sorted_education == [
        "PhD in Behavioral Science, Technion - Israel Institute of Technology, Israel (2028 (Expected))",
        "MS in Human Services (research track), University of Haifa, Israel (2025)",
        "MBA in Technology Management and Product Commercialization, Rochester Institute of Technology, USA (2016)",
        "MA in Linguistics and Pedagogy, Kazan Federal University, Russia (2011)",
    ]
