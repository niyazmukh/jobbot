import json

from jobbot.db.models import FieldMapping
from jobbot.execution.vendor_registry import (
    ExecutionVendorHandler,
    get_vendor_execution_handler,
)


def test_get_vendor_execution_handler_returns_supported_handlers():
    greenhouse = get_vendor_execution_handler("greenhouse")
    lever = get_vendor_execution_handler("lever")
    workday = get_vendor_execution_handler("workday")
    unknown = get_vendor_execution_handler("unknown_vendor")

    assert isinstance(greenhouse, ExecutionVendorHandler)
    assert isinstance(lever, ExecutionVendorHandler)
    assert greenhouse is not None and greenhouse.vendor == "greenhouse"
    assert lever is not None and lever.vendor == "lever"
    assert greenhouse is not None and greenhouse.supports_guarded_submit()
    assert lever is not None and lever.supports_guarded_submit()
    assert isinstance(workday, ExecutionVendorHandler)
    assert workday is not None and workday.vendor == "workday"
    assert workday is not None and workday.supports_guarded_submit()
    assert greenhouse is not None and greenhouse.submission_mode() == "greenhouse_guarded_submit"
    assert lever is not None and lever.submission_mode() == "lever_guarded_submit"
    assert workday is not None and workday.submission_mode() == "workday_guarded_submit"
    assert greenhouse is not None and greenhouse.guarded_submit_plan()["site_vendor"] == "greenhouse"
    assert lever is not None and lever.guarded_submit_plan()["site_vendor"] == "lever"
    assert workday is not None and workday.guarded_submit_plan()["site_vendor"] == "workday"
    assert unknown is None


def test_vendor_handler_builders_produce_expected_overlay_resolution_and_gate_signals():
    handler = get_vendor_execution_handler("greenhouse")
    assert handler is not None

    mappings = [
        FieldMapping(id=1, attempt_id=5, field_key="first_name"),
        FieldMapping(id=2, attempt_id=5, field_key="resume_upload"),
        FieldMapping(id=3, attempt_id=5, field_key="why_this_role"),
    ]

    overlay_entries = handler.overlay_entries(mappings)
    assert len(overlay_entries) == 3
    assert overlay_entries[0].site_vendor == "greenhouse"
    assert "input[name='first_name']" in overlay_entries[0].selector_candidates

    resolutions = handler.target_open_resolutions(mappings)
    assert resolutions[0].resolution_status == "resolved"
    assert resolutions[1].resolution_status == "resolved"
    assert resolutions[2].resolution_status == "manual_review"

    signature = json.loads(mappings[2].raw_dom_signature or "{}")
    assert signature["resolution_status"] == "manual_review"

    signals = handler.submit_gate_signals(mappings)
    assert "first_name" in signals.resolved_required_fields
    assert "resume_upload" in signals.resolved_required_fields
    assert "why_this_role" in signals.manual_review_fields
    assert "manual_review_required:why_this_role" in signals.stop_reasons
