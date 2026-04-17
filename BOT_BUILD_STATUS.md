# Job Bot Build Status

## Current State
- Phase: `Phase 4 - Controlled Application Execution`
- Overall status: `in_progress`
- Implementation mode: `local-first, deterministic-first`
- Primary spec: `FINAL_JOB_BOT_PRD.md`

## Completed
- Created persistent roadmap and ADR structure.
- Created initial Python package scaffold under `src/jobbot/`.
- Added initial application settings, domain enums, database models, and session helpers.
- Added Alembic migration scaffold with an initial foundation migration.
- Replaced blob-only candidate fact storage with authoritative `candidate_facts` records for provenance.
- Switched model timestamps to timezone-aware UTC generation.
- Added candidate profile ingestion schemas and service with slug generation and authoritative fact import.
- Added CLI commands for local schema bootstrap and candidate profile import.
- Moved volatile workflow and review states to validated string columns to reduce SQLite enum migration friction.
- Added browser profile registry persistence and service layer.
- Added CLI commands to register browser profiles, list them, and update session health.
- Added deterministic browser session-health evaluation and persistence of validation details.
- Added baseline tests for candidate profile import and browser profile/session services.
- Added explicit browser readiness policy helpers and re-auth-required decision boundaries.
- Added CLI readiness inspection for browser profiles.
- Added browser profile lifecycle coverage for touch/last-used, checkpoint, and quarantine policy paths.
- Added a CLI command to mark browser profiles as recently used.
- Added canonical discovery contracts and deterministic normalization helpers.
- Added initial Greenhouse discovery adapter skeleton with fixture-backed parsing tests.
- Added discovery fixture corpus baseline under `fixtures/discovery/`.
- Added `JobIngestionService`-style persistence bridge from discovery batches into `jobs` and `job_sources`.
- Added fixture-backed ingestion tests for insert, canonical-URL dedup, and fingerprint-based source attachment.
- Added Lever discovery adapter skeleton with fixture-backed parsing tests.
- Added Workday discovery adapter skeleton with fixture-backed parsing tests.
- Added a first custom-site adapter for Microsoft careers based on the new `careers/` reference patterns.
- Added a second custom-site adapter for Meta careers based on the new `careers/` reference patterns.
- Added a third custom-site adapter for Google careers results pages based on the new `careers/` reference patterns.
- Strengthened location normalization with deterministic alias handling for common duplicate variants.
- Expanded location normalization to handle common US and Canada region abbreviations deterministically.
- Added an inbox read-model baseline and CLI job listing on top of persisted discovered jobs.
- Added a first FastAPI HTTP surface with `/health` and `/api/jobs`.
- Added per-job inbox detail reads with source provenance via `/api/jobs/{job_id}`.
- Added inbox filtering by `status`, `ats_vendor`, and `remote_type` across CLI and HTTP reads.
- Added inbox pagination and sorting controls across CLI and HTTP reads.
- Added persisted source metadata on `job_sources` so discovery provenance can be reused without refetching.
- Split display titles from normalized titles and made discovery refresh additive instead of lossy.
- Added deterministic enrichment contracts and a first `enrich-job` service/CLI path.
- Added structured extraction from known ATS/custom-site metadata before text fallback.
- Added deterministic scoring persistence with explainable candidate/job score breakdowns.
- Added confidence scoring and blocking mismatch output to the deterministic scoring layer.
- Added candidate/job score reads to the HTTP API.
- Added optional score summaries to inbox list/detail reads for a chosen candidate profile.
- Added the first server-rendered inbox UI on top of the score-aware inbox reads.
- Added a manual review queue service for persisted candidate/job scores.
- Added review queue HTTP, HTML, and CLI surfaces for queueing, listing, and status updates.
- Added deterministic preparation persistence for generated resume variants and reusable answers.
- Added review queue creation for first-use inferred generated documents and answer pack entries.
- Added preparation read models and an HTTP endpoint for candidate/job prepared outputs.
- Added review-status writeback from manual review queue items into generated documents and answers.
- Added preparation summaries into inbox read models and HTML job inspection views.
- Added inbox/API triage support for preparation readiness filtering and sorting.
- Added deterministic application-readiness summaries built from scoring and preparation state.
- Added inbox/API/CLI triage support for application-readiness filtering and sorting.
- Added explicit ready-to-apply list/detail helpers across inbox read models, API, HTML, and CLI.
- Added persisted application-eligibility snapshots as a DB-backed handoff for later execution work.
- Added API and CLI helpers to materialize and list persisted eligibility records.
- Added the first execution-side consumer of persisted eligibility via draft application-attempt bootstrap.
- Added execution API and CLI helpers to bootstrap and list draft application attempts.
- Added staged draft-execution startup bundles with persisted startup artifacts for draft attempts.
- Added execution API and CLI helpers to start staged draft execution runs and record replay inputs.
- Added deterministic draft field-plan scaffolding backed by persisted `field_mappings`.
- Added execution API and CLI helpers to build replayable draft field-plan artifacts from staged attempts.
- Added the first site-aware execution overlay for Greenhouse with selector candidates and confidence gates.
- Added execution API and CLI helpers to build replayable site selector overlays from draft field plans.
- Added the first non-submitting Greenhouse target-open flow with resolved/manual field outcomes.
- Added execution API and CLI helpers to run target-open resolution passes on staged Greenhouse attempts.
- Added guarded submit-confidence evaluation for Greenhouse based on resolved field outcomes.
- Added execution API and CLI helpers to evaluate submit gates and emit explicit stop reasons.
- Added live HTTP target-page capture with deterministic fallback during Greenhouse target-open passes.
- Added target-open capture metadata (`capture_method`, `capture_error`) to execution read models for API/CLI visibility.
- Added durable guarded-execution persistence so submit-gate updates `submit_confidence`, blocked attempt result/failure code, and application review state.
- Added inbox/detail execution summaries so blocked guarded runs are visible from main API/HTML reads without replaying raw execution events.
- Added inbox/API/CLI execution-state filtering and sorting so blocked guarded runs are triageable from primary operational surfaces.
- Added a dedicated execution overview read model with API/HTML/CLI surfaces for draft attempts and blocked guarded runs.
- Added latest-stage and artifact-count visibility to execution overview rows so blocked attempts carry replay/debug evidence context.
- Added direct execution attempt drill-down surfaces with ordered events and artifact inventories across API/HTML/CLI.
- Added direct execution artifact drill-down surfaces with bounded safe previews across service/API/HTML/CLI.
- Added replay-oriented execution bundle surfaces across service/API/HTML/CLI so one attempt can be reconstructed from persisted startup, planning, resolution, and gate artifacts.
- Added open/inspect metadata to replay bundle assets so operators can tell which persisted artifacts are directly openable from local disk and which inspection route to use.
- Added a candidate-scoped execution dashboard across service/API/HTML/CLI for blocked, pending, review-state, and replay-ready draft execution triage.
- Added raw artifact and replay-asset file routes across service/API/HTML/CLI so persisted execution evidence can be opened directly instead of only previewed.
- Added overview/dashboard evidence jump routes so execution rows can link directly to latest artifacts and visual evidence without requiring a separate drill-down first.
- Added actionable route metadata onto execution attempt-detail artifact rows so inspect/raw/launch actions are available consistently across APIs and operator views.
- Added event-level artifact inspect routes onto execution attempt-detail event rows so operators can jump from an execution timeline event directly into the referenced persisted artifact.
- Added explicit execution `launch_target` metadata and image-aware launch handling so screenshot artifacts open into inspect/view pages while HTML/text/trace assets keep type-appropriate launch semantics.
- Added deterministic execution overview/dashboard filtering by `failure_code` and max submit confidence across service/API/HTML/CLI surfaces for targeted blocked-attempt triage.
- Added blocked-failure breakdown metrics (`blocked_failure_counts`, `manual_review_blocked_attempts`) to execution dashboard service/API/HTML/CLI surfaces for deterministic triage prioritization.
- Added `manual_review_only` filtering across execution overview/dashboard service/API/HTML/CLI surfaces so unresolved-manual-review failures can be isolated in one deterministic operations view.
- Added a repo-local `.venv` workflow for JobBot development and validation without relying on global Python packages.
- Added missing dev test dependency coverage (`httpx`) and repo-scoped `pytest` configuration so `pytest` targets JobBot tests instead of bundled comparison bots.
- Fixed draft execution startup artifact serialization for JSON-safe answer packs.
- Fixed guarded submit stop-reason classification so unresolved manual-review fields remain explicit `manual_review_required:*` blockers.
- Fixed execution bootstrap/dashboard state handling so blocked applications preserve `review` state across later draft attempts and dashboard review counts aggregate by application instead of double-counting attempts.
- Tightened deterministic enrichment/scoring rules for preferred-skill extraction and location mismatch detection.
- Brought the scoped JobBot test suite to green in `.venv` with `103 passed`.

