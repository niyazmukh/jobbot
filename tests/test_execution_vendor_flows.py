import json

from jobbot.db.models import FieldMapping
from jobbot.execution.vendor_flows import (
    build_overlay_entries,
    build_submit_gate_signals,
    build_target_open_resolutions,
)


def test_build_overlay_entries_populates_mapping_signatures_and_labels():
    mappings = [
        FieldMapping(id=1, attempt_id=9, field_key="email"),
        FieldMapping(id=2, attempt_id=9, field_key="resume_upload"),
    ]

    entries = build_overlay_entries(site_vendor="greenhouse", mappings=mappings)

    assert len(entries) == 2
    assert entries[0].site_vendor == "greenhouse"
    assert "input[name='email']" in entries[0].selector_candidates
    assert mappings[0].raw_label == "Email"
    parsed = json.loads(mappings[1].raw_dom_signature or "{}")
    assert parsed["site_vendor"] == "greenhouse"
    assert "input[type='file'][name='resume']" in parsed["selectors"]


def test_build_target_open_resolutions_sets_resolution_status_and_selector():
    mappings = [
        FieldMapping(
            id=1,
            attempt_id=3,
            field_key="email",
            raw_dom_signature=json.dumps(
                {
                    "selectors": ["input[name='email']"],
                    "confidence_gate": 0.99,
                    "manual_review_required": False,
                },
                ensure_ascii=True,
            ),
        ),
        FieldMapping(
            id=2,
            attempt_id=3,
            field_key="why_this_role",
            raw_dom_signature=json.dumps(
                {
                    "selectors": ["textarea[name='cover_letter']"],
                    "confidence_gate": 0.9,
                    "manual_review_required": True,
                },
                ensure_ascii=True,
            ),
        ),
    ]

    entries = build_target_open_resolutions(mappings)

    assert entries[0].resolution_status == "resolved"
    assert entries[0].resolved_selector == "input[name='email']"
    assert entries[1].resolution_status == "manual_review"
    parsed = json.loads(mappings[0].raw_dom_signature or "{}")
    assert parsed["resolved_selector"] == "input[name='email']"
    assert parsed["resolution_status"] == "resolved"


def test_build_submit_gate_signals_reduces_mappings_into_stop_reasons():
    mappings = [
        FieldMapping(
            id=1,
            attempt_id=4,
            field_key="first_name",
            raw_dom_signature=json.dumps(
                {"resolution_status": "resolved", "manual_review_required": False},
                ensure_ascii=True,
            ),
        ),
        FieldMapping(
            id=2,
            attempt_id=4,
            field_key="resume_upload",
            raw_dom_signature=json.dumps(
                {"resolution_status": "unresolved", "manual_review_required": False},
                ensure_ascii=True,
            ),
        ),
        FieldMapping(
            id=3,
            attempt_id=4,
            field_key="why_this_role",
            raw_dom_signature=json.dumps(
                {"resolution_status": "manual_review", "manual_review_required": True},
                ensure_ascii=True,
            ),
        ),
    ]

    signals = build_submit_gate_signals(
        required_fields=["first_name", "resume_upload"],
        mappings=mappings,
    )

    assert signals.resolved_required_fields == ["first_name"]
    assert "resume_upload" in signals.unresolved_fields
    assert "why_this_role" in signals.manual_review_fields
    assert "missing_required_field:resume_upload" in signals.stop_reasons
    assert "manual_review_required:why_this_role" in signals.stop_reasons
