"""ATS-specific execution handling helpers for target-open and submit-gate flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_SUPPORTED_PAGE_OPEN_SITES = {"greenhouse", "lever", "workday"}
_SUPPORTED_SUBMIT_GATE_SITES = {"greenhouse", "lever", "workday"}


@dataclass(frozen=True)
class FieldResolutionDecision:
    """Deterministic decision for one field during target-open resolution."""

    resolved_selector: str | None
    resolution_status: str


@dataclass(frozen=True)
class SubmitGateSignals:
    """Deterministic submit-gate reduction from resolved field outcomes."""

    resolved_required_fields: list[str]
    manual_review_fields: list[str]
    unresolved_fields: list[str]
    stop_reasons: list[str]


def supports_target_open(site_vendor: str) -> bool:
    """Return whether target-open resolution is supported for this ATS vendor."""

    return site_vendor.strip().lower() in _SUPPORTED_PAGE_OPEN_SITES


def supports_submit_gate(site_vendor: str) -> bool:
    """Return whether guarded submit-gate evaluation is supported for this ATS vendor."""

    return site_vendor.strip().lower() in _SUPPORTED_SUBMIT_GATE_SITES


def resolve_field_for_target_open(
    *,
    selectors: list[str],
    confidence_gate: float,
    manual_review_required: bool,
) -> FieldResolutionDecision:
    """Resolve one field deterministically for target-open page inspection."""

    resolved_selector = selectors[0] if selectors and confidence_gate >= 0.85 else None
    resolution_status = "resolved" if resolved_selector and not manual_review_required else "manual_review"
    if not selectors:
        resolution_status = "unresolved"
    return FieldResolutionDecision(
        resolved_selector=resolved_selector,
        resolution_status=resolution_status,
    )


def collect_submit_gate_signals(
    *,
    required_fields: list[str],
    resolution_entries: list[tuple[str, str, bool]],
) -> SubmitGateSignals:
    """Reduce resolved field outcomes into stop reasons for guarded submit decisions."""

    resolved_required_fields: list[str] = []
    manual_review_fields: list[str] = []
    unresolved_fields: list[str] = []

    for field_key, resolution_status, manual_review_required in resolution_entries:
        if field_key in required_fields and resolution_status == "resolved":
            resolved_required_fields.append(field_key)
        if resolution_status != "resolved":
            unresolved_fields.append(field_key)
        if manual_review_required or (
            manual_review_required is False and field_key.startswith("why_")
        ):
            manual_review_fields.append(field_key)

    stop_reasons: list[str] = []
    missing_required = [field for field in required_fields if field not in resolved_required_fields]
    if missing_required:
        stop_reasons.extend(f"missing_required_field:{field}" for field in missing_required)
    for field in manual_review_fields:
        stop_reasons.append(f"manual_review_required:{field}")
    for field in unresolved_fields:
        if field not in manual_review_fields and field not in missing_required:
            stop_reasons.append(f"unresolved_field:{field}")

    return SubmitGateSignals(
        resolved_required_fields=resolved_required_fields,
        manual_review_fields=manual_review_fields,
        unresolved_fields=unresolved_fields,
        stop_reasons=stop_reasons,
    )


def build_target_resolution_artifact_payload(
    *,
    application_id: int,
    attempt_id: int,
    job_id: int,
    candidate_profile_slug: str,
    site_vendor: str,
    resolved_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build deterministic target-open resolution artifact payload."""

    return {
        "application_id": application_id,
        "attempt_id": attempt_id,
        "job_id": job_id,
        "candidate_profile_slug": candidate_profile_slug,
        "site_vendor": site_vendor,
        "resolved": resolved_entries,
    }


def build_target_open_event_payload(
    *,
    site_vendor: str,
    target_url: str,
    capture_metadata: dict[str, Any],
    opened_page_artifact_id: int,
    resolution_artifact_id: int,
    screenshot_artifact_id: int | None = None,
    trace_artifact_id: int | None = None,
    resolved_count: int,
    unresolved_count: int,
) -> dict[str, Any]:
    """Build deterministic target-open event payload."""

    return {
        "site_vendor": site_vendor,
        "target_url": target_url,
        "target_capture": capture_metadata,
        "opened_page_artifact_id": opened_page_artifact_id,
        "resolution_artifact_id": resolution_artifact_id,
        "screenshot_artifact_id": screenshot_artifact_id,
        "trace_artifact_id": trace_artifact_id,
        "resolved_count": resolved_count,
        "unresolved_count": unresolved_count,
    }


