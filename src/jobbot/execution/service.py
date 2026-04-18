"""Execution bootstrap services built on persisted eligibility."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.config import get_settings
from jobbot.browser.service import get_browser_profile_policy
from jobbot.db.models import (
    Application,
    ApplicationAttempt,
    ApplicationEligibility,
    ApplicationEvent,
    Artifact,
    BrowserProfile,
    CandidateProfile,
    Company,
    FieldMapping,
    Job,
    utcnow,
)
from jobbot.execution.schemas import (
    DraftApplicationAttemptRead,
    DraftExecutionArtifactRead,
    DraftExecutionArtifactDetailRead,
    DraftExecutionDashboardRead,
    DraftExecutionAttemptDetailRead,
    DraftExecutionEventRead,
    DraftGuardedSubmitRead,
    DraftExecutionOverviewRead,
    DraftExecutionReplayAssetRead,
    DraftExecutionReplayBundleRead,
    DraftExecutionStartupRead,
    DraftFieldPlanEntryRead,
    DraftFieldPlanRead,
    DraftResolvedFieldRead,
    DraftSubmitGateRead,
    DraftSiteFieldPlanEntryRead,
    DraftSiteFieldPlanRead,
    DraftTargetOpenRead,
)
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
)
from jobbot.execution.vendor_registry import get_vendor_execution_handler
from jobbot.models.enums import (
    ApplicationMode,
    ApplicationState,
    ArtifactType,
    AttemptResult,
    BrowserProfileType,
    TruthTier,
)
from jobbot.preparation import get_prepared_job_read


def bootstrap_draft_application_attempt(
    session: Session,
    *,
    job_id: int,
    candidate_profile_slug: str,
    browser_profile_key: str | None = None,
) -> DraftApplicationAttemptRead:
    """Create a draft application attempt from a persisted ready-to-apply snapshot."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    eligibility = session.scalar(
        select(ApplicationEligibility).where(
            ApplicationEligibility.job_id == job_id,
            ApplicationEligibility.candidate_profile_id == candidate.id,
        )
    )
    if eligibility is None:
        raise ValueError("application_eligibility_not_found")
    if not eligibility.ready or eligibility.readiness_state != "ready_to_apply":
        raise ValueError("application_not_ready_to_apply")

    session_health = None
    if browser_profile_key is not None:
        profile = session.scalar(
            select(BrowserProfile).where(BrowserProfile.profile_key == browser_profile_key)
        )
        if profile is None:
            raise ValueError("browser_profile_not_found")
        if profile.profile_type != BrowserProfileType.APPLICATION:
            raise ValueError("browser_profile_not_application_type")
        policy = get_browser_profile_policy(session, browser_profile_key)
        if not policy.allow_application:
            raise ValueError("browser_profile_not_ready_for_application")
        session_health = profile.session_health

    application = session.scalar(
        select(Application).where(
            Application.job_id == job_id,
            Application.candidate_profile_id == candidate.id,
        )
    )
    created_application = False
    now = utcnow()
    if application is None:
        application = Application(
            job_id=job_id,
            candidate_profile_id=candidate.id,
            current_state=ApplicationState.PREPARED.value,
            created_at=now,
            updated_at=now,
        )
        session.add(application)
        session.flush()
        created_application = True
    else:
        if application.current_state == ApplicationState.APPLIED.value:
            raise ValueError("application_already_applied")
        if application.current_state != ApplicationState.REVIEW.value:
            application.current_state = ApplicationState.PREPARED.value
        application.updated_at = now
        session.flush()

    attempt = ApplicationAttempt(
        application_id=application.id,
        mode=ApplicationMode.DRAFT,
        browser_profile_key=browser_profile_key,
        session_health=session_health,
        started_at=now,
        notes="Bootstrapped from persisted eligibility snapshot.",
    )
    session.add(attempt)
    session.flush()

    event = ApplicationEvent(
        application_id=application.id,
        attempt_id=attempt.id,
        event_type="draft_attempt_bootstrapped",
        message="Draft attempt bootstrapped from persisted eligibility snapshot.",
        payload={
            "job_id": job_id,
            "candidate_profile_slug": candidate_profile_slug,
            "readiness_state": eligibility.readiness_state,
            "ready": eligibility.ready,
            "reasons": list(eligibility.reasons or []),
            "browser_profile_key": browser_profile_key,
            "eligibility_materialized_at": (
                eligibility.materialized_at.isoformat() if eligibility.materialized_at else None
            ),
        },
        created_at=now,
    )
    session.add(event)
    session.flush()

    application.last_attempt_id = attempt.id
    application.updated_at = now
    session.commit()
    session.refresh(application)
    session.refresh(attempt)
    session.refresh(event)

    return DraftApplicationAttemptRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=job_id,
        candidate_profile_slug=candidate_profile_slug,
        application_state=application.current_state,
        attempt_mode=attempt.mode.value,
        browser_profile_key=attempt.browser_profile_key,
        session_health=attempt.session_health,
        attempt_result=attempt.result,
        failure_code=attempt.failure_code,
        submit_confidence=attempt.submit_confidence,
        notes=attempt.notes,
        readiness_state=eligibility.readiness_state,
        ready=eligibility.ready,
        reasons=list(eligibility.reasons or []),
        created_application=created_application,
        started_at=attempt.started_at,
    )


