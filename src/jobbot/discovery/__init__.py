"""Discovery contracts and source adapters."""

from jobbot.discovery.contracts import CanonicalJob, DiscoveryBatch, DiscoverySource
from jobbot.discovery.custom_sites.google import parse_google_results_html
from jobbot.discovery.custom_sites.meta import parse_meta_search_payload
from jobbot.discovery.custom_sites.microsoft import parse_microsoft_search_payload
from jobbot.discovery.inbox import InboxJobDetail, InboxJobRow, InboxJobSourceRow, get_inbox_job_detail, list_inbox_jobs
from jobbot.discovery.ingestion import JobIngestionCounters, ingest_canonical_job, ingest_discovery_batch
from jobbot.discovery.normalization import (
    canonicalize_job_url,
    normalize_company_name,
    normalize_job_title,
    normalize_location,
)

__all__ = [
    "CanonicalJob",
    "DiscoveryBatch",
    "DiscoverySource",
    "InboxJobDetail",
    "InboxJobRow",
    "InboxJobSourceRow",
    "JobIngestionCounters",
    "canonicalize_job_url",
    "ingest_canonical_job",
    "ingest_discovery_batch",
    "get_inbox_job_detail",
    "list_inbox_jobs",
    "normalize_company_name",
    "normalize_job_title",
    "normalize_location",
    "parse_google_results_html",
    "parse_meta_search_payload",
    "parse_microsoft_search_payload",
]
