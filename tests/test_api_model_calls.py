from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from jobbot.api.app import app, get_db_session
from jobbot.db.base import Base
from jobbot.db.models import ModelCall


def make_session():
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def test_model_cost_dashboard_api_returns_budget_and_stage_breakdown():
    session = make_session()
    session.add_all(
        [
            ModelCall(
                stage="scoring",
                model_provider="openai",
                model_name="gpt-5.4-mini",
                prompt_version="score_v1",
                estimated_cost=1.1,
                created_at=datetime.now(timezone.utc),
            ),
            ModelCall(
                stage="enrichment",
                model_provider="openai",
                model_name="gpt-5.4-mini",
                prompt_version="enrich_v1",
                estimated_cost=1.2,
                created_at=datetime.now(timezone.utc),
            ),
        ]
    )
    session.commit()

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(
            "/api/model-calls/dashboard"
            "?lookback_days=7&daily_budget_usd=2.0&weekly_budget_usd=10.0"
        )
    finally:
        app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["lookback_days"] == 7
    assert payload["total_call_count"] == 2
    assert payload["stage_call_counts"]["scoring"] == 1
    assert payload["stage_call_counts"]["enrichment"] == 1
    assert payload["today_estimated_cost"] == 2.3
    assert payload["daily_budget_exceeded"] is True
    assert payload["weekly_budget_exceeded"] is False
    assert payload["non_essential_llm_calls_allowed"] is False
    assert payload["blocked_non_essential_call_count"] == 0
    assert payload["blocked_non_essential_stage_counts"] == {}