def list_draft_application_attempts(
    session: Session,
    *,
    candidate_profile_slug: str,
    limit: int = 50,
) -> list[DraftApplicationAttemptRead]:
    """List draft application attempts for one candidate."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    rows = session.execute(
        select(Application, ApplicationAttempt, ApplicationEligibility)
        .join(ApplicationAttempt, ApplicationAttempt.application_id == Application.id)
        .join(
            ApplicationEligibility,
            (ApplicationEligibility.job_id == Application.job_id)
            & (ApplicationEligibility.candidate_profile_id == Application.candidate_profile_id),
        )
        .where(
            Application.candidate_profile_id == candidate.id,
            ApplicationAttempt.mode == ApplicationMode.DRAFT,
        )
        .order_by(ApplicationAttempt.started_at.desc(), ApplicationAttempt.id.desc())
        .limit(limit)
    ).all()

    items: list[DraftApplicationAttemptRead] = []
    for application, attempt, eligibility in rows:
        event_id = session.scalar(
            select(ApplicationEvent.id)
            .where(
                ApplicationEvent.attempt_id == attempt.id,
                ApplicationEvent.event_type == "draft_attempt_bootstrapped",
            )
            .order_by(ApplicationEvent.id.desc())
        )
        items.append(
            DraftApplicationAttemptRead(
                application_id=application.id,
                attempt_id=attempt.id,
                event_id=int(event_id or 0),
                job_id=application.job_id,
                candidate_profile_slug=candidate_profile_slug,
                application_state=application.current_state,
                attempt_mode=attempt.mode.value,
                browser_profile_key=attempt.browser_profile_key,
                session_health=attempt.session_health,
                attempt_result=attempt.result,
                failure_code=attempt.failure_code,
                submit_confidence=attempt.submit_confidence,
                notes=attempt.notes,
                readiness_state=eligibility.readiness_state,
                ready=eligibility.ready,
                reasons=list(eligibility.reasons or []),
                created_application=False,
                started_at=attempt.started_at,
            )
        )

    return items


def list_execution_overview(
    session: Session,
    *,
    candidate_profile_slug: str,
    blocked_only: bool = False,
    manual_review_only: bool = False,
    failure_code: str | None = None,
    failure_classification: str | None = None,
    max_submit_confidence: float | None = None,
    sort_by: str = "started_at",
    descending: bool = True,
    limit: int = 50,
) -> list[DraftExecutionOverviewRead]:
    """Return operator-facing draft execution rows with job context and attempt outcome."""

    candidate = session.scalar(
        select(CandidateProfile).where(CandidateProfile.slug == candidate_profile_slug)
    )
    if candidate is None:
        raise ValueError("candidate_profile_not_found")

    rows = session.execute(
        select(Application, ApplicationAttempt, ApplicationEligibility, Job, Company.name)
        .join(ApplicationAttempt, ApplicationAttempt.application_id == Application.id)
        .join(
            ApplicationEligibility,
            (ApplicationEligibility.job_id == Application.job_id)
            & (ApplicationEligibility.candidate_profile_id == Application.candidate_profile_id),
        )
        .join(Job, Job.id == Application.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(
            Application.candidate_profile_id == candidate.id,
            ApplicationAttempt.mode == ApplicationMode.DRAFT,
        )
        .order_by(ApplicationAttempt.started_at.desc(), ApplicationAttempt.id.desc())
        .limit(limit)
    ).all()

    items: list[DraftExecutionOverviewRead] = []
    for application, attempt, eligibility, job, company_name in rows:
        latest_event = session.execute(
            select(ApplicationEvent)
            .where(ApplicationEvent.attempt_id == attempt.id)
            .order_by(ApplicationEvent.created_at.desc(), ApplicationEvent.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        submit_stage_event = session.execute(
            select(ApplicationEvent)
            .where(
                ApplicationEvent.attempt_id == attempt.id,
                ApplicationEvent.event_type.in_(
                    ["draft_submit_executed", "draft_submit_execution_blocked"]
                ),
            )
            .order_by(ApplicationEvent.created_at.desc(), ApplicationEvent.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        submit_interaction = _extract_submit_interaction_diagnostics_from_event(submit_stage_event)
        attempt_failure_classification = _resolve_attempt_failure_classification(
            session=session,
            attempt_id=attempt.id,
            latest_event=latest_event,
        )
        if (
            attempt.result == AttemptResult.BLOCKED.value
            and (
                attempt_failure_classification is None
                or not attempt_failure_classification.strip()
            )
        ):
            attempt_failure_classification = "unknown_classification"
        artifacts = session.scalars(
            select(Artifact).where(Artifact.attempt_id == attempt.id)
        ).all()
        artifact_type_counts = {
            ArtifactType.SCREENSHOT.value: 0,
            ArtifactType.HTML_SNAPSHOT.value: 0,
            ArtifactType.MODEL_IO.value: 0,
            ArtifactType.GENERATED_DOCUMENT.value: 0,
            ArtifactType.ANSWER_PACK.value: 0,
        }
        for artifact in artifacts:
            artifact_type_counts[artifact.artifact_type.value] = (
                artifact_type_counts.get(artifact.artifact_type.value, 0) + 1
            )
        attempt_route = f"/execution/attempts/{attempt.id}"
        replay_route = f"/execution/replay/{attempt.id}"
        if attempt.result == AttemptResult.BLOCKED.value:
            primary_action_route = replay_route
            primary_action_label = "Open replay bundle"
        else:
            primary_action_route = attempt_route
            primary_action_label = "Open attempt detail"
        latest_artifact = artifacts[-1] if artifacts else None
        latest_artifact_route = (
            f"/execution/artifacts/{latest_artifact.id}" if latest_artifact is not None else None
        )
        latest_artifact_label = (
            f"Inspect latest {latest_artifact.artifact_type.value.replace('_', ' ')}"
            if latest_artifact is not None
            else None
        )
        visual_artifact = next(
            (
                artifact
                for artifact in reversed(artifacts)
                if artifact.artifact_type in {ArtifactType.SCREENSHOT, ArtifactType.HTML_SNAPSHOT, ArtifactType.TRACE}
            ),
            None,
        )
        visual_evidence_route = (
            f"/execution/artifacts/{visual_artifact.id}/launch" if visual_artifact is not None else None
        )
        visual_evidence_label = (
            _artifact_type_action_label(visual_artifact.artifact_type.value)
            if visual_artifact is not None
            else None
        )

        items.append(
            DraftExecutionOverviewRead(
                application_id=application.id,
                attempt_id=attempt.id,
                job_id=job.id,
                candidate_profile_slug=candidate_profile_slug,
                company_name=company_name,
                job_title=job.title,
                site_vendor=job.ats_vendor,
                application_state=application.current_state,
                readiness_state=eligibility.readiness_state,
                ready=eligibility.ready,
                attempt_mode=attempt.mode.value,
                attempt_result=attempt.result,
                failure_code=attempt.failure_code,
                failure_classification=attempt_failure_classification,
                submit_confidence=attempt.submit_confidence,
                browser_profile_key=attempt.browser_profile_key,
                session_health=attempt.session_health,
                latest_event_type=(latest_event.event_type if latest_event is not None else None),
                latest_event_message=(latest_event.message if latest_event is not None else None),
                submit_interaction_mode=submit_interaction["mode"],
                submit_interaction_status=submit_interaction["status"],
                submit_interaction_clicked=submit_interaction["clicked"],
                submit_interaction_selector=submit_interaction["selector"],
                submit_interaction_confirmation_count=submit_interaction["confirmation_count"],
                attempt_route=attempt_route,
                replay_route=replay_route,
                primary_action_route=primary_action_route,
                primary_action_label=primary_action_label,
                latest_artifact_route=latest_artifact_route,
                latest_artifact_label=latest_artifact_label,
                visual_evidence_route=visual_evidence_route,
                visual_evidence_label=visual_evidence_label,
                artifact_count=len(artifacts),
                screenshot_count=artifact_type_counts.get(ArtifactType.SCREENSHOT.value, 0),
                html_snapshot_count=artifact_type_counts.get(ArtifactType.HTML_SNAPSHOT.value, 0),
                model_io_count=artifact_type_counts.get(ArtifactType.MODEL_IO.value, 0),
                generated_document_count=artifact_type_counts.get(ArtifactType.GENERATED_DOCUMENT.value, 0),
                answer_pack_count=artifact_type_counts.get(ArtifactType.ANSWER_PACK.value, 0),
                reasons=list(eligibility.reasons or []),
                started_at=attempt.started_at,
            )
        )
    if blocked_only:
        items = [item for item in items if item.attempt_result == AttemptResult.BLOCKED.value]
    if manual_review_only:
        items = [
            item
            for item in items
            if (item.failure_code or "").startswith("manual_review_required:")
        ]
    if failure_code is not None:
        normalized = failure_code.strip().lower()
        items = [
            item
            for item in items
            if (item.failure_code or "").strip().lower() == normalized
        ]
    if failure_classification is not None:
        normalized = failure_classification.strip().lower()
        items = [
            item
            for item in items
            if (item.failure_classification or "").strip().lower() == normalized
        ]
    if max_submit_confidence is not None:
        items = [
            item
            for item in items
            if item.submit_confidence is not None and item.submit_confidence <= max_submit_confidence
        ]
    if sort_by == "started_at":
        items.sort(key=lambda item: item.started_at, reverse=descending)
    elif sort_by == "artifact_count":
        items.sort(key=lambda item: item.artifact_count, reverse=descending)
    elif sort_by == "submit_confidence":
        non_null = [item for item in items if item.submit_confidence is not None]
        null_items = [item for item in items if item.submit_confidence is None]
        non_null.sort(key=lambda item: item.submit_confidence or 0.0, reverse=descending)
        items = non_null + null_items
    elif sort_by == "failure_code":
        non_null = [item for item in items if item.failure_code is not None]
        null_items = [item for item in items if item.failure_code is None]
        non_null.sort(key=lambda item: item.failure_code or "", reverse=descending)
        items = non_null + null_items
    elif sort_by == "failure_classification":
        non_null = [item for item in items if item.failure_classification is not None]
        null_items = [item for item in items if item.failure_classification is None]
        non_null.sort(key=lambda item: item.failure_classification or "", reverse=descending)
        items = non_null + null_items
    else:
        raise ValueError("invalid_execution_overview_sort")
    return items


def get_execution_dashboard(
    session: Session,
    *,
    candidate_profile_slug: str,
    manual_review_only: bool = False,
    failure_code: str | None = None,
    failure_classification: str | None = None,
    max_submit_confidence: float | None = None,
    sort_by: str = "started_at",
    descending: bool = True,
    limit: int = 10,
) -> DraftExecutionDashboardRead:
    """Return one candidate-scoped execution dashboard summary."""

    rows = list_execution_overview(
        session,
        candidate_profile_slug=candidate_profile_slug,
        blocked_only=False,
        manual_review_only=manual_review_only,
        failure_code=failure_code,
        failure_classification=failure_classification,
        max_submit_confidence=max_submit_confidence,
        sort_by=sort_by,
        descending=descending,
        limit=max(limit, 50),
    )
    blocked_rows = [row for row in rows if row.attempt_result == AttemptResult.BLOCKED.value]
    blocked_failure_counts: dict[str, int] = {}
    blocked_failure_classification_counts: dict[str, int] = {}
    for row in blocked_rows:
        key = row.failure_code or "unknown_failure"
        blocked_failure_counts[key] = blocked_failure_counts.get(key, 0) + 1
        class_key = row.failure_classification or "unknown_classification"
        blocked_failure_classification_counts[class_key] = (
            blocked_failure_classification_counts.get(class_key, 0) + 1
        )
    manual_review_blocked_attempts = sum(
        count
        for code, count in blocked_failure_counts.items()
        if code.startswith("manual_review_required:")
    )
    pending_rows = [row for row in rows if row.attempt_result is None]
    review_application_ids = {
        row.application_id
        for row in rows
        if row.application_state == ApplicationState.REVIEW.value
    }
    replay_ready_rows = [row for row in rows if row.artifact_count > 0]

    recommended_actions = [
        "Resolve blocked guarded attempts before retrying browser-driven execution.",
        "Open replay bundles for attempts with persisted artifacts before any manual browser retry.",
        "Track review_state_attempts to keep blocked applications from stalling silently.",
    ]
    if blocked_rows:
        recommended_actions.append("Prioritize attempts with submit_gate_blocked failure codes and manual-review stop reasons.")
        top_failure_code, top_failure_count = max(
            blocked_failure_counts.items(),
            key=lambda item: item[1],
        )
        recommended_actions.append(
            f"Top blocked failure code is {top_failure_code} ({top_failure_count} attempts)."
        )
        top_failure_classification, top_failure_classification_count = max(
            blocked_failure_classification_counts.items(),
            key=lambda item: item[1],
        )
        recommended_actions.append(
            "Top blocked failure classification is "
            f"{top_failure_classification} ({top_failure_classification_count} attempts)."
        )
    if failure_code:
        recommended_actions.append(f"Execution view is scoped to failure_code={failure_code}.")
    if failure_classification:
        recommended_actions.append(
            f"Execution view is scoped to failure_classification={failure_classification}."
        )
    if manual_review_only:
        recommended_actions.append("Execution view is scoped to manual-review-required failures only.")
    if max_submit_confidence is not None:
        recommended_actions.append(
            f"Execution view is scoped to submit_confidence <= {max_submit_confidence}."
        )
    if sort_by != "started_at" or not descending:
        order = "descending" if descending else "ascending"
        recommended_actions.append(f"Execution view is sorted by {sort_by} ({order}).")

    return DraftExecutionDashboardRead(
        candidate_profile_slug=candidate_profile_slug,
        total_attempts=len(rows),
        blocked_attempts=len(blocked_rows),
        manual_review_blocked_attempts=manual_review_blocked_attempts,
        pending_attempts=len(pending_rows),
        review_state_attempts=len(review_application_ids),
        replay_ready_attempts=len(replay_ready_rows),
        blocked_failure_counts=blocked_failure_counts,
        blocked_failure_classification_counts=blocked_failure_classification_counts,
        recent_attempts=rows[:limit],
        blocked_recent_attempts=blocked_rows[:limit],
        recommended_actions=recommended_actions,
    )


def get_execution_attempt_detail(
    session: Session,
    *,
    attempt_id: int,
) -> DraftExecutionAttemptDetailRead:
    """Return one execution attempt with ordered events and artifacts for drill-down."""

    row = session.execute(
        select(Application, ApplicationAttempt, ApplicationEligibility, Job, Company.name, CandidateProfile)
        .join(ApplicationAttempt, ApplicationAttempt.application_id == Application.id)
        .join(CandidateProfile, CandidateProfile.id == Application.candidate_profile_id)
        .join(
            ApplicationEligibility,
            (ApplicationEligibility.job_id == Application.job_id)
            & (ApplicationEligibility.candidate_profile_id == Application.candidate_profile_id),
        )
        .join(Job, Job.id == Application.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(ApplicationAttempt.id == attempt_id)
    ).first()
    if row is None:
        raise ValueError("application_attempt_not_found")

    application, attempt, eligibility, job, company_name, candidate = row
    events = session.scalars(
        select(ApplicationEvent)
        .where(ApplicationEvent.attempt_id == attempt.id)
        .order_by(ApplicationEvent.created_at, ApplicationEvent.id)
    ).all()
    submit_interaction = _extract_submit_interaction_diagnostics_from_events(events)
    failure_classification = _resolve_attempt_failure_classification_from_events(events)
    if (
        attempt.result == AttemptResult.BLOCKED.value
        and (failure_classification is None or not failure_classification.strip())
    ):
        failure_classification = "unknown_classification"
    artifacts = session.scalars(
        select(Artifact)
        .where(Artifact.attempt_id == attempt.id)
        .order_by(Artifact.created_at, Artifact.id)
    ).all()

    return DraftExecutionAttemptDetailRead(
        application_id=application.id,
        attempt_id=attempt.id,
        job_id=job.id,
        candidate_profile_slug=candidate.slug,
        company_name=company_name,
        job_title=job.title,
        site_vendor=job.ats_vendor,
        application_state=application.current_state,
        readiness_state=eligibility.readiness_state,
        ready=eligibility.ready,
        attempt_mode=attempt.mode.value,
        attempt_result=attempt.result,
        failure_code=attempt.failure_code,
        failure_classification=failure_classification,
        submit_confidence=attempt.submit_confidence,
        browser_profile_key=attempt.browser_profile_key,
        session_health=attempt.session_health,
        notes=attempt.notes,
        submit_interaction_mode=submit_interaction["mode"],
        submit_interaction_status=submit_interaction["status"],
        submit_interaction_clicked=submit_interaction["clicked"],
        submit_interaction_selector=submit_interaction["selector"],
        submit_interaction_confirmation_count=submit_interaction["confirmation_count"],
        reasons=list(eligibility.reasons or []),
        started_at=attempt.started_at,
        events=[
            DraftExecutionEventRead(
                event_id=event.id,
                event_type=event.event_type,
                message=event.message,
                created_at=event.created_at,
                payload=(event.payload or {}),
                artifact_routes=_event_artifact_routes(event),
            )
            for event in events
        ],
        artifacts=[
            _build_attempt_artifact_read(artifact)
            for artifact in artifacts
        ],
    )


def get_execution_artifact_detail(
    session: Session,
    *,
    artifact_id: int,
) -> DraftExecutionArtifactDetailRead:
    """Return one execution artifact with a bounded preview when text is safe to display."""

    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise ValueError("execution_artifact_not_found")

    artifact_path = Path(artifact.path)
    exists = artifact_path.exists()
    preview_kind = "unavailable"
    preview_text: str | None = None
    preview_truncated = False
    if exists:
        preview_kind, preview_text, preview_truncated = _build_artifact_preview(
            path=artifact_path,
            artifact_type=artifact.artifact_type,
        )
    raw_route = f"/execution/artifacts/{artifact.id}/raw" if exists else None
    launch_route, launch_label, launch_target = _determine_launch_action(
        raw_route=raw_route,
        open_hint=_determine_replay_openability(
            artifact_type=artifact.artifact_type.value,
            path=artifact.path,
            exists=exists,
        )[1],
        artifact_id=artifact.id,
    )

    return DraftExecutionArtifactDetailRead(
        artifact_id=artifact.id,
        attempt_id=artifact.attempt_id,
        artifact_type=artifact.artifact_type.value,
        path=artifact.path,
        size_bytes=artifact.size_bytes,
        created_at=artifact.created_at,
        exists=exists,
        raw_route=raw_route,
        launch_route=launch_route,
        launch_label=launch_label,
        launch_target=launch_target,
        preview_kind=preview_kind,
        preview_text=preview_text,
        preview_truncated=preview_truncated,
    )


def _build_attempt_artifact_read(artifact: Artifact) -> DraftExecutionArtifactRead:
    """Build an actionable artifact row for execution-attempt detail reads."""

    exists = Path(artifact.path).exists()
    raw_route = f"/execution/artifacts/{artifact.id}/raw" if exists else None
    launch_route, launch_label, launch_target = _determine_launch_action(
        raw_route=raw_route,
        open_hint=_determine_replay_openability(
            artifact_type=artifact.artifact_type.value,
            path=artifact.path,
            exists=exists,
        )[1],
        artifact_id=artifact.id,
    )
    return DraftExecutionArtifactRead(
        artifact_id=artifact.id,
        artifact_type=artifact.artifact_type.value,
        path=artifact.path,
        size_bytes=artifact.size_bytes,
        created_at=artifact.created_at,
        inspect_route=f"/execution/artifacts/{artifact.id}",
        raw_route=raw_route,
        launch_route=launch_route,
        launch_label=launch_label,
        launch_target=launch_target,
    )


def _event_artifact_routes(event: ApplicationEvent) -> list[str]:
    """Extract inspect routes for artifact ids referenced by an execution event payload."""

    payload = event.payload or {}
    routes: list[str] = []
    for key, value in payload.items():
        if not key.endswith("artifact_id"):
            continue
        artifact_id: int | None = None
        if isinstance(value, int):
            artifact_id = value
        elif isinstance(value, str) and value.isdigit():
            artifact_id = int(value)
        if artifact_id is None or artifact_id <= 0:
            continue
        route = f"/execution/artifacts/{artifact_id}"
        if route not in routes:
            routes.append(route)
    return routes


def get_execution_replay_bundle(
    session: Session,
    *,
    attempt_id: int,
) -> DraftExecutionReplayBundleRead:
    """Return a replay-oriented bundle for one execution attempt."""

    row = session.execute(
        select(Application, ApplicationAttempt, Job, Company.name, CandidateProfile)
        .join(ApplicationAttempt, ApplicationAttempt.application_id == Application.id)
        .join(Job, Job.id == Application.job_id)
        .join(CandidateProfile, CandidateProfile.id == Application.candidate_profile_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(ApplicationAttempt.id == attempt_id)
    ).first()
    if row is None:
        raise ValueError("application_attempt_not_found")

    application, attempt, job, company_name, candidate = row
    events = session.scalars(
        select(ApplicationEvent)
        .where(ApplicationEvent.attempt_id == attempt.id)
        .order_by(ApplicationEvent.created_at, ApplicationEvent.id)
    ).all()
    artifacts = session.scalars(
        select(Artifact)
        .where(Artifact.attempt_id == attempt.id)
        .order_by(Artifact.created_at, Artifact.id)
    ).all()

    startup_event = _event_by_type(events, "draft_execution_started")
    field_plan_event = _event_by_type(events, "draft_field_plan_created")
    overlay_event = _event_by_type(events, "draft_site_field_overlay_created")
    target_event = _event_by_type(events, "draft_target_opened")
    submit_gate_event = _event_by_type(events, "draft_submit_gate_evaluated")
    guarded_submit_event = _event_by_type(events, "draft_submit_executed")
    latest_event = events[-1] if events else None

    startup_payload = startup_event.payload if startup_event is not None else {}
    startup_dir = str(startup_payload.get("startup_dir")) if startup_payload.get("startup_dir") else None
    target_url = str(startup_payload.get("target_url")) if startup_payload.get("target_url") else None

    assets = [
        _build_replay_asset(
            attempt_id=attempt.id,
            label="startup_context",
            artifact=_artifact_by_filename(artifacts, "startup_context.json"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="prepared_answer_pack",
            artifact=_artifact_by_filename(artifacts, "prepared_answer_pack.json"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="startup_target_page",
            artifact=_artifact_by_filename(artifacts, "target_page.html"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="draft_field_plan",
            artifact=_artifact_from_event_payload(artifacts, field_plan_event, "artifact_id"),
            fallback_path=_payload_value(field_plan_event, "artifact_path"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="site_field_overlay",
            artifact=_artifact_from_event_payload(artifacts, overlay_event, "artifact_id"),
            fallback_path=_payload_value(overlay_event, "artifact_path"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="opened_target_capture",
            artifact=_artifact_from_event_payload(artifacts, target_event, "opened_page_artifact_id"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="opened_target_screenshot",
            artifact=_artifact_from_event_payload(artifacts, target_event, "screenshot_artifact_id"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="opened_target_trace",
            artifact=_artifact_from_event_payload(artifacts, target_event, "trace_artifact_id"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="field_resolution",
            artifact=_artifact_from_event_payload(artifacts, target_event, "resolution_artifact_id"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="submit_gate",
            artifact=_artifact_from_event_payload(artifacts, submit_gate_event, "artifact_id"),
            fallback_path=_payload_value(submit_gate_event, "artifact_path"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="guarded_submit",
            artifact=_artifact_from_event_payload(artifacts, guarded_submit_event, "artifact_id"),
            fallback_path=_payload_value(guarded_submit_event, "artifact_path"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="guarded_submit_screenshot",
            artifact=_artifact_from_event_payload(artifacts, guarded_submit_event, "screenshot_artifact_id"),
        ),
        _build_replay_asset(
            attempt_id=attempt.id,
            label="guarded_submit_trace",
            artifact=_artifact_from_event_payload(artifacts, guarded_submit_event, "trace_artifact_id"),
        ),
    ]

    recommended_actions = [
        "Inspect startup_context and prepared_answer_pack before any browser replay.",
        "Compare draft_field_plan, site_field_overlay, and field_resolution to understand deterministic selector decisions.",
        "Inspect submit_gate when attempt_result is blocked to see the exact stop reasons.",
    ]
    if startup_dir:
        recommended_actions.append(f"Use startup_dir for local replay inputs: {startup_dir}")
    if attempt.result == AttemptResult.BLOCKED.value:
        recommended_actions.append("Resolve manual-review or unresolved fields before attempting guarded submit again.")
    if attempt.result == AttemptResult.SUCCESS.value:
        recommended_actions.append("Inspect guarded_submit artifacts to verify submission evidence and post-submit diagnostics.")

    return DraftExecutionReplayBundleRead(
        application_id=application.id,
        attempt_id=attempt.id,
        job_id=job.id,
        candidate_profile_slug=candidate.slug,
        job_title=job.title,
        company_name=company_name,
        site_vendor=job.ats_vendor,
        application_state=application.current_state,
        attempt_result=attempt.result,
        failure_code=attempt.failure_code,
        latest_event_type=(latest_event.event_type if latest_event is not None else None),
        startup_dir=startup_dir,
        target_url=target_url,
        assets=assets,
        recommended_actions=recommended_actions,
    )


def start_draft_execution_attempt(
    session: Session,
    *,
    attempt_id: int,
) -> DraftExecutionStartupRead:
    """Stage a draft attempt for future browser execution and capture startup artifacts."""

    row = session.execute(
        select(ApplicationAttempt, Application, CandidateProfile, ApplicationEligibility)
        .join(Application, Application.id == ApplicationAttempt.application_id)
        .join(CandidateProfile, CandidateProfile.id == Application.candidate_profile_id)
        .join(
            ApplicationEligibility,
            (ApplicationEligibility.job_id == Application.job_id)
            & (ApplicationEligibility.candidate_profile_id == Application.candidate_profile_id),
        )
        .where(ApplicationAttempt.id == attempt_id)
    ).first()
    if row is None:
        raise ValueError("application_attempt_not_found")

    attempt, application, candidate, eligibility = row
    if attempt.mode != ApplicationMode.DRAFT:
        raise ValueError("application_attempt_not_draft")
    if application.current_state == ApplicationState.APPLIED.value:
        raise ValueError("application_already_applied")
    if not eligibility.ready or eligibility.readiness_state != "ready_to_apply":
        raise ValueError("application_not_ready_to_apply")

    profile = None
    if attempt.browser_profile_key:
        profile = session.scalar(
            select(BrowserProfile).where(BrowserProfile.profile_key == attempt.browser_profile_key)
        )
        if profile is None:
            raise ValueError("browser_profile_not_found")
        policy = get_browser_profile_policy(session, attempt.browser_profile_key)
        if not policy.allow_application:
            raise ValueError("browser_profile_not_ready_for_application")
        attempt.session_health = profile.session_health

    existing_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_execution_started",
        )
    )
    if existing_event is not None:
        return _build_startup_read(
            session,
            application=application,
            attempt=attempt,
            candidate=candidate,
            eligibility=eligibility,
            event=existing_event,
        )

    prepared = get_prepared_job_read(
        session,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
    )
    if prepared is None:
        raise ValueError("prepared_outputs_not_found")

    now = utcnow()
    startup_dir = _execution_startup_dir(candidate.slug, application.job_id, attempt.id)
    startup_dir.mkdir(parents=True, exist_ok=True)

    target_url = _target_url(application.job_id, session)
    startup_context_path = startup_dir / "startup_context.json"
    startup_context_payload = {
        "application_id": application.id,
        "attempt_id": attempt.id,
        "job_id": application.job_id,
        "candidate_profile_slug": candidate.slug,
        "browser_profile_key": attempt.browser_profile_key,
        "session_health": attempt.session_health,
        "readiness_state": eligibility.readiness_state,
        "ready": eligibility.ready,
        "reasons": list(eligibility.reasons or []),
        "target_url": target_url,
    }
    startup_context_path.write_text(
        json.dumps(startup_context_payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    answer_pack_path = startup_dir / "prepared_answer_pack.json"
    answer_pack_payload = [answer.model_dump(mode="json") for answer in prepared.answers]
    answer_pack_path.write_text(
        json.dumps(answer_pack_payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    target_page_path = startup_dir / "target_page.html"
    if attempt.browser_profile_key:
        target_page_html, target_capture = _capture_target_page_html(
            target_url=target_url,
            job_title=prepared.job_title,
            browser_profile_key=attempt.browser_profile_key,
            candidate_profile_slug=candidate.slug,
        )
    else:
        target_page_html = _render_target_page_stub(
            job_title=prepared.job_title,
            target_url=target_url,
            candidate_profile_slug=candidate.slug,
        )
        target_capture = {"capture_method": "stub_startup"}
    target_page_path.write_text(target_page_html, encoding="utf-8")
    startup_context_payload["target_capture"] = target_capture
    startup_context_path.write_text(
        json.dumps(startup_context_payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    artifacts = [
        Artifact(
            attempt_id=attempt.id,
            artifact_type=ArtifactType.MODEL_IO,
            path=str(startup_context_path),
            size_bytes=startup_context_path.stat().st_size,
            retention_days=get_settings().artifact_retention_days,
            created_at=now,
        ),
        Artifact(
            attempt_id=attempt.id,
            artifact_type=ArtifactType.ANSWER_PACK,
            path=str(answer_pack_path),
            size_bytes=answer_pack_path.stat().st_size,
            retention_days=get_settings().artifact_retention_days,
            created_at=now,
        ),
        Artifact(
            attempt_id=attempt.id,
            artifact_type=ArtifactType.HTML_SNAPSHOT,
            path=str(target_page_path),
            size_bytes=target_page_path.stat().st_size,
            retention_days=get_settings().artifact_retention_days,
            created_at=now,
        ),
    ]
    session.add_all(artifacts)
    session.flush()

    for document in prepared.documents:
        if document.content_path:
            document_path = Path(document.content_path)
            size_bytes = document_path.stat().st_size if document_path.exists() else None
            artifact = Artifact(
                attempt_id=attempt.id,
                artifact_type=ArtifactType.GENERATED_DOCUMENT,
                path=document.content_path,
                size_bytes=size_bytes,
                retention_days=get_settings().artifact_retention_days,
                created_at=now,
            )
            session.add(artifact)
            session.flush()
            artifacts.append(artifact)

    event = ApplicationEvent(
        application_id=application.id,
        attempt_id=attempt.id,
        event_type="draft_execution_started",
        message="Draft execution startup bundle created.",
        payload={
            "startup_dir": str(startup_dir),
            "target_url": target_url,
            "target_capture": target_capture,
            "prepared_document_count": len(prepared.documents),
            "prepared_answer_count": len(prepared.answers),
            "artifact_ids": [artifact.id for artifact in artifacts],
        },
        created_at=now,
    )
    session.add(event)
    attempt.notes = "Draft execution startup artifacts created."
    application.updated_at = now
    session.commit()
    session.refresh(event)
    session.refresh(attempt)

    return DraftExecutionStartupRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        browser_profile_key=attempt.browser_profile_key,
        readiness_state=eligibility.readiness_state,
        target_url=target_url,
        startup_dir=str(startup_dir),
        prepared_document_count=len(prepared.documents),
        prepared_answer_count=len(prepared.answers),
        startup_artifact_ids=[artifact.id for artifact in artifacts],
        started_at=attempt.started_at,
    )


def build_draft_field_plan(
    session: Session,
    *,
    attempt_id: int,
) -> DraftFieldPlanRead:
    """Create deterministic field mappings from staged startup artifacts and prepared outputs."""

    row = session.execute(
        select(ApplicationAttempt, Application, CandidateProfile, ApplicationEligibility)
        .join(Application, Application.id == ApplicationAttempt.application_id)
        .join(CandidateProfile, CandidateProfile.id == Application.candidate_profile_id)
        .join(
            ApplicationEligibility,
            (ApplicationEligibility.job_id == Application.job_id)
            & (ApplicationEligibility.candidate_profile_id == Application.candidate_profile_id),
        )
        .where(ApplicationAttempt.id == attempt_id)
    ).first()
    if row is None:
        raise ValueError("application_attempt_not_found")

    attempt, application, candidate, eligibility = row
    startup_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_execution_started",
        )
    )
    if startup_event is None:
        raise ValueError("draft_execution_not_started")

    existing_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_field_plan_created",
        )
    )
    if existing_event is not None:
        return _build_field_plan_read(
            session,
            application=application,
            attempt=attempt,
            candidate=candidate,
            event=existing_event,
        )

    prepared = get_prepared_job_read(
        session,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
    )
    if prepared is None:
        raise ValueError("prepared_outputs_not_found")

    startup_payload = startup_event.payload or {}
    startup_dir = Path(str(startup_payload.get("startup_dir") or _execution_startup_dir(candidate.slug, application.job_id, attempt.id)))
    startup_dir.mkdir(parents=True, exist_ok=True)

    session.query(FieldMapping).filter(FieldMapping.attempt_id == attempt.id).delete()
    session.flush()

    entries = _build_field_plan_entries(candidate, prepared)
    mappings: list[FieldMapping] = []
    for entry in entries:
        mapping = FieldMapping(
            attempt_id=attempt.id,
            field_key=entry["field_key"],
            raw_label=entry.get("raw_label"),
            raw_dom_signature=entry.get("raw_dom_signature"),
            inferred_type=entry.get("inferred_type"),
            confidence=entry.get("confidence"),
            answer_id=entry.get("answer_id"),
            truth_tier=entry.get("truth_tier"),
            chosen_answer=entry.get("chosen_answer"),
            answer_source=entry.get("answer_source"),
        )
        session.add(mapping)
        session.flush()
        mappings.append(mapping)

    artifact_path = startup_dir / "draft_field_plan.json"
    artifact_payload = {
        "application_id": application.id,
        "attempt_id": attempt.id,
        "job_id": application.job_id,
        "candidate_profile_slug": candidate.slug,
        "readiness_state": eligibility.readiness_state,
        "entries": [
            {
                "field_mapping_id": mapping.id,
                "field_key": mapping.field_key,
                "inferred_type": mapping.inferred_type,
                "confidence": mapping.confidence,
                "answer_id": mapping.answer_id,
                "truth_tier": (mapping.truth_tier.value if mapping.truth_tier else None),
                "chosen_answer": mapping.chosen_answer,
                "answer_source": mapping.answer_source,
            }
            for mapping in mappings
        ],
    }
    artifact_path.write_text(
        json.dumps(artifact_payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    now = utcnow()
    artifact = Artifact(
        attempt_id=attempt.id,
        artifact_type=ArtifactType.MODEL_IO,
        path=str(artifact_path),
        size_bytes=artifact_path.stat().st_size,
        retention_days=get_settings().artifact_retention_days,
        created_at=now,
    )
    session.add(artifact)
    session.flush()

    event = ApplicationEvent(
        application_id=application.id,
        attempt_id=attempt.id,
        event_type="draft_field_plan_created",
        message="Deterministic draft field plan created from staged startup bundle.",
        payload={
            "artifact_id": artifact.id,
            "artifact_path": str(artifact_path),
            "field_count": len(mappings),
        },
        created_at=now,
    )
    session.add(event)
    application.updated_at = now
    session.commit()
    session.refresh(event)

    return DraftFieldPlanRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        field_count=len(mappings),
        artifact_id=artifact.id,
        artifact_path=str(artifact_path),
        entries=[
            DraftFieldPlanEntryRead(
                field_mapping_id=mapping.id,
                field_key=mapping.field_key,
                inferred_type=mapping.inferred_type,
                confidence=mapping.confidence,
                answer_id=mapping.answer_id,
                truth_tier=(mapping.truth_tier.value if mapping.truth_tier else None),
                chosen_answer=mapping.chosen_answer,
                answer_source=mapping.answer_source,
            )
            for mapping in mappings
        ],
    )


def build_site_field_overlay(
    session: Session,
    *,
    attempt_id: int,
) -> DraftSiteFieldPlanRead:
    """Build a site-aware selector overlay on top of the generic draft field plan."""

    row = session.execute(
        select(ApplicationAttempt, Application, CandidateProfile, Job)
        .join(Application, Application.id == ApplicationAttempt.application_id)
        .join(CandidateProfile, CandidateProfile.id == Application.candidate_profile_id)
        .join(Job, Job.id == Application.job_id)
        .where(ApplicationAttempt.id == attempt_id)
    ).first()
    if row is None:
        raise ValueError("application_attempt_not_found")

    attempt, application, candidate, job = row
    if attempt.mode != ApplicationMode.DRAFT:
        raise ValueError("application_attempt_not_draft")

    field_plan_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_field_plan_created",
        )
    )
    if field_plan_event is None:
        raise ValueError("draft_field_plan_not_created")

    site_vendor = str(job.ats_vendor or "unknown").lower()
    handler = get_vendor_execution_handler(site_vendor)
    if handler is None:
        raise ValueError("site_overlay_not_supported")

    existing_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_site_field_overlay_created",
        )
    )
    if existing_event is not None:
        return _build_site_field_plan_read(
            session,
            application=application,
            attempt=attempt,
            candidate=candidate,
            job=job,
            event=existing_event,
        )

    mappings = session.scalars(
        select(FieldMapping)
        .where(FieldMapping.attempt_id == attempt.id)
        .order_by(FieldMapping.id)
    ).all()
    if not mappings:
        raise ValueError("draft_field_plan_empty")

    startup_dir = _execution_startup_dir(candidate.slug, application.job_id, attempt.id)
    startup_dir.mkdir(parents=True, exist_ok=True)
    entries = handler.overlay_entries(mappings)

    artifact_path = startup_dir / f"{site_vendor}_site_field_overlay.json"
    artifact_payload = {
        "application_id": application.id,
        "attempt_id": attempt.id,
        "job_id": application.job_id,
        "candidate_profile_slug": candidate.slug,
        "site_vendor": site_vendor,
        "entries": [entry.model_dump() for entry in entries],
    }
    artifact_path.write_text(
        json.dumps(artifact_payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    now = utcnow()
    artifact = Artifact(
        attempt_id=attempt.id,
        artifact_type=ArtifactType.MODEL_IO,
        path=str(artifact_path),
        size_bytes=artifact_path.stat().st_size,
        retention_days=get_settings().artifact_retention_days,
        created_at=now,
    )
    session.add(artifact)
    session.flush()

    event = ApplicationEvent(
        application_id=application.id,
        attempt_id=attempt.id,
        event_type="draft_site_field_overlay_created",
        message="Site-aware field overlay created for deterministic draft execution.",
        payload={
            "site_vendor": site_vendor,
            "artifact_id": artifact.id,
            "artifact_path": str(artifact_path),
            "entry_count": len(entries),
        },
        created_at=now,
    )
    session.add(event)
    application.updated_at = now
    session.commit()
    session.refresh(event)

    return DraftSiteFieldPlanRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=site_vendor,
        entry_count=len(entries),
        artifact_id=artifact.id,
        artifact_path=str(artifact_path),
        entries=entries,
    )


def open_site_target_page(
    session: Session,
    *,
    attempt_id: int,
) -> DraftTargetOpenRead:
    """Create a non-submitting page-open and field-resolution bundle for a site-aware draft attempt."""

    row = session.execute(
        select(ApplicationAttempt, Application, CandidateProfile, Job)
        .join(Application, Application.id == ApplicationAttempt.application_id)
        .join(CandidateProfile, CandidateProfile.id == Application.candidate_profile_id)
        .join(Job, Job.id == Application.job_id)
        .where(ApplicationAttempt.id == attempt_id)
    ).first()
    if row is None:
        raise ValueError("application_attempt_not_found")

    attempt, application, candidate, job = row
    if attempt.mode != ApplicationMode.DRAFT:
        raise ValueError("application_attempt_not_draft")
    if not attempt.browser_profile_key:
        raise ValueError("browser_profile_required_for_page_open")

    startup_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_execution_started",
        )
    )
    if startup_event is None:
        raise ValueError("draft_execution_not_started")

    overlay_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_site_field_overlay_created",
        )
    )
    if overlay_event is None:
        raise ValueError("draft_site_overlay_not_created")

    profile = session.scalar(
        select(BrowserProfile).where(BrowserProfile.profile_key == attempt.browser_profile_key)
    )
    if profile is None:
        raise ValueError("browser_profile_not_found")
    if profile.profile_type != BrowserProfileType.APPLICATION:
        raise ValueError("browser_profile_not_application_type")
    policy = get_browser_profile_policy(session, attempt.browser_profile_key)
    if not policy.allow_application:
        raise ValueError("browser_profile_not_ready_for_application")
    attempt.session_health = profile.session_health

    existing_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_target_opened",
        )
    )
    if existing_event is not None:
        return _build_target_open_read(
            session,
            application=application,
            attempt=attempt,
            candidate=candidate,
            job=job,
            event=existing_event,
        )

    site_vendor = str(job.ats_vendor or "").lower()
    handler = get_vendor_execution_handler(site_vendor)
    if handler is None or not handler.supports_target_open():
        raise ValueError("page_open_not_supported_for_site")

    startup_payload = startup_event.payload or {}
    startup_dir = Path(str(startup_payload.get("startup_dir") or _execution_startup_dir(candidate.slug, application.job_id, attempt.id)))
    startup_dir.mkdir(parents=True, exist_ok=True)
    target_url = str(startup_payload.get("target_url") or _target_url(application.job_id, session))

    mappings = session.scalars(
        select(FieldMapping)
        .where(FieldMapping.attempt_id == attempt.id)
        .order_by(FieldMapping.id)
    ).all()
    if not mappings:
        raise ValueError("draft_field_plan_empty")

    resolved_entries = handler.target_open_resolutions(mappings)

    opened_page_html, capture_metadata = _capture_target_page_html(
        target_url=target_url,
        job_title=job.title,
        browser_profile_key=attempt.browser_profile_key,
        candidate_profile_slug=candidate.slug,
    )
    opened_page_path = startup_dir / f"{site_vendor}_opened_target.html"
    opened_page_path.write_text(opened_page_html, encoding="utf-8")
    resolution_path = startup_dir / f"{site_vendor}_field_resolution.json"
    resolution_path.write_text(
        json.dumps(
            build_target_resolution_artifact_payload(
                application_id=application.id,
                attempt_id=attempt.id,
                job_id=application.job_id,
                candidate_profile_slug=candidate.slug,
                site_vendor=site_vendor,
                resolved_entries=[entry.model_dump() for entry in resolved_entries],
            ),
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    now = utcnow()
    opened_artifact = Artifact(
        attempt_id=attempt.id,
        artifact_type=ArtifactType.HTML_SNAPSHOT,
        path=str(opened_page_path),
        size_bytes=opened_page_path.stat().st_size,
        retention_days=get_settings().artifact_retention_days,
        created_at=now,
    )
    screenshot_artifact: Artifact | None = None
    trace_artifact: Artifact | None = None
    if str(capture_metadata.get("capture_method") or "").strip().lower() == "playwright":
        try:
            screenshot_bytes = _capture_target_page_screenshot_via_playwright(
                target_url=target_url,
                browser_profile_key=attempt.browser_profile_key,
            )
            screenshot_path = startup_dir / f"{site_vendor}_opened_target.png"
            screenshot_path.write_bytes(screenshot_bytes)
            screenshot_artifact = Artifact(
                attempt_id=attempt.id,
                artifact_type=ArtifactType.SCREENSHOT,
                path=str(screenshot_path),
                size_bytes=screenshot_path.stat().st_size,
                retention_days=get_settings().artifact_retention_days,
                created_at=now,
            )
        except Exception as exc:
            capture_metadata["screenshot_error"] = exc.__class__.__name__
        try:
            trace_bytes = _capture_target_page_trace_via_playwright(
                target_url=target_url,
                browser_profile_key=attempt.browser_profile_key,
            )
            trace_path = startup_dir / f"{site_vendor}_opened_target_trace.zip"
            trace_path.write_bytes(trace_bytes)
            trace_artifact = Artifact(
                attempt_id=attempt.id,
                artifact_type=ArtifactType.TRACE,
                path=str(trace_path),
                size_bytes=trace_path.stat().st_size,
                retention_days=get_settings().artifact_retention_days,
                created_at=now,
            )
        except Exception as exc:
            capture_metadata["trace_error"] = exc.__class__.__name__
    resolution_artifact = Artifact(
        attempt_id=attempt.id,
        artifact_type=ArtifactType.MODEL_IO,
        path=str(resolution_path),
        size_bytes=resolution_path.stat().st_size,
        retention_days=get_settings().artifact_retention_days,
        created_at=now,
    )
    artifacts_to_add = [opened_artifact, resolution_artifact]
    if screenshot_artifact is not None:
        artifacts_to_add.append(screenshot_artifact)
    if trace_artifact is not None:
        artifacts_to_add.append(trace_artifact)
    session.add_all(artifacts_to_add)
    session.flush()

    resolved_count = sum(1 for entry in resolved_entries if entry.resolution_status == "resolved")
    unresolved_count = len(resolved_entries) - resolved_count
    event = ApplicationEvent(
        application_id=application.id,
        attempt_id=attempt.id,
        event_type="draft_target_opened",
        message="Non-submitting target open and field resolution completed.",
        payload=build_target_open_event_payload(
            site_vendor=site_vendor,
            target_url=target_url,
            capture_metadata=capture_metadata,
            opened_page_artifact_id=opened_artifact.id,
            resolution_artifact_id=resolution_artifact.id,
            screenshot_artifact_id=(screenshot_artifact.id if screenshot_artifact is not None else None),
            trace_artifact_id=(trace_artifact.id if trace_artifact is not None else None),
            resolved_count=resolved_count,
            unresolved_count=unresolved_count,
        ),
        created_at=now,
    )
    session.add(event)
    attempt.notes = build_target_open_attempt_note(
        capture_method=str(capture_metadata.get("capture_method") or "unknown"),
        resolved_count=resolved_count,
        unresolved_count=unresolved_count,
    )
    application.updated_at = now
    session.commit()
    session.refresh(event)

    return DraftTargetOpenRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=site_vendor,
        browser_profile_key=attempt.browser_profile_key,
        target_url=target_url,
        capture_method=str(capture_metadata.get("capture_method") or "unknown"),
        capture_error=capture_metadata.get("error"),
        opened_page_artifact_id=opened_artifact.id,
        resolution_artifact_id=resolution_artifact.id,
        screenshot_artifact_id=(screenshot_artifact.id if screenshot_artifact is not None else None),
        trace_artifact_id=(trace_artifact.id if trace_artifact is not None else None),
        resolved_count=resolved_count,
        unresolved_count=unresolved_count,
        entries=resolved_entries,
    )


def evaluate_submit_gate(
    session: Session,
    *,
    attempt_id: int,
) -> DraftSubmitGateRead:
    """Evaluate guarded submit confidence from resolved field outcomes."""

    row = session.execute(
        select(ApplicationAttempt, Application, CandidateProfile, Job)
        .join(Application, Application.id == ApplicationAttempt.application_id)
        .join(CandidateProfile, CandidateProfile.id == Application.candidate_profile_id)
        .join(Job, Job.id == Application.job_id)
        .where(ApplicationAttempt.id == attempt_id)
    ).first()
    if row is None:
        raise ValueError("application_attempt_not_found")

    attempt, application, candidate, job = row
    if attempt.mode != ApplicationMode.DRAFT:
        raise ValueError("application_attempt_not_draft")

    target_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_target_opened",
        )
    )
    if target_event is None:
        raise ValueError("draft_target_not_opened")

    existing_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_submit_gate_evaluated",
        )
    )
    if existing_event is not None:
        return _build_submit_gate_read(
            session,
            application=application,
            attempt=attempt,
            candidate=candidate,
            job=job,
            event=existing_event,
        )

    site_vendor = str(job.ats_vendor or "").lower()
    handler = get_vendor_execution_handler(site_vendor)
    if handler is None or not handler.supports_submit_gate():
        raise ValueError("submit_gate_not_supported_for_site")

    mappings = session.scalars(
        select(FieldMapping)
        .where(FieldMapping.attempt_id == attempt.id)
        .order_by(FieldMapping.id)
    ).all()
    if not mappings:
        raise ValueError("draft_field_plan_empty")

    required_fields = handler.required_fields()
    signals = handler.submit_gate_signals(mappings)
    resolved_required_fields = signals.resolved_required_fields
    manual_review_fields = signals.manual_review_fields
    unresolved_fields = signals.unresolved_fields
    stop_reasons = signals.stop_reasons

    confidence_score = _compute_submit_confidence(
        required_fields=required_fields,
        resolved_required_fields=resolved_required_fields,
        manual_review_fields=manual_review_fields,
        unresolved_fields=unresolved_fields,
    )
    allow_submit = len(stop_reasons) == 0 and confidence_score >= get_settings().auto_submit_threshold
    attempt.submit_confidence = confidence_score
    if allow_submit:
        attempt.result = None
        attempt.failure_code = None
    else:
        attempt.result = AttemptResult.BLOCKED.value
        attempt.failure_code = "submit_gate_blocked"
        application.current_state = ApplicationState.REVIEW.value
    attempt.notes = build_submit_gate_attempt_note(
        allow_submit=allow_submit,
        stop_reasons=stop_reasons,
    )

    startup_dir = _execution_startup_dir(candidate.slug, application.job_id, attempt.id)
    startup_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = startup_dir / f"{site_vendor}_submit_gate.json"
    artifact_payload = build_submit_gate_artifact_payload(
        application_id=application.id,
        attempt_id=attempt.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=site_vendor,
        confidence_score=confidence_score,
        allow_submit=allow_submit,
        stop_reasons=stop_reasons,
        required_fields=required_fields,
        resolved_required_fields=resolved_required_fields,
        manual_review_fields=manual_review_fields,
    )
    artifact_path.write_text(
        json.dumps(artifact_payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    now = utcnow()
    artifact = Artifact(
        attempt_id=attempt.id,
        artifact_type=ArtifactType.MODEL_IO,
        path=str(artifact_path),
        size_bytes=artifact_path.stat().st_size,
        retention_days=get_settings().artifact_retention_days,
        created_at=now,
    )
    session.add(artifact)
    session.flush()

    event = ApplicationEvent(
        application_id=application.id,
        attempt_id=attempt.id,
        event_type="draft_submit_gate_evaluated",
        message="Guarded submit gate evaluated from resolved field outcomes.",
        payload=build_submit_gate_event_payload(
            site_vendor=site_vendor,
            artifact_id=artifact.id,
            artifact_path=str(artifact_path),
            confidence_score=confidence_score,
            allow_submit=allow_submit,
            stop_reasons=stop_reasons,
            required_fields=required_fields,
            resolved_required_fields=resolved_required_fields,
            manual_review_fields=manual_review_fields,
        ),
        created_at=now,
    )
    session.add(event)
    application.updated_at = now
    session.commit()
    session.refresh(event)
    session.refresh(attempt)
    session.refresh(application)

    return DraftSubmitGateRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=site_vendor,
        application_state=application.current_state,
        attempt_result=attempt.result,
        failure_code=attempt.failure_code,
        confidence_score=confidence_score,
        allow_submit=allow_submit,
        stop_reasons=stop_reasons,
        required_fields=required_fields,
        resolved_required_fields=resolved_required_fields,
        manual_review_fields=manual_review_fields,
        artifact_id=artifact.id,
        artifact_path=str(artifact_path),
    )


def execute_guarded_submit(
    session: Session,
    *,
    attempt_id: int,
) -> DraftGuardedSubmitRead:
    """Execute a guarded submit step after a passing submit-gate evaluation."""

    row = session.execute(
        select(ApplicationAttempt, Application, CandidateProfile, Job)
        .join(Application, Application.id == ApplicationAttempt.application_id)
        .join(CandidateProfile, CandidateProfile.id == Application.candidate_profile_id)
        .join(Job, Job.id == Application.job_id)
        .where(ApplicationAttempt.id == attempt_id)
    ).first()
    if row is None:
        raise ValueError("application_attempt_not_found")

    attempt, application, candidate, job = row
    if attempt.mode != ApplicationMode.DRAFT:
        raise ValueError("application_attempt_not_draft")

    existing_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_submit_executed",
        )
    )
    if existing_event is not None:
        return _build_guarded_submit_read(
            application=application,
            attempt=attempt,
            candidate=candidate,
            job=job,
            event=existing_event,
        )

    gate_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_submit_gate_evaluated",
        )
    )
    if gate_event is None:
        raise ValueError("draft_submit_gate_not_evaluated")

    gate_payload = gate_event.payload or {}
    allow_submit = bool(gate_payload.get("allow_submit"))
    confidence_score = float(gate_payload.get("confidence_score") or 0.0)
    if not allow_submit:
        attempt.result = AttemptResult.BLOCKED.value
        attempt.failure_code = "submit_gate_blocked"
        if application.current_state != ApplicationState.APPLIED.value:
            application.current_state = ApplicationState.REVIEW.value
        application.updated_at = utcnow()
        session.commit()
        raise ValueError("submit_gate_blocked")

    target_event = session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.attempt_id == attempt.id,
            ApplicationEvent.event_type == "draft_target_opened",
        )
    )
    if target_event is None:
        raise ValueError("draft_target_not_opened")

    target_payload = target_event.payload or {}
    target_url = str(target_payload.get("target_url") or _target_url(application.job_id, session))
    site_vendor = str(job.ats_vendor or "").lower()
    handler = get_vendor_execution_handler(site_vendor)
    if handler is None or not handler.supports_guarded_submit():
        raise ValueError("guarded_submit_not_supported_for_site")
    startup_dir = _execution_startup_dir(candidate.slug, application.job_id, attempt.id)
    startup_dir.mkdir(parents=True, exist_ok=True)

    now = utcnow()
    submission_mode = handler.submission_mode()
    submit_plan = handler.guarded_submit_plan()
    submit_probe = _evaluate_guarded_submit_probe(
        session=session,
        target_event=target_event,
        submit_plan=submit_plan,
    )
    if not list(submit_probe.get("matched_submit_selectors") or []):
        failure_classification = _classify_guarded_submit_probe_failure(
            submit_probe=submit_probe,
            target_event=target_event,
        )
        blocked_submit_probe = submit_probe | {
            "blocked_reason": "submit_selector_not_found",
            "failure_classification": failure_classification,
        }
        artifact_path = startup_dir / f"{site_vendor}_guarded_submit_probe_failed.json"
        artifact_payload = build_guarded_submit_artifact_payload(
            application_id=application.id,
            attempt_id=attempt.id,
            job_id=application.job_id,
            candidate_profile_slug=candidate.slug,
            site_vendor=site_vendor,
            confidence_score=confidence_score,
            target_url=target_url,
            submission_mode=submission_mode,
            submit_plan=submit_plan,
            submit_probe=blocked_submit_probe,
        )
        artifact_path.write_text(
            json.dumps(artifact_payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        artifact = Artifact(
            attempt_id=attempt.id,
            artifact_type=ArtifactType.MODEL_IO,
            path=str(artifact_path),
            size_bytes=artifact_path.stat().st_size,
            retention_days=get_settings().artifact_retention_days,
            created_at=now,
        )
        session.add(artifact)
        session.flush()
        blocked_event = ApplicationEvent(
            application_id=application.id,
            attempt_id=attempt.id,
            event_type="draft_submit_execution_blocked",
            message=(
                "Guarded submit blocked because submit selector probe failed "
                f"({failure_classification})."
            ),
            payload=build_guarded_submit_event_payload(
                site_vendor=site_vendor,
                confidence_score=confidence_score,
                allow_submit=False,
                submission_mode=submission_mode,
                target_url=target_url,
                artifact_id=artifact.id,
                artifact_path=str(artifact_path),
                submit_plan=submit_plan,
                submit_probe=blocked_submit_probe,
            ),
            created_at=now,
        )
        session.add(blocked_event)
        attempt.submit_confidence = confidence_score
        attempt.result = AttemptResult.BLOCKED.value
        attempt.failure_code = "guarded_submit_probe_failed"
        attempt.notes = (
            "Guarded submit probe failed: no submit selector matched captured target HTML "
            f"(classification={failure_classification})."
        )
        application.current_state = ApplicationState.REVIEW.value
        application.updated_at = now
        session.commit()
        raise ValueError("guarded_submit_probe_failed")

    submit_interaction = _execute_guarded_submit_interaction(
        target_url=target_url,
        browser_profile_key=attempt.browser_profile_key,
        submit_selectors=list(submit_probe.get("matched_submit_selectors") or []),
        confirmation_markers=list(submit_plan.get("confirmation_markers") or []),
    )
    interaction_status = str(
        submit_interaction.get("status") or submit_interaction.get("error") or ""
    )
    interaction_clicked = bool(submit_interaction.get("clicked"))
    if not interaction_clicked:
        blocked_submit_interaction = submit_interaction | {
            "blocked_reason": "submit_interaction_failed",
        }
        artifact_path = startup_dir / f"{site_vendor}_guarded_submit_interaction_failed.json"
        artifact_payload = build_guarded_submit_artifact_payload(
            application_id=application.id,
            attempt_id=attempt.id,
            job_id=application.job_id,
            candidate_profile_slug=candidate.slug,
            site_vendor=site_vendor,
            confidence_score=confidence_score,
            target_url=target_url,
            submission_mode=submission_mode,
            submit_plan=submit_plan,
            submit_probe=submit_probe,
            submit_interaction=blocked_submit_interaction,
        )
        artifact_path.write_text(
            json.dumps(artifact_payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        artifact = Artifact(
            attempt_id=attempt.id,
            artifact_type=ArtifactType.MODEL_IO,
            path=str(artifact_path),
            size_bytes=artifact_path.stat().st_size,
            retention_days=get_settings().artifact_retention_days,
            created_at=now,
        )
        session.add(artifact)
        session.flush()
        blocked_event = ApplicationEvent(
            application_id=application.id,
            attempt_id=attempt.id,
            event_type="draft_submit_execution_blocked",
            message=(
                "Guarded submit blocked because submit interaction failed "
                f"(status={interaction_status or 'unknown'})."
            ),
            payload=build_guarded_submit_event_payload(
                site_vendor=site_vendor,
                confidence_score=confidence_score,
                allow_submit=False,
                submission_mode=submission_mode,
                target_url=target_url,
                artifact_id=artifact.id,
                artifact_path=str(artifact_path),
                submit_plan=submit_plan,
                submit_probe=submit_probe,
                submit_interaction=blocked_submit_interaction,
            ),
            created_at=now,
        )
        session.add(blocked_event)
        attempt.submit_confidence = confidence_score
        attempt.result = AttemptResult.BLOCKED.value
        attempt.failure_code = "guarded_submit_interaction_failed"
        attempt.notes = (
            "Guarded submit interaction failed: no deterministic submit click executed "
            f"(interaction_status={interaction_status or 'unknown'})."
        )
        application.current_state = ApplicationState.REVIEW.value
        application.updated_at = now
        session.commit()
        raise ValueError("guarded_submit_interaction_failed")

    artifact_path = startup_dir / f"{site_vendor}_guarded_submit.json"
    artifact_payload = build_guarded_submit_artifact_payload(
        application_id=application.id,
        attempt_id=attempt.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=site_vendor,
        confidence_score=confidence_score,
        target_url=target_url,
        submission_mode=submission_mode,
        submit_plan=submit_plan,
        submit_probe=submit_probe,
        submit_interaction=submit_interaction,
    )
    artifact_path.write_text(
        json.dumps(artifact_payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    artifact = Artifact(
        attempt_id=attempt.id,
        artifact_type=ArtifactType.MODEL_IO,
        path=str(artifact_path),
        size_bytes=artifact_path.stat().st_size,
        retention_days=get_settings().artifact_retention_days,
        created_at=now,
    )
    session.add(artifact)
    session.flush()

    screenshot_artifact: Artifact | None = None
    trace_artifact: Artifact | None = None
    event_capture: dict[str, str] = {}
    if attempt.browser_profile_key:
        try:
            screenshot_bytes = _capture_target_page_screenshot_via_playwright(
                target_url=target_url,
                browser_profile_key=attempt.browser_profile_key,
            )
        except Exception as exc:  # pragma: no cover - exercised in tests via monkeypatch
            event_capture["screenshot_error"] = exc.__class__.__name__
        else:
            screenshot_path = startup_dir / f"{site_vendor}_guarded_submit_screenshot.png"
            screenshot_path.write_bytes(screenshot_bytes)
            screenshot_artifact = Artifact(
                attempt_id=attempt.id,
                artifact_type=ArtifactType.SCREENSHOT,
                path=str(screenshot_path),
                size_bytes=screenshot_path.stat().st_size,
                retention_days=get_settings().artifact_retention_days,
                created_at=now,
            )
            session.add(screenshot_artifact)
            session.flush()

        try:
            trace_bytes = _capture_target_page_trace_via_playwright(
                target_url=target_url,
                browser_profile_key=attempt.browser_profile_key,
            )
        except Exception as exc:  # pragma: no cover - exercised in tests via monkeypatch
            event_capture["trace_error"] = exc.__class__.__name__
        else:
            trace_path = startup_dir / f"{site_vendor}_guarded_submit_trace.zip"
            trace_path.write_bytes(trace_bytes)
            trace_artifact = Artifact(
                attempt_id=attempt.id,
                artifact_type=ArtifactType.TRACE,
                path=str(trace_path),
                size_bytes=trace_path.stat().st_size,
                retention_days=get_settings().artifact_retention_days,
                created_at=now,
            )
            session.add(trace_artifact)
            session.flush()

    event = ApplicationEvent(
        application_id=application.id,
        attempt_id=attempt.id,
        event_type="draft_submit_executed",
        message="Guarded submit executed from a passing submit-gate evaluation.",
        payload=build_guarded_submit_event_payload(
            site_vendor=site_vendor,
            confidence_score=confidence_score,
            allow_submit=True,
            submission_mode=submission_mode,
            target_url=target_url,
            artifact_id=artifact.id,
            artifact_path=str(artifact_path),
            screenshot_artifact_id=(screenshot_artifact.id if screenshot_artifact else None),
            trace_artifact_id=(trace_artifact.id if trace_artifact else None),
            submit_plan=submit_plan,
            submit_probe=submit_probe,
            submit_interaction=submit_interaction,
        )
        | ({"target_capture": event_capture} if event_capture else {}),
        created_at=now,
    )
    session.add(event)

    attempt.submit_confidence = confidence_score
    attempt.result = AttemptResult.SUCCESS.value
    attempt.failure_code = None
    attempt.ended_at = now
    attempt.notes = build_guarded_submit_attempt_note(
        submission_mode=submission_mode,
        target_url=target_url,
    )
    application.current_state = ApplicationState.APPLIED.value
    application.applied_at = now
    application.updated_at = now
    application.last_attempt_id = attempt.id
    session.commit()
    session.refresh(event)
    session.refresh(attempt)
    session.refresh(application)

    return DraftGuardedSubmitRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=site_vendor,
        application_state=application.current_state,
        attempt_result=attempt.result or AttemptResult.SUCCESS.value,
        failure_code=attempt.failure_code,
        confidence_score=confidence_score,
        allow_submit=True,
        submission_mode=submission_mode,
        target_url=target_url,
        artifact_id=artifact.id,
        artifact_path=str(artifact_path),
        screenshot_artifact_id=(screenshot_artifact.id if screenshot_artifact else None),
        trace_artifact_id=(trace_artifact.id if trace_artifact else None),
        submitted_at=now,
    )


def _execution_startup_dir(candidate_slug: str, job_id: int, attempt_id: int) -> Path:
    """Build the filesystem path for a staged execution startup bundle."""

    settings = get_settings()
    return settings.artifacts_dir / "execution" / candidate_slug / str(job_id) / str(attempt_id)


def _evaluate_guarded_submit_probe(
    *,
    session: Session,
    target_event: ApplicationEvent,
    submit_plan: dict[str, object],
) -> dict[str, object]:
    """Probe captured target HTML for vendor submit selectors and confirmation markers."""

    payload = target_event.payload or {}
    opened_page_artifact_id = payload.get("opened_page_artifact_id")
    if not isinstance(opened_page_artifact_id, int) or opened_page_artifact_id <= 0:
        return {
            "probe_available": False,
            "opened_page_artifact_id": None,
            "reason": "opened_page_artifact_missing",
            "matched_submit_selectors": [],
            "matched_review_selectors": [],
            "matched_confirmation_markers": [],
        }

    opened_artifact = session.get(Artifact, opened_page_artifact_id)
    if opened_artifact is None or not Path(opened_artifact.path).exists():
        return {
            "probe_available": False,
            "opened_page_artifact_id": opened_page_artifact_id,
            "reason": "opened_page_artifact_unreadable",
            "matched_submit_selectors": [],
            "matched_review_selectors": [],
            "matched_confirmation_markers": [],
        }

    html = Path(opened_artifact.path).read_text(encoding="utf-8", errors="replace")
    submit_selectors = [
        str(selector)
        for selector in (submit_plan.get("submit_button_selectors") or [])
    ]
    review_selectors = [
        str(selector)
        for selector in (submit_plan.get("review_step_selectors") or [])
    ]
    confirmation_markers = [
        str(marker)
        for marker in (submit_plan.get("confirmation_markers") or [])
    ]

    matched_submit_selectors = [
        selector for selector in submit_selectors if _selector_matches_html(selector, html)
    ]
    matched_review_selectors = [
        selector for selector in review_selectors if _selector_matches_html(selector, html)
    ]
    lowered_html = html.lower()
    matched_confirmation_markers = [
        marker for marker in confirmation_markers if marker.lower() in lowered_html
    ]
    return {
        "probe_available": True,
        "opened_page_artifact_id": opened_page_artifact_id,
        "matched_submit_selectors": matched_submit_selectors,
        "matched_review_selectors": matched_review_selectors,
        "matched_confirmation_markers": matched_confirmation_markers,
    }


def _classify_guarded_submit_probe_failure(
    *,
    submit_probe: dict[str, object],
    target_event: ApplicationEvent,
) -> str:
    """Classify deterministic guarded-submit probe failures for operator triage."""

    target_payload = target_event.payload or {}
    capture = target_payload.get("target_capture") or {}
    capture_error = str(capture.get("error") or "").lower()
    playwright_error = str(capture.get("playwright_error") or "").lower()
    target_url = str(target_payload.get("target_url") or "").lower()
    reason = str(submit_probe.get("reason") or "").lower()
    matched_review_selectors = list(submit_probe.get("matched_review_selectors") or [])
    matched_confirmation_markers = list(submit_probe.get("matched_confirmation_markers") or [])

    session_markers = (
        "login",
        "sign in",
        "unauthorized",
        "forbidden",
        "session",
        "checkpoint",
        "captcha",
    )
    if any(marker in capture_error for marker in session_markers):
        return "authentication_session_issue"
    if any(marker in target_url for marker in ("/login", "/signin", "/auth", "/checkpoint")):
        return "authentication_session_issue"
    if any(marker in playwright_error for marker in ("timeout", "context", "page", "browser")):
        return "browser_runtime_issue"
    if reason in {"opened_page_artifact_missing", "opened_page_artifact_unreadable"}:
        return "browser_runtime_issue"
    if matched_review_selectors or matched_confirmation_markers:
        return "page_changed_still_recognizable"
    return "unsupported_variant"


def _extract_failure_classification_from_event(event: ApplicationEvent | None) -> str | None:
    """Extract guarded-submit failure classification from one event payload."""

    if event is None:
        return None
    payload = event.payload or {}
    submit_probe = payload.get("submit_probe") or {}
    classification = submit_probe.get("failure_classification")
    if isinstance(classification, str) and classification.strip():
        return classification.strip()
    return None


def _resolve_attempt_failure_classification(
    *,
    session: Session,
    attempt_id: int,
    latest_event: ApplicationEvent | None,
) -> str | None:
    """Resolve operator-facing failure classification for one attempt."""

    direct = _extract_failure_classification_from_event(latest_event)
    if direct:
        return direct
    blocked_event = session.scalar(
        select(ApplicationEvent)
        .where(
            ApplicationEvent.attempt_id == attempt_id,
            ApplicationEvent.event_type == "draft_submit_execution_blocked",
        )
        .order_by(ApplicationEvent.created_at.desc(), ApplicationEvent.id.desc())
    )
    return _extract_failure_classification_from_event(blocked_event)


def _resolve_attempt_failure_classification_from_events(
    events: list[ApplicationEvent],
) -> str | None:
    """Resolve operator-facing failure classification from ordered attempt events."""

    for event in reversed(events):
        classification = _extract_failure_classification_from_event(event)
        if classification:
            return classification
    return None


def _extract_submit_interaction_diagnostics_from_event(
    event: ApplicationEvent | None,
) -> dict[str, object | None]:
    """Extract submit-stage interaction diagnostics from one submit-stage event payload."""

    empty = {
        "mode": None,
        "status": None,
        "clicked": None,
        "selector": None,
        "confirmation_count": None,
    }
    if event is None:
        return empty

    payload = event.payload or {}
    interaction = payload.get("submit_interaction")
    if isinstance(interaction, dict) and interaction:
        mode = interaction.get("interaction_mode")
        status = (
            interaction.get("status")
            or interaction.get("error")
            or interaction.get("blocked_reason")
        )
        clicked_value = interaction.get("clicked")
        clicked = clicked_value if isinstance(clicked_value, bool) else None
        selector = interaction.get("clicked_selector")
        markers = interaction.get("matched_confirmation_markers")
        confirmation_count = len(markers) if isinstance(markers, list) else None
        return {
            "mode": (str(mode) if mode is not None and str(mode).strip() else None),
            "status": (str(status) if status is not None and str(status).strip() else None),
            "clicked": clicked,
            "selector": (str(selector) if selector is not None and str(selector).strip() else None),
            "confirmation_count": confirmation_count,
        }

    submit_probe = payload.get("submit_probe")
    if isinstance(submit_probe, dict) and submit_probe:
        status = submit_probe.get("blocked_reason") or submit_probe.get("reason")
        matched_markers = submit_probe.get("matched_confirmation_markers")
        matched_selectors = submit_probe.get("matched_submit_selectors")
        selector = (
            matched_selectors[0]
            if isinstance(matched_selectors, list) and matched_selectors
            else None
        )
        confirmation_count = len(matched_markers) if isinstance(matched_markers, list) else None
        return {
            "mode": "probe_only",
            "status": (str(status) if status is not None and str(status).strip() else None),
            "clicked": None,
            "selector": (str(selector) if selector is not None and str(selector).strip() else None),
            "confirmation_count": confirmation_count,
        }

    return empty


def _extract_submit_interaction_diagnostics_from_events(
    events: list[ApplicationEvent],
) -> dict[str, object | None]:
    """Extract submit-stage interaction diagnostics from ordered attempt events."""

    for event in reversed(events):
        if event.event_type not in {"draft_submit_executed", "draft_submit_execution_blocked"}:
            continue
        diagnostics = _extract_submit_interaction_diagnostics_from_event(event)
        if any(value is not None for value in diagnostics.values()):
            return diagnostics
    return {
        "mode": None,
        "status": None,
        "clicked": None,
        "selector": None,
        "confirmation_count": None,
    }


def _selector_matches_html(selector: str, html: str) -> bool:
    """Check whether a CSS-like selector signature appears in raw HTML text."""

    normalized_html = html.lower()
    normalized_selector = selector.strip().lower()
    if not normalized_selector:
        return False

    checks: list[bool] = []
    has_structured_selector = any(token in normalized_selector for token in ("[", "#", "."))

    if "[type='submit']" in normalized_selector or '[type="submit"]' in normalized_selector:
        checks.append("type=\"submit\"" in normalized_html or "type='submit'" in normalized_html)
    attribute_pattern = re.compile(r"\[([a-z0-9_-]+)\s*=\s*['\"]([^'\"]+)['\"]\]")
    for key, value in attribute_pattern.findall(normalized_selector):
        checks.append(f"{key}=\"{value}\"" in normalized_html or f"{key}='{value}'" in normalized_html)

    if "#" in normalized_selector:
        selector_id = normalized_selector.split("#", 1)[1].split("[", 1)[0]
        if selector_id:
            checks.append(
                f"id=\"{selector_id}\"" in normalized_html or f"id='{selector_id}'" in normalized_html
            )

    class_pattern = re.compile(r"\.([a-z0-9_-]+)")
    for class_name in class_pattern.findall(normalized_selector):
        checks.append(
            f"class=\"{class_name}" in normalized_html
            or f"class='{class_name}" in normalized_html
            or f" {class_name} " in normalized_html
        )

    tag_pattern = re.match(r"^[a-z][a-z0-9_-]*", normalized_selector)
    if tag_pattern:
        tag_name = tag_pattern.group(0)
        if has_structured_selector:
            checks.append(f"<{tag_name}" in normalized_html)

    if checks:
        return all(checks)

    allow_tag_only_fallback = not has_structured_selector
    if allow_tag_only_fallback and tag_pattern and f"<{tag_pattern.group(0)}" in normalized_html:
        return True

    return normalized_selector in normalized_html


def _build_field_plan_entries(candidate: CandidateProfile, prepared) -> list[dict]:
    """Create deterministic plan entries from candidate details and prepared outputs."""

    entries: list[dict] = []
    personal = candidate.personal_details or {}
    full_name = candidate.name.strip()
    first_name, last_name = _split_name(full_name)

    entries.append(
        {
            "field_key": "full_name",
            "inferred_type": "full_name",
            "confidence": 0.99,
            "truth_tier": TruthTier.OBSERVED,
            "chosen_answer": full_name,
            "answer_source": "candidate_profile",
        }
    )
    if first_name:
        entries.append(
            {
                "field_key": "first_name",
                "inferred_type": "text",
                "confidence": 0.99,
                "truth_tier": TruthTier.OBSERVED,
                "chosen_answer": first_name,
                "answer_source": "candidate_profile",
            }
        )
    if last_name:
        entries.append(
            {
                "field_key": "last_name",
                "inferred_type": "text",
                "confidence": 0.99,
                "truth_tier": TruthTier.OBSERVED,
                "chosen_answer": last_name,
                "answer_source": "candidate_profile",
            }
        )

    for field_key, source_key, inferred_type in (
        ("email", "email", "email"),
        ("phone", "phone", "phone"),
        ("linkedin_url", "linkedin_url", "url"),
        ("location", "location", "location"),
        ("work_authorization", "work_authorization", "text"),
    ):
        value = personal.get(source_key)
        if value:
            entries.append(
                {
                    "field_key": field_key,
                    "inferred_type": inferred_type,
                    "confidence": 0.98,
                    "truth_tier": TruthTier.OBSERVED,
                    "chosen_answer": str(value),
                    "answer_source": "candidate_profile",
                }
            )

    if prepared.documents:
        document = prepared.documents[0]
        if document.content_path:
            entries.append(
                {
                    "field_key": "resume_upload",
                    "inferred_type": "file_upload",
                    "confidence": 0.99,
                    "truth_tier": TruthTier.OBSERVED,
                    "chosen_answer": document.content_path,
                    "answer_source": "generated_document",
                }
            )

    for index, answer in enumerate(prepared.answers, start=1):
        entries.append(
            {
                "field_key": _answer_field_key(answer.normalized_question_text, index),
                "inferred_type": "textarea",
                "confidence": 0.82 if answer.truth_tier == TruthTier.INFERENCE.value else 0.95,
                "answer_id": answer.answer_id,
                "truth_tier": (TruthTier(answer.truth_tier) if answer.truth_tier else None),
                "chosen_answer": answer.answer_text,
                "answer_source": "prepared_answer",
            }
        )

    return entries


def _compute_submit_confidence(
    *,
    required_fields: list[str],
    resolved_required_fields: list[str],
    manual_review_fields: list[str],
    unresolved_fields: list[str],
) -> float:
    """Compute a conservative submit-confidence score."""

    confidence = 0.4
    if required_fields:
        confidence += 0.4 * (len(resolved_required_fields) / len(required_fields))
    if not manual_review_fields:
        confidence += 0.1
    if not unresolved_fields:
        confidence += 0.1
    else:
        confidence -= min(len(unresolved_fields) * 0.05, 0.2)
    if manual_review_fields:
        confidence -= min(len(manual_review_fields) * 0.08, 0.24)
    return round(max(0.0, min(confidence, 1.0)), 4)


def _split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into first/last components conservatively."""

    parts = [part for part in full_name.split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _answer_field_key(question: str, index: int) -> str:
    """Map common prepared questions to stable field keys."""

    normalized = re.sub(r"[^a-z0-9]+", "_", question.lower()).strip("_")
    if "interested" in normalized or "why" in normalized:
        return "why_this_role"
    if "relevant_skills" in normalized or "skills" in normalized:
        return "relevant_skills"
    if "clarify" in normalized:
        return "fit_gap_clarification"
    return f"prepared_answer_{index:02d}"


def _target_url(job_id: int, session: Session) -> str:
    """Return the best known application target URL for the job."""

    from jobbot.db.models import Job

    job = session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise ValueError("job_not_found")
    return job.application_url or job.canonical_url


def _render_target_page_stub(*, job_title: str, target_url: str, candidate_profile_slug: str) -> str:
    """Render a local HTML stub representing the next execution target."""

    return (
        "<!doctype html>\n"
        "<html lang='en'>\n"
        "  <head><meta charset='utf-8'><title>Draft Execution Target</title></head>\n"
        "  <body>\n"
        f"    <h1>{job_title}</h1>\n"
        f"    <p>Candidate: {candidate_profile_slug}</p>\n"
        f"    <p>Target URL: <a href='{target_url}'>{target_url}</a></p>\n"
        "    <p>This file records the intended target for a non-submitting staged draft attempt.</p>\n"
        "  </body>\n"
        "</html>\n"
    )


def _render_opened_target_stub(
    *,
    job_title: str,
    target_url: str,
    browser_profile_key: str,
    candidate_profile_slug: str,
) -> str:
    """Render a local HTML stub representing a non-submitting page-open result."""

    return (
        "<!doctype html>\n"
        "<html lang='en'>\n"
        "  <head><meta charset='utf-8'><title>Opened Draft Target</title></head>\n"
        "  <body>\n"
        f"    <h1>{job_title}</h1>\n"
        f"    <p>Candidate: {candidate_profile_slug}</p>\n"
        f"    <p>Browser profile: {browser_profile_key}</p>\n"
        f"    <p>Opened target URL: <a href='{target_url}'>{target_url}</a></p>\n"
        "    <div class='application-review' data-qa='application-review'></div>\n"
        "    <button id='submit_app' type='submit' data-qa='submit-application'>Submit Application</button>\n"
        "    <p>This file records a non-submitting deterministic page-open pass.</p>\n"
        "  </body>\n"
        "</html>\n"
    )


def _execute_guarded_submit_interaction(
    *,
    target_url: str,
    browser_profile_key: str | None,
    submit_selectors: list[str],
    confirmation_markers: list[str],
) -> dict[str, object]:
    """Attempt deterministic guarded submit interaction with Playwright and safe fallback."""

    if not submit_selectors:
        return {
            "interaction_mode": "none",
            "attempted": False,
            "clicked": False,
            "clicked_selector": None,
            "final_url": target_url,
            "matched_confirmation_markers": [],
            "status": "no_submit_selectors",
        }
    if not browser_profile_key:
        return {
            "interaction_mode": "none",
            "attempted": False,
            "clicked": False,
            "clicked_selector": None,
            "final_url": target_url,
            "matched_confirmation_markers": [],
            "status": "browser_profile_missing",
        }
    try:
        return _execute_guarded_submit_interaction_via_playwright(
            target_url=target_url,
            browser_profile_key=browser_profile_key,
            submit_selectors=submit_selectors,
            confirmation_markers=confirmation_markers,
        )
    except Exception as exc:
        return {
            "interaction_mode": "simulated_probe_fallback",
            "attempted": False,
            "clicked": True,
            "clicked_selector": submit_selectors[0],
            "final_url": target_url,
            "matched_confirmation_markers": [],
            "status": "simulated_after_playwright_error",
            "error": exc.__class__.__name__,
        }


def _execute_guarded_submit_interaction_via_playwright(
    *,
    target_url: str,
    browser_profile_key: str,
    submit_selectors: list[str],
    confirmation_markers: list[str],
) -> dict[str, object]:
    """Execute one ATS submit interaction via Playwright persistent context."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised indirectly via fallback path
        raise RuntimeError("playwright_import_failed") from exc

    settings = get_settings()
    profile_dir = settings.browser_profiles_dir / browser_profile_key
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
        )
        try:
            page = context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(350)
            clicked_selector: str | None = None
            click_error: str | None = None
            for selector in submit_selectors:
                locator = page.locator(selector).first
                if locator.count() <= 0:
                    continue
                try:
                    locator.click(timeout=3_000)
                except Exception as exc:  # pragma: no cover - exercised via fallback path
                    click_error = exc.__class__.__name__
                    continue
                clicked_selector = selector
                break

            page.wait_for_timeout(600)
            lowered_html = page.content().lower()
            matched_confirmation_markers = [
                marker for marker in confirmation_markers if marker.lower() in lowered_html
            ]
            clicked = clicked_selector is not None
            return {
                "interaction_mode": "playwright",
                "attempted": True,
                "clicked": clicked,
                "clicked_selector": clicked_selector,
                "final_url": page.url,
                "matched_confirmation_markers": matched_confirmation_markers,
                "status": ("clicked" if clicked else "selector_click_failed"),
                "error": click_error,
            }
        finally:
            context.close()


def _capture_target_page_html(
    *,
    target_url: str,
    job_title: str,
    browser_profile_key: str,
    candidate_profile_slug: str,
) -> tuple[str, dict]:
    """Capture the live target page HTML with Playwright-first and deterministic fallbacks."""

    playwright_error: str | None = None
    try:
        return _capture_target_page_html_via_playwright(
            target_url=target_url,
            browser_profile_key=browser_profile_key,
        )
    except Exception as exc:
        playwright_error = exc.__class__.__name__

    request = Request(
        target_url,
        headers={
            "User-Agent": "jobbot-draft-runner/0.1 (+https://local.jobbot)",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            status_code = int(getattr(response, "status", 200))
            final_url = str(getattr(response, "url", target_url))
            content_type = str(response.headers.get("Content-Type", ""))
            raw = response.read(2_000_000)
            text = raw.decode("utf-8", errors="replace")
            capture_metadata = {
                "capture_method": "http_get",
                "status_code": status_code,
                "final_url": final_url,
                "content_type": content_type,
                "byte_length": len(raw),
            }
            if playwright_error:
                capture_metadata["playwright_error"] = playwright_error
            return text, capture_metadata
    except HTTPError as exc:
        error_code = f"http_error:{exc.code}"
    except URLError as exc:
        error_code = f"url_error:{exc.reason}"
    except TimeoutError:
        error_code = "timeout"
    except OSError as exc:
        error_code = f"os_error:{exc.__class__.__name__}"

    fallback_html = _render_opened_target_stub(
        job_title=job_title,
        target_url=target_url,
        browser_profile_key=browser_profile_key,
        candidate_profile_slug=candidate_profile_slug,
    )
    capture_metadata = {
        "capture_method": "stub_fallback",
        "error": error_code,
    }
    if playwright_error:
        capture_metadata["playwright_error"] = playwright_error
    return fallback_html, capture_metadata


def _capture_target_page_html_via_playwright(
    *,
    target_url: str,
    browser_profile_key: str,
) -> tuple[str, dict]:
    """Capture target HTML via Playwright persistent context."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised indirectly via fallback path
        raise RuntimeError("playwright_import_failed") from exc

    settings = get_settings()
    profile_dir = settings.browser_profiles_dir / browser_profile_key
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
        )
        try:
            page = context.new_page()
            response = page.goto(target_url, wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(350)
            html = page.content()
            metadata = {
                "capture_method": "playwright",
                "status_code": int(response.status) if response and response.status is not None else None,
                "final_url": page.url,
                "browser_profile_key": browser_profile_key,
            }
            return html, metadata
        finally:
            context.close()


def _capture_target_page_screenshot_via_playwright(
    *,
    target_url: str,
    browser_profile_key: str,
) -> bytes:
    """Capture a target screenshot via Playwright persistent context."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised indirectly via fallback path
        raise RuntimeError("playwright_import_failed") from exc

    settings = get_settings()
    profile_dir = settings.browser_profiles_dir / browser_profile_key
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
        )
        try:
            page = context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(350)
            return page.screenshot(type="png", full_page=True)
        finally:
            context.close()


def _capture_target_page_trace_via_playwright(
    *,
    target_url: str,
    browser_profile_key: str,
) -> bytes:
    """Capture a Playwright trace archive for target-open diagnostics."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised indirectly via fallback path
        raise RuntimeError("playwright_import_failed") from exc

    settings = get_settings()
    profile_dir = settings.browser_profiles_dir / browser_profile_key
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
        )
        trace_file_fd, trace_file_path = tempfile.mkstemp(suffix=".zip")
        os.close(trace_file_fd)
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            page = context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(350)
            context.tracing.stop(path=trace_file_path)
            return Path(trace_file_path).read_bytes()
        finally:
            context.close()
            try:
                Path(trace_file_path).unlink(missing_ok=True)
            except OSError:
                pass


