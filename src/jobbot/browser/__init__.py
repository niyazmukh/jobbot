"""Browser profile registry and session health helpers."""

from jobbot.browser.schemas import (
    BrowserAutomationPolicy,
    BrowserProfileCreate,
    BrowserProfileHealthUpdate,
    BrowserSessionObservation,
    BrowserSessionValidationResult,
)
from jobbot.browser.service import (
    build_browser_profile_policy,
    evaluate_session_health,
    get_browser_profile_policy,
    list_browser_profiles,
    register_browser_profile,
    update_browser_profile_health,
    validate_browser_profile_session,
)

__all__ = [
    "BrowserAutomationPolicy",
    "BrowserProfileCreate",
    "BrowserProfileHealthUpdate",
    "BrowserSessionObservation",
    "BrowserSessionValidationResult",
    "build_browser_profile_policy",
    "evaluate_session_health",
    "get_browser_profile_policy",
    "list_browser_profiles",
    "register_browser_profile",
    "update_browser_profile_health",
    "validate_browser_profile_session",
]
