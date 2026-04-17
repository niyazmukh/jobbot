"""Schemas for persisted application eligibility snapshots."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ApplicationEligibilityRead(BaseModel):
    """Read model for a persisted execution-eligibility snapshot."""

    model_config = ConfigDict(extra="forbid")

    job_id: int
    candidate_profile_slug: str
    readiness_state: str
    ready: bool
    reasons: list[str]
    score_summary: dict
    prepared_summary: dict
    materialized_at: datetime
    updated_at: datetime
