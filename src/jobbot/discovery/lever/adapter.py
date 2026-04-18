"""Deterministic Lever postings payload parsing."""

from __future__ import annotations

from datetime import datetime, timezone

from jobbot.discovery.contracts import CanonicalJob, DiscoveryBatch, DiscoverySource
from jobbot.discovery.normalization import (
    canonicalize_job_url,
    infer_remote_type,
    normalize_company_name,
    normalize_location,
)


def parse_lever_postings_payload(
    company_name: str,
    postings_url: str,
    payload: list[dict],
) -> DiscoveryBatch:
    """Parse a Lever postings payload into canonical jobs.

    Expected payload shape follows Lever postings APIs that expose a list of job
    records with `id`, `text`, `hostedUrl`, and optional `categories`.
    """

    fetched_at = datetime.now(timezone.utc)
    jobs: list[CanonicalJob] = []

    for item in payload:
        hosted_url = item.get("hostedUrl")
        title = item.get("text")
        if not hosted_url or not title:
            continue

        categories = item.get("categories") or {}
        location_raw = categories.get("location")

        metadata = {
            "postings_url": postings_url,
            "company_name_normalized": normalize_company_name(company_name),
            "team": categories.get("team"),
            "commitment": categories.get("commitment"),
            "department": categories.get("department"),
            "workplace_type": categories.get("workplaceType"),
        }

        jobs.append(
            CanonicalJob(
                source=DiscoverySource.LEVER,
                source_type="ats_board",
                external_job_id=str(item.get("id")) if item.get("id") is not None else None,
                canonical_url=canonicalize_job_url(hosted_url),
                company_name=company_name,
                title=title,
                location_raw=location_raw,
                location_normalized=normalize_location(location_raw),
                remote_type=infer_remote_type(location_raw, categories.get("workplaceType")),
                employment_type=categories.get("commitment"),
                application_url=canonicalize_job_url(hosted_url),
                ats_vendor="lever",
                discovered_at=fetched_at,
                metadata=metadata,
            )
        )

    return DiscoveryBatch(
        source=DiscoverySource.LEVER,
        source_label=company_name,
        fetched_at=fetched_at,
        jobs=jobs,
    )
