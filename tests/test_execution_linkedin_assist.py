from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import CandidateProfile
from jobbot.execution.linkedin import build_linkedin_assist_plan


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
