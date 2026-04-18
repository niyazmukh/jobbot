"""Read models for model-call telemetry and cost dashboarding."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelCallRead(BaseModel):
    """Read model for one recorded model call."""

    model_config = ConfigDict(extra="forbid")

    id: int
    stage: str
    model_provider: str
    model_name: str
    prompt_version: str
    linked_entity_id: int | None = None
    input_size: int | None = None
    output_size: int | None = None
    latency_ms: int | None = None
    estimated_cost: float | None = None
    created_at: datetime


class ModelCostDayRead(BaseModel):
    """Read model for one daily cost bucket."""

    model_config = ConfigDict(extra="forbid")

    day: str
    call_count: int
    estimated_cost: float


class ModelCostDashboardRead(BaseModel):
    """Read model for model-call cost totals and budget status."""

    model_config = ConfigDict(extra="forbid")

    lookback_days: int
    total_call_count: int
    total_estimated_cost: float
    today_call_count: int
    today_estimated_cost: float
    week_call_count: int
    week_estimated_cost: float
    stage_call_counts: dict[str, int]
    stage_estimated_costs: dict[str, float]
    daily: list[ModelCostDayRead]
    daily_budget_usd: float
    weekly_budget_usd: float
    daily_budget_exceeded: bool
    weekly_budget_exceeded: bool
    non_essential_llm_calls_allowed: bool
    blocked_non_essential_call_count: int
    blocked_non_essential_stage_counts: dict[str, int]
