from jobbot.execution.site_handlers import (
    build_guarded_submit_artifact_payload,
    build_guarded_submit_attempt_note,
    build_guarded_submit_event_payload,
    build_submit_gate_artifact_payload,
    build_submit_gate_attempt_note,
    build_submit_gate_event_payload,
    build_target_open_attempt_note,
    build_target_open_event_payload,
    build_target_resolution_artifact_payload,
    collect_submit_gate_signals,
    resolve_field_for_target_open,
    supports_submit_gate,
    supports_target_open,
)


def test_site_handler_support_matrix_covers_greenhouse_and_lever():
    assert supports_target_open("greenhouse")
    assert supports_target_open("lever")
    assert supports_target_open("workday")
    assert supports_submit_gate("greenhouse")
    assert supports_submit_gate("lever")
    assert supports_submit_gate("workday")
    assert not supports_target_open("unknown_vendor")
    assert not supports_submit_gate("unknown_vendor")


def test_resolve_field_for_target_open_uses_selector_gate_and_manual_review():
    resolved = resolve_field_for_target_open(
        selectors=["input[name='email']"],
        confidence_gate=0.95,
        manual_review_required=False,
    )
    manual = resolve_field_for_target_open(
        selectors=["textarea[name='cover_letter']"],
        confidence_gate=0.9,
        manual_review_required=True,
    )
    unresolved = resolve_field_for_target_open(
        selectors=[],
        confidence_gate=0.9,
        manual_review_required=False,
    )

    assert resolved.resolution_status == "resolved"
    assert resolved.resolved_selector == "input[name='email']"
    assert manual.resolution_status == "manual_review"
    assert manual.resolved_selector == "textarea[name='cover_letter']"
    assert unresolved.resolution_status == "unresolved"
    assert unresolved.resolved_selector is None


def test_collect_submit_gate_signals_produces_expected_stop_reasons():
    signals = collect_submit_gate_signals(
        required_fields=["first_name", "email", "resume_upload"],
        resolution_entries=[
            ("first_name", "resolved", False),
            ("email", "resolved", False),
            ("resume_upload", "unresolved", False),
            ("why_this_role", "manual_review", True),
            ("portfolio_url", "unresolved", False),
        ],
    )

    assert signals.resolved_required_fields == ["first_name", "email"]
    assert "why_this_role" in signals.manual_review_fields
    assert "resume_upload" in signals.unresolved_fields
    assert "missing_required_field:resume_upload" in signals.stop_reasons
    assert "manual_review_required:why_this_role" in signals.stop_reasons
    assert "unresolved_field:portfolio_url" in signals.stop_reasons


def test_target_open_payload_and_note_builders_are_stable():
    resolution_payload = build_target_resolution_artifact_payload(
        application_id=11,
        attempt_id=22,
        job_id=33,
        candidate_profile_slug="alex-doe",
        site_vendor="lever",
        resolved_entries=[{"field_key": "email", "resolution_status": "resolved"}],
    )
    event_payload = build_target_open_event_payload(
        site_vendor="lever",
        target_url="https://example.com/jobs/33",
        capture_metadata={"capture_method": "http_get", "status_code": 200},
        opened_page_artifact_id=101,
        resolution_artifact_id=102,
        screenshot_artifact_id=103,
        trace_artifact_id=104,
        resolved_count=6,
        unresolved_count=2,
    )
    note = build_target_open_attempt_note(
        capture_method="http_get",
        resolved_count=6,
        unresolved_count=2,
    )

    assert resolution_payload["application_id"] == 11
    assert resolution_payload["site_vendor"] == "lever"
    assert resolution_payload["resolved"][0]["field_key"] == "email"
    assert event_payload["opened_page_artifact_id"] == 101
    assert event_payload["resolution_artifact_id"] == 102
    assert event_payload["screenshot_artifact_id"] == 103
    assert event_payload["trace_artifact_id"] == 104
    assert event_payload["target_capture"]["capture_method"] == "http_get"
    assert note == "Target opened via http_get; resolved=6 unresolved=2."


