"""Deterministic LinkedIn question extraction helpers."""

from __future__ import annotations

import re
from html import unescape

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.browser.service import get_browser_profile_policy
from jobbot.db.models import CandidateProfile
from jobbot.execution.schemas import (
    DraftLinkedInAssistFieldRead,
    DraftLinkedInAssistPlanRead,
    DraftLinkedInGuardedSubmitCriteriaRead,
    DraftLinkedInQuestionExtractionRead,
    DraftLinkedInQuestionRead,
)

_LABEL_RE = re.compile(
    r"<label\b(?P<attrs>[^>]*)>(?P<text>.*?)</label>",
    flags=re.IGNORECASE | re.DOTALL,
)
_FIELD_RE = re.compile(
    r"<(?P<tag>input|select|textarea)\b(?P<attrs>[^>]*)>",
    flags=re.IGNORECASE | re.DOTALL,
)
_ATTR_RE = re.compile(
    r"(?P<key>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|[^\s\"'>/]+)",
    flags=re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_linkedin_question_widgets(*, page_html: str) -> DraftLinkedInQuestionExtractionRead:
    """Extract typed LinkedIn question widgets and assist-mode routing signal."""

    if not page_html.strip():
        return DraftLinkedInQuestionExtractionRead(
            question_count=0,
            unknown_field_count=0,
            assist_required=True,
            recommended_mode="assist",
            questions=[],
        )

    labels_by_for: dict[str, str] = {}
    for match in _LABEL_RE.finditer(page_html):
        attrs = _parse_attrs(match.group("attrs"))
        field_id = attrs.get("for")
        if not field_id:
            continue
        label_text = _clean_text(match.group("text"))
        if label_text:
            labels_by_for[field_id] = label_text

    questions: list[DraftLinkedInQuestionRead] = []
    unknown_count = 0
    for index, match in enumerate(_FIELD_RE.finditer(page_html), start=1):
        tag = match.group("tag").lower()
        attrs = _parse_attrs(match.group("attrs"))

        if attrs.get("type", "").lower() == "hidden":
            continue

        field_id = attrs.get("id")
        field_name = attrs.get("name")
        field_key = field_id or field_name or f"field_{index}"
        field_type = _resolve_field_type(tag=tag, attrs=attrs)

        question_text = ""
        source = "unknown"
        confidence = 0.35

        if field_id and field_id in labels_by_for:
            question_text = labels_by_for[field_id]
            source = "label_for"
            confidence = 0.93
        elif attrs.get("aria-label"):
            question_text = _clean_text(attrs.get("aria-label", ""))
            source = "aria_label"
            confidence = 0.84
        elif attrs.get("placeholder"):
            question_text = _clean_text(attrs.get("placeholder", ""))
            source = "placeholder"
            confidence = 0.74
        elif field_name:
            question_text = _clean_name(field_name)
            source = "name_attr"
            confidence = 0.58

        if not question_text:
            question_text = "Unlabeled LinkedIn question widget"
            source = "unknown"
            confidence = 0.35

        assist_required = confidence < 0.8 or source in {"name_attr", "unknown"}
        if assist_required:
            unknown_count += 1

        questions.append(
            DraftLinkedInQuestionRead(
                field_key=field_key,
                question_text=question_text,
                field_type=field_type,
                confidence=confidence,
                source=source,
                assist_required=assist_required,
            )
        )

    assist_required = unknown_count > 0 or len(questions) == 0
    recommended_mode = "assist" if assist_required else "draft"
    return DraftLinkedInQuestionExtractionRead(
        question_count=len(questions),
        unknown_field_count=unknown_count,
        assist_required=assist_required,
        recommended_mode=recommended_mode,
        questions=questions,
    )


def build_linkedin_assist_plan(
    session: Session,
    *,
    page_html: str,
    candidate_profile_slug: str | None = None,
    min_auto_confidence: float = 0.8,
) -> DraftLinkedInAssistPlanRead:
    """Build deterministic assist-mode field-fill decisions for LinkedIn widgets."""

    if min_auto_confidence < 0.0 or min_auto_confidence > 1.0:
        raise ValueError("invalid_linkedin_assist_confidence_threshold")

    candidate = None
    if candidate_profile_slug is not None:
        candidate = session.scalar(
            select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
        )
        if candidate is None:
            raise ValueError("candidate_profile_not_found")

    extraction = extract_linkedin_question_widgets(page_html=page_html)
    answer_bank = _build_candidate_answer_bank(candidate)
    fields: list[DraftLinkedInAssistFieldRead] = []
    auto_fill_count = 0
    assist_review_count = 0
    blocked_auto_action_count = 0

    for question in extraction.questions:
        answer = _resolve_candidate_answer(answer_bank, question.question_text)
        low_confidence = question.confidence < min_auto_confidence

        if low_confidence:
            action = "assist_review"
            reason = "blocked_low_extraction_confidence"
            assist_review_count += 1
            blocked_auto_action_count += 1
        elif answer is None:
            action = "assist_review"
            reason = "no_deterministic_candidate_answer"
            assist_review_count += 1
        else:
            action = "auto_fill_candidate_fact"
            reason = "deterministic_candidate_profile_match"
            auto_fill_count += 1

        fields.append(
            DraftLinkedInAssistFieldRead(
                field_key=question.field_key,
                question_text=question.question_text,
                field_type=question.field_type,
                confidence=question.confidence,
                source=question.source,
                action=action,
                proposed_answer=answer,
                reason=reason,
            )
        )

    recommended_mode = "assist" if assist_review_count > 0 else "draft"
    recommended_actions = [
        "Auto-fill only deterministic high-confidence fields from approved candidate profile data.",
        "Route low-confidence or unmatched LinkedIn questions to assist review before applying.",
    ]
    if blocked_auto_action_count > 0:
        recommended_actions.append(
            f"Blocked {blocked_auto_action_count} potential auto actions due to extraction confidence below {min_auto_confidence}."
        )

    return DraftLinkedInAssistPlanRead(
        candidate_profile_slug=candidate_profile_slug,
        question_count=extraction.question_count,
        auto_fill_count=auto_fill_count,
        assist_review_count=assist_review_count,
        blocked_auto_action_count=blocked_auto_action_count,
        recommended_mode=recommended_mode,
        fields=fields,
        recommended_actions=recommended_actions,
    )


def evaluate_linkedin_guarded_submit_criteria(
    session: Session,
    *,
    profile_key: str,
    page_html: str,
    candidate_profile_slug: str | None = None,
    min_auto_confidence: float = 0.8,
) -> DraftLinkedInGuardedSubmitCriteriaRead:
    """Evaluate deterministic LinkedIn guarded-submit eligibility criteria."""

    if min_auto_confidence < 0.0 or min_auto_confidence > 1.0:
        raise ValueError("invalid_linkedin_assist_confidence_threshold")

    try:
        policy = get_browser_profile_policy(session, profile_key)
    except ValueError as exc:
        detail = str(exc)
        if detail.startswith("Unknown browser profile key:"):
            raise ValueError("browser_profile_not_found") from exc
        raise

    plan = build_linkedin_assist_plan(
        session,
        page_html=page_html,
        candidate_profile_slug=candidate_profile_slug,
        min_auto_confidence=min_auto_confidence,
    )

    stop_reasons: list[str] = []
    if not policy.allow_application:
        stop_reasons.append(f"linkedin_session_not_ready:{policy.session_health.value}")
    if plan.recommended_mode != "draft":
        stop_reasons.append("linkedin_assist_mode_required")
    if plan.assist_review_count > 0:
        stop_reasons.append(f"assist_review_required:{plan.assist_review_count}")
    if plan.blocked_auto_action_count > 0:
        stop_reasons.append(f"blocked_auto_actions:{plan.blocked_auto_action_count}")

    allow_guarded_submit = len(stop_reasons) == 0
    recommended_actions = [
        "Allow LinkedIn guarded submit only when browser session health is application-ready.",
        "Allow LinkedIn guarded submit only when assist-plan mode stays draft with zero assist-review blockers.",
    ]
    if not policy.allow_application:
        recommended_actions.append(
            f"LinkedIn browser profile requires remediation before submit: {policy.recommended_action}."
        )
    if plan.assist_review_count > 0:
        recommended_actions.append(
            "Resolve LinkedIn assist-review questions and rerun guarded-submit criteria."
        )

    return DraftLinkedInGuardedSubmitCriteriaRead(
        profile_key=profile_key,
        candidate_profile_slug=candidate_profile_slug,
        session_health=policy.session_health.value,
        session_requires_reauth=policy.requires_reauth,
        allow_session_automation=policy.allow_application,
        question_count=plan.question_count,
        assist_review_count=plan.assist_review_count,
        blocked_auto_action_count=plan.blocked_auto_action_count,
        recommended_mode=plan.recommended_mode,
        min_auto_confidence=min_auto_confidence,
        allow_guarded_submit=allow_guarded_submit,
        stop_reasons=stop_reasons,
        recommended_actions=recommended_actions,
    )


def _resolve_field_type(*, tag: str, attrs: dict[str, str]) -> str:
    if tag == "select":
        return "select"
    if tag == "textarea":
        return "textarea"
    raw_input_type = attrs.get("type", "").strip().lower() or "text"
    if raw_input_type in {"text", "email", "tel", "url", "number", "checkbox", "radio"}:
        return raw_input_type
    return "text"


def _parse_attrs(raw_attrs: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw_attrs):
        key = match.group("key").strip().lower()
        value = match.group("value").strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        parsed[key] = unescape(value)
    return parsed


def _clean_text(value: str) -> str:
    without_tags = _TAG_RE.sub(" ", value)
    compact = " ".join(unescape(without_tags).split())
    return compact.strip()


def _clean_name(value: str) -> str:
    normalized = value.replace("_", " ").replace("-", " ").replace(".", " ")
    return " ".join(part for part in normalized.split() if part).strip().capitalize()


def _build_candidate_answer_bank(candidate: CandidateProfile | None) -> dict[str, str]:
    if candidate is None:
        return {}

    personal = dict(candidate.personal_details or {})
    source = dict(candidate.source_profile_data or {})
    full_name = str(personal.get("name") or candidate.name or "").strip()
    name_parts = full_name.split()
    first_name = str(personal.get("first_name") or (name_parts[0] if name_parts else "")).strip()
    last_name = str(personal.get("last_name") or (name_parts[-1] if len(name_parts) > 1 else "")).strip()

    return {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": str(personal.get("email") or "").strip(),
        "phone": str(personal.get("phone") or "").strip(),
        "location": str(personal.get("location") or "").strip(),
        "linkedin_url": str(personal.get("linkedin_url") or source.get("linkedin_url") or "").strip(),
        "portfolio_url": str(personal.get("portfolio_url") or source.get("portfolio_url") or "").strip(),
    }


def _resolve_candidate_answer(answer_bank: dict[str, str], question_text: str) -> str | None:
    text = question_text.lower()
    if "first name" in text:
        return _non_empty(answer_bank.get("first_name"))
    if "last name" in text:
        return _non_empty(answer_bank.get("last_name"))
    if "full name" in text or text.strip() == "name":
        return _non_empty(answer_bank.get("full_name"))
    if "email" in text:
        return _non_empty(answer_bank.get("email"))
    if "phone" in text or "mobile" in text:
        return _non_empty(answer_bank.get("phone"))
    if "linkedin" in text:
        return _non_empty(answer_bank.get("linkedin_url"))
    if "portfolio" in text or "website" in text:
        return _non_empty(answer_bank.get("portfolio_url"))
    if "location" in text or "city" in text:
        return _non_empty(answer_bank.get("location"))
    return None


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