## In Progress
- Hardening review queue semantics before generated documents and answer packs depend on them.
- Deciding where app-level validation should wrap DB string state columns.
- Extending enrichment from deterministic extraction into richer preparation inputs.
- Converting deterministic preparation outputs into explicit claim-level structures before LLM-assisted tailoring exists.
- Building out Phase 4 from draft attempt bootstrap into real browser-backed guarded execution.
- Upgrading execution capture from HTTP-backed target-open to full Playwright session capture and trace/screenshot artifacts.
- Extending execution checkpoint state into broader operational filters and dashboards beyond inbox/detail reads.
- Extending the replay bundle into direct artifact opening and browser replay actions beyond bounded preview reads.
- Converting the new green `.venv` test workflow into routine validation for each major Phase 4 iteration.
- Investigating a local Windows temp-directory ACL issue that is currently breaking fresh pytest temp-root cleanup despite the repo code remaining importable and manually verifiable.

## Blocked
- None currently.

## Next Tasks
1. Decide whether review approval/rejection should automatically rematerialize eligibility or if that should stay explicit.
2. Add Playwright-backed page-open startup with real session capture on top of the current HTTP-backed Greenhouse target-open flow.
3. Convert the current Greenhouse target-open and submit-gate layers into a real guarded submit handler with attempt-level screenshots/traces.
4. Expand screenshot/trace launch handling from inspect/download routing into richer browser-assisted replay actions.
5. Add Playwright-specific tests once browser-backed execution replaces the current HTTP/stub target-open flow.
6. Promote the replay bundle and dashboard into a broader multi-ATS execution control center once multiple handlers exist.

## Decisions
- New implementation lives in `src/jobbot/` instead of modifying existing bot repos.
- `FastAPI + SQLAlchemy + Alembic + Pydantic Settings` is the Phase 0 base stack.
- Browser execution will target native Playwright flows, not a proprietary external agent CLI.
- Progress tracking is kept in repo via `BOT_BUILD_STATUS.md`, `docs/roadmap/`, and `docs/adr/`.
- Volatile workflow states are stored as strings in SQLite; more stable classifications can remain DB enums.

## Open Questions
- Final product/repo-facing name beyond package name `jobbot`.
- Whether LinkedIn discovery should enter Phase 1 immediately or remain secondary to ATS adapters.
- Whether artifacts should be attempt-addressed or content-addressed first.
- Whether application state and review status should stay as DB enums or move to validated string columns before more migrations land.

## Definition of Done For Phase 0
- Project scaffold exists and imports cleanly.
- Settings and app directories are centralized.
- Initial schema is modeled and migrated.
- Prompt/versioning and artifact conventions are documented.
- PRD phases are represented in roadmap docs with acceptance criteria.
