from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from jobbot.db.base import Base
from jobbot.db.models import ModelCall
from jobbot.model_calls.service import (
    allow_non_essential_model_call,
    get_model_cost_dashboard,
    record_model_call,
)


def make_session():
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def test_record_model_call_persists_row_and_prompt_version():
    session = make_session()
    try:
        created = record_model_call(
            session,
            stage="scoring",
            model_provider="openai",
            model_name="gpt-5.4-mini",
            prompt_version="score_v1",
            linked_entity_id=42,
            input_size=800,
            output_size=220,
            latency_ms=1500,
            estimated_cost=0.031,
        )
        row = session.scalar(select(ModelCall).where(ModelCall.id == created.id))
    finally:
        session.close()

    assert row is not None
    assert row.prompt_version == "score_v1"
    assert row.stage == "scoring"
    assert row.estimated_cost == 0.031


def test_get_model_cost_dashboard_aggregates_costs_and_budget_flags():
    session = make_session()
    now = datetime.now(timezone.utc)
    try:
        session.add_all(
            [
                ModelCall(
                    stage="discovery",
                    model_provider="openai",
                    model_name="gpt-5.4-mini",
                    prompt_version="disc_v1",
                    estimated_cost=0.7,
                    created_at=now,
                ),
                ModelCall(
                    stage="scoring",
                    model_provider="openai",
                    model_name="gpt-5.4-mini",
                    prompt_version="score_v1",
                    estimated_cost=1.5,
                    created_at=now - timedelta(hours=1),
                ),
                ModelCall(
                    stage="enrichment",
                    model_provider="openai",
                    model_name="gpt-5.4-mini",
                    prompt_version="enrich_v1",
                    estimated_cost=2.0,
                    created_at=now - timedelta(days=1),
                ),
                ModelCall(
                    stage="preparation",
                    model_provider="openai",
                    model_name="gpt-5.4-mini",
                    prompt_version="prep_v1",
                    estimated_cost=3.0,
                    created_at=now - timedelta(days=8),
                ),
            ]
        )
        session.commit()

        dashboard = get_model_cost_dashboard(
            session,
            lookback_days=10,
            daily_budget_usd=2.0,
            weekly_budget_usd=4.0,
        )
    finally:
        session.close()

    assert dashboard.lookback_days == 10
    assert dashboard.total_call_count == 4
    assert dashboard.total_estimated_cost == 7.2
    assert dashboard.today_call_count == 2
    assert dashboard.today_estimated_cost == 2.2
    assert dashboard.week_call_count == 3
    assert dashboard.week_estimated_cost == 4.2
    assert dashboard.stage_call_counts["discovery"] == 1
    assert dashboard.stage_call_counts["scoring"] == 1
    assert dashboard.stage_call_counts["enrichment"] == 1
    assert dashboard.stage_call_counts["preparation"] == 1
    assert dashboard.daily_budget_exceeded is True
    assert dashboard.weekly_budget_exceeded is True
    assert dashboard.non_essential_llm_calls_allowed is False
    assert dashboard.blocked_non_essential_call_count == 0
    assert dashboard.blocked_non_essential_stage_counts == {}
    assert len(dashboard.daily) == 10


def test_allow_non_essential_model_call_records_budget_block_and_updates_dashboard():
    session = make_session()
    now = datetime.now(timezone.utc)
    try:
        session.add(
            ModelCall(
                stage="scoring",
                model_provider="openai",
                model_name="gpt-5.4-mini",
                prompt_version="score_v1",
                estimated_cost=3.0,
                created_at=now,
            )
        )
        session.commit()

        allowed = allow_non_essential_model_call(
            session,
            stage="preparation_extension_answer",
            linked_entity_id=99,
            lookback_days=7,
            daily_budget_usd=2.0,
            weekly_budget_usd=10.0,
        )
        dashboard = get_model_cost_dashboard(
            session,
            lookback_days=7,
            daily_budget_usd=2.0,
            weekly_budget_usd=10.0,
        )
    finally:
        session.close()

    assert allowed is False
    assert dashboard.blocked_non_essential_call_count == 1
    assert dashboard.blocked_non_essential_stage_counts["preparation_extension_answer"] == 1


def test_allow_non_essential_model_call_uses_registry_prompt_version_when_not_provided():
    session = make_session()
    now = datetime.now(timezone.utc)
    try:
        session.add(
            ModelCall(
                stage="scoring",
                model_provider="openai",
                model_name="gpt-5.4-mini",
                prompt_version="score_v1",
                estimated_cost=3.0,
                created_at=now,
            )
        )
        session.commit()

        allowed = allow_non_essential_model_call(
            session,
            stage="preparation_extension_answer",
            linked_entity_id=88,
            lookback_days=7,
            daily_budget_usd=2.0,
            weekly_budget_usd=10.0,
        )
        blocked = session.scalar(
            select(ModelCall)
            .where(
                ModelCall.model_provider == "budget_guardrail",
                ModelCall.model_name == "blocked_non_essential",
                ModelCall.stage == "preparation_extension_answer",
            )
            .order_by(ModelCall.id.desc())
        )
    finally:
        session.close()

    assert allowed is False
    assert blocked is not None
    assert blocked.prompt_version == "budget_guardrail_v1"
