# Phase 1: Discovery MVP

## Goal
Discover jobs from ATS and targeted sources, normalize them, and land them in a deduplicated inbox.

## Deliverables
- Greenhouse discovery adapter
- Lever discovery adapter
- Workday discovery adapter
- first custom-site discovery adapter
- second custom-site discovery adapter
- third custom-site discovery adapter
- canonical job normalization
- deduplication pipeline
- inbox view/API

## Checklist
- [x] Add canonical job ingestion service
- [x] Implement Greenhouse listing adapter skeleton
- [x] Implement Lever listing adapter skeleton
- [x] Implement Workday listing adapter
- [x] Implement first custom-site listing adapter
- [x] Implement second custom-site listing adapter
- [x] Implement third custom-site listing adapter
- [x] Implement source provenance tracking baseline
- [x] Implement exact and fingerprint-based deduplication baseline
- [x] Add inbox read path baseline
- [x] Add inbox HTTP endpoint baseline
- [x] Add inbox UI

## Acceptance Criteria
- New jobs can be discovered and stored with provenance.
- Exact duplicates are blocked.
- Same job from multiple sources maps to a single canonical record.

## Status
- `in_progress`
