from typer.testing import CliRunner
from rich.console import Console
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from pathlib import Path

import jobbot.cli.main as cli_main
from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import BrowserProfile, CandidateProfile
from jobbot.execution.schemas import (
    DraftSubmitRemediationBatchRead,
    DraftSubmitRemediationActionRead,
    DraftSubmitRemediationFailureRead,
    DraftExecutionDashboardRead,
    DraftExecutionDashboardRemediationHistoryRead,
    DraftExecutionOverviewRead,
    DraftLinkedInGuardedSubmitCriteriaRead,
)
from jobbot.models.enums import BrowserProfileType, SessionHealth
from types import SimpleNamespace


def make_session_factory():
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def test_cli_probe_linkedin_browser_profile_updates_health(monkeypatch):
    session_factory = make_session_factory()
    session = session_factory()
    session.add(
        BrowserProfile(
            profile_key="linkedin-main",
            profile_type=BrowserProfileType.APPLICATION,
            display_name="LinkedIn Main",
            storage_path="C:/profiles/linkedin-main",
            session_health=SessionHealth.HEALTHY.value,
            validation_details={},
        )
    )
    session.commit()
    session.close()

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "probe-linkedin-browser-profile",
            "--profile-key",
            "linkedin-main",
            "--page-url",
            "https://www.linkedin.com/checkpoint/challenge",
            "--page-title",
            "Security Verification",
            "--page-content",
            "Please verify your identity and complete CAPTCHA.",
            "--redirect-count",
            "1",
            "--authenticated",
            "true",
        ],
    )

    assert result.exit_code == 0
    assert "LinkedIn probe saved for browser profile:" in result.stdout
    assert "Health: checkpointed" in result.stdout
    assert "Recommended action: manual_checkpoint_recovery" in result.stdout


def test_cli_extract_linkedin_questions_reports_assist_mode(tmp_path: Path):
    html_file = tmp_path / "linkedin_capture.html"
    html_file.write_text(
        (
            "<form>"
            "<label for='emailAddress'>Email address</label>"
            "<input id='emailAddress' name='emailAddress' type='email'>"
            "<input name='customQuestion_77' type='text'>"
            "</form>"
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "extract-linkedin-questions",
            "--file",
            str(html_file),
        ],
    )

    assert result.exit_code == 0
    assert "Question count:" in result.stdout
    assert "Recommended mode:" in result.stdout
    assert "assist" in result.stdout


def test_cli_build_linkedin_assist_plan_reports_blocked_auto_actions(monkeypatch, tmp_path: Path):
    session_factory = make_session_factory()
    session = session_factory()
    session.add(
        CandidateProfile(
            name="Alex Doe",
            slug="alex-doe",
            personal_details={"email": "alex@example.com"},
            source_profile_data={"linkedin_url": "https://www.linkedin.com/in/alex-doe"},
        )
    )
    session.commit()
    session.close()

    html_file = tmp_path / "linkedin_assist.html"
    html_file.write_text(
        (
            "<form>"
            "<label for='emailAddress'>Email address</label>"
            "<input id='emailAddress' name='emailAddress' type='email'>"
            "<input name='customQuestion_77' type='text'>"
            "</form>"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "build-linkedin-assist-plan",
            "--file",
            str(html_file),
            "--candidate-profile",
            "alex-doe",
            "--min-auto-confidence",
            "0.8",
        ],
    )

    assert result.exit_code == 0
    assert "Blocked auto actions:" in result.stdout
    assert "1" in result.stdout
    assert "Recommended mode:" in result.stdout
    assert "assist" in result.stdout


def test_cli_evaluate_linkedin_guarded_submit_criteria_reports_session_block(monkeypatch, tmp_path: Path):
    session_factory = make_session_factory()
    session = session_factory()
    session.add(
        BrowserProfile(
            profile_key="linkedin-main",
            profile_type=BrowserProfileType.APPLICATION,
            display_name="LinkedIn Main",
            storage_path="C:/profiles/linkedin-main",
            session_health=SessionHealth.CHECKPOINTED.value,
            validation_details={"reasons": ["checkpoint_detected"]},
        )
    )
    session.commit()
    session.close()

    html_file = tmp_path / "linkedin_guarded_submit.html"
    html_file.write_text(
        (
            "<form>"
            "<label for='emailAddress'>Email address</label>"
            "<input id='emailAddress' name='emailAddress' type='email'>"
            "</form>"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "evaluate-linkedin-guarded-submit-criteria",
            "--profile-key",
            "linkedin-main",
            "--file",
            str(html_file),
        ],
    )

    assert result.exit_code == 0
    assert "Allow guarded submit:" in result.stdout
    assert "False" in result.stdout
    assert "linkedin_session_not_ready:checkpointed" in result.stdout


