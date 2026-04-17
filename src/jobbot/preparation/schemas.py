"""Schemas for deterministic preparation outputs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PreparedClaim(BaseModel):
    """A single generated claim with provenance."""

    model_config = ConfigDict(extra="forbid")

    text: str
    truth_tier: str
    provenance_facts: list[str] = Field(default_factory=list)


class PreparedAnswerPlan(BaseModel):
    """Deterministic answer content before persistence."""

    model_config = ConfigDict(extra="forbid")

    question: str
    answer_text: str
    truth_tier: str
    provenance_facts: list[str] = Field(default_factory=list)
    interview_prep_notes: str | None = None


class PreparedJobSummary(BaseModel):
    """Summary of persisted preparation outputs for a candidate/job pair."""

    model_config = ConfigDict(extra="forbid")

    job_id: int
    candidate_profile_slug: str
    resume_variant_id: int
    generated_document_ids: list[int]
    answer_ids: list[int]
    queued_review_ids: list[int]
