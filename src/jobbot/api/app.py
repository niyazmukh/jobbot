"""FastAPI application for local inbox and review workflows."""

from __future__ import annotations

from html import escape
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from jobbot.config import get_settings
from jobbot.db import SessionLocal
from jobbot.eligibility import (
    ApplicationEligibilityRead,
    get_application_eligibility,
    list_application_eligibility,
    materialize_application_eligibility,
)
from jobbot.execution import (
    DraftApplicationAttemptRead,
    DraftExecutionArtifactDetailRead,
    DraftExecutionDashboardRead,
    DraftExecutionAttemptDetailRead,
    DraftExecutionOverviewRead,
    DraftExecutionReplayBundleRead,
    DraftGuardedSubmitRead,
    DraftExecutionStartupRead,
    DraftFieldPlanRead,
    DraftSiteFieldPlanRead,
    DraftSubmitGateRead,
    DraftTargetOpenRead,
    bootstrap_draft_application_attempt,
    build_draft_field_plan,
    build_site_field_overlay,
    execute_guarded_submit,
    evaluate_submit_gate,
    get_execution_artifact_detail,
    get_execution_artifact_file,
    get_execution_dashboard,
    get_execution_attempt_detail,
    get_execution_replay_asset_file,
    get_execution_replay_bundle,
    list_execution_overview,
    list_draft_application_attempts,
    open_site_target_page,
    start_draft_execution_attempt,
)
from jobbot.discovery.inbox import (
    InboxJobDetail,
    InboxJobRow,
    get_inbox_job_detail,
    get_ready_to_apply_job_detail,
    list_inbox_jobs,
    list_ready_to_apply_jobs,
)
from jobbot.models.enums import ReviewStatus
from jobbot.preparation import PreparedJobRead, get_prepared_job_read
from jobbot.review.schemas import ReviewQueueRead
from jobbot.review.service import list_review_queue, queue_score_review, set_review_status
from jobbot.scoring.schemas import JobScoreRead
from jobbot.scoring.service import get_job_score_for_candidate


def get_db_session():
    """Yield a request-scoped database session."""

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db_session)]


