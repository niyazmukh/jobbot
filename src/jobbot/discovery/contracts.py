"""Canonical discovery contracts for ATS and targeted sources."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class DiscoverySource(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    WORKDAY = "workday"
    LINKEDIN = "linkedin"
    CUSTOM_SITE = "custom_site"


class CanonicalJob(BaseModel):
    """Normalized job shape produced by discovery adapters."""

    model_config = ConfigDict(extra="forbid")

    source: DiscoverySource
    source_type: str
    external_job_id: str | None = None
    canonical_url: HttpUrl
    company_name: str
    company_domain: str | None = None
    title: str
    location_raw: str | None = None
    location_normalized: str | None = None
    remote_type: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    salary_text: str | None = None
    application_url: HttpUrl | None = None
    ats_vendor: str | None = None
    discovered_at: datetime
    metadata: dict = Field(default_factory=dict)


class DiscoveryBatch(BaseModel):
    """A deterministic batch of discovered jobs from one adapter run."""

    model_config = ConfigDict(extra="forbid")

    source: DiscoverySource
    source_label: str
    fetched_at: datetime
    jobs: list[CanonicalJob] = Field(default_factory=list)
