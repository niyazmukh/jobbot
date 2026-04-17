"""Custom career-site discovery adapters."""

from jobbot.discovery.custom_sites.google import parse_google_results_html
from jobbot.discovery.custom_sites.meta import parse_meta_search_payload
from jobbot.discovery.custom_sites.microsoft import parse_microsoft_search_payload

__all__ = [
    "parse_google_results_html",
    "parse_meta_search_payload",
    "parse_microsoft_search_payload",
]
