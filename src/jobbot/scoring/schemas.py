"""Schemas for explainable deterministic scoring."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JobScoreResult(BaseModel):
    """Explainable candidate-specific fit score."""

    model_config = ConfigDict(extra="forbid")

    overall_score: float
    skill_score: float
    location_score: float
    seniority_score: float
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    matched_location_preferences: list[str] = Field(default_factory=list)
    seniority_matches: list[str] = Field(default_factory=list)
    confidence_score: float
    blocked: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    explanations: list[str] = Field(default_factory=list)
    scoring_method: str = "deterministic_rules_v1"


class JobScoreRead(BaseModel):
    """Read model for a persisted candidate/job score."""

    model_config = ConfigDict(extra="forbid")

    job_id: int
    candidate_profile_slug: str
    overall_score: float
    score_json: dict
