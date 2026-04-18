"""Vendor flow helpers for execution overlay, resolution, and submit-gate derivation."""

from __future__ import annotations

import json

from jobbot.db.models import FieldMapping
from jobbot.execution.schemas import DraftResolvedFieldRead, DraftSiteFieldPlanEntryRead
from jobbot.execution.site_handlers import collect_submit_gate_signals, resolve_field_for_target_open
from jobbot.execution.site_profiles import selector_overlay_for_site


def build_overlay_entries(
    *,
    site_vendor: str,
    mappings: list[FieldMapping],
) -> list[DraftSiteFieldPlanEntryRead]:
    """Build site overlay entries and persist selector metadata onto mappings."""

    entries: list[DraftSiteFieldPlanEntryRead] = []
    for mapping in mappings:
        selectors, confidence_gate, manual_review_required = selector_overlay_for_site(
            site_vendor,
            mapping.field_key,
        )
        mapping.raw_dom_signature = json.dumps(
            {
                "site_vendor": site_vendor,
                "selectors": selectors,
                "confidence_gate": confidence_gate,
                "manual_review_required": manual_review_required,
            },
            ensure_ascii=True,
        )
        mapping.raw_label = _label_for_field_key(mapping.field_key)
        entries.append(
            DraftSiteFieldPlanEntryRead(
                field_mapping_id=mapping.id,
                field_key=mapping.field_key,
                site_vendor=site_vendor,
                selector_candidates=selectors,
                confidence_gate=confidence_gate,
                manual_review_required=manual_review_required,
            )
        )
    return entries


def build_target_open_resolutions(
    mappings: list[FieldMapping],
) -> list[DraftResolvedFieldRead]:
    """Resolve selectors for target-open stage and persist outcomes onto mappings."""

    resolved_entries: list[DraftResolvedFieldRead] = []
    for mapping in mappings:
        parsed = _parse_signature(mapping.raw_dom_signature)
        selectors = list(parsed.get("selectors") or [])
        confidence_gate = float(parsed.get("confidence_gate") or 0.0)
        manual_review_required = bool(parsed.get("manual_review_required"))
        decision = resolve_field_for_target_open(
            selectors=selectors,
            confidence_gate=confidence_gate,
            manual_review_required=manual_review_required,
        )
        parsed["resolved_selector"] = decision.resolved_selector
        parsed["resolution_status"] = decision.resolution_status
        mapping.raw_dom_signature = json.dumps(parsed, ensure_ascii=True)
        resolved_entries.append(
            DraftResolvedFieldRead(
                field_mapping_id=mapping.id,
                field_key=mapping.field_key,
                resolved_selector=decision.resolved_selector,
                resolution_status=decision.resolution_status,
                confidence_gate=confidence_gate,
                manual_review_required=manual_review_required,
            )
        )
    return resolved_entries


def build_submit_gate_signals(
    *,
    required_fields: list[str],
    mappings: list[FieldMapping],
):
    """Reduce mapping resolution outcomes into submit-gate signals."""

    resolution_entries: list[tuple[str, str, bool]] = []
    for mapping in mappings:
        parsed = _parse_signature(mapping.raw_dom_signature)
        resolution_status = str(parsed.get("resolution_status") or "unresolved")
        manual_review_required = bool(parsed.get("manual_review_required"))
        resolution_entries.append((mapping.field_key, resolution_status, manual_review_required))
    return collect_submit_gate_signals(
        required_fields=required_fields,
        resolution_entries=resolution_entries,
    )


def _parse_signature(raw_dom_signature: str | None) -> dict:
    """Parse mapping signature JSON into a dictionary."""

    if not raw_dom_signature:
        return {}
    try:
        return json.loads(raw_dom_signature)
    except json.JSONDecodeError:
        return {}


def _label_for_field_key(field_key: str) -> str:
    """Create a human-readable label for a deterministic field key."""

    return field_key.replace("_", " ").title()

