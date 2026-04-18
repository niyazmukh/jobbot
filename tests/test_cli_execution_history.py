from typer.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import jobbot.cli.main as cli_main
from jobbot.db.base import Base
from jobbot.db.models import CandidateProfile


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
