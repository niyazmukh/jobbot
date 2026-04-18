"""Model-call telemetry and cost dashboard services."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.db.models import ModelCall
from jobbot.model_calls.prompts import get_prompt_version
from jobbot.model_calls.schemas import ModelCallRead, ModelCostDashboardRead, ModelCostDayRead

_BUDGET_GUARDRAIL_PROVIDER = "budget_guardrail"
_BUDGET_GUARDRAIL_MODEL = "blocked_non_essential"


def record_model_call(
    session: Session,
    *,
    stage: str,
    model_provider: str,
    model_name: str,
    prompt_version: str,
    linked_entity_id: int | None = None,
    input_size: int | None = None,
    output_size: int | None = None,
    latency_ms: int | None = None,
    estimated_cost: float | None = None,
) -> ModelCallRead:
    """Persist one model-call telemetry row."""

    row = ModelCall(
        stage=stage,
        model_provider=model_provider,
        model_name=model_name,
        prompt_version=prompt_version,
        linked_entity_id=linked_entity_id,
        input_size=input_size,
        output_size=output_size,
        latency_ms=latency_ms,
        estimated_cost=estimated_cost,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_read(row)


def get_model_cost_dashboard(
    session: Session,
    *,
    lookback_days: int = 7,
    daily_budget_usd: float = 5.0,
    weekly_budget_usd: float = 25.0,
) -> ModelCostDashboardRead:
    """Return dashboard-style model-call counts/costs with budget pressure signals."""

    if lookback_days < 1:
        raise ValueError("invalid_model_cost_lookback_days")
    if daily_budget_usd < 0.0 or weekly_budget_usd < 0.0:
        raise ValueError("invalid_model_cost_budget")

    now = datetime.now(UTC)
    today = now.date()
    lookback_start = datetime.combine(today - timedelta(days=lookback_days - 1), time.min, tzinfo=UTC)
    week_start = datetime.combine(today - timedelta(days=6), time.min, tzinfo=UTC)
    today_start = datetime.combine(today, time.min, tzinfo=UTC)

    lookback_rows = list(
        session.scalars(select(ModelCall).where(ModelCall.created_at >= lookback_start)).all()
    )
    today_rows = [row for row in lookback_rows if _to_utc(row.created_at) >= today_start]
    week_rows = [
        row
        for row in lookback_rows
        if _to_utc(row.created_at) >= week_start
    ]

    stage_call_counts: dict[str, int] = defaultdict(int)
    stage_estimated_costs: dict[str, float] = defaultdict(float)
    blocked_non_essential_stage_counts: dict[str, int] = defaultdict(int)
    day_call_counts: dict[date, int] = defaultdict(int)
    day_costs: dict[date, float] = defaultdict(float)
    for row in lookback_rows:
        stage_call_counts[row.stage] += 1
        stage_estimated_costs[row.stage] += _cost(row.estimated_cost)
        if (
            row.model_provider == _BUDGET_GUARDRAIL_PROVIDER
            and row.model_name == _BUDGET_GUARDRAIL_MODEL
        ):
            blocked_non_essential_stage_counts[row.stage] += 1

        bucket_day = _to_utc(row.created_at).date()
        day_call_counts[bucket_day] += 1
        day_costs[bucket_day] += _cost(row.estimated_cost)

    daily = _build_daily_buckets(
        lookback_days=lookback_days,
        today=today,
        day_call_counts=day_call_counts,
        day_costs=day_costs,
    )
    total_cost = round(sum(_cost(row.estimated_cost) for row in lookback_rows), 4)
    today_cost = round(sum(_cost(row.estimated_cost) for row in today_rows), 4)
    week_cost = round(sum(_cost(row.estimated_cost) for row in week_rows), 4)

    daily_budget_exceeded = today_cost > daily_budget_usd
    weekly_budget_exceeded = week_cost > weekly_budget_usd
    blocked_non_essential_call_count = sum(blocked_non_essential_stage_counts.values())
    return ModelCostDashboardRead(
        lookback_days=lookback_days,
        total_call_count=len(lookback_rows),
        total_estimated_cost=total_cost,
        today_call_count=len(today_rows),
        today_estimated_cost=today_cost,
        week_call_count=len(week_rows),
        week_estimated_cost=week_cost,
        stage_call_counts=dict(sorted(stage_call_counts.items())),
        stage_estimated_costs={k: round(v, 4) for k, v in sorted(stage_estimated_costs.items())},
        daily=daily,
        daily_budget_usd=round(daily_budget_usd, 4),
        weekly_budget_usd=round(weekly_budget_usd, 4),
        daily_budget_exceeded=daily_budget_exceeded,
        weekly_budget_exceeded=weekly_budget_exceeded,
        non_essential_llm_calls_allowed=not (daily_budget_exceeded or weekly_budget_exceeded),
        blocked_non_essential_call_count=blocked_non_essential_call_count,
        blocked_non_essential_stage_counts=dict(sorted(blocked_non_essential_stage_counts.items())),
    )


def allow_non_essential_model_call(
    session: Session,
    *,
    stage: str,
    linked_entity_id: int | None = None,
    lookback_days: int = 7,
    daily_budget_usd: float = 5.0,
    weekly_budget_usd: float = 25.0,
    prompt_version: str | None = None,
) -> bool:
    """Return whether a non-essential model call is allowed under current budget ceilings."""

    dashboard = get_model_cost_dashboard(
        session,
        lookback_days=lookback_days,
        daily_budget_usd=daily_budget_usd,
        weekly_budget_usd=weekly_budget_usd,
    )
    if dashboard.non_essential_llm_calls_allowed:
        return True

    resolved_prompt_version = prompt_version or get_prompt_version(
        "budget_guardrail_non_essential_call"
    )

    record_model_call(
        session,
        stage=stage,
        model_provider=_BUDGET_GUARDRAIL_PROVIDER,
        model_name=_BUDGET_GUARDRAIL_MODEL,
        prompt_version=resolved_prompt_version,
        linked_entity_id=linked_entity_id,
        latency_ms=0,
        estimated_cost=0.0,
    )
    return False


def _build_daily_buckets(
    *,
    lookback_days: int,
    today: date,
    day_call_counts: dict[date, int],
    day_costs: dict[date, float],
) -> list[ModelCostDayRead]:
    """Build contiguous lookback daily buckets including zero-traffic days."""

    rows: list[ModelCostDayRead] = []
    for days_ago in range(lookback_days - 1, -1, -1):
        day = today - timedelta(days=days_ago)
        rows.append(
            ModelCostDayRead(
                day=day.isoformat(),
                call_count=day_call_counts.get(day, 0),
                estimated_cost=round(day_costs.get(day, 0.0), 4),
            )
        )
    return rows


def _cost(estimated_cost: float | None) -> float:
    return float(estimated_cost or 0.0)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_read(row: ModelCall) -> ModelCallRead:
    return ModelCallRead(
        id=row.id,
        stage=row.stage,
        model_provider=row.model_provider,
        model_name=row.model_name,
        prompt_version=row.prompt_version,
        linked_entity_id=row.linked_entity_id,
        input_size=row.input_size,
        output_size=row.output_size,
        latency_ms=row.latency_ms,
        estimated_cost=row.estimated_cost,
        created_at=_to_utc(row.created_at),
    )