def test_cli_evaluate_linkedin_guarded_submit_attempt_reports_stop_reasons(monkeypatch, tmp_path: Path):
    session_factory = make_session_factory()
    html_file = tmp_path / "linkedin_attempt.html"
    html_file.write_text(
        (
            "<form>"
            "<label for='emailAddress'>Email address</label>"
            "<input id='emailAddress' name='emailAddress' type='email'>"
            "</form>"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    monkeypatch.setattr(
        cli_main,
        "evaluate_linkedin_guarded_submit_criteria_for_attempt",
        lambda *args, **kwargs: DraftLinkedInGuardedSubmitCriteriaRead(
            application_id=1,
            attempt_id=42,
            event_id=9,
            artifact_id=11,
            artifact_path="C:/tmp/linkedin_guarded_submit_criteria.json",
            profile_key="linkedin-main",
            candidate_profile_slug="alex-doe",
            session_health="healthy",
            session_requires_reauth=False,
            allow_session_automation=True,
            question_count=2,
            assist_review_count=1,
            blocked_auto_action_count=1,
            recommended_mode="assist",
            min_auto_confidence=0.8,
            allow_guarded_submit=False,
            stop_reasons=["linkedin_assist_mode_required"],
            recommended_actions=["Resolve LinkedIn assist-review questions and rerun."],
        ),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "evaluate-linkedin-guarded-submit-attempt",
            "--attempt-id",
            "42",
            "--file",
            str(html_file),
        ],
    )

    assert result.exit_code == 0
    assert "Evaluated LinkedIn guarded-submit criteria:" in result.stdout
    assert "Allow guarded submit:" in result.stdout
    assert "False" in result.stdout
    assert "linkedin_assist_mode_required" in result.stdout


def test_cli_list_execution_overview_passes_linkedin_stop_reason(monkeypatch):
    session_factory = make_session_factory()
    captured: dict[str, object] = {}

    def fake_list_execution_overview(_session, **kwargs):
        captured.update(kwargs)
        return [
            DraftExecutionOverviewRead(
                application_id=1,
                attempt_id=42,
                job_id=9,
                candidate_profile_slug="alex-doe",
                company_name="Example",
                job_title="Backend Engineer",
                site_vendor="linkedin",
                application_state="review",
                readiness_state="ready_to_apply",
                ready=True,
                attempt_mode="draft",
                attempt_result="blocked",
                failure_code="linkedin_guarded_submit_criteria_blocked",
                failure_classification="unknown_classification",
                submit_confidence=0.42,
                browser_profile_key="linkedin-main",
                session_health="checkpointed",
                latest_event_type="draft_linkedin_guarded_submit_criteria_evaluated",
                latest_event_message="criteria blocked",
                submit_interaction_mode=None,
                submit_interaction_status=None,
                submit_interaction_clicked=None,
                submit_interaction_selector=None,
                submit_interaction_confirmation_count=None,
                submit_troubleshoot_event_route=None,
                submit_troubleshoot_artifact_route=None,
                submit_remediation_message=None,
                submit_remediation_primary_route=None,
                submit_remediation_primary_label=None,
                submit_remediation_secondary_route=None,
                submit_remediation_secondary_label=None,
                submit_remediation_retry_route=None,
                submit_remediation_retry_label=None,
                attempt_route="/execution/attempts/42",
                replay_route="/execution/replay/42",
                primary_action_route="/execution/replay/42",
                primary_action_label="Open replay bundle",
                latest_artifact_route=None,
                latest_artifact_label=None,
                visual_evidence_route=None,
                visual_evidence_label=None,
                artifact_count=1,
                screenshot_count=0,
                html_snapshot_count=0,
                model_io_count=1,
                generated_document_count=0,
                answer_pack_count=0,
                reasons=["ok"],
                started_at="2026-04-18T10:00:00+00:00",
            )
        ]

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    monkeypatch.setattr(cli_main, "list_execution_overview", fake_list_execution_overview)
    monkeypatch.setattr(
        cli_main,
        "console",
        Console(width=220, force_terminal=False, color_system=None),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "list-execution-overview",
            "--candidate-profile",
            "alex-doe",
            "--linkedin-stop-reason",
            "linkedin_session_not_ready:checkpointed",
        ],
    )

    assert result.exit_code == 0
    assert captured["linkedin_stop_reason"] == "linkedin_session_not_ready:checkpointed"
    assert "Execution Overview" in result.stdout


