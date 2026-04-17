# Phase 2: Enrichment and Scoring

## Goal
Extract structured job requirements and compute explainable fit scores.

## Deliverables
- structured extraction cascade
- deterministic parsers
- LLM extraction fallback
- multi-score fit engine
- score explanations

## Checklist
- [x] Add enrichment pipeline contracts
- [x] Add structured extraction for JSON-LD and known ATS payloads
- [x] Add deterministic parser fallback
- [ ] Add LLM fallback with prompt versioning
- [x] Add qualification/location/compensation/seniority scoring
- [x] Add confidence scoring and blocking mismatch output

## Acceptance Criteria
- Jobs can be enriched into structured requirements.
- Scoring output is explainable and auditable.

## Status
- `in_progress`
