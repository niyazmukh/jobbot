"""Deterministic Google careers results-page parsing."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin

from jobbot.discovery.contracts import CanonicalJob, DiscoveryBatch, DiscoverySource
from jobbot.discovery.normalization import canonicalize_job_url, normalize_company_name


def parse_google_results_html(
    company_name: str,
    page_url: str,
    html: str,
) -> DiscoveryBatch:
    """Parse a Google careers results page into canonical jobs."""

    fetched_at = datetime.now(timezone.utc)
    jobs: list[CanonicalJob] = []
    seen_ids: set[str] = set()

    pattern = re.compile(
        r'href="(jobs/results/([^"?]+)[^"]*)"\s+aria-label="Learn more about ([^"]+)"',
        re.IGNORECASE,
    )

    for match in pattern.finditer(html):
        relative_href = unescape(match.group(1))
        slug_fragment = match.group(2)
        title = unescape(match.group(3)).strip()
        external_job_id = slug_fragment.split("-", 1)[0]
        if not external_job_id or external_job_id in seen_ids:
            continue

        seen_ids.add(external_job_id)
        public_url = urljoin(
            "https://www.google.com/about/careers/applications/",
            relative_href,
        )

        jobs.append(
            CanonicalJob(
                source=DiscoverySource.CUSTOM_SITE,
                source_type="career_site_html",
                external_job_id=external_job_id,
                canonical_url=canonicalize_job_url(public_url),
                company_name=company_name,
                title=title,
                location_raw=None,
                location_normalized=None,
                remote_type=None,
                employment_type=None,
                application_url=canonicalize_job_url(public_url),
                ats_vendor="google-careers",
                discovered_at=fetched_at,
                metadata={
                    "page_url": page_url,
                    "company_name_normalized": normalize_company_name(company_name),
                },
            )
        )

    return DiscoveryBatch(
        source=DiscoverySource.CUSTOM_SITE,
        source_label=company_name,
        fetched_at=fetched_at,
        jobs=jobs,
    )