def create_app() -> FastAPI:
    """Create the FastAPI application."""

    settings = get_settings()
    app = FastAPI(
        title="JobBot API",
        version="0.1.0",
        description="Local-first inbox and workflow API for JobBot.",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Return a lightweight health payload for the local service."""

        return {
            "status": "ok",
            "app": "jobbot",
            "database_url": settings.resolved_database_url,
        }

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(
        db: DbSession,
        limit: Annotated[int, Query(ge=1, le=200)] = 25,
        offset: Annotated[int, Query(ge=0)] = 0,
        candidate_profile_slug: str | None = None,
        status: str | None = None,
        ats_vendor: str | None = None,
        remote_type: str | None = None,
        preparation_state: str | None = None,
        application_readiness: str | None = None,
        execution_state: str | None = None,
        sort_by: str = "last_seen_at",
        descending: bool = True,
    ) -> HTMLResponse:
        """Render a simple local inbox UI."""

        jobs = list_inbox_jobs(
            db,
            limit=limit,
            offset=offset,
            candidate_profile_slug=candidate_profile_slug,
            status=status,
            ats_vendor=ats_vendor,
            remote_type=remote_type,
            preparation_state=preparation_state,
            application_readiness=application_readiness,
            execution_state=execution_state,
            sort_by=sort_by,
            descending=descending,
        )
        html = _render_inbox_page(
            jobs=jobs,
            candidate_profile_slug=candidate_profile_slug,
            filters={
                "status": status,
                "ats_vendor": ats_vendor,
                "remote_type": remote_type,
                "preparation_state": preparation_state,
                "application_readiness": application_readiness,
                "execution_state": execution_state,
                "sort_by": sort_by,
                "descending": descending,
                "limit": limit,
                "offset": offset,
            },
        )
        return HTMLResponse(html)

    @app.get("/inbox/jobs/{job_id}", response_class=HTMLResponse)
    def inbox_job_page(
        job_id: int,
        db: DbSession,
        candidate_profile_slug: str | None = None,
    ) -> HTMLResponse:
        """Render a single-job inbox detail view."""

        detail = get_inbox_job_detail(db, job_id, candidate_profile_slug=candidate_profile_slug)
        if detail is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return HTMLResponse(_render_job_detail_page(detail))

    @app.get("/review-queue", response_class=HTMLResponse)
    def review_queue_page(
        db: DbSession,
        status: str | None = None,
        entity_type: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> HTMLResponse:
        """Render a simple manual review queue UI."""

        reviews = list_review_queue(db, status=status, entity_type=entity_type, limit=limit)
        return HTMLResponse(_render_review_queue_page(reviews))

    @app.get("/execution/overview/{candidate_profile_slug}", response_class=HTMLResponse)
    def execution_overview_page(
        candidate_profile_slug: str,
        db: DbSession,
        blocked_only: bool = False,
        manual_review_only: bool = False,
        failure_code: str | None = None,
        max_submit_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
        sort_by: str = "started_at",
        descending: bool = True,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> HTMLResponse:
        """Render a focused draft-execution operations view for one candidate."""

        try:
            rows = list_execution_overview(
                db,
                candidate_profile_slug=candidate_profile_slug,
                blocked_only=blocked_only,
                manual_review_only=manual_review_only,
                failure_code=failure_code,
                max_submit_confidence=max_submit_confidence,
                sort_by=sort_by,
                descending=descending,
                limit=limit,
            )
        except ValueError as exc:
            if str(exc) == "candidate_profile_not_found":
                raise HTTPException(status_code=404, detail="candidate_profile_not_found") from exc
            if str(exc) == "invalid_execution_overview_sort":
                raise HTTPException(status_code=400, detail="invalid_execution_overview_sort") from exc
            raise
        return HTMLResponse(
            _render_execution_overview_page(
                rows=rows,
                candidate_profile_slug=candidate_profile_slug,
                blocked_only=blocked_only,
            )
        )

    @app.get("/execution/dashboard/{candidate_profile_slug}", response_class=HTMLResponse)
    def execution_dashboard_page(
        candidate_profile_slug: str,
        db: DbSession,
        manual_review_only: bool = False,
        failure_code: str | None = None,
        max_submit_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
        sort_by: str = "started_at",
        descending: bool = True,
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> HTMLResponse:
        """Render a candidate-scoped execution dashboard."""

        try:
            detail = get_execution_dashboard(
                db,
                candidate_profile_slug=candidate_profile_slug,
                manual_review_only=manual_review_only,
                failure_code=failure_code,
                max_submit_confidence=max_submit_confidence,
                sort_by=sort_by,
                descending=descending,
                limit=limit,
            )
        except ValueError as exc:
            if str(exc) == "candidate_profile_not_found":
                raise HTTPException(status_code=404, detail="candidate_profile_not_found") from exc
            if str(exc) == "invalid_execution_overview_sort":
                raise HTTPException(status_code=400, detail="invalid_execution_overview_sort") from exc
            raise
        return HTMLResponse(_render_execution_dashboard_page(detail))

    @app.get("/execution/attempts/{attempt_id}", response_class=HTMLResponse)
    def execution_attempt_detail_page(
        attempt_id: int,
        db: DbSession,
    ) -> HTMLResponse:
        """Render one execution attempt drill-down with events and artifacts."""

        try:
            detail = get_execution_attempt_detail(db, attempt_id=attempt_id)
        except ValueError as exc:
            if str(exc) == "application_attempt_not_found":
                raise HTTPException(status_code=404, detail="application_attempt_not_found") from exc
            raise
        return HTMLResponse(_render_execution_attempt_detail_page(detail))

    @app.get("/execution/replay/{attempt_id}", response_class=HTMLResponse)
    def execution_replay_bundle_page(
        attempt_id: int,
        db: DbSession,
    ) -> HTMLResponse:
        """Render one replay-oriented execution bundle."""

        try:
            detail = get_execution_replay_bundle(db, attempt_id=attempt_id)
        except ValueError as exc:
            if str(exc) == "application_attempt_not_found":
                raise HTTPException(status_code=404, detail="application_attempt_not_found") from exc
            raise
        return HTMLResponse(_render_execution_replay_bundle_page(detail))

    @app.get("/execution/artifacts/{artifact_id}", response_class=HTMLResponse)
    def execution_artifact_detail_page(
        artifact_id: int,
        db: DbSession,
    ) -> HTMLResponse:
        """Render one execution artifact detail page with a bounded preview."""

        try:
            detail = get_execution_artifact_detail(db, artifact_id=artifact_id)
        except ValueError as exc:
            if str(exc) == "execution_artifact_not_found":
                raise HTTPException(status_code=404, detail="execution_artifact_not_found") from exc
            raise
        return HTMLResponse(_render_execution_artifact_detail_page(detail))

    @app.get("/execution/artifacts/{artifact_id}/raw", response_class=FileResponse)
    def execution_artifact_raw_page(
        artifact_id: int,
        db: DbSession,
    ) -> FileResponse:
        """Serve one persisted execution artifact as a raw local file response."""

        try:
            path, filename, media_type = get_execution_artifact_file(db, artifact_id=artifact_id)
        except ValueError as exc:
            detail = str(exc)
            if detail in {"execution_artifact_not_found", "execution_artifact_missing"}:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise
        return FileResponse(path=str(path), filename=filename, media_type=media_type)

    @app.get("/execution/artifacts/{artifact_id}/launch", response_class=RedirectResponse)
    def execution_artifact_launch_page(
        artifact_id: int,
        db: DbSession,
    ) -> RedirectResponse:
        """Launch one execution artifact through its operator-facing route."""

        try:
            detail = get_execution_artifact_detail(db, artifact_id=artifact_id)
        except ValueError as exc:
            detail_code = str(exc)
            if detail_code == "execution_artifact_not_found":
                raise HTTPException(status_code=404, detail=detail_code) from exc
            raise
        if detail.launch_route is None:
            raise HTTPException(status_code=404, detail="execution_artifact_not_launchable")
        if detail.launch_target == "inspect_image":
            return RedirectResponse(url=f"/execution/artifacts/{artifact_id}")
        return RedirectResponse(url=detail.raw_route or detail.launch_route)

    @app.get("/execution/replay/{attempt_id}/assets/{label}/raw", response_class=FileResponse)
    def execution_replay_asset_raw_page(
        attempt_id: int,
        label: str,
        db: DbSession,
    ) -> FileResponse:
        """Serve one replay asset label as a raw local file response."""

        try:
            path, filename, media_type = get_execution_replay_asset_file(
                db,
                attempt_id=attempt_id,
                label=label,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {
                "application_attempt_not_found",
                "execution_replay_asset_not_found",
                "execution_replay_asset_missing",
            }:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise
        return FileResponse(path=str(path), filename=filename, media_type=media_type)

    @app.get("/execution/replay/{attempt_id}/assets/{label}/launch", response_class=RedirectResponse)
    def execution_replay_asset_launch_page(
        attempt_id: int,
        label: str,
        db: DbSession,
    ) -> RedirectResponse:
        """Launch one replay asset through its operator-facing route."""

        try:
            replay = get_execution_replay_bundle(db, attempt_id=attempt_id)
        except ValueError as exc:
            detail_code = str(exc)
            if detail_code == "application_attempt_not_found":
                raise HTTPException(status_code=404, detail=detail_code) from exc
            raise
        asset = next((item for item in replay.assets if item.label == label), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="execution_replay_asset_not_found")
        if asset.launch_route is None:
            raise HTTPException(status_code=404, detail="execution_replay_asset_not_launchable")
        if asset.launch_target == "inspect_image" and asset.inspect_route is not None:
            return RedirectResponse(url=asset.inspect_route)
        return RedirectResponse(url=asset.raw_route or asset.launch_route)

    @app.get("/ready-to-apply/{candidate_profile_slug}", response_class=HTMLResponse)
    def ready_to_apply_page(
        candidate_profile_slug: str,
        db: DbSession,
        limit: Annotated[int, Query(ge=1, le=200)] = 25,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> HTMLResponse:
        """Render a focused ready-to-apply inbox for one candidate."""

        jobs = list_ready_to_apply_jobs(
            db,
            candidate_profile_slug=candidate_profile_slug,
            limit=limit,
            offset=offset,
        )
        html = _render_inbox_page(
            jobs=jobs,
            candidate_profile_slug=candidate_profile_slug,
            filters={
                "application_readiness": "ready_to_apply",
                "limit": limit,
                "offset": offset,
            },
        )
        return HTMLResponse(html)

    @app.get("/api/jobs", response_model=list[InboxJobRow])
    def list_jobs(
        db: DbSession,
        limit: Annotated[int, Query(ge=1, le=500)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        candidate_profile_slug: str | None = None,
        status: str | None = None,
        ats_vendor: str | None = None,
        remote_type: str | None = None,
        preparation_state: str | None = None,
        application_readiness: str | None = None,
        execution_state: str | None = None,
        sort_by: str = "last_seen_at",
        descending: bool = True,
    ) -> list[InboxJobRow]:
        """Return the current local inbox view of persisted jobs."""

        return list_inbox_jobs(
            db,
            limit=limit,
            offset=offset,
            candidate_profile_slug=candidate_profile_slug,
            status=status,
            ats_vendor=ats_vendor,
            remote_type=remote_type,
            preparation_state=preparation_state,
            application_readiness=application_readiness,
            execution_state=execution_state,
            sort_by=sort_by,
            descending=descending,
        )

    @app.get("/api/jobs/{job_id}", response_model=InboxJobDetail)
    def get_job(
        job_id: int,
        db: DbSession,
        candidate_profile_slug: str | None = None,
    ) -> InboxJobDetail:
        """Return a single job inbox record with source provenance."""

        detail = get_inbox_job_detail(db, job_id, candidate_profile_slug=candidate_profile_slug)
        if detail is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return detail

    @app.get("/api/jobs/ready-to-apply/{candidate_profile_slug}", response_model=list[InboxJobRow])
    def get_ready_to_apply_jobs(
        candidate_profile_slug: str,
        db: DbSession,
        limit: Annotated[int, Query(ge=1, le=500)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[InboxJobRow]:
        """Return jobs that are ready to apply for a candidate."""

        return list_ready_to_apply_jobs(
            db,
            candidate_profile_slug=candidate_profile_slug,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/jobs/{job_id}/ready-to-apply/{candidate_profile_slug}", response_model=InboxJobDetail)
    def get_ready_to_apply_job(
        job_id: int,
        candidate_profile_slug: str,
        db: DbSession,
    ) -> InboxJobDetail:
        """Return job detail only when the candidate/job pair is ready to apply."""

        detail = get_ready_to_apply_job_detail(
            db,
            job_id=job_id,
            candidate_profile_slug=candidate_profile_slug,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="ready_to_apply_job_not_found")
        return detail

    @app.get("/api/jobs/{job_id}/scores/{candidate_profile_slug}", response_model=JobScoreRead)
    def get_job_score(job_id: int, candidate_profile_slug: str, db: DbSession) -> JobScoreRead:
        """Return a persisted score for a specific candidate/job pair."""

        score = get_job_score_for_candidate(db, job_id, candidate_profile_slug)
        if score is None:
            raise HTTPException(status_code=404, detail="job_score_not_found")
        return score

    @app.get("/api/jobs/{job_id}/prepared/{candidate_profile_slug}", response_model=PreparedJobRead)
    def get_prepared_job(job_id: int, candidate_profile_slug: str, db: DbSession) -> PreparedJobRead:
        """Return persisted preparation outputs for a candidate/job pair."""

        prepared = get_prepared_job_read(
            db,
            job_id=job_id,
            candidate_profile_slug=candidate_profile_slug,
        )
        if prepared is None:
            raise HTTPException(status_code=404, detail="prepared_job_not_found")
        return prepared

    @app.post("/api/eligibility/jobs/{job_id}/{candidate_profile_slug}", response_model=ApplicationEligibilityRead)
    def materialize_eligibility_endpoint(
        job_id: int,
        candidate_profile_slug: str,
        db: DbSession,
    ) -> ApplicationEligibilityRead:
        """Persist the current candidate/job execution eligibility snapshot."""

        try:
            return materialize_application_eligibility(
                db,
                job_id=job_id,
                candidate_profile_slug=candidate_profile_slug,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {"candidate_profile_not_found", "job_not_found"}:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise

    @app.get("/api/eligibility/{candidate_profile_slug}", response_model=list[ApplicationEligibilityRead])
    def list_eligibility_endpoint(
        candidate_profile_slug: str,
        db: DbSession,
        ready_only: bool = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[ApplicationEligibilityRead]:
        """Return persisted eligibility snapshots for one candidate."""

        try:
            return list_application_eligibility(
                db,
                candidate_profile_slug=candidate_profile_slug,
                ready_only=ready_only,
                limit=limit,
            )
        except ValueError as exc:
            if str(exc) == "candidate_profile_not_found":
                raise HTTPException(status_code=404, detail="candidate_profile_not_found") from exc
            raise

    @app.get("/api/eligibility/jobs/{job_id}/{candidate_profile_slug}", response_model=ApplicationEligibilityRead)
    def get_eligibility_endpoint(
        job_id: int,
        candidate_profile_slug: str,
        db: DbSession,
    ) -> ApplicationEligibilityRead:
        """Return one persisted eligibility snapshot if present."""

        row = get_application_eligibility(
            db,
            job_id=job_id,
            candidate_profile_slug=candidate_profile_slug,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="application_eligibility_not_found")
        return row

    @app.post(
        "/api/execution/draft-attempts/jobs/{job_id}/{candidate_profile_slug}",
        response_model=DraftApplicationAttemptRead,
    )
    def bootstrap_draft_attempt_endpoint(
        job_id: int,
        candidate_profile_slug: str,
        db: DbSession,
        browser_profile_key: str | None = None,
    ) -> DraftApplicationAttemptRead:
        """Create a draft application attempt from persisted readiness."""

        try:
            return bootstrap_draft_application_attempt(
                db,
                job_id=job_id,
                candidate_profile_slug=candidate_profile_slug,
                browser_profile_key=browser_profile_key,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {
                "candidate_profile_not_found",
                "application_eligibility_not_found",
                "application_not_ready_to_apply",
                "browser_profile_not_found",
                "browser_profile_not_application_type",
                "browser_profile_not_ready_for_application",
                "application_already_applied",
            }:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise

    @app.get(
        "/api/execution/draft-attempts/{candidate_profile_slug}",
        response_model=list[DraftApplicationAttemptRead],
    )
    def list_draft_attempts_endpoint(
        candidate_profile_slug: str,
        db: DbSession,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[DraftApplicationAttemptRead]:
        """List draft application attempts for one candidate."""

        try:
            return list_draft_application_attempts(
                db,
                candidate_profile_slug=candidate_profile_slug,
                limit=limit,
            )
        except ValueError as exc:
            if str(exc) == "candidate_profile_not_found":
                raise HTTPException(status_code=404, detail="candidate_profile_not_found") from exc
            raise

    @app.get(
        "/api/execution/overview/{candidate_profile_slug}",
        response_model=list[DraftExecutionOverviewRead],
    )
    def execution_overview_endpoint(
        candidate_profile_slug: str,
        db: DbSession,
        blocked_only: bool = False,
        manual_review_only: bool = False,
        failure_code: str | None = None,
        failure_classification: str | None = None,
        max_submit_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
        sort_by: str = "started_at",
        descending: bool = True,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[DraftExecutionOverviewRead]:
        """Return a focused draft-execution operations view for one candidate."""

        try:
            return list_execution_overview(
                db,
                candidate_profile_slug=candidate_profile_slug,
                blocked_only=blocked_only,
                manual_review_only=manual_review_only,
                failure_code=failure_code,
                failure_classification=failure_classification,
                max_submit_confidence=max_submit_confidence,
                sort_by=sort_by,
                descending=descending,
                limit=limit,
            )
        except ValueError as exc:
            if str(exc) == "candidate_profile_not_found":
                raise HTTPException(status_code=404, detail="candidate_profile_not_found") from exc
            if str(exc) == "invalid_execution_overview_sort":
                raise HTTPException(status_code=400, detail="invalid_execution_overview_sort") from exc
            raise

    @app.get(
        "/api/execution/dashboard/{candidate_profile_slug}",
        response_model=DraftExecutionDashboardRead,
    )
    def execution_dashboard_endpoint(
        candidate_profile_slug: str,
        db: DbSession,
        manual_review_only: bool = False,
        failure_code: str | None = None,
        failure_classification: str | None = None,
        max_submit_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
        sort_by: str = "started_at",
        descending: bool = True,
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> DraftExecutionDashboardRead:
        """Return a candidate-scoped execution dashboard."""

        try:
            return get_execution_dashboard(
                db,
                candidate_profile_slug=candidate_profile_slug,
                manual_review_only=manual_review_only,
                failure_code=failure_code,
                failure_classification=failure_classification,
                max_submit_confidence=max_submit_confidence,
                sort_by=sort_by,
                descending=descending,
                limit=limit,
            )
        except ValueError as exc:
            if str(exc) == "candidate_profile_not_found":
                raise HTTPException(status_code=404, detail="candidate_profile_not_found") from exc
            if str(exc) == "invalid_execution_overview_sort":
                raise HTTPException(status_code=400, detail="invalid_execution_overview_sort") from exc
            raise

    @app.get(
        "/api/execution/attempts/{attempt_id}",
        response_model=DraftExecutionAttemptDetailRead,
    )
    def execution_attempt_detail_endpoint(
        attempt_id: int,
        db: DbSession,
    ) -> DraftExecutionAttemptDetailRead:
        """Return one execution attempt drill-down with events and artifacts."""

        try:
            return get_execution_attempt_detail(db, attempt_id=attempt_id)
        except ValueError as exc:
            if str(exc) == "application_attempt_not_found":
                raise HTTPException(status_code=404, detail="application_attempt_not_found") from exc
            raise

    @app.get(
        "/api/execution/artifacts/{artifact_id}",
        response_model=DraftExecutionArtifactDetailRead,
    )
    def execution_artifact_detail_endpoint(
        artifact_id: int,
        db: DbSession,
    ) -> DraftExecutionArtifactDetailRead:
        """Return one execution artifact with a bounded preview."""

        try:
            return get_execution_artifact_detail(db, artifact_id=artifact_id)
        except ValueError as exc:
            if str(exc) == "execution_artifact_not_found":
                raise HTTPException(status_code=404, detail="execution_artifact_not_found") from exc
            raise

    @app.get("/api/execution/artifacts/{artifact_id}/raw", response_class=FileResponse)
    def execution_artifact_raw_endpoint(
        artifact_id: int,
        db: DbSession,
    ) -> FileResponse:
        """Return one persisted execution artifact as a raw file response."""

        try:
            path, filename, media_type = get_execution_artifact_file(db, artifact_id=artifact_id)
        except ValueError as exc:
            detail = str(exc)
            if detail in {"execution_artifact_not_found", "execution_artifact_missing"}:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise
        return FileResponse(path=str(path), filename=filename, media_type=media_type)

    @app.get("/api/execution/artifacts/{artifact_id}/launch", response_class=RedirectResponse)
    def execution_artifact_launch_endpoint(
        artifact_id: int,
        db: DbSession,
    ) -> RedirectResponse:
        """Launch one persisted execution artifact through its operator-facing route."""

        try:
            detail = get_execution_artifact_detail(db, artifact_id=artifact_id)
        except ValueError as exc:
            detail_code = str(exc)
            if detail_code == "execution_artifact_not_found":
                raise HTTPException(status_code=404, detail=detail_code) from exc
            raise
        if detail.launch_route is None:
            raise HTTPException(status_code=404, detail="execution_artifact_not_launchable")
        if detail.launch_target == "inspect_image":
            return RedirectResponse(url=f"/execution/artifacts/{artifact_id}")
        return RedirectResponse(url=detail.raw_route or detail.launch_route)

    @app.get(
        "/api/execution/replay/{attempt_id}",
        response_model=DraftExecutionReplayBundleRead,
    )
    def execution_replay_bundle_endpoint(
        attempt_id: int,
        db: DbSession,
    ) -> DraftExecutionReplayBundleRead:
        """Return one replay-oriented execution bundle."""

        try:
            return get_execution_replay_bundle(db, attempt_id=attempt_id)
        except ValueError as exc:
            if str(exc) == "application_attempt_not_found":
                raise HTTPException(status_code=404, detail="application_attempt_not_found") from exc
            raise

    @app.get("/api/execution/replay/{attempt_id}/assets/{label}/raw", response_class=FileResponse)
    def execution_replay_asset_raw_endpoint(
        attempt_id: int,
        label: str,
        db: DbSession,
    ) -> FileResponse:
        """Return one replay asset label as a raw file response."""

        try:
            path, filename, media_type = get_execution_replay_asset_file(
                db,
                attempt_id=attempt_id,
                label=label,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {
                "application_attempt_not_found",
                "execution_replay_asset_not_found",
                "execution_replay_asset_missing",
            }:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise
        return FileResponse(path=str(path), filename=filename, media_type=media_type)

    @app.get("/api/execution/replay/{attempt_id}/assets/{label}/launch", response_class=RedirectResponse)
    def execution_replay_asset_launch_endpoint(
        attempt_id: int,
        label: str,
        db: DbSession,
    ) -> RedirectResponse:
        """Launch one replay asset label through its operator-facing route."""

        try:
            replay = get_execution_replay_bundle(db, attempt_id=attempt_id)
        except ValueError as exc:
            detail_code = str(exc)
            if detail_code == "application_attempt_not_found":
                raise HTTPException(status_code=404, detail=detail_code) from exc
            raise
        asset = next((item for item in replay.assets if item.label == label), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="execution_replay_asset_not_found")
        if asset.launch_route is None:
            raise HTTPException(status_code=404, detail="execution_replay_asset_not_launchable")
        if asset.launch_target == "inspect_image" and asset.inspect_route is not None:
            return RedirectResponse(url=asset.inspect_route)
        return RedirectResponse(url=asset.raw_route or asset.launch_route)

    @app.post(
        "/api/execution/draft-attempts/{attempt_id}/start",
        response_model=DraftExecutionStartupRead,
    )
    def start_draft_execution_endpoint(
        attempt_id: int,
        db: DbSession,
    ) -> DraftExecutionStartupRead:
        """Create the staged startup bundle for a draft application attempt."""

        try:
            return start_draft_execution_attempt(
                db,
                attempt_id=attempt_id,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {
                "application_attempt_not_found",
                "application_attempt_not_draft",
                "application_already_applied",
                "application_not_ready_to_apply",
                "browser_profile_not_found",
                "browser_profile_not_application_type",
                "browser_profile_not_ready_for_application",
                "prepared_outputs_not_found",
            }:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise

    @app.post(
        "/api/execution/draft-attempts/{attempt_id}/field-plan",
        response_model=DraftFieldPlanRead,
    )
    def build_draft_field_plan_endpoint(
        attempt_id: int,
        db: DbSession,
    ) -> DraftFieldPlanRead:
        """Create deterministic field mappings for a staged draft attempt."""

        try:
            return build_draft_field_plan(
                db,
                attempt_id=attempt_id,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {
                "application_attempt_not_found",
                "draft_execution_not_started",
                "prepared_outputs_not_found",
            }:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise

    @app.post(
        "/api/execution/draft-attempts/{attempt_id}/site-overlay",
        response_model=DraftSiteFieldPlanRead,
    )
    def build_site_field_overlay_endpoint(
        attempt_id: int,
        db: DbSession,
    ) -> DraftSiteFieldPlanRead:
        """Create a site-aware selector overlay for a draft attempt field plan."""

        try:
            return build_site_field_overlay(
                db,
                attempt_id=attempt_id,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {
                "application_attempt_not_found",
                "application_attempt_not_draft",
                "draft_field_plan_not_created",
                "draft_field_plan_empty",
                "site_overlay_not_supported",
            }:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise

    @app.post(
        "/api/execution/draft-attempts/{attempt_id}/open-target",
        response_model=DraftTargetOpenRead,
    )
    def open_site_target_endpoint(
        attempt_id: int,
        db: DbSession,
    ) -> DraftTargetOpenRead:
        """Run a non-submitting target-open and field-resolution pass."""

        try:
            return open_site_target_page(
                db,
                attempt_id=attempt_id,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {
                "application_attempt_not_found",
                "application_attempt_not_draft",
                "browser_profile_required_for_page_open",
                "draft_execution_not_started",
                "draft_site_overlay_not_created",
                "browser_profile_not_found",
                "browser_profile_not_application_type",
                "browser_profile_not_ready_for_application",
                "page_open_not_supported_for_site",
                "draft_field_plan_empty",
            }:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise

    @app.post(
        "/api/execution/draft-attempts/{attempt_id}/submit-gate",
        response_model=DraftSubmitGateRead,
    )
    def evaluate_submit_gate_endpoint(
        attempt_id: int,
        db: DbSession,
    ) -> DraftSubmitGateRead:
        """Evaluate guarded submit confidence for a draft attempt."""

        try:
            return evaluate_submit_gate(
                db,
                attempt_id=attempt_id,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {
                "application_attempt_not_found",
                "application_attempt_not_draft",
                "draft_target_not_opened",
                "submit_gate_not_supported_for_site",
                "draft_field_plan_empty",
            }:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise

    @app.post(
        "/api/execution/draft-attempts/{attempt_id}/guarded-submit",
        response_model=DraftGuardedSubmitRead,
    )
    def execute_guarded_submit_endpoint(
        attempt_id: int,
        db: DbSession,
    ) -> DraftGuardedSubmitRead:
        """Execute guarded submit after submit-gate approval."""

        try:
            return execute_guarded_submit(
                db,
                attempt_id=attempt_id,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail in {
                "application_attempt_not_found",
                "application_attempt_not_draft",
                "draft_submit_gate_not_evaluated",
                "draft_target_not_opened",
                "guarded_submit_not_supported_for_site",
            }:
                raise HTTPException(status_code=404, detail=detail) from exc
            if detail in {
                "submit_gate_blocked",
                "guarded_submit_probe_failed",
                "guarded_submit_interaction_failed",
            }:
                raise HTTPException(status_code=409, detail=detail) from exc
            raise

    @app.get("/api/review-queue", response_model=list[ReviewQueueRead])
    def list_review_queue_endpoint(
        db: DbSession,
        status: str | None = None,
        entity_type: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[ReviewQueueRead]:
        """Return persisted manual review items."""

        return list_review_queue(db, status=status, entity_type=entity_type, limit=limit)

    @app.post("/api/review-queue/jobs/{job_id}/scores/{candidate_profile_slug}", response_model=ReviewQueueRead)
    def queue_score_review_endpoint(
        job_id: int,
        candidate_profile_slug: str,
        db: DbSession,
        reason: str | None = None,
    ) -> ReviewQueueRead:
        """Queue a candidate/job score for manual review."""

        try:
            return queue_score_review(
                db,
                job_id=job_id,
                candidate_profile_slug=candidate_profile_slug,
                reason=reason,
            )
        except ValueError as exc:
            if str(exc) == "job_score_not_found":
                raise HTTPException(status_code=404, detail="job_score_not_found") from exc
            raise

    @app.post("/api/review-queue/{review_id}/status/{status_value}", response_model=ReviewQueueRead)
    def update_review_queue_status(
        review_id: int,
        status_value: ReviewStatus,
        db: DbSession,
    ) -> ReviewQueueRead:
        """Update a review queue item status."""

        try:
            return set_review_status(db, review_id=review_id, status=status_value)
        except ValueError as exc:
            if str(exc) == "review_item_not_found":
                raise HTTPException(status_code=404, detail="review_item_not_found") from exc
            raise

    return app


def _render_inbox_page(
    *,
    jobs: list[InboxJobRow],
    candidate_profile_slug: str | None,
    filters: dict,
) -> str:
    """Render the main inbox list as HTML."""

    cards = "\n".join(_render_job_card(job, candidate_profile_slug) for job in jobs) or (
        "<div class='empty'>No jobs matched the current filters.</div>"
    )
    candidate_line = (
        f"<span class='candidate'>Candidate: {escape(candidate_profile_slug)}</span>"
        if candidate_profile_slug
        else "<span class='candidate'>Candidate: none selected</span>"
    )
    filter_line = " | ".join(
        f"{escape(key)}={escape(str(value))}"
        for key, value in filters.items()
        if value not in (None, "", False)
    ) or "default filters"

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>JobBot Inbox</title>
    <style>
      :root {{
        --bg: #f4f1ea;
        --panel: #fffdf8;
        --ink: #18221c;
        --muted: #5f6f64;
        --accent: #0c7c59;
        --accent-soft: #d9efe6;
        --warn: #c24d2c;
        --border: #d7d1c7;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Segoe UI", "Trebuchet MS", sans-serif;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, #fffaf1 0, #f4f1ea 42%),
          linear-gradient(135deg, #f4f1ea 0%, #ece6dc 100%);
      }}
      .shell {{
        max-width: 1100px;
        margin: 0 auto;
        padding: 28px 20px 48px;
      }}
      .hero {{
        background: linear-gradient(140deg, #173b2d 0%, #0c7c59 70%, #2d9b7d 100%);
        color: #f7fff9;
        border-radius: 24px;
        padding: 28px;
        box-shadow: 0 20px 45px rgba(12, 41, 33, 0.18);
      }}
      .hero h1 {{
        margin: 0 0 8px;
        font-size: 2.1rem;
        letter-spacing: 0.02em;
      }}
      .meta {{
        display: flex;
        gap: 14px;
        flex-wrap: wrap;
        margin-top: 10px;
        color: #dff7eb;
      }}
      .filters {{
        margin-top: 18px;
        color: #e4f5ed;
        font-size: 0.95rem;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 18px;
        margin-top: 24px;
      }}
      .card {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 20px;
        padding: 18px;
        box-shadow: 0 12px 30px rgba(46, 53, 47, 0.08);
      }}
      .card h2 {{
        margin: 0 0 8px;
        font-size: 1.15rem;
        line-height: 1.35;
      }}
      .company {{
        color: var(--muted);
        font-size: 0.95rem;
        margin-bottom: 12px;
      }}
      .badges {{
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-bottom: 12px;
      }}
      .badge {{
        padding: 5px 10px;
        border-radius: 999px;
        background: #efe8dc;
        color: #564c3d;
        font-size: 0.82rem;
      }}
      .score {{
        padding: 10px 12px;
        border-radius: 14px;
        background: var(--accent-soft);
        margin-bottom: 12px;
      }}
      .score.blocked {{
        background: #f8ddd4;
        color: var(--warn);
      }}
      .cta {{
        display: inline-block;
        margin-top: 8px;
        color: var(--accent);
        text-decoration: none;
        font-weight: 600;
      }}
      .empty {{
        padding: 28px;
        border-radius: 18px;
        background: var(--panel);
        border: 1px dashed var(--border);
        color: var(--muted);
      }}
      @media (max-width: 700px) {{
        .hero h1 {{ font-size: 1.7rem; }}
        .shell {{ padding: 20px 14px 36px; }}
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="hero">
        <h1>JobBot Inbox</h1>
        <div class="meta">
          <span>{len(jobs)} jobs in view</span>
          {candidate_line}
        </div>
        <div class="filters">Filters: {escape(filter_line)}</div>
      </section>
      <section class="grid">
        {cards}
      </section>
    </main>
  </body>
</html>"""


def _render_job_card(job: InboxJobRow, candidate_profile_slug: str | None) -> str:
    """Render a single inbox card."""

    score_summary = ""
    if job.score_summary:
        blocked = bool(job.score_summary.get("blocked"))
        blocked_class = " blocked" if blocked else ""
        reasons = ", ".join(job.score_summary.get("blocking_reasons", []))
        score_summary = (
            f"<div class='score{blocked_class}'>"
            f"<div>Score: {escape(str(job.score_summary.get('overall_score')))}"
            f" | Confidence: {escape(str(job.score_summary.get('confidence_score')))}</div>"
            f"<div>Blocked: {escape(str(job.score_summary.get('blocked')))}</div>"
            f"{f'<div>{escape(reasons)}</div>' if reasons else ''}"
            f"</div>"
        )

    prepared_summary = ""
    if job.prepared_summary:
        prepared_summary = (
            "<div class='score'>"
            f"<div>Prepared docs: {escape(str(job.prepared_summary.get('document_count')))}"
            f" | Answers: {escape(str(job.prepared_summary.get('answer_count')))}</div>"
            f"<div>All docs approved: {escape(str(job.prepared_summary.get('all_documents_approved')))}</div>"
            f"<div>Pending review: {escape(str(job.prepared_summary.get('pending_document_review')))}</div>"
            "</div>"
        )

    readiness_summary = ""
    if job.application_readiness:
        readiness_reasons = ", ".join(job.application_readiness.get("reasons", []))
        readiness_summary = (
            "<div class='score'>"
            f"<div>Apply readiness: {escape(str(job.application_readiness.get('state')))}</div>"
            f"<div>Ready: {escape(str(job.application_readiness.get('ready')))}</div>"
            f"{f'<div>{escape(readiness_reasons)}</div>' if readiness_reasons else ''}"
            "</div>"
        )

    execution_summary = ""
    if job.execution_summary:
        execution_summary = (
            "<div class='score'>"
            f"<div>Execution result: {escape(str(job.execution_summary.get('attempt_result') or 'pending'))}</div>"
            f"<div>Failure code: {escape(str(job.execution_summary.get('failure_code') or 'none'))}</div>"
            f"<div>Failure class: {escape(str(job.execution_summary.get('failure_classification') or 'none'))}</div>"
            f"<div>Submit confidence: {escape(str(job.execution_summary.get('submit_confidence')))}</div>"
            "</div>"
        )

    detail_url = f"/inbox/jobs/{job.job_id}"
    if candidate_profile_slug:
        detail_url += f"?candidate_profile_slug={escape(candidate_profile_slug)}"

    badges = "".join(
        f"<span class='badge'>{escape(value)}</span>"
        for value in [
            job.status,
            job.ats_vendor or "unknown-vendor",
            job.remote_type or "unspecified-location",
            f"{job.source_count} sources",
        ]
    )

    return (
        "<article class='card'>"
        f"<h2>{escape(job.title)}</h2>"
        f"<div class='company'>{escape(job.company_name or 'Unknown company')}</div>"
        f"<div class='badges'>{badges}</div>"
        f"{score_summary}"
        f"{prepared_summary}"
        f"{execution_summary}"
        f"{readiness_summary}"
        f"<div>{escape(job.location_normalized or 'location unavailable')}</div>"
        f"<a class='cta' href='{detail_url}'>Inspect job</a>"
        "</article>"
    )


def _render_job_detail_page(detail: InboxJobDetail) -> str:
    """Render a single job detail page."""

    score_block = ""
    if detail.score_summary:
        reasons = ", ".join(detail.score_summary.get("blocking_reasons", []))
        score_block = (
            "<section class='panel'>"
            "<h2>Score Summary</h2>"
            f"<p>Overall: {escape(str(detail.score_summary.get('overall_score')))}</p>"
            f"<p>Confidence: {escape(str(detail.score_summary.get('confidence_score')))}</p>"
            f"<p>Blocked: {escape(str(detail.score_summary.get('blocked')))}</p>"
            f"{f'<p>Reasons: {escape(reasons)}</p>' if reasons else ''}"
            "</section>"
        )

    prepared_block = ""
    if detail.prepared_summary:
        statuses = ", ".join(detail.prepared_summary.get("document_review_statuses", []))
        prepared_block = (
            "<section class='panel'>"
            "<h2>Prepared Outputs</h2>"
            f"<p>Documents: {escape(str(detail.prepared_summary.get('document_count')))}</p>"
            f"<p>Answers: {escape(str(detail.prepared_summary.get('answer_count')))}</p>"
            f"<p>Resume variant id: {escape(str(detail.prepared_summary.get('resume_variant_id')))}</p>"
            f"<p>Document review statuses: {escape(statuses)}</p>"
            "</section>"
        )

    readiness_block = ""
    if detail.application_readiness:
        reasons = ", ".join(detail.application_readiness.get("reasons", []))
        readiness_block = (
            "<section class='panel'>"
            "<h2>Application Readiness</h2>"
            f"<p>State: {escape(str(detail.application_readiness.get('state')))}</p>"
            f"<p>Ready: {escape(str(detail.application_readiness.get('ready')))}</p>"
            f"{f'<p>Reasons: {escape(reasons)}</p>' if reasons else ''}"
            "</section>"
        )

    execution_block = ""
    if detail.execution_summary:
        execution_block = (
            "<section class='panel'>"
            "<h2>Execution Summary</h2>"
            f"<p>Application state: {escape(str(detail.execution_summary.get('application_state')))}</p>"
            f"<p>Attempt result: {escape(str(detail.execution_summary.get('attempt_result') or 'pending'))}</p>"
            f"<p>Failure code: {escape(str(detail.execution_summary.get('failure_code') or 'none'))}</p>"
            f"<p>Failure class: {escape(str(detail.execution_summary.get('failure_classification') or 'none'))}</p>"
            f"<p>Submit confidence: {escape(str(detail.execution_summary.get('submit_confidence')))}</p>"
            f"<p>Notes: {escape(str(detail.execution_summary.get('notes') or ''))}</p>"
            "</section>"
        )

    sources = "".join(
        "<li>"
        f"<strong>{escape(source.source_type)}</strong> | {escape(source.source_url)}"
        f"{f'<br><small>{escape(str(source.metadata_json))}</small>' if source.metadata_json else ''}"
        "</li>"
        for source in detail.sources
    )

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(detail.title)} | JobBot</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", "Trebuchet MS", sans-serif;
        background: #f4f1ea;
        color: #18221c;
      }}
      .shell {{
        max-width: 900px;
        margin: 0 auto;
        padding: 28px 18px 40px;
      }}
      .panel {{
        background: #fffdf8;
        border: 1px solid #d7d1c7;
        border-radius: 20px;
        padding: 20px;
        margin-bottom: 18px;
      }}
      a {{
        color: #0c7c59;
        text-decoration: none;
      }}
      ul {{
        padding-left: 18px;
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="panel">
        <a href="/inbox">Back to inbox</a>
        <h1>{escape(detail.title)}</h1>
        <p>{escape(detail.company_name or 'Unknown company')}</p>
        <p>Status: {escape(detail.status)} | Vendor: {escape(detail.ats_vendor or 'unknown')}</p>
        <p>Location: {escape(detail.location_normalized or detail.location_raw or 'unknown')}</p>
        <p>Canonical URL: <a href="{escape(detail.canonical_url)}">{escape(detail.canonical_url)}</a></p>
      </section>
      {score_block}
      {prepared_block}
      {execution_block}
      {readiness_block}
      <section class="panel">
        <h2>Sources</h2>
        <ul>{sources}</ul>
      </section>
    </main>
  </body>
</html>"""


def _render_review_queue_page(reviews: list[ReviewQueueRead]) -> str:
    """Render the manual review queue as HTML."""

    items = "\n".join(
        (
            "<article class='card'>"
            f"<h2>Review #{review.id}</h2>"
            f"<div><strong>{escape(review.entity_type)}</strong> | entity {review.entity_id}</div>"
            f"<div>Reason: {escape(review.reason)}</div>"
            f"<div>Status: {escape(review.status)}</div>"
            f"<div>Confidence: {escape(str(review.confidence)) if review.confidence is not None else 'n/a'}</div>"
            f"{f'<pre>{escape(str(review.context))}</pre>' if review.context else ''}"
            "</article>"
        )
        for review in reviews
    ) or "<div class='empty'>No review items matched the current filters.</div>"

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>JobBot Review Queue</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", "Trebuchet MS", sans-serif;
        background: #f4f1ea;
        color: #18221c;
      }}
      .shell {{
        max-width: 980px;
        margin: 0 auto;
        padding: 28px 18px 40px;
      }}
      .hero {{
        background: linear-gradient(140deg, #3a2f4b 0%, #6b4f8c 70%, #9a7fc2 100%);
        color: #f9f4ff;
        border-radius: 24px;
        padding: 24px;
        margin-bottom: 18px;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 16px;
      }}
      .card {{
        background: #fffdf8;
        border: 1px solid #d7d1c7;
        border-radius: 18px;
        padding: 18px;
      }}
      pre {{
        white-space: pre-wrap;
        background: #f3ede3;
        border-radius: 12px;
        padding: 10px;
      }}
      .empty {{
        padding: 24px;
        border-radius: 18px;
        background: #fffdf8;
        border: 1px dashed #d7d1c7;
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="hero">
        <h1>JobBot Review Queue</h1>
        <div>{len(reviews)} items currently tracked</div>
      </section>
      <section class="grid">{items}</section>
    </main>
  </body>
</html>"""


def _render_execution_overview_page(
    *,
    rows: list[DraftExecutionOverviewRead],
    candidate_profile_slug: str,
    blocked_only: bool,
) -> str:
    """Render the focused draft-execution operations view."""

    card_items: list[str] = []
    for row in rows:
        reasons_block = ""
        if row.reasons:
            reasons_block = f"<div class='reasons'>Reasons: {escape(', '.join(row.reasons))}</div>"
        evidence_links: list[str] = []
        if row.latest_artifact_route and row.latest_artifact_label:
            evidence_links.append(
                f"<a href='{escape(row.latest_artifact_route)}'>{escape(row.latest_artifact_label)}</a>"
            )
        if row.visual_evidence_route and row.visual_evidence_label:
            evidence_links.append(
                f"<a href='{escape(row.visual_evidence_route)}'>{escape(row.visual_evidence_label)}</a>"
            )
        evidence_block = ""
        if evidence_links:
            evidence_block = f"<div class='status'>{' | '.join(evidence_links)}</div>"
        card_items.append(
            "<article class='card'>"
            f"<h2>{escape(row.job_title)}</h2>"
            f"<div class='company'>{escape(row.company_name or 'Unknown company')}</div>"
            f"<div class='meta'>Attempt #{row.attempt_id} | Job #{row.job_id}</div>"
            f"<div class='meta'>Vendor: {escape(row.site_vendor or 'unknown')} | "
            f"Browser: {escape(row.browser_profile_key or 'none')}</div>"
            f"<div class='status'>Application: {escape(row.application_state)} | "
            f"Readiness: {escape(row.readiness_state)}</div>"
            f"<div class='status'>Result: {escape(str(row.attempt_result or 'pending'))} | "
            f"Failure: {escape(str(row.failure_code or 'none'))}</div>"
            f"<div class='status'>Failure class: {escape(str(row.failure_classification or 'none'))}</div>"
            f"<div class='status'>Confidence: {escape(str(row.submit_confidence))} | "
            f"Session: {escape(str(row.session_health or 'unknown'))}</div>"
            f"<div class='status'>Submit interaction: {escape(str(row.submit_interaction_status or 'none'))} | "
            f"Mode: {escape(str(row.submit_interaction_mode or 'none'))} | "
            f"Clicked: {escape(str(row.submit_interaction_clicked))} | "
            f"Selector: {escape(str(row.submit_interaction_selector or 'none'))} | "
            f"Confirmations: {escape(str(row.submit_interaction_confirmation_count))}</div>"
            f"<div class='status'>Latest stage: {escape(str(row.latest_event_type or 'none'))}</div>"
            f"<div class='status'>Artifacts: {row.artifact_count} total | "
            f"HTML {row.html_snapshot_count} | Model IO {row.model_io_count} | "
            f"Docs {row.generated_document_count} | Answer packs {row.answer_pack_count}</div>"
            f"<div class='status'><a href='{escape(row.primary_action_route)}'>{escape(row.primary_action_label)}</a> | "
            f"<a href='{escape(row.attempt_route)}'>Inspect attempt</a> | "
            f"<a href='{escape(row.replay_route)}'>Replay bundle</a></div>"
            f"{evidence_block}"
            f"{reasons_block}"
            "</article>"
        )
    cards = "\n".join(card_items) or "<div class='empty'>No execution attempts matched the current filter.</div>"

    subtitle = "blocked only" if blocked_only else "all draft attempts"
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>JobBot Execution Overview</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", "Trebuchet MS", sans-serif;
        background: #f4f1ea;
        color: #18221c;
      }}
      .shell {{
        max-width: 1100px;
        margin: 0 auto;
        padding: 28px 18px 40px;
      }}
      .hero {{
        background: linear-gradient(140deg, #3a2c1b 0%, #8c5d22 75%, #d29b52 100%);
        color: #fff7ea;
        border-radius: 24px;
        padding: 24px;
        margin-bottom: 18px;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 16px;
      }}
      .card {{
        background: #fffdf8;
        border: 1px solid #d7d1c7;
        border-radius: 18px;
        padding: 18px;
      }}
      .company, .meta, .status, .reasons {{
        margin-top: 8px;
      }}
      .empty {{
        padding: 24px;
        border-radius: 18px;
        background: #fffdf8;
        border: 1px dashed #d7d1c7;
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="hero">
        <h1>Execution Overview</h1>
        <div>Candidate: {escape(candidate_profile_slug)} | Mode: {escape(subtitle)} | Rows: {len(rows)}</div>
      </section>
      <section class="grid">{cards}</section>
    </main>
  </body>
</html>"""


def _render_execution_dashboard_page(detail: DraftExecutionDashboardRead) -> str:
    """Render a candidate-scoped execution dashboard page."""

    metric_cards = "\n".join(
        [
            f"<article class='card'><h2>Total</h2><div>{detail.total_attempts}</div></article>",
            f"<article class='card'><h2>Blocked</h2><div>{detail.blocked_attempts}</div></article>",
            f"<article class='card'><h2>Manual-Review Blocked</h2><div>{detail.manual_review_blocked_attempts}</div></article>",
            f"<article class='card'><h2>Pending</h2><div>{detail.pending_attempts}</div></article>",
            f"<article class='card'><h2>Review State</h2><div>{detail.review_state_attempts}</div></article>",
            f"<article class='card'><h2>Replay Ready</h2><div>{detail.replay_ready_attempts}</div></article>",
        ]
    )

    def _dashboard_evidence_link(row: DraftExecutionOverviewRead) -> str:
        if row.visual_evidence_route and row.visual_evidence_label:
            return f" | <a href='{escape(row.visual_evidence_route)}'>{escape(row.visual_evidence_label)}</a>"
        return ""

    recent_html = "\n".join(
        (
            "<li>"
            f"Attempt #{row.attempt_id} | {escape(row.job_title)} | {escape(str(row.attempt_result or 'pending'))}"
            f" | <a href='{escape(row.primary_action_route)}'>{escape(row.primary_action_label)}</a>"
            f" | <a href='{escape(row.replay_route)}'>replay</a>"
            f"{_dashboard_evidence_link(row)}"
            "</li>"
        )
        for row in detail.recent_attempts
    ) or "<li>No execution attempts recorded.</li>"

    blocked_html = "\n".join(
        (
            "<li>"
            f"Attempt #{row.attempt_id} | {escape(row.failure_code or 'none')} "
            f"({escape(row.failure_classification or 'none')}) | {escape(row.job_title)}"
            f" | <a href='{escape(row.primary_action_route)}'>{escape(row.primary_action_label)}</a>"
            f" | <a href='{escape(row.attempt_route)}'>attempt</a>"
            f"{_dashboard_evidence_link(row)}"
            "</li>"
        )
        for row in detail.blocked_recent_attempts
    ) or "<li>No blocked attempts recorded.</li>"

    blocked_failure_html = "\n".join(
        f"<li>{escape(code)}: {count}</li>"
        for code, count in sorted(
            detail.blocked_failure_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ) or "<li>No blocked failures recorded.</li>"
    blocked_failure_classification_html = "\n".join(
        f"<li>{escape(classification)}: {count}</li>"
        for classification, count in sorted(
            detail.blocked_failure_classification_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ) or "<li>No blocked classifications recorded.</li>"

    actions_html = "\n".join(f"<li>{escape(action)}</li>" for action in detail.recommended_actions)

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>JobBot Execution Dashboard</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", "Trebuchet MS", sans-serif;
        background: #f4f1ea;
        color: #18221c;
      }}
      .shell {{
        max-width: 1100px;
        margin: 0 auto;
        padding: 28px 18px 40px;
      }}
      .hero {{
        background: linear-gradient(140deg, #18323b 0%, #1d5968 70%, #67a6a5 100%);
        color: #eef8f7;
        border-radius: 24px;
        padding: 24px;
        margin-bottom: 18px;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 16px;
        margin-bottom: 18px;
      }}
      .card, .panel {{
        background: #fffdf8;
        border: 1px solid #d7d1c7;
        border-radius: 18px;
        padding: 18px;
      }}
      ul {{
        padding-left: 18px;
      }}
      a {{
        color: #0c7c59;
        text-decoration: none;
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="hero">
        <h1>Execution Dashboard</h1>
        <div>Candidate: {escape(detail.candidate_profile_slug)}</div>
        <div><a href="/execution/overview/{escape(detail.candidate_profile_slug)}">Open execution overview</a></div>
      </section>
      <section class="grid">{metric_cards}</section>
      <section class="panel">
        <h2>Blocked Attempts</h2>
        <ul>{blocked_html}</ul>
      </section>
      <section class="panel">
        <h2>Blocked Failure Breakdown</h2>
        <ul>{blocked_failure_html}</ul>
      </section>
      <section class="panel">
        <h2>Blocked Failure Classification Breakdown</h2>
        <ul>{blocked_failure_classification_html}</ul>
      </section>
      <section class="panel">
        <h2>Recent Attempts</h2>
        <ul>{recent_html}</ul>
      </section>
      <section class="panel">
        <h2>Recommended Actions</h2>
        <ul>{actions_html}</ul>
      </section>
    </main>
  </body>
</html>"""


def _render_execution_attempt_detail_page(detail: DraftExecutionAttemptDetailRead) -> str:
    """Render one execution attempt detail page with ordered events and artifacts."""

    def _event_artifact_links(event: DraftExecutionEventRead) -> str:
        if not event.artifact_routes:
            return ""
        links = " | ".join(
            f"<a href='{escape(route)}'>artifact</a>"
            for route in event.artifact_routes
        )
        return f"<br><small>{links}</small>"

    events_html = "\n".join(
        (
            "<li>"
            f"<strong>{escape(event.event_type)}</strong> | {escape(event.created_at.isoformat())}"
            f"<br>{escape(event.message)}"
            f"{_event_artifact_links(event)}"
            f"{f'<br><small>{escape(str(event.payload))}</small>' if event.payload else ''}"
            "</li>"
        )
        for event in detail.events
    ) or "<li>No execution events recorded.</li>"

    artifacts_html = "\n".join(
        (
            "<li>"
            f"<strong>{escape(artifact.artifact_type)}</strong> | {escape(artifact.path)}"
            f"<br><small>size={escape(str(artifact.size_bytes))} created={escape(artifact.created_at.isoformat())}</small>"
            f"<br><a href='{escape(artifact.inspect_route)}'>Inspect artifact</a>"
            f" | <a href='{escape(str(artifact.launch_route or '#'))}'>{escape(str(artifact.launch_label or _artifact_launch_label(artifact.artifact_type)))}</a>"
            f" | <a href='{escape(str(artifact.raw_route or '#'))}'>Open raw file</a>"
            "</li>"
        )
        for artifact in detail.artifacts
    ) or "<li>No artifacts recorded.</li>"

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>JobBot Execution Attempt</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", "Trebuchet MS", sans-serif;
        background: #f4f1ea;
        color: #18221c;
      }}
      .shell {{
        max-width: 1000px;
        margin: 0 auto;
        padding: 28px 18px 40px;
      }}
      .panel {{
        background: #fffdf8;
        border: 1px solid #d7d1c7;
        border-radius: 20px;
        padding: 20px;
        margin-bottom: 18px;
      }}
      ul {{
        padding-left: 18px;
      }}
      a {{
        color: #0c7c59;
        text-decoration: none;
      }}
      small {{
        color: #5f6f64;
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="panel">
        <a href="/execution/overview/{escape(detail.candidate_profile_slug)}">Back to execution overview</a>
        <h1>{escape(detail.job_title)}</h1>
        <p>Attempt #{detail.attempt_id} | Job #{detail.job_id} | Candidate {escape(detail.candidate_profile_slug)}</p>
        <p>Application state: {escape(detail.application_state)} | Result: {escape(str(detail.attempt_result or 'pending'))}</p>
        <p>Failure: {escape(str(detail.failure_code or 'none'))} | Confidence: {escape(str(detail.submit_confidence))}</p>
        <p>Failure class: {escape(str(detail.failure_classification or 'none'))}</p>
        <p>Browser: {escape(str(detail.browser_profile_key or 'none'))} | Session: {escape(str(detail.session_health or 'unknown'))}</p>
        <p>Notes: {escape(str(detail.notes or ''))}</p>
        <p><a href="/execution/replay/{detail.attempt_id}">Open replay bundle</a></p>
      </section>
      <section class="panel">
        <h2>Submit-Stage Diagnostics</h2>
        <p>Interaction mode: {escape(str(detail.submit_interaction_mode or 'none'))}</p>
        <p>Interaction status: {escape(str(detail.submit_interaction_status or 'none'))}</p>
        <p>Submit clicked: {escape(str(detail.submit_interaction_clicked))}</p>
        <p>Clicked selector: {escape(str(detail.submit_interaction_selector or 'none'))}</p>
        <p>Confirmation markers matched: {escape(str(detail.submit_interaction_confirmation_count))}</p>
      </section>
      <section class="panel">
        <h2>Execution Events</h2>
        <ul>{events_html}</ul>
      </section>
      <section class="panel">
        <h2>Execution Artifacts</h2>
        <ul>{artifacts_html}</ul>
      </section>
    </main>
  </body>
</html>"""


def _render_execution_replay_bundle_page(detail: DraftExecutionReplayBundleRead) -> str:
    """Render one replay-oriented execution bundle page."""

    asset_items: list[str] = []
    for asset in detail.assets:
        inspect_link = (
            f"<br><a href='/execution/artifacts/{asset.artifact_id}'>Inspect artifact</a>"
            if asset.artifact_id is not None
            else ""
        )
        raw_link = (
            f"<br><a href='{escape(asset.raw_route)}'>Open raw asset</a>"
            if asset.raw_route is not None
            else ""
        )
        launch_link = (
            f"<br><a href='{escape(asset.launch_route)}'>{escape(asset.launch_label or 'Open asset')}</a>"
            if asset.launch_route is not None
            else ""
        )
        asset_items.append(
            "<li>"
            f"<strong>{escape(asset.label)}</strong> | {escape(str(asset.artifact_type or 'n/a'))}"
            f"<br>{escape(str(asset.path or 'missing'))}"
            f"<br><small>exists={escape(str(asset.exists))} artifact_id={escape(str(asset.artifact_id))} "
            f"openable={escape(str(asset.openable_locally))} hint={escape(str(asset.open_hint or 'none'))}</small>"
            f"{inspect_link}"
            f"{launch_link}"
            f"{raw_link}"
            "</li>"
        )
    assets_html = "\n".join(asset_items) or "<li>No replay assets recorded.</li>"

    actions_html = "\n".join(f"<li>{escape(action)}</li>" for action in detail.recommended_actions)

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>JobBot Execution Replay Bundle</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", "Trebuchet MS", sans-serif;
        background: #f4f1ea;
        color: #18221c;
      }}
      .shell {{
        max-width: 1000px;
        margin: 0 auto;
        padding: 28px 18px 40px;
      }}
      .panel {{
        background: #fffdf8;
        border: 1px solid #d7d1c7;
        border-radius: 20px;
        padding: 20px;
        margin-bottom: 18px;
      }}
      ul {{
        padding-left: 18px;
      }}
      a {{
        color: #0c7c59;
        text-decoration: none;
      }}
      small {{
        color: #5f6f64;
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="panel">
        <a href="/execution/attempts/{detail.attempt_id}">Back to execution attempt</a>
        <h1>Replay Bundle</h1>
        <p>Attempt #{detail.attempt_id} | Job #{detail.job_id} | Candidate {escape(detail.candidate_profile_slug)}</p>
        <p>Title: {escape(detail.job_title)} | Company: {escape(detail.company_name or 'Unknown company')}</p>
        <p>State: {escape(detail.application_state)} | Result: {escape(str(detail.attempt_result or 'pending'))}</p>
        <p>Failure: {escape(str(detail.failure_code or 'none'))} | Latest event: {escape(str(detail.latest_event_type or 'none'))}</p>
        <p>Target URL: {escape(str(detail.target_url or 'unknown'))}</p>
        <p>Startup dir: {escape(str(detail.startup_dir or 'unknown'))}</p>
      </section>
      <section class="panel">
        <h2>Replay Assets</h2>
        <ul>{assets_html}</ul>
      </section>
      <section class="panel">
        <h2>Recommended Actions</h2>
        <ul>{actions_html}</ul>
      </section>
    </main>
  </body>
</html>"""


def _render_execution_artifact_detail_page(detail: DraftExecutionArtifactDetailRead) -> str:
    """Render one execution artifact detail page with a bounded preview."""

    if detail.preview_kind == "binary_image" and detail.raw_route is not None:
        preview_block = (
            f"<figure><img src=\"{escape(detail.raw_route)}\" "
            "alt=\"Execution artifact preview\" style=\"max-width: 100%; border-radius: 14px; "
            "border: 1px solid #ddd4c8; background: #f6f2ea;\"></figure>"
        )
    elif detail.preview_kind == "html" and detail.raw_route is not None:
        preview_block = (
            f"<iframe src=\"{escape(detail.raw_route)}\" title=\"Execution HTML preview\" "
            "style=\"width: 100%; min-height: 480px; border: 1px solid #ddd4c8; "
            "border-radius: 14px; background: white;\"></iframe>"
        )
    else:
        preview_block = (
            "<p>No preview available for this artifact type.</p>"
            if detail.preview_text is None
            else f"<pre>{escape(detail.preview_text)}</pre>"
        )
    truncated_note = (
        "<p><small>Preview truncated for safety.</small></p>" if detail.preview_truncated else ""
    )

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>JobBot Execution Artifact</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", "Trebuchet MS", sans-serif;
        background: #f4f1ea;
        color: #18221c;
      }}
      .shell {{
        max-width: 1000px;
        margin: 0 auto;
        padding: 28px 18px 40px;
      }}
      .panel {{
        background: #fffdf8;
        border: 1px solid #d7d1c7;
        border-radius: 20px;
        padding: 20px;
        margin-bottom: 18px;
      }}
      a {{
        color: #0c7c59;
        text-decoration: none;
      }}
      pre {{
        white-space: pre-wrap;
        word-break: break-word;
        background: #f6f2ea;
        border-radius: 14px;
        border: 1px solid #ddd4c8;
        padding: 14px;
        overflow-x: auto;
      }}
      small {{
        color: #5f6f64;
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="panel">
        <a href="/execution/attempts/{detail.attempt_id}">Back to execution attempt</a>
        <h1>Artifact #{detail.artifact_id}</h1>
        <p>Attempt: {escape(str(detail.attempt_id))} | Type: {escape(detail.artifact_type)}</p>
        <p>Path: {escape(detail.path)}</p>
        <p>Exists: {escape(str(detail.exists))} | Preview: {escape(detail.preview_kind)}</p>
        <p>Size: {escape(str(detail.size_bytes))} | Created: {escape(detail.created_at.isoformat())}</p>
        <p><a href="{escape(str(detail.launch_route or '#'))}">{escape(str(detail.launch_label or 'Open file'))}</a> | <a href="{escape(str(detail.raw_route or '#'))}">Open raw file</a></p>
      </section>
      <section class="panel">
        <h2>Preview</h2>
        {truncated_note}
        {preview_block}
      </section>
    </main>
  </body>
</html>"""


def _artifact_launch_label(artifact_type: str) -> str:
    """Map an artifact type string to a user-facing launch action label."""

    lowered = artifact_type.lower()
    if lowered == "screenshot":
        return "View image"
    if lowered == "trace":
        return "Download trace"
    if lowered == "html_snapshot":
        return "Open HTML"
    if lowered in {"model_io", "answer_pack"}:
        return "Open text"
    return "Open file"


app = create_app()
