# Phase 3: Tailoring

## Goal
Generate fact-grounded resume variants, answers, and optional cover letters with explicit truth-tier provenance.

## Deliverables
- candidate knowledge model ingestion
- truth-tier claim engine
- resume variant generation
- reusable answer pack generation
- review queue integration
- preparation read surfaces

## Checklist
- [x] Add candidate fact store and provenance linking
- [ ] Add tier classification engine
- [x] Add Tier 2 first-use review flow
- [ ] Add Tier 3 proposal and interview-prep generation
- [x] Add generated document persistence
- [x] Add answer pack reuse and approval tracking
- [x] Add preparation read surfaces for documents and answers

## Acceptance Criteria
- Every generated claim is linked to source facts and a truth tier.
- Tier 3 content is blocked from auto-submit.

## Status
- `in_progress`
