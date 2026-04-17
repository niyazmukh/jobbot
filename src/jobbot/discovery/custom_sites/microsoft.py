"""Deterministic Microsoft careers search payload parsing."""

from __future__ import annotations

from datetime import datetime, timezone

from jobbot.discovery.contracts import CanonicalJob, DiscoveryBatch, DiscoverySource
from jobbot.discovery.normalization import canonicalize_job_url, normalize_company_name, normalize_location


def parse_microsoft_search_payload(
    company_name: str,
    search_url: str,
    payload: dict,
) -> DiscoveryBatch:
    """Parse a Microsoft careers search payload into canonical jobs."""

    fetched_at = datetime.now(timezone.utc)
    data = payload.get("data") or {}
    positions = data.get("positions") or []
    jobs: list[CanonicalJob] = []

    for item in positions:
        position_id = item.get("id")
        title = item.get("name")
        public_url = _resolve_public_url(item)
        if position_id is None or not title or not public_url:
            continue

        location_raw = _pick_location(item)
        metadata = {
            "search_url": search_url,
            "company_name_normalized": normalize_company_name(company_name),
            "display_job_id": item.get("displayJobId"),
            "department": item.get("department"),
            "work_location_option": item.get("workLocationOption"),
            "location_flexibility": item.get("locationFlexibility"),
            "posted_ts": item.get("postedTs"),
        }

        jobs.append(
            CanonicalJob(
                source=DiscoverySource.CUSTOM_SITE,
                source_type="career_site_api",
                external_job_id=str(item.get("displayJobId") or position_id),
                canonical_url=canonicalize_job_url(public_url),
                company_name=company_name,
                title=title,
                location_raw=location_raw,
                location_normalized=normalize_location(location_raw),
                remote_type=_infer_remote_type(item, location_raw),
                employment_type=None,
                application_url=canonicalize_job_url(public_url),
                ats_vendor="microsoft-careers",
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


def _resolve_public_url(item: dict) -> str | None:
    """Resolve a public Microsoft job URL from the search payload."""

    public_url = item.get("publicUrl")
    if public_url:
        return str(public_url)

    position_url = item.get("positionUrl")
    if position_url:
        return canonicalize_job_url(f"https://apply.careers.microsoft.com{position_url}")

    position_id = item.get("id")
    if position_id is not None:
        return f"https://apply.careers.microsoft.com/careers/job/{position_id}"

    return None


def _pick_location(item: dict) -> str | None:
    """Choose the best available location string from Microsoft search data."""

    locations = item.get("locations")
    if isinstance(locations, list) and locations:
        first = locations[0]
        if first:
            return str(first)

    standardized = item.get("standardizedLocations")
    if isinstance(standardized, list) and standardized:
        first = standardized[0]
        if isinstance(first, dict):
            parts = [first.get("city"), first.get("state"), first.get("country")]
            location = ", ".join(str(part).strip() for part in parts if part)
            return location or None
        if first:
            return str(first)

    return None


def _infer_remote_type(item: dict, location_raw: str | None) -> str | None:
    """Infer remote classification from Microsoft search metadata."""

    tokens = [
        str(item.get("workLocationOption") or ""),
        str(item.get("locationFlexibility") or ""),
        str(location_raw or ""),
    ]
    combined = " ".join(tokens).lower()
    if not combined.strip():
        return None
    if "remote" in combined or "up to 100% work from home" in combined:
        return "remote"
    if "hybrid" in combined or "up to 50%" in combined or "partial work from home" in combined:
        return "hybrid"
    return "onsite"