def _build_artifact_preview(
    *,
    path: Path,
    artifact_type: ArtifactType,
    max_chars: int = 4000,
) -> tuple[str, str | None, bool]:
    """Return a bounded preview for text-like artifacts and suppress binary payloads."""

    if artifact_type == ArtifactType.SCREENSHOT:
        return "binary_image", None, False
    if artifact_type == ArtifactType.TRACE:
        return "binary_trace", None, False

    preview_kind = "text"
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"} or artifact_type == ArtifactType.HTML_SNAPSHOT:
        preview_kind = "html"
    elif suffix == ".json" or artifact_type in {ArtifactType.MODEL_IO, ArtifactType.ANSWER_PACK}:
        preview_kind = "json"

    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return preview_kind, text, truncated


def _event_by_type(events: list[ApplicationEvent], event_type: str) -> ApplicationEvent | None:
    """Return the latest event of one type from an ordered event list."""

    for event in reversed(events):
        if event.event_type == event_type:
            return event
    return None


def _artifact_by_filename(artifacts: list[Artifact], filename: str) -> Artifact | None:
    """Return the latest artifact whose basename matches the given filename."""

    for artifact in reversed(artifacts):
        if Path(artifact.path).name == filename:
            return artifact
    return None


def _artifact_from_event_payload(
    artifacts: list[Artifact],
    event: ApplicationEvent | None,
    key: str,
) -> Artifact | None:
    """Resolve an artifact from an event payload artifact id key."""

    if event is None or not event.payload:
        return None
    artifact_id = event.payload.get(key)
    if artifact_id is None:
        return None
    for artifact in artifacts:
        if artifact.id == artifact_id:
            return artifact
    return None


