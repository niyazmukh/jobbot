"""Browser profile registry service."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.browser.schemas import (
    BrowserAutomationPolicy,
    BrowserProfileCreate,
    BrowserProfileHealthUpdate,
    BrowserSessionObservation,
    BrowserSessionValidationResult,
)
from jobbot.db.models import BrowserProfile, CandidateProfile, utcnow
from jobbot.models.enums import SessionHealth


def register_browser_profile(session: Session, payload: BrowserProfileCreate) -> BrowserProfile:
    """Create or update a browser profile registry entry."""

    candidate_profile_id = None
    if payload.candidate_profile_slug:
        candidate = session.scalar(
            select(CandidateProfile).where(CandidateProfile.slug == payload.candidate_profile_slug)
        )
        if candidate is None:
            raise ValueError(f"Unknown candidate profile slug: {payload.candidate_profile_slug}")
        candidate_profile_id = candidate.id

    profile = session.scalar(
        select(BrowserProfile).where(BrowserProfile.profile_key == payload.profile_key)
    )
    if profile is None:
        profile = BrowserProfile(
            candidate_profile_id=candidate_profile_id,
            profile_key=payload.profile_key,
            profile_type=payload.profile_type,
            display_name=payload.display_name,
            storage_path=payload.storage_path,
            validation_details={},
            notes=payload.notes,
        )
        session.add(profile)
    else:
        profile.candidate_profile_id = candidate_profile_id
        profile.profile_type = payload.profile_type
        profile.display_name = payload.display_name
        profile.storage_path = payload.storage_path
        profile.notes = payload.notes
        profile.updated_at = utcnow()

    session.commit()
    session.refresh(profile)
    return profile


def update_browser_profile_health(
    session: Session,
    profile_key: str,
    payload: BrowserProfileHealthUpdate,
) -> BrowserProfile:
    """Update session health and validation metadata for a browser profile."""

    profile = session.scalar(
        select(BrowserProfile).where(BrowserProfile.profile_key == profile_key)
    )
    if profile is None:
        raise ValueError(f"Unknown browser profile key: {profile_key}")

    profile.session_health = payload.session_health.value
    profile.notes = payload.notes or profile.notes
    profile.last_validated_at = utcnow()
    profile.updated_at = utcnow()
    session.commit()
    session.refresh(profile)
    return profile


def mark_browser_profile_used(session: Session, profile_key: str) -> BrowserProfile:
    """Update the last-used timestamp for a browser profile."""

    profile = session.scalar(
        select(BrowserProfile).where(BrowserProfile.profile_key == profile_key)
    )
    if profile is None:
        raise ValueError(f"Unknown browser profile key: {profile_key}")

    profile.last_used_at = utcnow()
    profile.updated_at = utcnow()
    session.commit()
    session.refresh(profile)
    return profile


def list_browser_profiles(session: Session) -> list[BrowserProfile]:
    """Return all browser profiles ordered by profile key."""

    return list(
        session.scalars(select(BrowserProfile).order_by(BrowserProfile.profile_key)).all()
    )


def evaluate_session_health(observation: BrowserSessionObservation) -> BrowserSessionValidationResult:
    """Classify session health from deterministic browser signals."""

    reasons: list[str] = []

    if observation.checkpoint_detected:
        reasons.append("checkpoint_detected")
        return BrowserSessionValidationResult(
            session_health=SessionHealth.CHECKPOINTED,
            reasons=reasons,
            requires_reauth=True,
            block_automation=True,
        )

    if observation.rate_limit_detected:
        reasons.append("rate_limit_detected")
        return BrowserSessionValidationResult(
            session_health=SessionHealth.RATE_LIMITED,
            reasons=reasons,
            requires_reauth=False,
            block_automation=True,
        )

    if observation.login_page_detected or observation.authenticated is False:
        if observation.login_page_detected:
            reasons.append("login_page_detected")
        if observation.authenticated is False:
            reasons.append("authenticated_false")
        return BrowserSessionValidationResult(
            session_health=SessionHealth.LOGIN_REQUIRED,
            reasons=reasons,
            requires_reauth=True,
            block_automation=True,
        )

    if (
        observation.challenge_page_detected
        or observation.repeated_redirects
        or observation.degraded_visibility
    ):
        if observation.challenge_page_detected:
            reasons.append("challenge_page_detected")
        if observation.repeated_redirects:
            reasons.append("repeated_redirects")
        if observation.degraded_visibility:
            reasons.append("degraded_visibility")
        return BrowserSessionValidationResult(
            session_health=SessionHealth.SUSPECTED_FLAGGED,
            reasons=reasons,
            requires_reauth=False,
            block_automation=True,
        )

    reasons.append("session_signals_healthy")
    return BrowserSessionValidationResult(
        session_health=SessionHealth.HEALTHY,
        reasons=reasons,
        requires_reauth=False,
        block_automation=False,
    )


def validate_browser_profile_session(
    session: Session,
    profile_key: str,
    observation: BrowserSessionObservation,
) -> BrowserProfile:
    """Evaluate a profile's observed signals and persist the resulting health."""

    profile = session.scalar(
        select(BrowserProfile).where(BrowserProfile.profile_key == profile_key)
    )
    if profile is None:
        raise ValueError(f"Unknown browser profile key: {profile_key}")

    result = evaluate_session_health(observation)
    profile.session_health = result.session_health.value
    profile.validation_details = {
        "reasons": result.reasons,
        "requires_reauth": result.requires_reauth,
        "block_automation": result.block_automation,
        "observation": observation.model_dump(),
    }
    if observation.notes:
        profile.notes = observation.notes
    profile.last_validated_at = utcnow()
    profile.updated_at = utcnow()
    session.commit()
    session.refresh(profile)
    return profile


