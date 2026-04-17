# Phase 0: Foundation

## Goal
Establish the operational and architectural base for the new bot so later discovery, enrichment, tailoring, and execution work can land without structural rework.

## Deliverables
- Python package scaffold
- centralized settings/config
- initial SQLAlchemy schema
- Alembic migration baseline
- candidate fact provenance model
- progress tracking and ADR structure
- artifact and prompt versioning conventions

## Checklist
- [x] Create package layout
- [x] Add project metadata and dependencies
- [x] Add application settings model
- [x] Add initial database models
- [x] Add initial Alembic migration scaffold
- [x] Add authoritative candidate fact storage for provenance
- [x] Add candidate profile ingestion flow
- [x] Reduce SQLite enum migration risk for volatile state fields
- [x] Add browser profile registry baseline
- [x] Add session health checker behavior
- [x] Add explicit browser profile readiness and re-auth policy boundaries
- [x] Add fixture corpus baseline for ATS/source adapters
- [x] Add baseline test suite

## Acceptance Criteria
- Code imports cleanly.
- Initial tables exist for jobs, sources, applications, attempts, answers, field mappings, artifacts, model calls, and review queue.
- Candidate fact provenance is modeled with stable fact IDs rather than only a profile JSON blob.
- App directories and local storage conventions are defined.
- Future work can proceed without redesigning the package layout.

## Dependencies
- `FINAL_JOB_BOT_PRD.md`

## Status
- `complete`
