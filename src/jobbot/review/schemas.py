"""Schemas for manual review queue workflows."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ReviewQueueRead(BaseModel):
    """Read model for a persisted review queue item."""

    model_config = ConfigDict(extra="forbid")

    id: int
    entity_type: str
    entity_id: int
    reason: str
    truth_tier: str | None = None
    confidence: float | None = None
    status: str
    created_at: datetime
    updated_at: datetime
    context: dict | None = None
