from datetime import timedelta
import json

from typer.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import jobbot.cli.main as cli_main
from jobbot.db.base import Base
from jobbot.db.models import AutoApplyQueueItem, CandidateProfile, Job, utcnow
from jobbot.models.enums import AutoApplyQueueStatus


def make_session_factory():
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def test_cli_list_remediation_history_shows_rows(monkeypatch):
    session_factory = make_session_factory()
    session = session_factory()
    session.add(
        CandidateProfile(
            name="Alex Doe",
            slug="alex-doe",
            personal_details={"email": "alex@example.com"},
            target_preferences={"preferred_locations": ["Remote"], "remote": True},
            source_profile_data={
                "resume_path": "/profiles/alex-doe/resume.pdf",
                "execution_dashboard_bulk_history": [
                    {
                        "history_id": "hist-cli-001",
                        "created_at": "2026-04-18T10:00:00+00:00",
                        "requested_count": 4,
                        "remediated_count": 3,
                        "failed_count": 1,
                        "failure_code": "submit_gate_blocked",
                        "limit": 4,
                        "sort_by": "started_at",
                        "descending": True,
                        "first_failure_attempt_id": 77,
                        "first_failure_code": "browser_profile_not_ready_for_application",
                    }
                ],
            },
        )
    )
    session.commit()
    session.close()

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "list-remediation-history",
            "--candidate-profile",
            "alex-doe",
            "--history-sort",
            "newest",
            "--limit",
            "5",
        ],
    )

    assert result.exit_code == 0
    assert "Execution Remediation History" in result.stdout
    assert "4/3/1" in result.stdout
    assert "hist-cli-001" in result.stdout


def test_cli_replay_remediation_history_replays_scope(monkeypatch):
    session_factory = make_session_factory()
    session = session_factory()
    session.add(
        CandidateProfile(
            name="Alex Doe",
            slug="alex-doe",
            personal_details={"email": "alex@example.com"},
            target_preferences={"preferred_locations": ["Remote"], "remote": True},
            source_profile_data={
                "resume_path": "/profiles/alex-doe/resume.pdf",
                "execution_dashboard_bulk_history": [
                    {
                        "history_id": "hist-cli-002",
                        "created_at": "2026-04-18T09:00:00+00:00",
                        "requested_count": 5,
                        "remediated_count": 2,
                        "failed_count": 3,
                        "manual_review_only": True,
                        "limit": 5,
                        "sort_by": "started_at",
                        "descending": True,
                        "first_failure_attempt_id": 101,
                        "first_failure_code": "draft_field_plan_not_created",
                    }
                ],
            },
        )
    )
    session.commit()
    session.close()

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "replay-remediation-history",
            "--candidate-profile",
            "alex-doe",
            "--history-id",
            "hist-cli-002",
        ],
    )

    assert result.exit_code == 0
    assert "Replayed remediation scope:" in result.stdout
    assert "targeted=0 remediated=0 failed=0" in result.stdout

    check = session_factory()
    candidate = check.query(CandidateProfile).filter_by(slug="alex-doe").one()
    history = list(candidate.source_profile_data.get("execution_dashboard_bulk_history") or [])
    check.close()
    assert len(history) >= 2
    assert history[0]["manual_review_only"] is True