def build_browser_profile_policy(profile: BrowserProfile) -> BrowserAutomationPolicy:
    """Convert persisted session state into an explicit automation policy."""

    health = SessionHealth(profile.session_health)
    reasons = list(profile.validation_details.get("reasons", [])) if profile.validation_details else []

    if health is SessionHealth.HEALTHY:
        return BrowserAutomationPolicy(
            profile_key=profile.profile_key,
            session_health=health,
            allow_discovery=True,
            allow_application=True,
            requires_reauth=False,
            reasons=reasons or ["session_healthy"],
            recommended_action="proceed",
        )

    if health is SessionHealth.LOGIN_REQUIRED:
        return BrowserAutomationPolicy(
            profile_key=profile.profile_key,
            session_health=health,
            allow_discovery=False,
            allow_application=False,
            requires_reauth=True,
            reasons=reasons or ["login_required"],
            recommended_action="reauthenticate_profile",
        )

    if health is SessionHealth.CHECKPOINTED:
        return BrowserAutomationPolicy(
            profile_key=profile.profile_key,
            session_health=health,
            allow_discovery=False,
            allow_application=False,
            requires_reauth=True,
            reasons=reasons or ["checkpointed"],
            recommended_action="manual_checkpoint_recovery",
        )

    if health is SessionHealth.RATE_LIMITED:
        return BrowserAutomationPolicy(
            profile_key=profile.profile_key,
            session_health=health,
            allow_discovery=False,
            allow_application=False,
            requires_reauth=False,
            reasons=reasons or ["rate_limited"],
            recommended_action="cooldown_and_revalidate",
        )

    return BrowserAutomationPolicy(
        profile_key=profile.profile_key,
        session_health=health,
        allow_discovery=False,
        allow_application=False,
        requires_reauth=False,
        reasons=reasons or ["suspected_flagged"],
        recommended_action="quarantine_profile",
    )


def get_browser_profile_policy(session: Session, profile_key: str) -> BrowserAutomationPolicy:
    """Load a browser profile and return its current automation policy."""

    profile = session.scalar(
        select(BrowserProfile).where(BrowserProfile.profile_key == profile_key)
    )
    if profile is None:
        raise ValueError(f"Unknown browser profile key: {profile_key}")
    return build_browser_profile_policy(profile)
