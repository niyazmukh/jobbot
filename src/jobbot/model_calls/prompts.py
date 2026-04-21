"""Prompt version registry and replay-compatibility helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re


_VERSION_RE = re.compile(r"^(?P<family>[a-z0-9_]+)_v(?P<major>\d+)$")


@dataclass(frozen=True)
class PromptVersionSpec:
    """One registered prompt version entry."""

    key: str
    version_id: str
    description: str


_PROMPT_REGISTRY: dict[str, PromptVersionSpec] = {
    "budget_guardrail_non_essential_call": PromptVersionSpec(
        key="budget_guardrail_non_essential_call",
        version_id="budget_guardrail_v1",
        description="Budget ceiling gate for non-essential model calls.",
    ),
    "enrichment_fallback": PromptVersionSpec(
        key="enrichment_fallback",
        version_id="enrich_v1",
        description="Fallback extraction for unstructured job descriptions.",
    ),
    "scoring_fit_eval": PromptVersionSpec(
        key="scoring_fit_eval",
        version_id="score_v1",
        description="Candidate/job fit scoring evaluation prompt.",
    ),
    "preparation_extension_answer": PromptVersionSpec(
        key="preparation_extension_answer",
        version_id="prep_extension_v1",
        description="Tier-3 extension answer generation prompt.",
    ),
    "preparation_cv_draft": PromptVersionSpec(
        key="preparation_cv_draft",
        version_id="prep_cv_draft_v1",
        description="Iterative CV writer draft prompt for job-targeted resume structure.",
    ),
    "preparation_cv_review": PromptVersionSpec(
        key="preparation_cv_review",
        version_id="prep_cv_review_v1",
        description="Iterative CV writer reviewer/second-opinion quality-control prompt.",
    ),
    "preparation_cv_finalize": PromptVersionSpec(
        key="preparation_cv_finalize",
        version_id="prep_cv_finalize_v1",
        description="Iterative CV writer finalization prompt using reviewer feedback.",
    ),
}


def get_prompt_version(prompt_key: str) -> str:
    """Return the stable version id for a registered prompt key."""

    key = prompt_key.strip().lower()
    spec = _PROMPT_REGISTRY.get(key)
    if spec is None:
        raise ValueError("unknown_prompt_key")
    return spec.version_id


def list_prompt_registry() -> list[PromptVersionSpec]:
    """Return all registered prompts in deterministic key order."""

    return [_PROMPT_REGISTRY[key] for key in sorted(_PROMPT_REGISTRY.keys())]


def is_prompt_replay_compatible(
    recorded_prompt_version: str,
    replay_prompt_version: str,
) -> bool:
    """Return whether replay with target prompt version is semantically compatible.

    Compatibility policy is deterministic and conservative:
    - same prompt family and same major version => compatible
    - otherwise => incompatible
    """

    recorded_family, recorded_major = _parse_prompt_version(recorded_prompt_version)
    replay_family, replay_major = _parse_prompt_version(replay_prompt_version)
    return recorded_family == replay_family and recorded_major == replay_major


def assert_prompt_replay_compatible(
    recorded_prompt_version: str,
    replay_prompt_version: str,
) -> None:
    """Raise when replay prompt version is incompatible with recorded version."""

    if not is_prompt_replay_compatible(recorded_prompt_version, replay_prompt_version):
        raise ValueError("prompt_replay_incompatible")


def _parse_prompt_version(version_id: str) -> tuple[str, int]:
    """Parse <family>_v<major> prompt ids into comparable components."""

    normalized = version_id.strip().lower()
    match = _VERSION_RE.match(normalized)
    if match is None:
        raise ValueError("invalid_prompt_version_id")
    return (match.group("family"), int(match.group("major")))