def _payload_value(event: ApplicationEvent | None, key: str) -> str | None:
    """Extract one payload value as string when present."""

    if event is None or not event.payload:
        return None
    value = event.payload.get(key)
    return None if value is None else str(value)


def _build_replay_asset(
    *,
    attempt_id: int,
    label: str,
    artifact: Artifact | None,
    fallback_path: str | None = None,
) -> DraftExecutionReplayAssetRead:
    """Build a replay asset row from an artifact or fallback event path."""

    path = artifact.path if artifact is not None else fallback_path
    exists = Path(path).exists() if path else False
    inspect_route = f"/execution/artifacts/{artifact.id}" if artifact is not None else None
    raw_route = None
    if artifact is not None and exists:
        raw_route = f"/execution/artifacts/{artifact.id}/raw"
    elif fallback_path and exists:
        raw_route = f"/execution/replay/{attempt_id}/assets/{label}/raw"
    openable_locally, open_hint = _determine_replay_openability(
        artifact_type=(artifact.artifact_type.value if artifact is not None else None),
        path=path,
        exists=exists,
    )
    launch_route, launch_label, launch_target = _determine_launch_action(
        raw_route=raw_route,
        open_hint=open_hint,
        artifact_id=(artifact.id if artifact is not None else None),
        attempt_id=attempt_id,
        label=label,
    )
    return DraftExecutionReplayAssetRead(
        label=label,
        artifact_id=(artifact.id if artifact is not None else None),
        artifact_type=(artifact.artifact_type.value if artifact is not None else None),
        path=path,
        exists=exists,
        inspect_route=inspect_route,
        raw_route=raw_route,
        launch_route=launch_route,
        launch_label=launch_label,
        launch_target=launch_target,
        openable_locally=openable_locally,
        open_hint=open_hint,
    )


