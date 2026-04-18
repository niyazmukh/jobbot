from jobbot.model_calls.prompts import (
    assert_prompt_replay_compatible,
    get_prompt_version,
    is_prompt_replay_compatible,
    list_prompt_registry,
)


def test_get_prompt_version_returns_registered_version_id():
    version_id = get_prompt_version("budget_guardrail_non_essential_call")

    assert version_id == "budget_guardrail_v1"


def test_list_prompt_registry_is_deterministic_and_non_empty():
    rows = list_prompt_registry()

    assert rows
    assert [row.key for row in rows] == sorted(row.key for row in rows)


def test_prompt_replay_compatibility_requires_same_family_and_major_version():
    assert is_prompt_replay_compatible("score_v1", "score_v1") is True
    assert is_prompt_replay_compatible("score_v1", "score_v2") is False
    assert is_prompt_replay_compatible("score_v1", "enrich_v1") is False


def test_assert_prompt_replay_compatible_raises_on_incompatible_versions():
    try:
        assert_prompt_replay_compatible("score_v1", "score_v2")
        assert False, "expected prompt replay incompatibility"
    except ValueError as exc:
        assert str(exc) == "prompt_replay_incompatible"