def build_target_open_attempt_note(
    *,
    capture_method: str | None,
    resolved_count: int,
    unresolved_count: int,
) -> str:
    """Build operator-facing attempt note for target-open outcomes."""

    return (
        f"Target opened via {capture_method or 'unknown'}; "
        f"resolved={resolved_count} unresolved={unresolved_count}."
    )


def build_submit_gate_artifact_payload(
    *,
    application_id: int,
    attempt_id: int,
    job_id: int,
    candidate_profile_slug: str,
    site_vendor: str,
    confidence_score: float,
    allow_submit: bool,
    stop_reasons: list[str],
    required_fields: list[str],
    resolved_required_fields: list[str],
    manual_review_fields: list[str],
) -> dict[str, Any]:
    """Build deterministic guarded submit-gate artifact payload."""

    return {
        "application_id": application_id,
        "attempt_id": attempt_id,
        "job_id": job_id,
        "candidate_profile_slug": candidate_profile_slug,
        "site_vendor": site_vendor,
        "confidence_score": confidence_score,
        "allow_submit": allow_submit,
        "stop_reasons": stop_reasons,
        "required_fields": required_fields,
        "resolved_required_fields": resolved_required_fields,
        "manual_review_fields": manual_review_fields,
    }


def build_submit_gate_event_payload(
    *,
    site_vendor: str,
    artifact_id: int,
    artifact_path: str,
    confidence_score: float,
    allow_submit: bool,
    stop_reasons: list[str],
    required_fields: list[str],
    resolved_required_fields: list[str],
    manual_review_fields: list[str],
) -> dict[str, Any]:
    """Build deterministic guarded submit-gate event payload."""

    return {
        "site_vendor": site_vendor,
        "artifact_id": artifact_id,
        "artifact_path": artifact_path,
        "confidence_score": confidence_score,
        "allow_submit": allow_submit,
        "stop_reasons": stop_reasons,
        "required_fields": required_fields,
        "resolved_required_fields": resolved_required_fields,
        "manual_review_fields": manual_review_fields,
    }


def build_submit_gate_attempt_note(*, allow_submit: bool, stop_reasons: list[str]) -> str:
    """Build operator-facing attempt note for submit-gate outcomes."""

    if allow_submit:
        return "Submit gate passed; ready for guarded submit execution."
    return "Submit gate blocked guarded submit execution: " + ", ".join(stop_reasons[:5])


def build_guarded_submit_artifact_payload(
    *,
    application_id: int,
    attempt_id: int,
    job_id: int,
    candidate_profile_slug: str,
    site_vendor: str,
    confidence_score: float,
    target_url: str,
    submission_mode: str,
    submit_plan: dict[str, Any] | None = None,
    submit_probe: dict[str, Any] | None = None,
    submit_interaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build deterministic guarded-submit artifact payload."""

    return {
        "application_id": application_id,
        "attempt_id": attempt_id,
        "job_id": job_id,
        "candidate_profile_slug": candidate_profile_slug,
        "site_vendor": site_vendor,
        "confidence_score": confidence_score,
        "target_url": target_url,
        "submission_mode": submission_mode,
        "submit_plan": submit_plan or {},
        "submit_probe": submit_probe or {},
        "submit_interaction": submit_interaction or {},
    }


def build_guarded_submit_event_payload(
    *,
    site_vendor: str,
    confidence_score: float,
    allow_submit: bool,
    submission_mode: str,
    target_url: str,
    artifact_id: int,
    artifact_path: str,
    screenshot_artifact_id: int | None = None,
    trace_artifact_id: int | None = None,
    submit_plan: dict[str, Any] | None = None,
    submit_probe: dict[str, Any] | None = None,
    submit_interaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build deterministic guarded-submit event payload."""

    return {
        "site_vendor": site_vendor,
        "confidence_score": confidence_score,
        "allow_submit": allow_submit,
        "submission_mode": submission_mode,
        "target_url": target_url,
        "artifact_id": artifact_id,
        "artifact_path": artifact_path,
        "screenshot_artifact_id": screenshot_artifact_id,
        "trace_artifact_id": trace_artifact_id,
        "submit_plan": submit_plan or {},
        "submit_probe": submit_probe or {},
        "submit_interaction": submit_interaction or {},
    }


def build_guarded_submit_attempt_note(
    *,
    submission_mode: str,
    target_url: str,
) -> str:
    """Build operator-facing attempt note for guarded submit outcomes."""

    return f"Guarded submit executed via {submission_mode} for {target_url}."