def get_execution_artifact_file(
    session: Session,
    *,
    artifact_id: int,
) -> tuple[Path, str, str | None]:
    """Resolve one persisted execution artifact into a downloadable local file."""

    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise ValueError("execution_artifact_not_found")

    path = Path(artifact.path)
    if not path.exists():
        raise ValueError("execution_artifact_missing")

    media_type, _ = mimetypes.guess_type(str(path))
    return path, path.name, media_type


def get_execution_replay_asset_file(
    session: Session,
    *,
    attempt_id: int,
    label: str,
) -> tuple[Path, str, str | None]:
    """Resolve one replay asset label into a downloadable local file."""

    replay = get_execution_replay_bundle(session, attempt_id=attempt_id)
    asset = next((item for item in replay.assets if item.label == label), None)
    if asset is None:
        raise ValueError("execution_replay_asset_not_found")
    if not asset.path:
        raise ValueError("execution_replay_asset_missing")

    path = Path(asset.path)
    if not path.exists():
        raise ValueError("execution_replay_asset_missing")

    media_type, _ = mimetypes.guess_type(str(path))
    return path, path.name, media_type


def _determine_replay_openability(
    *,
    artifact_type: str | None,
    path: str | None,
    exists: bool,
) -> tuple[bool, str | None]:
    """Classify whether a replay asset is directly openable from local disk."""

    if not exists or not path:
        return False, None

    suffix = Path(path).suffix.lower()
    if artifact_type == ArtifactType.SCREENSHOT.value or suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return True, "open_image"
    if artifact_type == ArtifactType.HTML_SNAPSHOT.value or suffix in {".html", ".htm"}:
        return True, "open_html"
    if artifact_type in {ArtifactType.MODEL_IO.value, ArtifactType.ANSWER_PACK.value} or suffix in {
        ".json",
        ".txt",
        ".log",
    }:
        return True, "open_text"
    if artifact_type == ArtifactType.TRACE.value or suffix in {".zip", ".trace"}:
        return True, "open_trace"
    return True, "open_path"


