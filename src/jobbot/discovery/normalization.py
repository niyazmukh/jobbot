"""Deterministic normalization helpers for discovery."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


TRACKING_QUERY_KEYS = {
    "gh_jid",
    "gh_src",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
}

LOCATION_ALIASES = {
    "nyc": "new york city",
    "new york, ny": "new york city",
    "new york city, ny": "new york city",
    "san francisco, ca": "san francisco bay area",
    "sf, ca": "san francisco bay area",
    "remote - canada": "remote canada",
    "remote, canada": "remote canada",
    "remote - united states": "remote united states",
    "remote, united states": "remote united states",
}

REGION_ABBREVIATIONS = {
    "ab": "alberta",
    "az": "arizona",
    "bc": "british columbia",
    "ca": "california",
    "co": "colorado",
    "dc": "district of columbia",
    "fl": "florida",
    "ga": "georgia",
    "il": "illinois",
    "ma": "massachusetts",
    "mi": "michigan",
    "nc": "north carolina",
    "nj": "new jersey",
    "ny": "new york",
    "on": "ontario",
    "or": "oregon",
    "pa": "pennsylvania",
    "qc": "quebec",
    "qu": "quebec",
    "sc": "south carolina",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "va": "virginia",
    "wa": "washington",
}


def canonicalize_job_url(url: str) -> str:
    """Normalize a job URL by removing fragments and tracking parameters."""

    parsed = urlparse(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key not in TRACKING_QUERY_KEYS
    ]
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        query=urlencode(query, doseq=True),
        fragment="",
    )
    path = normalized.path.rstrip("/") or normalized.path
    normalized = normalized._replace(path=path)
    return urlunparse(normalized)


def normalize_company_name(name: str) -> str:
    """Normalize company names for deterministic matching."""

    return " ".join(name.lower().split())


def normalize_job_title(title: str) -> str:
    """Normalize job titles for deterministic matching."""

    return " ".join(title.lower().split())


def normalize_location(location: str | None) -> str | None:
    """Normalize common location strings conservatively."""

    if not location:
        return None
    normalized = " ".join(location.strip().lower().split())
    normalized = re.sub(r"\s*-\s*", " ", normalized)
    normalized = re.sub(r"\s*,\s*", ", ", normalized)
    normalized = LOCATION_ALIASES.get(normalized, normalized)
    return _expand_region_abbreviation(normalized)


def _expand_region_abbreviation(location: str) -> str:
    """Expand a trailing region abbreviation when it is unambiguous."""

    parts = [part.strip() for part in location.split(",")]
    if len(parts) < 2:
        return location

    trailing = parts[-1]
    expanded = REGION_ABBREVIATIONS.get(trailing)
    if expanded is None:
        return location

    parts[-1] = expanded
    return ", ".join(parts)
