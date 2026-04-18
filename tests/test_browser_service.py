from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.browser.schemas import BrowserProfileCreate, BrowserSessionObservation
from jobbot.browser.service import (
    build_linkedin_session_observation,
    get_browser_profile_policy,
    evaluate_session_health,
    evaluate_linkedin_session_health,
    mark_browser_profile_used,
    register_browser_profile,
    validate_browser_profile_session,
)
from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.models.enums import BrowserProfileType, SessionHealth


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_evaluate_session_health_prioritizes_login_required():
    result = evaluate_session_health(
        BrowserSessionObservation(login_page_detected=True, repeated_redirects=True)
    )

    assert result.session_health == SessionHealth.LOGIN_REQUIRED
    assert result.requires_reauth is True
    assert result.block_automation is True


def test_evaluate_session_health_marks_suspected_flagged():
    result = evaluate_session_health(
        BrowserSessionObservation(challenge_page_detected=True, degraded_visibility=True)
    )

    assert result.session_health == SessionHealth.SUSPECTED_FLAGGED
    assert result.block_automation is True


def test_validate_browser_profile_session_persists_details():
    session = make_session()
    profile = register_browser_profile(
        session,
        BrowserProfileCreate(
            profile_key="apply-main",
            profile_type=BrowserProfileType.APPLICATION,
            display_name="Main Apply Profile",
            storage_path="C:/profiles/apply-main",
        ),
    )

    updated = validate_browser_profile_session(
        session,
        profile.profile_key,
        BrowserSessionObservation(rate_limit_detected=True, notes="429 on LinkedIn search"),
    )

    assert updated.session_health == SessionHealth.RATE_LIMITED.value
    assert updated.validation_details["block_automation"] is True
    assert "rate_limit_detected" in updated.validation_details["reasons"]


def test_get_browser_profile_policy_requires_reauth_for_login_required():
    session = make_session()
    register_browser_profile(
        session,
        BrowserProfileCreate(
            profile_key="discovery-main",
            profile_type=BrowserProfileType.DISCOVERY,
            display_name="Discovery Profile",
            storage_path="C:/profiles/discovery-main",
        ),
    )

    validate_browser_profile_session(
        session,
        "discovery-main",
        BrowserSessionObservation(login_page_detected=True),
    )

    policy = get_browser_profile_policy(session, "discovery-main")

    assert policy.allow_discovery is False
    assert policy.allow_application is False
    assert policy.requires_reauth is True
    assert policy.recommended_action == "reauthenticate_profile"


def test_get_browser_profile_policy_allows_healthy_profile():
    session = make_session()
    register_browser_profile(
        session,
        BrowserProfileCreate(
            profile_key="healthy-apply",
            profile_type=BrowserProfileType.APPLICATION,
            display_name="Healthy Apply Profile",
            storage_path="C:/profiles/healthy-apply",
        ),
    )

    validate_browser_profile_session(
        session,
        "healthy-apply",
        BrowserSessionObservation(authenticated=True),
    )

    policy = get_browser_profile_policy(session, "healthy-apply")

    assert policy.allow_discovery is True
    assert policy.allow_application is True
    assert policy.requires_reauth is False
    assert policy.recommended_action == "proceed"


def test_mark_browser_profile_used_updates_last_used_timestamp():
    session = make_session()
    register_browser_profile(
        session,
        BrowserProfileCreate(
            profile_key="touch-me",
            profile_type=BrowserProfileType.DISCOVERY,
            display_name="Touch Me",
            storage_path="C:/profiles/touch-me",
        ),
    )

    touched = mark_browser_profile_used(session, "touch-me")

    assert touched.last_used_at is not None


def test_get_browser_profile_policy_quarantines_suspected_flagged_profile():
    session = make_session()
    register_browser_profile(
        session,
        BrowserProfileCreate(
            profile_key="flagged-profile",
            profile_type=BrowserProfileType.APPLICATION,
            display_name="Flagged Profile",
            storage_path="C:/profiles/flagged-profile",
        ),
    )

    validate_browser_profile_session(
        session,
        "flagged-profile",
        BrowserSessionObservation(challenge_page_detected=True, degraded_visibility=True),
    )

    policy = get_browser_profile_policy(session, "flagged-profile")

    assert policy.allow_discovery is False
    assert policy.allow_application is False
    assert policy.requires_reauth is False
    assert policy.recommended_action == "quarantine_profile"


def test_get_browser_profile_policy_requires_manual_recovery_for_checkpointed():
    session = make_session()
    register_browser_profile(
        session,
        BrowserProfileCreate(
            profile_key="checkpointed-profile",
            profile_type=BrowserProfileType.APPLICATION,
            display_name="Checkpointed Profile",
            storage_path="C:/profiles/checkpointed-profile",
        ),
    )

    validate_browser_profile_session(
        session,
        "checkpointed-profile",
        BrowserSessionObservation(checkpoint_detected=True),
    )

    policy = get_browser_profile_policy(session, "checkpointed-profile")

    assert policy.allow_discovery is False
    assert policy.allow_application is False
    assert policy.requires_reauth is True
    assert policy.recommended_action == "manual_checkpoint_recovery"


def test_build_linkedin_session_observation_detects_login_and_redirect_risk():
    observation = build_linkedin_session_observation(
        page_url="https://www.linkedin.com/login",
        page_title="LinkedIn Login",
        page_content="Sign in to LinkedIn",
        redirect_count=4,
        authenticated=True,
    )

    assert observation.login_page_detected is True
    assert observation.repeated_redirects is True


def test_evaluate_linkedin_session_health_detects_challenge():
    result = evaluate_linkedin_session_health(
        page_url="https://www.linkedin.com/checkpoint/challenge",
        page_title="Security Verification",
        page_content="Please verify your identity and complete the CAPTCHA.",
        redirect_count=1,
        authenticated=True,
    )

    assert result.session_health == SessionHealth.CHECKPOINTED
    assert result.block_automation is True
