"""Operational CLI entry points for the foundation phase."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from jobbot.browser.schemas import BrowserProfileCreate, BrowserProfileHealthUpdate
from jobbot.browser.service import (
    get_browser_profile_policy,
    list_browser_profiles,
    mark_browser_profile_used,
    register_browser_profile,
    update_browser_profile_health,
    validate_linkedin_browser_profile_session,
    validate_browser_profile_session,
)
from jobbot.config import get_settings
from jobbot.db.bootstrap import create_all_tables
from jobbot.db.session import SessionLocal
from jobbot.eligibility.service import (
    get_application_eligibility,
    list_application_eligibility,
    materialize_application_eligibility,
)
from jobbot.execution.service import (
    bootstrap_draft_application_attempt,
    build_draft_field_plan,
    build_site_field_overlay,
    execute_guarded_submit,
    evaluate_submit_gate,
    get_execution_artifact_detail,
    get_execution_attempt_detail,
    get_execution_dashboard,
    get_execution_replay_bundle,
    list_execution_dashboard_bulk_history_reads,
    list_execution_overview,
    list_draft_application_attempts,
    open_site_target_page,
    prune_execution_dashboard_bulk_history,
    replay_execution_dashboard_bulk_history_by_id,
    set_execution_dashboard_bulk_history_limit,
    start_draft_execution_attempt,
)
from jobbot.execution.linkedin import extract_linkedin_question_widgets
from jobbot.discovery.inbox import list_inbox_jobs, list_ready_to_apply_jobs
from jobbot.enrichment.service import enrich_job
from jobbot.models.enums import BrowserProfileType, ReviewStatus, SessionHealth
from jobbot.preparation.service import prepare_job_for_candidate
from jobbot.profiles.schemas import CandidateProfileImport
from jobbot.profiles.service import import_candidate_profile
from jobbot.review.service import list_review_queue, queue_score_review, set_review_status
from jobbot.scoring.service import score_job_for_candidate

app = typer.Typer(help="JobBot operational CLI.")
console = Console()


@app.command()
def doctor() -> None:
    """Print local paths and key thresholds."""

    settings = get_settings()
    console.print(f"App dir: {settings.data_dir}")
    console.print(f"DB URL: {settings.resolved_database_url}")
    console.print(f"Artifacts: {settings.artifacts_dir}")
    console.print(f"Browser profiles: {settings.browser_profiles_dir}")
    console.print(f"Auto-submit threshold: {settings.auto_submit_threshold}")


@app.command("init-db")
def init_db() -> None:
    """Create the local database tables from ORM metadata."""

    create_all_tables()
    console.print("[green]Database tables created.[/green]")


@app.command("import-profile")
def import_profile(
    file: Path = typer.Option(..., "--file", exists=True, file_okay=True, dir_okay=False),
    replace: bool = typer.Option(False, "--replace", help="Replace an existing profile with the same slug."),
) -> None:
    """Import a candidate profile and authoritative facts from JSON."""

    payload = CandidateProfileImport.model_validate(
        json.loads(file.read_text(encoding="utf-8"))
    )
    session = SessionLocal()
    try:
        profile = import_candidate_profile(session, payload, replace_existing=replace)
    finally:
        session.close()

    console.print(f"[green]Imported candidate profile:[/green] {profile.slug}")
    console.print(f"Facts imported: {len(payload.facts)}")


@app.command("register-browser-profile")
def register_browser_profile_cmd(
    profile_key: str = typer.Option(..., "--profile-key"),
    profile_type: BrowserProfileType = typer.Option(..., "--profile-type"),
    display_name: str = typer.Option(..., "--display-name"),
    storage_path: Path = typer.Option(..., "--storage-path"),
    candidate_profile_slug: str | None = typer.Option(None, "--candidate-profile"),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    """Register or update a persistent browser profile."""

    session = SessionLocal()
    try:
        profile = register_browser_profile(
            session,
            BrowserProfileCreate(
                profile_key=profile_key,
                profile_type=profile_type,
                display_name=display_name,
                storage_path=str(storage_path),
                candidate_profile_slug=candidate_profile_slug,
                notes=notes,
            ),
        )
    finally:
        session.close()

    console.print(f"[green]Registered browser profile:[/green] {profile.profile_key}")
    console.print(f"Health: {profile.session_health}")


@app.command("set-browser-profile-health")
def set_browser_profile_health(
    profile_key: str = typer.Option(..., "--profile-key"),
    session_health: SessionHealth = typer.Option(..., "--session-health"),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    """Update the health state of a registered browser profile."""

    session = SessionLocal()
    try:
        profile = update_browser_profile_health(
            session,
            profile_key,
            BrowserProfileHealthUpdate(session_health=session_health, notes=notes),
        )
    finally:
        session.close()

    console.print(f"[green]Updated browser profile:[/green] {profile.profile_key}")
    console.print(f"Health: {profile.session_health}")


@app.command("list-browser-profiles")
def list_browser_profiles_cmd() -> None:
    """List registered browser profiles and their health state."""

    session = SessionLocal()
    try:
        profiles = list_browser_profiles(session)
    finally:
        session.close()

    table = Table(title="Browser Profiles", show_header=True, header_style="bold cyan")
    table.add_column("Key", style="bold")
    table.add_column("Type")
    table.add_column("Health")
    table.add_column("Candidate")
    table.add_column("Path")

    for profile in profiles:
        table.add_row(
            profile.profile_key,
            profile.profile_type.value,
            profile.session_health,
            str(profile.candidate_profile_id or ""),
            profile.storage_path,
        )

    console.print(table)


@app.command("validate-browser-profile")
def validate_browser_profile_cmd(
    profile_key: str = typer.Option(..., "--profile-key"),
    observation_file: Path = typer.Option(..., "--file", exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Classify and persist browser session health from an observation payload."""

    from jobbot.browser.schemas import BrowserSessionObservation

    observation = BrowserSessionObservation.model_validate(
        json.loads(observation_file.read_text(encoding="utf-8"))
    )

    session = SessionLocal()
    try:
        profile = validate_browser_profile_session(session, profile_key, observation)
    finally:
        session.close()

    console.print(f"[green]Validated browser profile:[/green] {profile.profile_key}")
    console.print(f"Health: {profile.session_health}")
    console.print(f"Validation details: {profile.validation_details}")