def test_submit_gate_payload_and_note_builders_are_stable():
    artifact_payload = build_submit_gate_artifact_payload(
        application_id=11,
        attempt_id=22,
        job_id=33,
        candidate_profile_slug="alex-doe",
        site_vendor="greenhouse",
        confidence_score=0.74,
        allow_submit=False,
        stop_reasons=["manual_review_required:why_this_role"],
        required_fields=["first_name", "email"],
        resolved_required_fields=["first_name", "email"],
        manual_review_fields=["why_this_role"],
    )
    event_payload = build_submit_gate_event_payload(
        site_vendor="greenhouse",
        artifact_id=201,
        artifact_path="/tmp/gate.json",
        confidence_score=0.74,
        allow_submit=False,
        stop_reasons=["manual_review_required:why_this_role"],
        required_fields=["first_name", "email"],
        resolved_required_fields=["first_name", "email"],
        manual_review_fields=["why_this_role"],
    )
    blocked_note = build_submit_gate_attempt_note(
        allow_submit=False,
        stop_reasons=[
            "manual_review_required:why_this_role",
            "unresolved_field:portfolio_url",
        ],
    )
    passed_note = build_submit_gate_attempt_note(allow_submit=True, stop_reasons=[])

    assert artifact_payload["application_id"] == 11
    assert artifact_payload["site_vendor"] == "greenhouse"
    assert artifact_payload["allow_submit"] is False
    assert event_payload["artifact_id"] == 201
    assert event_payload["artifact_path"] == "/tmp/gate.json"
    assert "manual_review_required:why_this_role" in event_payload["stop_reasons"]
    assert blocked_note.startswith("Submit gate blocked guarded submit execution:")
    assert "manual_review_required:why_this_role" in blocked_note
    assert passed_note == "Submit gate passed; ready for guarded submit execution."


def test_guarded_submit_payload_and_note_builders_are_stable():
    artifact_payload = build_guarded_submit_artifact_payload(
        application_id=11,
        attempt_id=22,
        job_id=33,
        candidate_profile_slug="alex-doe",
        site_vendor="greenhouse",
        confidence_score=1.0,
        target_url="https://boards.greenhouse.io/example/jobs/123",
        submission_mode="deterministic_guarded_submit",
        submit_plan={"submit_button_selectors": ["button[type='submit']"]},
        submit_probe={"probe_available": True, "matched_submit_selectors": ["button[type='submit']"]},
    )
    event_payload = build_guarded_submit_event_payload(
        site_vendor="greenhouse",
        confidence_score=1.0,
        allow_submit=True,
        submission_mode="deterministic_guarded_submit",
        target_url="https://boards.greenhouse.io/example/jobs/123",
        artifact_id=301,
        artifact_path="/tmp/guarded_submit.json",
        screenshot_artifact_id=302,
        trace_artifact_id=303,
        submit_plan={"submit_button_selectors": ["button[type='submit']"]},
        submit_probe={"probe_available": True, "matched_submit_selectors": ["button[type='submit']"]},
    )
    note = build_guarded_submit_attempt_note(
        submission_mode="deterministic_guarded_submit",
        target_url="https://boards.greenhouse.io/example/jobs/123",
    )

    assert artifact_payload["application_id"] == 11
    assert artifact_payload["submission_mode"] == "deterministic_guarded_submit"
    assert event_payload["artifact_id"] == 301
    assert event_payload["screenshot_artifact_id"] == 302
    assert event_payload["trace_artifact_id"] == 303
    assert event_payload["submit_plan"]["submit_button_selectors"] == ["button[type='submit']"]
    assert event_payload["submit_probe"]["probe_available"] is True
    assert artifact_payload["submit_probe"]["matched_submit_selectors"] == ["button[type='submit']"]
    assert note == (
        "Guarded submit executed via deterministic_guarded_submit for "
        "https://boards.greenhouse.io/example/jobs/123."
    )
