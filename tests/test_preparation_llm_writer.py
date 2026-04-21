from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import CandidateFact, CandidateProfile, Company, Job, ModelCall
from jobbot.preparation.llm_cv_writer import build_iterative_llm_resume


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_llm_cv_writer_runs_iterative_pipeline_and_records_model_calls(monkeypatch):
    session = make_session()
    company = Company(name="Example Co")
    session.add(company)
    session.flush()
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={
            "email": "alex@example.com",
            "phone": "+1-555-0100",
            "linkedin_url": "https://linkedin.com/in/alex",
            "location": "Remote",
        },
    )
    session.add(candidate)
    session.flush()
    facts = [
        CandidateFact(
            candidate_profile_id=candidate.id,
            fact_key="exp-001",
            category="experience",
            content="Led enterprise GTM accounts and negotiated multi-year contracts.",
        ),
        CandidateFact(
            candidate_profile_id=candidate.id,
            fact_key="ach-001",
            category="achievement",
            content="Grew annual recurring revenue by 25% through expansion strategy.",
        ),
        CandidateFact(
            candidate_profile_id=candidate.id,
            fact_key="skill-001",
            category="skill",
            content="Enterprise sales negotiation and solution selling.",
        ),
    ]
    session.add_all(facts)
    job = Job(
        company_id=company.id,
        canonical_url="https://example.com/jobs/1",
        title="Account Executive, AI Sales",
        title_normalized="account executive ai sales",
        description_text="Drive enterprise sales cycles and complex commercial negotiation.",
        requirements_structured={"required_skills": ["enterprise sales", "negotiation"]},
    )
    session.add(job)
    session.commit()

    calls = {"count": 0}

    def fake_provider(*, provider: str, model_name: str, prompt_text: str):
        calls["count"] += 1
        if "STAGE: DRAFT" in prompt_text:
            return (
                '{"contact":{"name":"Alex Doe","email":"alex@example.com","phone":"+1-555-0100","linkedin":"https://linkedin.com/in/alex","location":"Remote"},'
                '"target":{"role":"Account Executive, AI Sales","company":"Example Co"},'
                '"professional_summary":["Enterprise sales leader with revenue growth history."],'
                '"experience":[{"employer":"Example Employer","role":"Senior AE","period":"2021-2025",'
                '"bullets":[{"text":"Owned enterprise deals end-to-end.","evidence_fact_keys":["exp-001"]},'
                '{"text":"Invalid evidence bullet should be filtered.","evidence_fact_keys":["unknown-key"]}]}],'
                '"education":["MBA"],"skills":["Enterprise sales"],'
                '"requirement_alignment":[{"requirement":"Enterprise sales","coverage":"strong","evidence":["Owned enterprise deals"]}],'
                '"gap_notes":["Need stronger AI-native sales examples."]}'
            )
        if "STAGE: REVIEW" in prompt_text:
            return '{"issues":[{"severity":"high","issue":"Missing stronger requirement mapping","fix_instruction":"Expand alignment section"}],"coverage_score":72,"conversion_recommendations":["Prioritize revenue outcomes"]}'
        return (
            '{"contact":{"name":"Alex Doe","email":"alex@example.com","phone":"+1-555-0100","linkedin":"https://linkedin.com/in/alex","location":"Remote"},'
            '"target":{"role":"Account Executive, AI Sales","company":"Example Co"},'
            '"professional_summary":["Enterprise sales leader with proven revenue impact."],'
            '"experience":[{"employer":"Example Employer","role":"Senior AE","period":"2021-2025",'
            '"bullets":[{"text":"Led enterprise GTM accounts and negotiated complex contracts.","evidence_fact_keys":["exp-001","ach-001"]}]}],'
            '"education":["MBA"],"skills":["Enterprise sales negotiation"],'
            '"requirement_alignment":[{"requirement":"Complex negotiation","coverage":"strong","evidence":["Led enterprise GTM accounts"]}],'
            '"gap_notes":["Increase explicit AI-industry case studies."]}'
        )

    monkeypatch.setattr("jobbot.preparation.llm_cv_writer._provider_invoke_json", fake_provider)

    result = build_iterative_llm_resume(
        session,
        job=job,
        candidate=candidate,
        facts=facts,
        score_json={"blocked": False},
    )

    assert calls["count"] == 3
    assert "## Professional Experience" in result.markdown
    assert "## Requirements Alignment" in result.markdown
    assert "Invalid evidence bullet should be filtered" not in result.markdown

    model_calls = list(session.scalars(select(ModelCall).order_by(ModelCall.id.asc())).all())
    assert len(model_calls) == 3
    assert [row.stage for row in model_calls] == [
        "preparation_cv_draft",
        "preparation_cv_review",
        "preparation_cv_finalize",
    ]


def test_llm_cv_writer_respects_rpm_limit(monkeypatch):
    from jobbot.preparation import llm_cv_writer as module

    module._RPM_CALL_TIMESTAMPS.clear()

    class _Settings:
        llm_api_rpm = 5

    now = {"value": 120.0}
    sleep_calls: list[float] = []

    def fake_monotonic():
        return now["value"]

    def fake_sleep(seconds: float):
        sleep_calls.append(seconds)
        # Advance time enough to expire oldest timestamps in a single wait.
        now["value"] += 61.0

    monkeypatch.setattr(module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(module.time, "sleep", fake_sleep)

    for _ in range(5):
        module._respect_llm_rpm_limit()

    # Sixth call should require sleep because rpm cap is reached.
    module._respect_llm_rpm_limit()
    assert sleep_calls


def test_gemini_fallback_model_is_used_when_primary_unavailable(monkeypatch):
    from jobbot.preparation import llm_cv_writer as module

    class _Settings:
        llm_cv_writer_temperature = 0.2
        llm_cv_writer_max_tokens = 128
        llm_cv_writer_fallback_model = "gemini-3.1-flash-lite-preview"
        gemini_api_key = "test-key"

    seen_urls: list[str] = []

    def fake_http_post_json(*, url: str, payload: dict, headers: dict[str, str]):
        seen_urls.append(url)
        if "gemini-3-flash-preview" in url:
            raise ValueError("llm_provider_http_error:404:model not found")
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": '{"contact": {}, "professional_summary": []}'}],
                    }
                }
            ]
        }

    monkeypatch.setattr(module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(module, "_http_post_json", fake_http_post_json)

    text, resolved_model = module._provider_invoke_json(
        provider="gemini",
        model_name="gemini-3.0-flash",
        prompt_text="{}",
    )

    assert text
    assert resolved_model == "gemini-3.1-flash-lite-preview"
    assert any("gemini-3-flash-preview" in url for url in seen_urls)
    assert any("gemini-3.1-flash-lite-preview" in url for url in seen_urls)


def test_parse_json_or_raise_extracts_first_json_object_from_mixed_output():
    from jobbot.preparation import llm_cv_writer as module

    raw = "Model preface text\n```json\n{\"contact\":{\"name\":\"Alex\"},\"professional_summary\":[]}\n```\nextra"
    parsed = module._parse_json_or_raise(raw)
    assert parsed["contact"]["name"] == "Alex"