@app.command("probe-linkedin-browser-profile")
def probe_linkedin_browser_profile_cmd(
    profile_key: str = typer.Option(..., "--profile-key"),
    page_url: str = typer.Option(..., "--page-url"),
    page_title: str | None = typer.Option(None, "--page-title"),
    page_content: str | None = typer.Option(None, "--page-content"),
    redirect_count: int = typer.Option(0, "--redirect-count", min=0),
    visible_job_count: int | None = typer.Option(None, "--visible-job-count", min=0),
    authenticated: str | None = typer.Option(None, "--authenticated"),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    """Run deterministic LinkedIn session probe and persist browser health."""

    normalized_authenticated: bool | None
    if authenticated is None:
        normalized_authenticated = None
    else:
        token = authenticated.strip().lower()
        if token in {"true", "1", "yes", "y"}:
            normalized_authenticated = True
        elif token in {"false", "0", "no", "n"}:
            normalized_authenticated = False
        else:
            raise typer.BadParameter("--authenticated must be true or false")

    session = SessionLocal()
    try:
        profile = validate_linkedin_browser_profile_session(
            session,
            profile_key,
            page_url=page_url,
            page_title=page_title,
            page_content=page_content,
            redirect_count=redirect_count,
            visible_job_count=visible_job_count,
            authenticated=normalized_authenticated,
            notes=notes,
        )
        policy = get_browser_profile_policy(session, profile_key)
    finally:
        session.close()

    console.print(f"[green]LinkedIn probe saved for browser profile:[/green] {profile.profile_key}")
    console.print(f"Health: {profile.session_health}")
    console.print(f"Recommended action: {policy.recommended_action}")
    console.print(f"Validation details: {profile.validation_details}")


@app.command("browser-profile-readiness")
def browser_profile_readiness(
    profile_key: str = typer.Option(..., "--profile-key"),
) -> None:
    """Show whether a browser profile is currently allowed for discovery/apply."""

    session = SessionLocal()
    try:
        policy = get_browser_profile_policy(session, profile_key)
    finally:
        session.close()

    console.print(f"[bold]Profile:[/bold] {policy.profile_key}")
    console.print(f"Health: {policy.session_health.value}")
    console.print(f"Allow discovery: {policy.allow_discovery}")
    console.print(f"Allow application: {policy.allow_application}")
    console.print(f"Requires reauth: {policy.requires_reauth}")
    console.print(f"Recommended action: {policy.recommended_action}")
    console.print(f"Reasons: {', '.join(policy.reasons)}")


@app.command("touch-browser-profile")
def touch_browser_profile_cmd(
    profile_key: str = typer.Option(..., "--profile-key"),
) -> None:
    """Mark a browser profile as recently used."""

    session = SessionLocal()
    try:
        profile = mark_browser_profile_used(session, profile_key)
    finally:
        session.close()

    console.print(f"[green]Touched browser profile:[/green] {profile.profile_key}")
    console.print(f"Last used at: {profile.last_used_at}")


@app.command("list-jobs")
def list_jobs_cmd(
    limit: int = typer.Option(20, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    candidate_profile: str | None = typer.Option(None, "--candidate-profile"),
    status: str | None = typer.Option(None, "--status"),
    ats_vendor: str | None = typer.Option(None, "--ats-vendor"),
    remote_type: str | None = typer.Option(None, "--remote-type"),
    preparation_state: str | None = typer.Option(None, "--preparation-state"),
    application_readiness: str | None = typer.Option(None, "--application-readiness"),
    execution_state: str | None = typer.Option(None, "--execution-state"),
    sort_by: str = typer.Option("last_seen_at", "--sort-by"),
    descending: bool = typer.Option(True, "--descending/--ascending"),
) -> None:
    """List persisted jobs in the local inbox."""

    session = SessionLocal()
    try:
        jobs = list_inbox_jobs(
            session,
            limit=limit,
            offset=offset,
            candidate_profile_slug=candidate_profile,
            status=status,
            ats_vendor=ats_vendor,
            remote_type=remote_type,
            preparation_state=preparation_state,
            application_readiness=application_readiness,
            execution_state=execution_state,
            sort_by=sort_by,
            descending=descending,
        )
    finally:
        session.close()

    table = Table(title="Job Inbox", show_header=True, header_style="bold cyan")
    table.add_column("ID", justify="right")
    table.add_column("Company", style="bold")
    table.add_column("Title")
    table.add_column("Location")
    table.add_column("Status")
    table.add_column("ATS")
    table.add_column("Prep")
    table.add_column("Ready")
    table.add_column("Exec")
    table.add_column("Sources", justify="right")

    for job in jobs:
        table.add_row(
            str(job.job_id),
            job.company_name or "",
            job.title,
            job.location_normalized or "",
            job.status,
            job.ats_vendor or "",
            "" if job.prepared_summary is None else str(job.prepared_summary.get("preparation_state")),
            "" if job.application_readiness is None else str(job.application_readiness.get("state")),
            "" if job.execution_summary is None else str(job.execution_summary.get("attempt_result") or "pending"),
            str(job.source_count),
        )

    console.print(table)


@app.command("list-ready-to-apply")
def list_ready_to_apply_cmd(
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    limit: int = typer.Option(20, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
) -> None:
    """List jobs that are currently ready to apply for a candidate."""

    session = SessionLocal()
    try:
        jobs = list_ready_to_apply_jobs(
            session,
            candidate_profile_slug=candidate_profile,
            limit=limit,
            offset=offset,
        )
    finally:
        session.close()

    table = Table(title="Ready To Apply", show_header=True, header_style="bold cyan")
    table.add_column("ID", justify="right")
    table.add_column("Company", style="bold")
    table.add_column("Title")
    table.add_column("Location")
    table.add_column("Ready")

    for job in jobs:
        table.add_row(
            str(job.job_id),
            job.company_name or "",
            job.title,
            job.location_normalized or "",
            "" if job.application_readiness is None else str(job.application_readiness.get("state")),
        )

    console.print(table)


@app.command("materialize-eligibility")
def materialize_eligibility_cmd(
    job_id: int = typer.Option(..., "--job-id", min=1),
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
) -> None:
    """Persist the current candidate/job execution eligibility snapshot."""

    session = SessionLocal()
    try:
        eligibility = materialize_application_eligibility(
            session,
            job_id=job_id,
            candidate_profile_slug=candidate_profile,
        )
    finally:
        session.close()

    console.print(f"[green]Materialized eligibility:[/green] {eligibility.job_id}")
    console.print(f"Candidate: {eligibility.candidate_profile_slug}")
    console.print(f"State: {eligibility.readiness_state}")
    console.print(f"Ready: {eligibility.ready}")
    console.print(f"Reasons: {eligibility.reasons}")


@app.command("list-eligibility")
def list_eligibility_cmd(
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    ready_only: bool = typer.Option(False, "--ready-only"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """List persisted eligibility snapshots for one candidate."""

    session = SessionLocal()
    try:
        rows = list_application_eligibility(
            session,
            candidate_profile_slug=candidate_profile,
            ready_only=ready_only,
            limit=limit,
        )
    finally:
        session.close()

    table = Table(title="Application Eligibility", show_header=True, header_style="bold cyan")
    table.add_column("Job", justify="right")
    table.add_column("Candidate")
    table.add_column("State")
    table.add_column("Ready")
    table.add_column("Reasons")

    for row in rows:
        table.add_row(
            str(row.job_id),
            row.candidate_profile_slug,
            row.readiness_state,
            str(row.ready),
            ", ".join(row.reasons),
        )

    console.print(table)


@app.command("bootstrap-draft-attempt")
def bootstrap_draft_attempt_cmd(
    job_id: int = typer.Option(..., "--job-id", min=1),
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    browser_profile_key: str | None = typer.Option(None, "--browser-profile-key"),
) -> None:
    """Create a draft application attempt from a persisted ready-to-apply snapshot."""

    session = SessionLocal()
    try:
        attempt = bootstrap_draft_application_attempt(
            session,
            job_id=job_id,
            candidate_profile_slug=candidate_profile,
            browser_profile_key=browser_profile_key,
        )
    finally:
        session.close()

    console.print(f"[green]Bootstrapped draft attempt:[/green] {attempt.attempt_id}")
    console.print(f"Application: {attempt.application_id}")
    console.print(f"Job: {attempt.job_id}")
    console.print(f"Candidate: {attempt.candidate_profile_slug}")
    console.print(f"Readiness: {attempt.readiness_state}")
    console.print(f"Browser profile: {attempt.browser_profile_key or 'none'}")
    console.print(f"Result: {attempt.attempt_result or 'pending'}")


@app.command("list-draft-attempts")
def list_draft_attempts_cmd(
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """List persisted draft application attempts for one candidate."""

    session = SessionLocal()
    try:
        rows = list_draft_application_attempts(
            session,
            candidate_profile_slug=candidate_profile,
            limit=limit,
        )
    finally:
        session.close()

    table = Table(title="Draft Application Attempts", show_header=True, header_style="bold cyan")
    table.add_column("Attempt", justify="right")
    table.add_column("Application", justify="right")
    table.add_column("Job", justify="right")
    table.add_column("State")
    table.add_column("Ready")
    table.add_column("Browser")
    table.add_column("Result")
    table.add_column("Confidence")

    for row in rows:
        table.add_row(
            str(row.attempt_id),
            str(row.application_id),
            str(row.job_id),
            row.readiness_state,
            str(row.ready),
            row.browser_profile_key or "",
            row.attempt_result or "",
            "" if row.submit_confidence is None else str(row.submit_confidence),
        )

    console.print(table)


@app.command("list-execution-overview")
def list_execution_overview_cmd(
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    blocked_only: bool = typer.Option(False, "--blocked-only"),
    manual_review_only: bool = typer.Option(False, "--manual-review-only"),
    failure_code: str | None = typer.Option(None, "--failure-code"),
    failure_classification: str | None = typer.Option(None, "--failure-classification"),
    max_submit_confidence: float | None = typer.Option(
        None,
        "--max-submit-confidence",
        min=0.0,
        max=1.0,
    ),
    sort_by: str = typer.Option("started_at", "--sort-by"),
    descending: bool = typer.Option(True, "--descending/--ascending"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """List operator-facing draft execution rows for one candidate."""

    session = SessionLocal()
    try:
        rows = list_execution_overview(
            session,
            candidate_profile_slug=candidate_profile,
            blocked_only=blocked_only,
            manual_review_only=manual_review_only,
            failure_code=failure_code,
            failure_classification=failure_classification,
            max_submit_confidence=max_submit_confidence,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
        )
    finally:
        session.close()

    table = Table(title="Execution Overview", show_header=True, header_style="bold cyan")
    table.add_column("Attempt", justify="right")
    table.add_column("Job", justify="right")
    table.add_column("Company")
    table.add_column("Title")
    table.add_column("Vendor")
    table.add_column("Result")
    table.add_column("Failure")
    table.add_column("Failure Class")
    table.add_column("Confidence")
    table.add_column("Stage")
    table.add_column("Artifacts")
    table.add_column("Action")

    for row in rows:
        table.add_row(
            str(row.attempt_id),
            str(row.job_id),
            row.company_name or "",
            row.job_title,
            row.site_vendor or "",
            row.attempt_result or "pending",
            row.failure_code or "",
            row.failure_classification or "",
            "" if row.submit_confidence is None else str(row.submit_confidence),
            row.latest_event_type or "",
            str(row.artifact_count),
            f"{row.primary_action_label}: {row.primary_action_route}",
        )

    console.print(table)


@app.command("show-execution-dashboard")
def show_execution_dashboard_cmd(
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    manual_review_only: bool = typer.Option(False, "--manual-review-only"),
    failure_code: str | None = typer.Option(None, "--failure-code"),
    failure_classification: str | None = typer.Option(None, "--failure-classification"),
    max_submit_confidence: float | None = typer.Option(
        None,
        "--max-submit-confidence",
        min=0.0,
        max=1.0,
    ),
    sort_by: str = typer.Option("started_at", "--sort-by"),
    descending: bool = typer.Option(True, "--descending/--ascending"),
    limit: int = typer.Option(10, "--limit", min=1, max=100),
) -> None:
    """Show a candidate-scoped execution dashboard summary."""

    session = SessionLocal()
    try:
        detail = get_execution_dashboard(
            session,
            candidate_profile_slug=candidate_profile,
            manual_review_only=manual_review_only,
            failure_code=failure_code,
            failure_classification=failure_classification,
            max_submit_confidence=max_submit_confidence,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
        )
    finally:
        session.close()

    console.print(f"[bold]Execution dashboard:[/bold] {detail.candidate_profile_slug}")
    console.print(
        "Attempts: "
        f"total={detail.total_attempts} "
        f"blocked={detail.blocked_attempts} "
        f"manual_review_blocked={detail.manual_review_blocked_attempts} "
        f"pending={detail.pending_attempts} "
        f"review={detail.review_state_attempts} "
        f"replay_ready={detail.replay_ready_attempts}"
    )

    if detail.blocked_failure_counts:
        breakdown = ", ".join(
            f"{code}={count}"
            for code, count in sorted(
                detail.blocked_failure_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        console.print(f"Blocked failure breakdown: {breakdown}")
    if detail.blocked_failure_classification_counts:
        class_breakdown = ", ".join(
            f"{classification}={count}"
            for classification, count in sorted(
                detail.blocked_failure_classification_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        console.print(f"Blocked failure classification breakdown: {class_breakdown}")

    blocked_table = Table(title="Blocked Attempts", show_header=True, header_style="bold cyan")
    blocked_table.add_column("Attempt", justify="right")
    blocked_table.add_column("Job", justify="right")
    blocked_table.add_column("Company")
    blocked_table.add_column("Title")
    blocked_table.add_column("Failure")
    blocked_table.add_column("Failure Class")
    blocked_table.add_column("Confidence")
    for row in detail.blocked_recent_attempts:
        blocked_table.add_row(
            str(row.attempt_id),
            str(row.job_id),
            row.company_name or "",
            row.job_title,
            row.failure_code or "",
            row.failure_classification or "",
            "" if row.submit_confidence is None else str(row.submit_confidence),
        )
    console.print(blocked_table)

    recent_table = Table(title="Recent Attempts", show_header=True, header_style="bold cyan")
    recent_table.add_column("Attempt", justify="right")
    recent_table.add_column("Job", justify="right")
    recent_table.add_column("Company")
    recent_table.add_column("Title")
    recent_table.add_column("Result")
    recent_table.add_column("Stage")
    for row in detail.recent_attempts:
        recent_table.add_row(
            str(row.attempt_id),
            str(row.job_id),
            row.company_name or "",
            row.job_title,
            row.attempt_result or "pending",
            row.latest_event_type or "",
        )
    console.print(recent_table)

    console.print("[bold]Recommended actions[/bold]")
    for action in detail.recommended_actions:
        console.print(f"- {action}")


@app.command("list-remediation-history")
def list_remediation_history_cmd(
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    history_sort: str = typer.Option("newest", "--history-sort"),
    limit: int = typer.Option(5, "--limit", min=1, max=50),
) -> None:
    """List persisted dashboard remediation-history entries for one candidate."""

    session = SessionLocal()
    try:
        rows = list_execution_dashboard_bulk_history_reads(
            session,
            candidate_profile_slug=candidate_profile,
            history_sort=history_sort,
            limit=limit,
        )
    except ValueError as exc:
        session.close()
        raise typer.BadParameter(str(exc)) from exc
    else:
        session.close()

    table = Table(title="Execution Remediation History", show_header=True, header_style="bold cyan")
    table.add_column("#", justify="right")
    table.add_column("History ID", no_wrap=True)
    table.add_column("Recorded")
    table.add_column("Targeted/Remediated/Failed")
    table.add_column("Scope")
    table.add_column("First Failure")
    table.add_column("Replay")

    for index, row in enumerate(rows, start=1):
        scope_bits: list[str] = []
        if row.failure_code:
            scope_bits.append(f"failure_code={row.failure_code}")
        if row.failure_classification:
            scope_bits.append(f"failure_classification={row.failure_classification}")
        if row.manual_review_only:
            scope_bits.append("manual_review_only=true")
        first_failure = ""
        if row.first_failure_attempt_id is not None and row.first_failure_code is not None:
            first_failure = f"{row.first_failure_attempt_id}:{row.first_failure_code}"
        table.add_row(
            str(index),
            row.history_id,
            row.created_at,
            f"{row.requested_count}/{row.remediated_count}/{row.failed_count}",
            " | ".join(scope_bits),
            first_failure,
            row.rerun_route,
        )

    console.print(table)


@app.command("replay-remediation-history")
def replay_remediation_history_cmd(
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    history_id: str | None = typer.Option(None, "--history-id"),
    history_index: int | None = typer.Option(None, "--history-index", min=1),
    history_sort: str = typer.Option("newest", "--history-sort"),
) -> None:
    """Replay a persisted remediation-history scope by stable id or fallback index."""

    session = SessionLocal()
    try:
        if history_id and history_index is not None:
            raise typer.BadParameter("choose_history_id_or_history_index")
        replay_history_id = history_id
        if replay_history_id is None:
            resolved_history_index = history_index or 1
            history_rows = list_execution_dashboard_bulk_history_reads(
                session,
                candidate_profile_slug=candidate_profile,
                history_sort=history_sort,
                limit=max(resolved_history_index, 50),
            )
            if resolved_history_index > len(history_rows):
                raise typer.BadParameter("remediation_history_index_out_of_range")
            replay_history_id = history_rows[resolved_history_index - 1].history_id
        batch = replay_execution_dashboard_bulk_history_by_id(
            session,
            candidate_profile_slug=candidate_profile,
            history_id=replay_history_id,
        )
    except ValueError as exc:
        session.close()
        raise typer.BadParameter(str(exc)) from exc
    finally:
        session.close()

    console.print(
        "[green]Replayed remediation scope:[/green] "
        f"targeted={batch.requested_count} remediated={batch.remediated_count} failed={batch.failed_count}"
    )


@app.command("set-remediation-history-limit")
def set_remediation_history_limit_cmd(
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    history_limit: int = typer.Option(10, "--history-limit", min=1, max=500),
) -> None:
    """Set remediation-history retention limit and prune persisted rows to that bound."""

    session = SessionLocal()
    try:
        result = set_execution_dashboard_bulk_history_limit(
            session,
            candidate_profile_slug=candidate_profile,
            history_limit=history_limit,
        )
    except ValueError as exc:
        session.close()
        raise typer.BadParameter(str(exc)) from exc
    finally:
        session.close()

    console.print(
        "[green]Updated remediation-history limit:[/green] "
        f"configured_limit={result.configured_limit} "
        f"before={result.before_count} after={result.after_count} removed={result.removed_count}"
    )


@app.command("prune-remediation-history")
def prune_remediation_history_cmd(
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    keep_limit: int | None = typer.Option(None, "--keep-limit", min=1, max=500),
) -> None:
    """Prune remediation-history rows to keep-limit or configured retention limit."""

    session = SessionLocal()
    try:
        result = prune_execution_dashboard_bulk_history(
            session,
            candidate_profile_slug=candidate_profile,
            keep_limit=keep_limit,
        )
    except ValueError as exc:
        session.close()
        raise typer.BadParameter(str(exc)) from exc
    finally:
        session.close()

    console.print(
        "[green]Pruned remediation-history:[/green] "
        f"before={result.before_count} after={result.after_count} "
        f"removed={result.removed_count} keep={result.keep_limit}"
    )


@app.command("show-execution-attempt")
def show_execution_attempt_cmd(
    attempt_id: int = typer.Option(..., "--attempt-id", min=1),
) -> None:
    """Show one execution attempt with ordered events and artifacts."""

    session = SessionLocal()
    try:
        detail = get_execution_attempt_detail(session, attempt_id=attempt_id)
    finally:
        session.close()

    console.print(f"[bold]Attempt:[/bold] {detail.attempt_id}")
    console.print(f"Job: {detail.job_id} | {detail.job_title}")
    console.print(f"Candidate: {detail.candidate_profile_slug}")
    console.print(f"State: {detail.application_state} | Result: {detail.attempt_result or 'pending'}")
    console.print(f"Failure: {detail.failure_code or 'none'} | Confidence: {detail.submit_confidence}")
    console.print(f"Failure class: {detail.failure_classification or 'none'}")
    console.print(f"Notes: {detail.notes or ''}")

    event_table = Table(title="Execution Events", show_header=True, header_style="bold cyan")
    event_table.add_column("ID", justify="right")
    event_table.add_column("Type")
    event_table.add_column("Message")
    event_table.add_column("Artifacts")
    event_table.add_column("Created")
    for event in detail.events:
        event_table.add_row(
            str(event.event_id),
            event.event_type,
            event.message,
            ", ".join(event.artifact_routes),
            event.created_at.isoformat(),
        )
    console.print(event_table)

    artifact_table = Table(title="Execution Artifacts", show_header=True, header_style="bold cyan")
    artifact_table.add_column("ID", justify="right")
    artifact_table.add_column("Type")
    artifact_table.add_column("Inspect")
    artifact_table.add_column("Launch")
    artifact_table.add_column("Target")
    artifact_table.add_column("Raw")
    artifact_table.add_column("Path")
    artifact_table.add_column("Size")
    for artifact in detail.artifacts:
        artifact_table.add_row(
            str(artifact.artifact_id),
            artifact.artifact_type,
            artifact.inspect_route,
            artifact.launch_route or "",
            artifact.launch_target or "",
            artifact.raw_route or "",
            artifact.path,
            "" if artifact.size_bytes is None else str(artifact.size_bytes),
        )
    console.print(artifact_table)


@app.command("show-execution-artifact")
def show_execution_artifact_cmd(
    artifact_id: int = typer.Option(..., "--artifact-id", min=1),
) -> None:
    """Show one execution artifact with a bounded safe preview."""

    session = SessionLocal()
    try:
        detail = get_execution_artifact_detail(session, artifact_id=artifact_id)
    finally:
        session.close()

    console.print(f"[bold]Artifact:[/bold] {detail.artifact_id}")
    console.print(f"Attempt: {detail.attempt_id} | Type: {detail.artifact_type}")
    console.print(f"Path: {detail.path}")
    console.print(f"Exists: {detail.exists} | Preview: {detail.preview_kind}")
    console.print(f"Raw route: {detail.raw_route or 'unavailable'}")
    console.print(
        f"Launch: {detail.launch_label or 'unavailable'} | "
        f"{detail.launch_route or 'unavailable'} | target={detail.launch_target or 'unavailable'}"
    )
    console.print(f"Size: {detail.size_bytes} | Created: {detail.created_at.isoformat()}")
    if detail.preview_truncated:
        console.print("[yellow]Preview truncated for safety.[/yellow]")
    if detail.preview_text is None:
        console.print("[dim]No preview available for this artifact.[/dim]")
    else:
        console.print(detail.preview_text)


@app.command("show-execution-replay")
def show_execution_replay_cmd(
    attempt_id: int = typer.Option(..., "--attempt-id", min=1),
) -> None:
    """Show one replay-oriented execution bundle."""

    session = SessionLocal()
    try:
        detail = get_execution_replay_bundle(session, attempt_id=attempt_id)
    finally:
        session.close()

    console.print(f"[bold]Replay bundle:[/bold] attempt {detail.attempt_id}")
    console.print(f"Job: {detail.job_id} | {detail.job_title}")
    console.print(f"Candidate: {detail.candidate_profile_slug}")
    console.print(f"State: {detail.application_state} | Result: {detail.attempt_result or 'pending'}")
    console.print(f"Failure: {detail.failure_code or 'none'} | Latest event: {detail.latest_event_type or 'none'}")
    console.print(f"Target URL: {detail.target_url or 'unknown'}")
    console.print(f"Startup dir: {detail.startup_dir or 'unknown'}")

    asset_table = Table(title="Replay Assets", show_header=True, header_style="bold cyan")
    asset_table.add_column("Label")
    asset_table.add_column("Artifact", justify="right")
    asset_table.add_column("Type")
    asset_table.add_column("Exists")
    asset_table.add_column("Openable")
    asset_table.add_column("Hint")
    asset_table.add_column("Launch")
    asset_table.add_column("Target")
    asset_table.add_column("Raw route")
    asset_table.add_column("Path")
    for asset in detail.assets:
        asset_table.add_row(
            asset.label,
            "" if asset.artifact_id is None else str(asset.artifact_id),
            asset.artifact_type or "",
            str(asset.exists),
            str(asset.openable_locally),
            asset.open_hint or "",
            asset.launch_label or "",
            asset.launch_target or "",
            asset.raw_route or "",
            asset.path or "",
        )
    console.print(asset_table)

    console.print("[bold]Recommended actions[/bold]")
    for action in detail.recommended_actions:
        console.print(f"- {action}")


@app.command("start-draft-execution")
def start_draft_execution_cmd(
    attempt_id: int = typer.Option(..., "--attempt-id", min=1),
) -> None:
    """Create the staged startup bundle for a draft application attempt."""

    session = SessionLocal()
    try:
        startup = start_draft_execution_attempt(
            session,
            attempt_id=attempt_id,
        )
    finally:
        session.close()

    console.print(f"[green]Started draft execution:[/green] {startup.attempt_id}")
    console.print(f"Application: {startup.application_id}")
    console.print(f"Job: {startup.job_id}")
    console.print(f"Target URL: {startup.target_url}")
    console.print(f"Startup dir: {startup.startup_dir}")
    console.print(f"Artifacts: {startup.startup_artifact_ids}")


@app.command("build-draft-field-plan")
def build_draft_field_plan_cmd(
    attempt_id: int = typer.Option(..., "--attempt-id", min=1),
) -> None:
    """Create deterministic field mappings for a staged draft attempt."""

    session = SessionLocal()
    try:
        plan = build_draft_field_plan(
            session,
            attempt_id=attempt_id,
        )
    finally:
        session.close()

    console.print(f"[green]Built draft field plan:[/green] {plan.attempt_id}")
    console.print(f"Application: {plan.application_id}")
    console.print(f"Job: {plan.job_id}")
    console.print(f"Field count: {plan.field_count}")
    console.print(f"Artifact path: {plan.artifact_path}")


@app.command("build-site-field-overlay")
def build_site_field_overlay_cmd(
    attempt_id: int = typer.Option(..., "--attempt-id", min=1),
) -> None:
    """Create a site-aware selector overlay for a draft field plan."""

    session = SessionLocal()
    try:
        overlay = build_site_field_overlay(
            session,
            attempt_id=attempt_id,
        )
    finally:
        session.close()

    console.print(f"[green]Built site field overlay:[/green] {overlay.attempt_id}")
    console.print(f"Site vendor: {overlay.site_vendor}")
    console.print(f"Entries: {overlay.entry_count}")
    console.print(f"Artifact path: {overlay.artifact_path}")


@app.command("open-site-target")
def open_site_target_cmd(
    attempt_id: int = typer.Option(..., "--attempt-id", min=1),
) -> None:
    """Run a non-submitting target-open and field-resolution pass."""

    session = SessionLocal()
    try:
        opened = open_site_target_page(
            session,
            attempt_id=attempt_id,
        )
    finally:
        session.close()

    console.print(f"[green]Opened site target:[/green] {opened.attempt_id}")
    console.print(f"Site vendor: {opened.site_vendor}")
    console.print(f"Target URL: {opened.target_url}")
    console.print(f"Capture: {opened.capture_method}")
    console.print(f"Capture error: {opened.capture_error or 'none'}")
    console.print(f"Resolved: {opened.resolved_count}")
    console.print(f"Unresolved: {opened.unresolved_count}")


@app.command("evaluate-submit-gate")
def evaluate_submit_gate_cmd(
    attempt_id: int = typer.Option(..., "--attempt-id", min=1),
) -> None:
    """Evaluate guarded submit confidence for a draft attempt."""

    session = SessionLocal()
    try:
        gate = evaluate_submit_gate(
            session,
            attempt_id=attempt_id,
        )
    finally:
        session.close()

    console.print(f"[green]Evaluated submit gate:[/green] {gate.attempt_id}")
    console.print(f"Site vendor: {gate.site_vendor}")
    console.print(f"Application state: {gate.application_state}")
    console.print(f"Attempt result: {gate.attempt_result or 'pending'}")
    console.print(f"Failure code: {gate.failure_code or 'none'}")
    console.print(f"Confidence: {gate.confidence_score}")
    console.print(f"Allow submit: {gate.allow_submit}")
    console.print(f"Stop reasons: {gate.stop_reasons}")


@app.command("execute-guarded-submit")
def execute_guarded_submit_cmd(
    attempt_id: int = typer.Option(..., "--attempt-id", min=1),
) -> None:
    """Execute guarded submit for a draft attempt after gate approval."""

    session = SessionLocal()
    try:
        submitted = execute_guarded_submit(
            session,
            attempt_id=attempt_id,
        )
    finally:
        session.close()

    console.print(f"[green]Executed guarded submit:[/green] {submitted.attempt_id}")
    console.print(f"Site vendor: {submitted.site_vendor}")
    console.print(f"Application state: {submitted.application_state}")
    console.print(f"Attempt result: {submitted.attempt_result}")
    console.print(f"Submission mode: {submitted.submission_mode}")
    console.print(f"Target URL: {submitted.target_url}")
    console.print(f"Artifact path: {submitted.artifact_path}")


@app.command("enrich-job")
def enrich_job_cmd(
    job_id: int = typer.Option(..., "--job-id", min=1),
) -> None:
    """Run deterministic enrichment for a persisted job."""

    session = SessionLocal()
    try:
        job = enrich_job(session, job_id)
    finally:
        session.close()

    console.print(f"[green]Enriched job:[/green] {job.id}")
    console.print(f"Status: {job.status}")
    console.print(f"Requirements: {job.requirements_structured}")


@app.command("score-job")
def score_job_cmd(
    job_id: int = typer.Option(..., "--job-id", min=1),
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
) -> None:
    """Run deterministic scoring for a candidate/job pair."""

    session = SessionLocal()
    try:
        score = score_job_for_candidate(session, job_id, candidate_profile)
    finally:
        session.close()

    console.print(f"[green]Scored job:[/green] {score.job_id}")
    console.print(f"Candidate id: {score.candidate_profile_id}")
    console.print(f"Overall score: {score.overall_score}")
    console.print(f"Confidence: {score.score_json.get('confidence_score')}")
    console.print(f"Blocked: {score.score_json.get('blocked')}")
    console.print(f"Blocking reasons: {score.score_json.get('blocking_reasons')}")
    console.print(f"Breakdown: {score.score_json}")


@app.command("queue-score-review")
def queue_score_review_cmd(
    job_id: int = typer.Option(..., "--job-id", min=1),
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    """Queue a persisted score for manual review."""

    session = SessionLocal()
    try:
        review = queue_score_review(
            session,
            job_id=job_id,
            candidate_profile_slug=candidate_profile,
            reason=reason,
        )
    finally:
        session.close()

    console.print(f"[green]Queued review:[/green] {review.id}")
    console.print(f"Reason: {review.reason}")
    console.print(f"Status: {review.status}")
    console.print(f"Context: {review.context}")


@app.command("list-review-queue")
def list_review_queue_cmd(
    status: str | None = typer.Option(None, "--status"),
    entity_type: str | None = typer.Option(None, "--entity-type"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """List review queue items."""

    session = SessionLocal()
    try:
        reviews = list_review_queue(session, status=status, entity_type=entity_type, limit=limit)
    finally:
        session.close()

    table = Table(title="Review Queue", show_header=True, header_style="bold cyan")
    table.add_column("ID", justify="right")
    table.add_column("Entity")
    table.add_column("Reason")
    table.add_column("Status")
    table.add_column("Confidence")
    table.add_column("Context")

    for review in reviews:
        table.add_row(
            str(review.id),
            f"{review.entity_type}:{review.entity_id}",
            review.reason,
            review.status,
            "" if review.confidence is None else str(review.confidence),
            json.dumps(review.context or {}, ensure_ascii=True),
        )

    console.print(table)


@app.command("set-review-status")
def set_review_status_cmd(
    review_id: int = typer.Option(..., "--review-id", min=1),
    status: ReviewStatus = typer.Option(..., "--status"),
) -> None:
    """Update a review queue item status."""

    session = SessionLocal()
    try:
        review = set_review_status(session, review_id=review_id, status=status)
    finally:
        session.close()

    console.print(f"[green]Updated review:[/green] {review.id}")
    console.print(f"Status: {review.status}")


@app.command("prepare-job")
def prepare_job_cmd(
    job_id: int = typer.Option(..., "--job-id", min=1),
    candidate_profile: str = typer.Option(..., "--candidate-profile"),
) -> None:
    """Create deterministic preparation records for a candidate/job pair."""

    session = SessionLocal()
    try:
        summary = prepare_job_for_candidate(
            session,
            job_id=job_id,
            candidate_profile_slug=candidate_profile,
        )
    finally:
        session.close()

    console.print(f"[green]Prepared job:[/green] {summary.job_id}")
    console.print(f"Candidate: {summary.candidate_profile_slug}")
    console.print(f"Resume variant id: {summary.resume_variant_id}")
    console.print(f"Generated documents: {summary.generated_document_ids}")
    console.print(f"Answers: {summary.answer_ids}")
    console.print(f"Queued reviews: {summary.queued_review_ids}")


@app.command("extract-linkedin-questions")
def extract_linkedin_questions_cmd(
    file: Path = typer.Option(..., "--file", exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Extract deterministic LinkedIn question widgets from an HTML capture file."""

    extraction = extract_linkedin_question_widgets(
        page_html=file.read_text(encoding="utf-8"),
    )

    console.print(f"[bold]Question count:[/bold] {extraction.question_count}")
    console.print(f"[bold]Unknown/low-confidence:[/bold] {extraction.unknown_field_count}")
    console.print(f"[bold]Assist required:[/bold] {extraction.assist_required}")
    console.print(f"[bold]Recommended mode:[/bold] {extraction.recommended_mode}")

    table = Table(title="LinkedIn Question Widgets", show_header=True, header_style="bold cyan")
    table.add_column("Field key")
    table.add_column("Question")
    table.add_column("Type")
    table.add_column("Confidence")
    table.add_column("Source")
    table.add_column("Assist")
    for row in extraction.questions:
        table.add_row(
            row.field_key,
            row.question_text,
            row.field_type,
            str(row.confidence),
            row.source,
            str(row.assist_required),
        )
    console.print(table)


if __name__ == "__main__":
    app()
