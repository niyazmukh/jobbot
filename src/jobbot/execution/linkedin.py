"""Deterministic LinkedIn question extraction helpers."""

from __future__ import annotations

import re
from html import unescape

from jobbot.execution.schemas import (
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
