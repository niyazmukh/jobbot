"""Schemas for deterministic job enrichment."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EnrichedRequirements(BaseModel):
    """Deterministic structured extraction from a job description."""

    model_config = ConfigDict(extra="forbid")

    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    required_years_experience: int | None = None
    seniority_signals: list[str] = Field(default_factory=list)
    education_signals: list[str] = Field(default_factory=list)
    domain_signals: list[str] = Field(default_factory=list)
    workplace_signals: list[str] = Field(default_factory=list)
    source_attributes: dict = Field(default_factory=dict)
    extraction_method: str = "deterministic_rules"
