"""Deterministic Workday search payload parsing."""

from __future__ import annotations

from datetime import datetime, timezone

from jobbot.discovery.contracts import CanonicalJob, DiscoveryBatch, DiscoverySource
from jobbot.discovery.normalization import (
    canonicalize_job_url,
    infer_remote_type,
    normalize_company_name,
    normalize_location,
)


def parse_workday_search_payload(
    company_name: str,
    base_url: str,
    site_id: str,
    payload: dict,
) -> DiscoveryBatch:
    """Parse a Workday CXS search payload into canonical jobs."""

    fetched_at = datetime.now(timezone.utc)
    jobs: list[CanonicalJob] = []

    for item in payload.get("jobPostings", []):
        external_path = item.get("externalPath")
        title = item.get("title")
        if not external_path or not title:
            continue

        public_url = _build_public_job_url(base_url, site_id, external_path)
        bullet_fields = item.get("bulletFields") or []
        employment_type = _extract_bullet_field(bullet_fields, "time type")
        location_raw = item.get("locationsText") or _extract_bullet_field(bullet_fields, "locations")

        metadata = {
            "base_url": base_url,
            "site_id": site_id,
            "company_name_normalized": normalize_company_name(company_name),
            "posted_on": item.get("postedOn"),
            "bullet_fields": bullet_fields,
        }

        jobs.append(
            CanonicalJob(
                source=DiscoverySource.WORKDAY,
                source_type="ats_board",
                external_job_id=_extract_external_job_id(item, external_path),
                canonical_url=canonicalize_job_url(public_url),
                company_name=company_name,
                title=title,
                location_raw=location_raw,
                location_normalized=normalize_location(location_raw),
                remote_type=infer_remote_type(location_raw, item.get("remoteType")),
                employment_type=employment_type,
                application_url=canonicalize_job_url(public_url),
                ats_vendor="workday",
                discovered_at=fetched_at,
                metadata=metadata,
            )
        )

    return DiscoveryBatch(
        source=DiscoverySource.WORKDAY,
        source_label=company_name,
        fetched_at=fetched_at,
        jobs=jobs,
    )


def _build_public_job_url(base_url: str, site_id: str, external_path: str) -> str:
    """Construct the public Workday job URL from the site base and external path."""

    root = base_url.rstrip("/")
    site = site_id.strip("/")
    path = external_path if external_path.startswith("/") else f"/{external_path}"
    return f"{root}/{site}{path}"


def _extract_bullet_field(bullet_fields: list[dict], label: str) -> str | None:
    """Return the first Workday bullet-field value whose label matches."""

    target = label.lower()
    for field in bullet_fields:
        if str(field.get("label", "")).strip().lower() != target:
            continue
        value = field.get("text")
        if value:
            return str(value)
    return None


def _extract_external_job_id(item: dict, external_path: str) -> str | None:
    """Extract a stable external identifier when Workday exposes one."""

    job_req_id = item.get("jobReqId")
    if job_req_id:
        return str(job_req_id)

    parts = [segment for segment in external_path.split("/") if segment]
    return parts[-1] if parts else None
