"""Pydantic schemas for candidate profile ingestion."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CandidateFactInput(BaseModel):
    """A single observed candidate fact."""

    model_config = ConfigDict(extra="forbid")

    fact_key: str | None = None
    category: str
    content: str
    structured_data: dict = Field(default_factory=dict)
    confidence: float = 1.0


class CandidateProfileImport(BaseModel):
    """Authoritative import payload for a candidate profile."""

    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str | None = None
    personal_details: dict = Field(default_factory=dict)
    target_preferences: dict = Field(default_factory=dict)
    source_profile_data: dict = Field(default_factory=dict)
    banned_claims: list = Field(default_factory=list)
    facts: list[CandidateFactInput] = Field(default_factory=list)
