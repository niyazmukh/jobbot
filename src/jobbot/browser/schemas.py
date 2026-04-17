"""Schemas for browser profile registry operations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from jobbot.models.enums import BrowserProfileType, SessionHealth


class BrowserProfileCreate(BaseModel):
    """Create or register a browser profile for jobbot use."""

    model_config = ConfigDict(extra="forbid")

    profile_key: str
    profile_type: BrowserProfileType
    display_name: str
    storage_path: str
    candidate_profile_slug: str | None = None
    notes: str | None = None


class BrowserProfileHealthUpdate(BaseModel):
    """Update the health state of a registered browser profile."""

    model_config = ConfigDict(extra="forbid")

    session_health: SessionHealth
    notes: str | None = None


class BrowserSessionObservation(BaseModel):
    """Observed signals used to classify session health deterministically."""

    model_config = ConfigDict(extra="forbid")

    login_page_detected: bool = False
    authenticated: bool | None = None
    checkpoint_detected: bool = False
    challenge_page_detected: bool = False
    rate_limit_detected: bool = False
    repeated_redirects: bool = False
    degraded_visibility: bool = False
    visible_job_count: int | None = None
    notes: str | None = None


class BrowserSessionValidationResult(BaseModel):
    """Deterministic classification output for a browser session."""

    model_config = ConfigDict(extra="forbid")

    session_health: SessionHealth
    reasons: list[str] = Field(default_factory=list)
    requires_reauth: bool = False
    block_automation: bool = False


class BrowserAutomationPolicy(BaseModel):
    """Operational decision for whether a browser profile may be used."""

    model_config = ConfigDict(extra="forbid")

    profile_key: str
    session_health: SessionHealth
    allow_discovery: bool = False
    allow_application: bool = False
    requires_reauth: bool = False
    reasons: list[str] = Field(default_factory=list)
    recommended_action: str