def _artifact_type_action_label(artifact_type: str) -> str:
    """Map an artifact type string to a launch-oriented action label."""

    lowered = artifact_type.lower()
    if lowered == ArtifactType.SCREENSHOT.value:
        return "View image"
    if lowered == ArtifactType.TRACE.value:
        return "Download trace"
    if lowered == ArtifactType.HTML_SNAPSHOT.value:
        return "Open HTML"
    if lowered in {ArtifactType.MODEL_IO.value, ArtifactType.ANSWER_PACK.value}:
        return "Open text"
    return "Open file"


def _determine_launch_action(
    *,
    raw_route: str | None,
    open_hint: str | None,
    artifact_id: int | None = None,
    attempt_id: int | None = None,
    label: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Map one raw route plus open hint into an operator-facing launch action."""

    if raw_route is None or open_hint is None:
        return None, None, None

    if artifact_id is not None:
        launch_route = f"/execution/artifacts/{artifact_id}/launch"
    elif attempt_id is not None and label is not None:
        launch_route = f"/execution/replay/{attempt_id}/assets/{label}/launch"
    else:
        launch_route = raw_route

    launch_label_map = {
        "open_image": "View image",
        "open_html": "Open HTML",
        "open_text": "Open text",
        "open_trace": "Download trace",
        "open_path": "Open file",
    }
    launch_target_map = {
        "open_image": "inspect_image",
        "open_html": "open_html",
        "open_text": "open_text",
        "open_trace": "download_trace",
        "open_path": "open_path",
    }
    return (
        launch_route,
        launch_label_map.get(open_hint, "Open file"),
        launch_target_map.get(open_hint, "open_path"),
    )


def _build_startup_read(
    session: Session,
    *,
    application: Application,
    attempt: ApplicationAttempt,
    candidate: CandidateProfile,
    eligibility: ApplicationEligibility,
    event: ApplicationEvent,
) -> DraftExecutionStartupRead:
    """Build a read model from an existing startup event."""

    payload = event.payload or {}
    prepared = get_prepared_job_read(
        session,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
    )
    artifact_ids = [int(value) for value in payload.get("artifact_ids", [])]
    return DraftExecutionStartupRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        browser_profile_key=attempt.browser_profile_key,
        readiness_state=eligibility.readiness_state,
        target_url=str(payload.get("target_url") or _target_url(application.job_id, session)),
        startup_dir=str(payload.get("startup_dir") or _execution_startup_dir(candidate.slug, application.job_id, attempt.id)),
        prepared_document_count=(len(prepared.documents) if prepared else 0),
        prepared_answer_count=(len(prepared.answers) if prepared else 0),
        startup_artifact_ids=artifact_ids,
        started_at=attempt.started_at,
    )


def _build_field_plan_read(
    session: Session,
    *,
    application: Application,
    attempt: ApplicationAttempt,
    candidate: CandidateProfile,
    event: ApplicationEvent,
) -> DraftFieldPlanRead:
    """Build a field-plan read model from persisted mappings and event payload."""

    payload = event.payload or {}
    mappings = session.scalars(
        select(FieldMapping)
        .where(FieldMapping.attempt_id == attempt.id)
        .order_by(FieldMapping.id)
    ).all()
    return DraftFieldPlanRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        field_count=int(payload.get("field_count") or len(mappings)),
        artifact_id=int(payload.get("artifact_id") or 0),
        artifact_path=str(payload.get("artifact_path") or ""),
        entries=[
            DraftFieldPlanEntryRead(
                field_mapping_id=mapping.id,
                field_key=mapping.field_key,
                inferred_type=mapping.inferred_type,
                confidence=mapping.confidence,
                answer_id=mapping.answer_id,
                truth_tier=(mapping.truth_tier.value if mapping.truth_tier else None),
                chosen_answer=mapping.chosen_answer,
                answer_source=mapping.answer_source,
            )
            for mapping in mappings
        ],
    )


def _build_site_field_plan_read(
    session: Session,
    *,
    application: Application,
    attempt: ApplicationAttempt,
    candidate: CandidateProfile,
    job: Job,
    event: ApplicationEvent,
) -> DraftSiteFieldPlanRead:
    """Build a site-aware field-plan read model from persisted mappings and event payload."""

    payload = event.payload or {}
    site_vendor = str(payload.get("site_vendor") or job.ats_vendor or "unknown").lower()
    mappings = session.scalars(
        select(FieldMapping)
        .where(FieldMapping.attempt_id == attempt.id)
        .order_by(FieldMapping.id)
    ).all()
    entries: list[DraftSiteFieldPlanEntryRead] = []
    for mapping in mappings:
        parsed = {}
        if mapping.raw_dom_signature:
            try:
                parsed = json.loads(mapping.raw_dom_signature)
            except json.JSONDecodeError:
                parsed = {}
        entries.append(
            DraftSiteFieldPlanEntryRead(
                field_mapping_id=mapping.id,
                field_key=mapping.field_key,
                site_vendor=str(parsed.get("site_vendor") or site_vendor),
                selector_candidates=list(parsed.get("selectors") or []),
                confidence_gate=float(parsed.get("confidence_gate") or 0.0),
                manual_review_required=bool(parsed.get("manual_review_required")),
            )
        )

    return DraftSiteFieldPlanRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=site_vendor,
        entry_count=int(payload.get("entry_count") or len(entries)),
        artifact_id=int(payload.get("artifact_id") or 0),
        artifact_path=str(payload.get("artifact_path") or ""),
        entries=entries,
    )


def _build_target_open_read(
    session: Session,
    *,
    application: Application,
    attempt: ApplicationAttempt,
    candidate: CandidateProfile,
    job: Job,
    event: ApplicationEvent,
) -> DraftTargetOpenRead:
    """Build a target-open read model from persisted mappings and event payload."""

    payload = event.payload or {}
    mappings = session.scalars(
        select(FieldMapping)
        .where(FieldMapping.attempt_id == attempt.id)
        .order_by(FieldMapping.id)
    ).all()
    entries: list[DraftResolvedFieldRead] = []
    for mapping in mappings:
        parsed = {}
        if mapping.raw_dom_signature:
            try:
                parsed = json.loads(mapping.raw_dom_signature)
            except json.JSONDecodeError:
                parsed = {}
        entries.append(
            DraftResolvedFieldRead(
                field_mapping_id=mapping.id,
                field_key=mapping.field_key,
                resolved_selector=parsed.get("resolved_selector"),
                resolution_status=str(parsed.get("resolution_status") or "unresolved"),
                confidence_gate=float(parsed.get("confidence_gate") or 0.0),
                manual_review_required=bool(parsed.get("manual_review_required")),
            )
        )

    return DraftTargetOpenRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=str(payload.get("site_vendor") or job.ats_vendor or ""),
        browser_profile_key=str(attempt.browser_profile_key),
        target_url=str(payload.get("target_url") or _target_url(application.job_id, session)),
        capture_method=str((payload.get("target_capture") or {}).get("capture_method") or "unknown"),
        capture_error=(payload.get("target_capture") or {}).get("error"),
        opened_page_artifact_id=int(payload.get("opened_page_artifact_id") or 0),
        resolution_artifact_id=int(payload.get("resolution_artifact_id") or 0),
        screenshot_artifact_id=(
            int(payload.get("screenshot_artifact_id"))
            if payload.get("screenshot_artifact_id") is not None
            else None
        ),
        trace_artifact_id=(
            int(payload.get("trace_artifact_id"))
            if payload.get("trace_artifact_id") is not None
            else None
        ),
        resolved_count=int(payload.get("resolved_count") or 0),
        unresolved_count=int(payload.get("unresolved_count") or 0),
        entries=entries,
    )


def _build_submit_gate_read(
    session: Session,
    *,
    application: Application,
    attempt: ApplicationAttempt,
    candidate: CandidateProfile,
    job: Job,
    event: ApplicationEvent,
) -> DraftSubmitGateRead:
    """Build a submit-gate read model from persisted event payload."""

    payload = event.payload or {}
    return DraftSubmitGateRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=str(payload.get("site_vendor") or job.ats_vendor or ""),
        application_state=application.current_state,
        attempt_result=attempt.result,
        failure_code=attempt.failure_code,
        confidence_score=float(payload.get("confidence_score") or 0.0),
        allow_submit=bool(payload.get("allow_submit")),
        stop_reasons=list(payload.get("stop_reasons") or []),
        required_fields=list(payload.get("required_fields") or []),
        resolved_required_fields=list(payload.get("resolved_required_fields") or []),
        manual_review_fields=list(payload.get("manual_review_fields") or []),
        artifact_id=int(payload.get("artifact_id") or 0),
        artifact_path=str(payload.get("artifact_path") or ""),
    )


def _build_guarded_submit_read(
    *,
    application: Application,
    attempt: ApplicationAttempt,
    candidate: CandidateProfile,
    job: Job,
    event: ApplicationEvent,
) -> DraftGuardedSubmitRead:
    """Build a guarded-submit read model from persisted event payload."""

    payload = event.payload or {}
    site_vendor = str(payload.get("site_vendor") or job.ats_vendor or "")
    return DraftGuardedSubmitRead(
        application_id=application.id,
        attempt_id=attempt.id,
        event_id=event.id,
        job_id=application.job_id,
        candidate_profile_slug=candidate.slug,
        site_vendor=site_vendor,
        application_state=application.current_state,
        attempt_result=attempt.result or AttemptResult.SUCCESS.value,
        failure_code=attempt.failure_code,
        confidence_score=float(payload.get("confidence_score") or attempt.submit_confidence or 0.0),
        allow_submit=bool(payload.get("allow_submit")),
        submission_mode=str(
            payload.get("submission_mode")
            or (f"{site_vendor}_guarded_submit" if site_vendor else "guarded_submit")
        ),
        target_url=str(payload.get("target_url") or job.application_url or job.canonical_url),
        artifact_id=int(payload.get("artifact_id") or 0),
        artifact_path=str(payload.get("artifact_path") or ""),
        screenshot_artifact_id=(
            int(payload.get("screenshot_artifact_id"))
            if payload.get("screenshot_artifact_id") is not None
            else None
        ),
        trace_artifact_id=(
            int(payload.get("trace_artifact_id"))
            if payload.get("trace_artifact_id") is not None
            else None
        ),
        submitted_at=event.created_at,
    )