def test_cli_remediation_history_limit_and_prune(monkeypatch):
    session_factory = make_session_factory()
    session = session_factory()
    session.add(
        CandidateProfile(
            name="Alex Doe",
            slug="alex-doe",
            personal_details={"email": "alex@example.com"},
            target_preferences={"preferred_locations": ["Remote"], "remote": True},
            source_profile_data={
                "resume_path": "/profiles/alex-doe/resume.pdf",
                "execution_dashboard_bulk_history": [
                    {
                        "history_id": "hist-limit-1",
                        "created_at": "2026-04-18T08:00:00+00:00",
                        "requested_count": 1,
                        "remediated_count": 1,
                        "failed_count": 0,
                    },
                    {
                        "history_id": "hist-limit-2",
                        "created_at": "2026-04-18T09:00:00+00:00",
                        "requested_count": 1,
                        "remediated_count": 1,
                        "failed_count": 0,
                    },
                    {
                        "history_id": "hist-limit-3",
                        "created_at": "2026-04-18T10:00:00+00:00",
                        "requested_count": 1,
                        "remediated_count": 1,
                        "failed_count": 0,
                    },
                ],
            },
        )
    )
    session.commit()
    session.close()

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    runner = CliRunner()
    set_limit = runner.invoke(
        cli_main.app,
        [
            "set-remediation-history-limit",
            "--candidate-profile",
            "alex-doe",
            "--history-limit",
            "2",
        ],
    )
    prune = runner.invoke(
        cli_main.app,
        [
            "prune-remediation-history",
            "--candidate-profile",
            "alex-doe",
        ],
    )

    assert set_limit.exit_code == 0
    assert "configured_limit=2" in set_limit.stdout
    assert "removed=1" in set_limit.stdout
    assert prune.exit_code == 0
    assert "before=2 after=2 removed=0 keep=2" in prune.stdout


def test_cli_list_prompt_registry_shows_registered_rows(monkeypatch):
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "list-prompt-registry",
        ],
    )

    assert result.exit_code == 0
    assert "Prompt Registry" in result.stdout
    assert "scoring_fit_eval" in result.stdout
    assert "score_v1" in result.stdout


def test_cli_check_prompt_replay_reports_compatibility_and_validation_error(monkeypatch):
    runner = CliRunner()

    compatible = runner.invoke(
        cli_main.app,
        [
            "check-prompt-replay",
            "--recorded-prompt-version",
            "score_v1",
            "--replay-prompt-version",
            "score_v1",
        ],
    )
    incompatible = runner.invoke(
        cli_main.app,
        [
            "check-prompt-replay",
            "--recorded-prompt-version",
            "score_v1",
            "--replay-prompt-version",
            "score_v2",
        ],
    )
    invalid = runner.invoke(
        cli_main.app,
        [
            "check-prompt-replay",
            "--recorded-prompt-version",
            "bad-version",
            "--replay-prompt-version",
            "score_v1",
        ],
    )

    assert compatible.exit_code == 0
    assert "Compatible: True" in compatible.stdout
    assert incompatible.exit_code == 0
    assert "Compatible: False" in incompatible.stdout
    assert invalid.exit_code != 0


def test_cli_list_auto_apply_summaries_supports_cursor_and_sort(monkeypatch):
    session_factory = make_session_factory()
    session = session_factory()
    now = utcnow()

    alex = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={"email": "alex@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/alex-doe/resume.pdf"},
    )
    blake = CandidateProfile(
        name="Blake Doe",
        slug="blake-doe",
        personal_details={"email": "blake@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/blake-doe/resume.pdf"},
    )
    session.add_all([alex, blake])
    session.flush()

    job_1 = Job(
        canonical_url="https://example.com/jobs/1",
        title="Engineer I",
        title_normalized="engineer i",
        source="fixture",
        source_type="company",
    )
    job_2 = Job(
        canonical_url="https://example.com/jobs/2",
        title="Engineer II",
        title_normalized="engineer ii",
        source="fixture",
        source_type="company",
    )
    session.add_all([job_1, job_2])
    session.flush()

    session.add_all(
        [
            AutoApplyQueueItem(
                candidate_profile_id=alex.id,
                job_id=job_1.id,
                status=AutoApplyQueueStatus.QUEUED,
                priority=100,
                attempt_count=0,
                max_attempts=3,
                created_at=now - timedelta(minutes=15),
                updated_at=now - timedelta(minutes=15),
            ),
            AutoApplyQueueItem(
                candidate_profile_id=blake.id,
                job_id=job_2.id,
                status=AutoApplyQueueStatus.SUCCEEDED,
                priority=100,
                attempt_count=1,
                max_attempts=3,
                created_at=now - timedelta(minutes=12),
                updated_at=now - timedelta(minutes=10),
                finished_at=now - timedelta(minutes=9),
            ),
        ]
    )
    session.commit()
    session.close()

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    runner = CliRunner()
    first_page = runner.invoke(
        cli_main.app,
        [
            "list-auto-apply-summaries",
            "--limit",
            "1",
            "--sort-by",
            "candidate_asc",
            "--queue-slo-filter",
            "all",
        ],
    )

    assert first_page.exit_code == 0
    assert "Auto-Apply Fleet Summaries" in first_page.stdout
    assert "alex-doe" in first_page.stdout
    assert "delta_" in first_page.stdout
    assert "Next cursor: alex-doe" in first_page.stdout

    second_page = runner.invoke(
        cli_main.app,
        [
            "list-auto-apply-summaries",
            "--limit",
            "1",
            "--sort-by",
            "candidate_asc",
            "--queue-slo-filter",
            "all",
            "--cursor",
            "alex-doe",
        ],
    )
    assert second_page.exit_code == 0
    assert "blake" in second_page.stdout