def test_cli_show_execution_dashboard_prints_linkedin_stop_reason_breakdown(monkeypatch):
    session_factory = make_session_factory()
    captured: dict[str, object] = {}

    def fake_get_execution_dashboard(_session, **kwargs):
        captured.update(kwargs)
        return DraftExecutionDashboardRead(
            candidate_profile_slug="alex-doe",
            total_attempts=1,
            blocked_attempts=1,
            manual_review_blocked_attempts=0,
            extension_review_blocked_attempts=0,
            pending_attempts=0,
            review_state_attempts=1,
            replay_ready_attempts=1,
            remediation_history_count=0,
            remediation_history_limit=10,
            blocked_failure_counts={"linkedin_guarded_submit_criteria_blocked": 1},
            blocked_failure_classification_counts={"unknown_classification": 1},
            linkedin_guarded_stop_reason_counts={"linkedin_session_not_ready:checkpointed": 1},
            recent_attempts=[],
            blocked_recent_attempts=[],
            recommended_actions=["scope action"],
        )

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    monkeypatch.setattr(cli_main, "get_execution_dashboard", fake_get_execution_dashboard)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "show-execution-dashboard",
            "--candidate-profile",
            "alex-doe",
            "--linkedin-stop-reason",
            "linkedin_session_not_ready:checkpointed",
        ],
    )

    assert result.exit_code == 0
    assert captured["linkedin_stop_reason"] == "linkedin_session_not_ready:checkpointed"
    assert "LinkedIn guarded stop-reason breakdown:" in result.stdout
    assert "linkedin_session_not_ready:checkpointed=1" in result.stdout


def test_cli_list_remediation_history_prints_linkedin_stop_reason_scope(monkeypatch):
    session_factory = make_session_factory()
    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    monkeypatch.setattr(
        cli_main,
        "console",
        Console(width=240, force_terminal=False, color_system=None),
    )
    monkeypatch.setattr(
        cli_main,
        "list_execution_dashboard_bulk_history_reads",
        lambda *_args, **_kwargs: [
            DraftExecutionDashboardRemediationHistoryRead(
                history_id="hist-1",
                created_at="2026-04-18T10:00:00+00:00",
                requested_count=1,
                remediated_count=0,
                failed_count=1,
                failure_code=None,
                failure_classification=None,
                linkedin_stop_reason="linkedin_session_not_ready:checkpointed",
                manual_review_only=False,
                max_submit_confidence=None,
                sort_by="started_at",
                descending=True,
                limit=10,
                first_failure_attempt_id=42,
                first_failure_code="draft_execution_not_started",
                rerun_route="/execution/dashboard/alex-doe/bulk-remediate-submit/history/hist-1",
            )
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        ["list-remediation-history", "--candidate-profile", "alex-doe"],
    )

    assert result.exit_code == 0
    assert "linkedin_stop_reason=linkedin_session_not_ready:checkpointed" in result.stdout


def test_cli_run_bulk_submit_remediation_scopes_linkedin_stop_reason(monkeypatch):
    session_factory = make_session_factory()
    captured: dict[str, object] = {}

    def fake_run_dashboard_bulk_submit_remediation(_session, **kwargs):
        captured.update(kwargs)
        return DraftSubmitRemediationBatchRead(
            candidate_profile_slug="alex-doe",
            requested_count=1,
            remediated_count=1,
            failed_count=0,
            targeted_attempt_ids=[42],
            results=[
                DraftSubmitRemediationActionRead(
                    source_attempt_id=42,
                    application_id=11,
                    attempt_id=43,
                    job_id=9,
                    candidate_profile_slug="alex-doe",
                    remediation_action="refresh_target_and_submit_gate",
                    executed_steps=["bootstrap", "start", "submit_gate"],
                    stop_reason=None,
                    failure_code="linkedin_guarded_submit_criteria_blocked",
                    failure_classification="unknown_classification",
                    allow_submit=False,
                    submit_confidence=0.5,
                    final_attempt_result="blocked",
                    final_failure_code="linkedin_guarded_submit_criteria_blocked",
                    final_failure_classification="unknown_classification",
                    detail_route="/execution/attempts/43",
                    replay_route="/execution/replay/43",
                )
            ],
            failures=[],
        )

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    monkeypatch.setattr(
        cli_main,
        "run_dashboard_bulk_submit_remediation",
        fake_run_dashboard_bulk_submit_remediation,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "run-bulk-submit-remediation",
            "--candidate-profile",
            "alex-doe",
            "--linkedin-stop-reason",
            "linkedin_session_not_ready:checkpointed",
            "--limit",
            "10",
        ],
    )

    assert result.exit_code == 0
    assert captured["candidate_profile_slug"] == "alex-doe"
    assert captured["linkedin_stop_reason"] == "linkedin_session_not_ready:checkpointed"
    assert captured["limit"] == 10
    assert "Bulk remediation run:" in result.stdout
    assert "targeted=1 remediated=1 failed=0" in result.stdout


