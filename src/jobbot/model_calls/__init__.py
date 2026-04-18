"""Model-call telemetry services and read models."""

from jobbot.model_calls.prompts import (
    assert_prompt_replay_compatible,
    get_prompt_version,
    is_prompt_replay_compatible,
    list_prompt_registry,
)
from jobbot.model_calls.schemas import ModelCallRead, ModelCostDashboardRead, ModelCostDayRead
from jobbot.model_calls.service import (
    allow_non_essential_model_call,
    get_model_cost_dashboard,
    record_model_call,
)

__all__ = [
    "ModelCallRead",
    "ModelCostDayRead",
    "ModelCostDashboardRead",
    "get_prompt_version",
    "list_prompt_registry",
    "is_prompt_replay_compatible",
    "assert_prompt_replay_compatible",
    "allow_non_essential_model_call",
    "record_model_call",
    "get_model_cost_dashboard",
]
