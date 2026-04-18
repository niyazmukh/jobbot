from typer.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from pathlib import Path

import jobbot.cli.main as cli_main
from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import BrowserProfile
from jobbot.models.enums import BrowserProfileType, SessionHealth


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
