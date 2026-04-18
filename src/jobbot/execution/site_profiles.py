"""Deterministic ATS-specific execution profiles."""

from __future__ import annotations


_DEFAULT_SELECTOR = ("[data-jobbot-field='{field_key}']", 0.7, True)

_GREENHOUSE_SELECTOR_OVERLAYS: dict[str, tuple[list[str], float, bool]] = {
    "full_name": (["input[name='name']"], 0.98, False),
    "first_name": (["input[name='first_name']"], 0.99, False),
    "last_name": (["input[name='last_name']"], 0.99, False),
    "email": (["input[name='email']"], 0.99, False),
    "phone": (["input[name='phone']", "input[name='phone_number']"], 0.97, False),
    "location": (["input[name='location']"], 0.94, False),
    "linkedin_url": (
        ["input[name='urls[LinkedIn]']", "input[name*='linkedin']", "input[name='linkedin']"],
        0.9,
        True,
    ),
    "work_authorization": (["select[name*='work_authorization']", "input[name*='work_authorization']"], 0.86, True),
    "resume_upload": (["input[type='file'][name='resume']", "input[type='file']"], 0.99, False),
    "why_this_role": (["textarea[name='cover_letter']", "textarea[name*='question']"], 0.88, True),
    "relevant_skills": (["textarea[name*='question']"], 0.8, True),
    "fit_gap_clarification": (["textarea[name*='question']"], 0.75, True),
}

_LEVER_SELECTOR_OVERLAYS: dict[str, tuple[list[str], float, bool]] = {
    "full_name": (["input[name='name']", "input[name='full_name']"], 0.97, False),
    "first_name": (["input[name='first_name']"], 0.98, False),
    "last_name": (["input[name='last_name']"], 0.98, False),
    "email": (["input[name='email']"], 0.99, False),
    "phone": (["input[name='phone']", "input[name='phone_number']"], 0.96, False),
    "location": (
        ["input[name='location']", "input[name='city']", "input[name='location[city]']"],
        0.9,
        False,
    ),
    "linkedin_url": (["input[name*='linkedin']", "input[name='urls[LinkedIn]']"], 0.86, True),
    "work_authorization": (["select[name*='work_authorization']", "input[name*='work_authorization']"], 0.83, True),
    "resume_upload": (
        ["input[type='file'][name='resume']", "input[name='resume']", "input[type='file']"],
        0.99,
        False,
    ),
    "why_this_role": (["textarea[name*='question']", "textarea[name='comments']"], 0.84, True),
    "relevant_skills": (["textarea[name*='question']", "textarea[name='comments']"], 0.78, True),
    "fit_gap_clarification": (["textarea[name*='question']", "textarea[name='comments']"], 0.74, True),
}

_WORKDAY_SELECTOR_OVERLAYS: dict[str, tuple[list[str], float, bool]] = {
    "first_name": (
        [
            "input[name='firstName']",
            "input[name*='firstName']",
            "input[id*='firstName']",
        ],
        0.98,
        False,
    ),
    "last_name": (
        [
            "input[name='lastName']",
            "input[name*='lastName']",
            "input[id*='lastName']",
        ],
        0.98,
        False,
    ),
    "email": (
        [
            "input[type='email']",
            "input[name='email']",
            "input[name*='email']",
        ],
        0.97,
        False,
    ),
    "phone": (
        [
            "input[type='tel']",
            "input[name='phoneNumber']",
            "input[name*='phone']",
        ],
        0.94,
        False,
    ),
    "linkedin_url": (
        [
            "input[name*='linkedin']",
            "input[name*='profileUrl']",
        ],
        0.86,
        True,
    ),
    "resume_upload": (
        [
            "input[type='file'][name*='resume']",
            "input[type='file'][aria-label*='Resume']",
            "input[type='file']",
        ],
        0.99,
        False,
    ),
    "why_this_role": (
        [
            "textarea[name*='question']",
            "textarea[name*='coverLetter']",
            "textarea",
        ],
        0.82,
        True,
    ),
}

_SITE_REQUIRED_FIELDS: dict[str, list[str]] = {
    "greenhouse": ["first_name", "last_name", "email", "resume_upload"],
    "lever": ["full_name", "email", "resume_upload"],
    "workday": ["first_name", "last_name", "email", "resume_upload"],
}

_SITE_GUARDED_SUBMIT_PLANS: dict[str, dict[str, object]] = {
    "greenhouse": {
        "submit_button_selectors": [
            "button[type='submit']",
            "button#submit_app",
            "button[data-qa='submit-application']",
        ],
        "review_step_selectors": [
            "[data-qa='application-review']",
            ".application-review",
        ],
        "confirmation_markers": [
            "application submitted",
            "thank you for applying",
        ],
    },
    "lever": {
        "submit_button_selectors": [
            "button[type='submit']",
            "button[data-qa='application-submit']",
            "button.postings-btn--large",
        ],
        "review_step_selectors": [
            ".application-page",
            "[data-qa='application-form']",
        ],
        "confirmation_markers": [
            "application submitted",
            "thanks for applying",
        ],
    },
    "workday": {
        "submit_button_selectors": [
            "button[type='submit']",
            "button[data-automation-id='bottom-navigation-next-button']",
            "button[data-automation-id='apply-button']",
        ],
        "review_step_selectors": [
            "[data-automation-id='reviewSubmit']",
            "[data-automation-id='bottom-navigation-next-button']",
        ],
        "confirmation_markers": [
            "application submitted",
            "thank you for applying",
            "submission complete",
        ],
    },
}


def selector_overlay_for_site(
    site_vendor: str,
    field_key: str,
) -> tuple[list[str], float, bool]:
    """Resolve selector overlays for supported ATS vendors."""

    normalized_site = site_vendor.strip().lower()
    if normalized_site == "greenhouse":
        overlay = _GREENHOUSE_SELECTOR_OVERLAYS.get(field_key)
    elif normalized_site == "lever":
        overlay = _LEVER_SELECTOR_OVERLAYS.get(field_key)
    elif normalized_site == "workday":
        overlay = _WORKDAY_SELECTOR_OVERLAYS.get(field_key)
    else:
        overlay = None

    if overlay is not None:
        return overlay

    selector_template, confidence_gate, manual_review_required = _DEFAULT_SELECTOR
    return ([selector_template.format(field_key=field_key)], confidence_gate, manual_review_required)


def required_fields_for_site(site_vendor: str) -> list[str]:
    """Return deterministic required fields for a supported ATS vendor."""

    return list(_SITE_REQUIRED_FIELDS.get(site_vendor.strip().lower(), []))


def guarded_submit_plan_for_site(site_vendor: str) -> dict[str, object]:
    """Return deterministic guarded-submit strategy for one ATS vendor."""

    normalized_site = site_vendor.strip().lower()
    plan = _SITE_GUARDED_SUBMIT_PLANS.get(normalized_site)
    if plan is not None:
        return {
            "site_vendor": normalized_site,
            "submit_button_selectors": list(plan.get("submit_button_selectors") or []),
            "review_step_selectors": list(plan.get("review_step_selectors") or []),
            "confirmation_markers": list(plan.get("confirmation_markers") or []),
        }
    return {
        "site_vendor": normalized_site,
        "submit_button_selectors": ["button[type='submit']"],
        "review_step_selectors": [],
        "confirmation_markers": [],
    }
