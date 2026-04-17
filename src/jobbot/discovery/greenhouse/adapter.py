"""Deterministic Greenhouse board payload parsing."""

from __future__ import annotations

from datetime import datetime, timezone

from jobbot.discovery.contracts import CanonicalJob, DiscoveryBatch, DiscoverySource
from jobbot.discovery.normalization import canonicalize_job_url, normalize_company_name, normalize_location


def parse_greenhouse_board_payload(
    company_name: str,
    board_url: str,
    payload: dict,
) -> DiscoveryBatch:
    """Parse a Greenhouse board JSON payload into canonical jobs.

    Expected payload shape follows Greenhouse board endpoints that expose a top-level
    `jobs` list with job records containing `id`, `title`, `absolute_url`, and optional
    location/metadata fields.
    """

    fetched_at = datetime.now(timezone.utc)
    jobs: list[CanonicalJob] = []

    for item in payload.get("jobs", []):
        absolute_url = item.get("absolute_url")
        title = item.get("title")
        if not absolute_url or not title:
            continue

        location_raw = None
        if isinstance(item.get("location"), dict):
            location_raw = item["location"].get("name")
        elif item.get("location"):
            location_raw = str(item.get("location"))

        metadata = {
            "board_url": board_url,
            "company_name_normalized": normalize_company_name(company_name),
            "data_compliance": item.get("data_compliance", []),
            "updated_at": item.get("updated_at"),
        }

        jobs.append(
            CanonicalJob(
                source=DiscoverySource.GREENHOUSE,
                source_type="ats_board",
                external_job_id=str(item.get("id")) if item.get("id") is not None else None,
                canonical_url=canonicalize_job_url(absolute_url),
                company_name=company_name,
                title=title,
                location_raw=location_raw,
                location_normalized=normalize_location(location_raw),
                remote_type=_infer_remote_type(location_raw),
                application_url=canonicalize_job_url(absolute_url),
                ats_vendor="greenhouse",
                discovered_at=fetched_at,
                metadata=metadata,
            )
        )

    return DiscoveryBatch(
        source=DiscoverySource.GREENHOUSE,
        source_label=company_name,
        fetched_at=fetched_at,
        jobs=jobs,
    )


def _infer_remote_type(location_raw: str | None) -> str | None:
    """Infer remote classification conservatively from Greenhouse location text."""

    if not location_raw:
        return None

    loc = location_raw.lower()
    if "remote" in loc:
        return "remote"
    if "hybrid" in loc:
        return "hybrid"
    return "onsite"
