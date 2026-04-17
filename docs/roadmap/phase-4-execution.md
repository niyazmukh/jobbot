# Phase 4: Controlled Application Execution

## Goal
Execute applications safely on deterministic ATS flows with explicit confidence gates and replay artifacts.

## Deliverables
- Playwright browser manager
- deterministic field handlers
- guarded submit flow
- attempt tracking
- replay/debug artifact capture
- draft attempt bootstrap from persisted execution eligibility
- staged draft execution startup bundle with replay inputs
- deterministic draft field-plan bundle backed by persisted field mappings
- site-aware selector overlays with confidence gates for the first ATS handlers
- non-submitting target-open and field-resolution flow for Greenhouse
- guarded submit-confidence evaluation with explicit stop reasons
- live HTTP target-page capture with deterministic fallback during Greenhouse target-open
- durable attempt/application checkpoint updates from guarded submit evaluation
- inbox/detail execution summaries for blocked guarded runs
- inbox/API/CLI execution-state filtering and sorting for blocked guarded runs
- dedicated execution overview read model with API/HTML/CLI surfaces
- latest-stage and artifact-count visibility in execution overview rows
- direct attempt drill-down surfaces with ordered events and artifact inventories
- direct artifact drill-down surfaces with bounded safe previews
- replay-oriented execution bundle surfaces across service/API/HTML/CLI
- actionable open/inspect metadata on replay bundle assets
- candidate-scoped execution dashboard across service/API/HTML/CLI
- raw artifact and replay-asset file routes across service/API/HTML/CLI
- overview/dashboard evidence jump routes for latest artifacts and visual evidence
- actionable route metadata on execution attempt-detail artifact rows
- event-level artifact inspect routes on execution attempt-detail event rows
- explicit launch-target metadata with image-aware screenshot launch handling
- deterministic failure-code and submit-confidence filters on execution overview/dashboard surfaces
- review-state preservation across later draft attempts so blocked applications stay triageable
- green repo-scoped JobBot validation pass in `.venv` (`pytest`: 101 passed)

## Checklist
- [x] Add browser profile registry
- [~] Add Greenhouse apply handler
- [~] Add browser-backed page-open and field-resolution flow for Greenhouse
- [ ] Add Lever apply handler
- [x] Add typed field mapping plan
- [x] Add submit confidence gates
- [ ] Add screenshots/traces/snapshots per attempt
- [x] Add staged startup bundle for draft attempts with artifact capture
- [x] Add draft attempt bootstrap surface backed by persisted eligibility
- [x] Add first site-aware selector overlay for Greenhouse
- [x] Add candidate-scoped execution dashboard for blocked/pending/replay-ready triage

## Acceptance Criteria
- Stable ATS flows can be executed in draft and guarded-submit modes.
- Failed attempts are replayable with artifacts.

## Status
- `in_progress`
