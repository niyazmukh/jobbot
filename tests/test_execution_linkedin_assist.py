from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import BrowserProfile, CandidateProfile
from jobbot.execution.linkedin import (
    build_linkedin_assist_plan,
    evaluate_linkedin_guarded_submit_criteria,
)
from jobbot.models.enums import BrowserProfileType, SessionHealth


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_build_linkedin_assist_plan_blocks_low_confidence_auto_actions():
    session = make_session()
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={"email": "alex@example.com"},
        source_profile_data={"linkedin_url": "https://www.linkedin.com/in/alex-doe"},
    )
    session.add(candidate)
    session.commit()

    html = (
        "<form>"
        "<label for='emailAddress'>Email address</label>"
        "<input id='emailAddress' name='emailAddress' type='email'>"
        "<input name='customQuestion_77' type='text'>"
        "</form>"
    )
    plan = build_linkedin_assist_plan(
        session,
        page_html=html,
        candidate_profile_slug="alex-doe",
        min_auto_confidence=0.8,
    )

    assert plan.question_count == 2
    assert plan.auto_fill_count == 1
    assert plan.assist_review_count == 1
    assert plan.blocked_auto_action_count == 1
    assert plan.recommended_mode == "assist"
    assert any(row.action == "auto_fill_candidate_fact" for row in plan.fields)
    assert any(row.action == "assist_review" for row in plan.fields)


def test_build_linkedin_assist_plan_returns_draft_when_all_fields_are_safe():
    session = make_session()
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={
            "email": "alex@example.com",
            "phone": "+1-555-0101",
        },
    )
    session.add(candidate)
    session.commit()

    html = (
        "<form>"
        "<label for='emailAddress'>Email address</label>"
        "<input id='emailAddress' name='emailAddress' type='email'>"
        "<label for='phoneNumber'>Phone number</label>"
        "<input id='phoneNumber' name='phoneNumber' type='tel'>"
        "</form>"
    )
    plan = build_linkedin_assist_plan(
        session,
        page_html=html,
        candidate_profile_slug="alex-doe",
        min_auto_confidence=0.8,
    )

    assert plan.question_count == 2
    assert plan.auto_fill_count == 2
    assert plan.assist_review_count == 0
    assert plan.blocked_auto_action_count == 0
    assert plan.recommended_mode == "draft"


def test_evaluate_linkedin_guarded_submit_criteria_blocks_on_session_health():
    session = make_session()
    session.add(
        BrowserProfile(
            profile_key="linkedin-main",
            profile_type=BrowserProfileType.APPLICATION,
            display_name="LinkedIn Main",
            storage_path="C:/profiles/linkedin-main",
            session_health=SessionHealth.CHECKPOINTED.value,
            validation_details={"reasons": ["checkpoint_detected"]},
        )
    )
    session.commit()

    html = (
        "<form>"
        "<label for='emailAddress'>Email address</label>"
        "<input id='emailAddress' name='emailAddress' type='email'>"
        "</form>"
    )
    result = evaluate_linkedin_guarded_submit_criteria(
        session,
        profile_key="linkedin-main",
        page_html=html,
    )

    assert result.allow_session_automation is False
    assert result.allow_guarded_submit is False
    assert "linkedin_session_not_ready:checkpointed" in result.stop_reasons


def test_evaluate_linkedin_guarded_submit_criteria_allows_when_session_and_assist_are_clear():
    session = make_session()
    session.add(
        BrowserProfile(
            profile_key="linkedin-main",
            profile_type=BrowserProfileType.APPLICATION,
            display_name="LinkedIn Main",
            storage_path="C:/profiles/linkedin-main",
            session_health=SessionHealth.HEALTHY.value,
            validation_details={"reasons": ["session_signals_healthy"]},
        )
    )
    candidate = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={
            "email": "alex@example.com",
            "phone": "+1-555-0101",
        },
    )
    session.add(candidate)
    session.commit()

    html = (
        "<form>"
        "<label for='emailAddress'>Email address</label>"
        "<input id='emailAddress' name='emailAddress' type='email'>"
        "<label for='phoneNumber'>Phone number</label>"
        "<input id='phoneNumber' name='phoneNumber' type='tel'>"
        "</form>"
    )
    result = evaluate_linkedin_guarded_submit_criteria(
        session,
        profile_key="linkedin-main",
        page_html=html,
        candidate_profile_slug="alex-doe",
    )

    assert result.allow_session_automation is True
    assert result.allow_guarded_submit is True
    assert result.recommended_mode == "draft"
    assert result.assist_review_count == 0
    assert result.stop_reasons == []