def test_cli_retry_submit_attempt_runs_single_remediation(monkeypatch):
    session_factory = make_session_factory()
    captured: dict[str, object] = {}

    def fake_run_submit_remediation_action(_session, *, attempt_id: int):
        captured["attempt_id"] = attempt_id
        return DraftSubmitRemediationActionRead(
            source_attempt_id=attempt_id,
            application_id=11,
            attempt_id=43,
            job_id=9,
            candidate_profile_slug="alex-doe",
            remediation_action="refresh_target_and_submit_gate",
            executed_steps=["bootstrap", "start", "submit_gate"],
            stop_reason=None,
            failure_code="submit_gate_blocked",
            failure_classification="unknown_classification",
            allow_submit=False,
            submit_confidence=0.55,
            final_attempt_result="blocked",
            final_failure_code="submit_gate_blocked",
            final_failure_classification="unknown_classification",
            detail_route="/execution/attempts/43",
            replay_route="/execution/replay/43",
        )

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    monkeypatch.setattr(cli_main, "run_submit_remediation_action", fake_run_submit_remediation_action)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        ["retry-submit-attempt", "--attempt-id", "42"],
    )

    assert result.exit_code == 0
    assert captured["attempt_id"] == 42
    assert "Retried submit remediation:" in result.stdout
    assert "source_attempt=42 new_attempt=43" in result.stdout


def test_cli_reauth_browser_profile_marks_profile_healthy(monkeypatch):
    session_factory = make_session_factory()
    session = session_factory()
    session.add(
        BrowserProfile(
            profile_key="apply-main",
            profile_type=BrowserProfileType.APPLICATION,
            display_name="Apply Main",
            storage_path="C:/profiles/apply-main",
            session_health=SessionHealth.CHECKPOINTED.value,
            validation_details={"reasons": ["checkpoint_detected"]},
        )
    )
    session.commit()
    session.close()

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        ["reauth-browser-profile", "--profile-key", "apply-main"],
    )

    assert result.exit_code == 0
    assert "Reauth completed for browser profile:" in result.stdout
    assert "Health: healthy" in result.stdout

    check = session_factory()
    profile = check.query(BrowserProfile).filter_by(profile_key="apply-main").one()
    assert profile.session_health == SessionHealth.HEALTHY.value
    check.close()


def test_cli_enrich_job_passes_replay_prompt_version(monkeypatch):
    session_factory = make_session_factory()
    captured: dict[str, object] = {}

    def fake_enrich_job(_session, job_id: int, *, replay_prompt_version: str | None = None):
        captured["job_id"] = job_id
        captured["replay_prompt_version"] = replay_prompt_version
        return SimpleNamespace(
            id=job_id,
            status="enriched",
            requirements_structured={"required_skills": ["python"]},
        )

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    monkeypatch.setattr(cli_main, "enrich_job", fake_enrich_job)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "enrich-job",
            "--job-id",
            "7",
            "--replay-prompt-version",
            "enrich_v1",
        ],
    )

    assert result.exit_code == 0
    assert captured["job_id"] == 7
    assert captured["replay_prompt_version"] == "enrich_v1"
    assert "Enriched job:" in result.stdout


def test_cli_score_job_passes_replay_prompt_version(monkeypatch):
    session_factory = make_session_factory()
    captured: dict[str, object] = {}

    def fake_score_job_for_candidate(
        _session,
        job_id: int,
        candidate_profile: str,
        *,
        replay_prompt_version: str | None = None,
        scoring_model_pass=None,
    ):
        captured["job_id"] = job_id
        captured["candidate_profile"] = candidate_profile
        captured["replay_prompt_version"] = replay_prompt_version
        return SimpleNamespace(
            job_id=job_id,
            candidate_profile_id=42,
            overall_score=0.91,
            score_json={
                "confidence_score": 0.88,
                "blocked": False,
                "blocking_reasons": [],
            },
        )

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    monkeypatch.setattr(cli_main, "score_job_for_candidate", fake_score_job_for_candidate)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "score-job",
            "--job-id",
            "11",
            "--candidate-profile",
            "alex-doe",
            "--replay-prompt-version",
            "score_v1",
        ],
    )

    assert result.exit_code == 0
    assert captured["job_id"] == 11
    assert captured["candidate_profile"] == "alex-doe"
    assert captured["replay_prompt_version"] == "score_v1"
    assert "Scored job:" in result.stdout
