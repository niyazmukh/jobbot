"""Deterministic Meta careers search payload parsing."""

from __future__ import annotations

from datetime import datetime, timezone

from jobbot.discovery.contracts import CanonicalJob, DiscoveryBatch, DiscoverySource
from jobbot.discovery.normalization import (
    canonicalize_job_url,
    infer_remote_type,
    normalize_company_name,
    normalize_location,
)


def parse_meta_search_payload(
    company_name: str,
    search_url: str,
    payload: dict,
) -> DiscoveryBatch:
    """Parse a Meta careers search payload into canonical jobs."""

    fetched_at = datetime.now(timezone.utc)
    jobs_payload = (
        payload.get("data", {})
        .get("job_search_with_featured_jobs", {})
        .get("all_jobs", [])
    )
    jobs: list[CanonicalJob] = []

    for item in jobs_payload:
        job_id = item.get("id")
        title = item.get("title")
        if job_id is None or not title:
            continue

        public_url = f"https://www.metacareers.com/profile/job_details/{job_id}"
        locations = item.get("locations") if isinstance(item.get("locations"), list) else []
        location_raw = str(locations[0]).strip() if locations else None
        teams = item.get("teams") if isinstance(item.get("teams"), list) else []
        sub_teams = item.get("sub_teams") if isinstance(item.get("sub_teams"), list) else []

        metadata = {
            "search_url": search_url,
            "company_name_normalized": normalize_company_name(company_name),
            "teams": teams,
            "sub_teams": sub_teams,
        }

        jobs.append(
            CanonicalJob(
                source=DiscoverySource.CUSTOM_SITE,
                source_type="career_site_graphql",
                external_job_id=str(job_id),
                canonical_url=canonicalize_job_url(public_url),
                company_name=company_name,
                title=title,
                location_raw=location_raw,
                location_normalized=normalize_location(location_raw),
                remote_type=infer_remote_type(location_raw),
                employment_type=None,
                application_url=canonicalize_job_url(public_url),
                ats_vendor="meta-careers",
                discovered_at=fetched_at,
                metadata=metadata,
            )
        )

    return DiscoveryBatch(
        source=DiscoverySource.CUSTOM_SITE,
        source_label=company_name,
        fetched_at=fetched_at,
        jobs=jobs,
    )
