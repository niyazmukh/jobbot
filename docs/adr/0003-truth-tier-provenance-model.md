# ADR 0003: Truth-Tier Provenance Model

## Status
Accepted

## Context
The PRD makes fact preservation central and requires generated claims to be auditable, tiered, and reviewable.

## Decision
Model generated content and reusable answers with explicit truth tiers, provenance metadata, approval state, and interview-prep support for Tier 3 extensions.

## Consequences
- Tailoring logic must operate on structured candidate facts, not only free-form resume text.
- Review queue and answer persistence are foundational, not optional add-ons.
- Auto-submit gates depend on tier metadata from the start.