def test_cli_export_auto_apply_summaries_writes_csv(monkeypatch, tmp_path):
    session_factory = make_session_factory()
    session = session_factory()
    now = utcnow()

    alex = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={"email": "alex@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/alex-doe/resume.pdf"},
    )
    session.add(alex)
    session.flush()

    job = Job(
        canonical_url="https://example.com/jobs/export-1",
        title="Export Engineer",
        title_normalized="export engineer",
        source="fixture",
        source_type="company",
    )
    session.add(job)
    session.flush()

    session.add(
        AutoApplyQueueItem(
            candidate_profile_id=alex.id,
            job_id=job.id,
            status=AutoApplyQueueStatus.QUEUED,
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=20),
        )
    )
    session.commit()
    session.close()

    output_file = tmp_path / "auto_apply_summaries.csv"

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "export-auto-apply-summaries",
            "--output-file",
            str(output_file),
            "--queue-slo-filter",
            "all",
            "--sort-by",
            "candidate_asc",
        ],
    )

    assert result.exit_code == 0
    assert "Exported auto-apply summaries:" in result.stdout
    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    assert content.startswith("candidate_profile_slug,slo_status,total_count")
    assert "summary_delta_marker" in content.splitlines()[0]
    assert "alex-doe" in content


def test_cli_list_auto_apply_summaries_json_output(monkeypatch):
    session_factory = make_session_factory()
    session = session_factory()
    now = utcnow()

    alex = CandidateProfile(
        name="Alex Doe",
        slug="alex-doe",
        personal_details={"email": "alex@example.com"},
        target_preferences={"preferred_locations": ["Remote"], "remote": True},
        source_profile_data={"resume_path": "/profiles/alex-doe/resume.pdf"},
    )
    session.add(alex)
    session.flush()

    job = Job(
        canonical_url="https://example.com/jobs/json-1",
        title="JSON Engineer",
        title_normalized="json engineer",
        source="fixture",
        source_type="company",
    )
    session.add(job)
    session.flush()

    session.add(
        AutoApplyQueueItem(
            candidate_profile_id=alex.id,
            job_id=job.id,
            status=AutoApplyQueueStatus.QUEUED,
            priority=120,
            attempt_count=0,
            max_attempts=3,
            created_at=now - timedelta(minutes=8),
            updated_at=now - timedelta(minutes=8),
        )
    )
    session.commit()
    session.close()

    monkeypatch.setattr(cli_main, "SessionLocal", session_factory)
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "list-auto-apply-summaries",
            "--limit",
            "10",
            "--sort-by",
            "candidate_asc",
            "--queue-slo-filter",
            "all",
            "--json-output",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["sort_by"] == "candidate_asc"
    assert payload["next_cursor"] is None
    assert len(payload["items"]) == 1
    assert payload["items"][0]["candidate_profile_slug"] == "alex-doe"
    assert payload["items"][0]["summary_delta_marker"].startswith("delta_")
