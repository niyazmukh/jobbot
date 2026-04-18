"""Deterministic vendor handler registry for execution flows."""

from __future__ import annotations

from dataclasses import dataclass

from jobbot.db.models import FieldMapping
from jobbot.execution.schemas import DraftResolvedFieldRead, DraftSiteFieldPlanEntryRead
from jobbot.execution.site_profiles import guarded_submit_plan_for_site, required_fields_for_site
from jobbot.execution.vendor_flows import (
    build_overlay_entries,
    build_submit_gate_signals,
    build_target_open_resolutions,
)


@dataclass(frozen=True)
class ExecutionVendorHandler:
    """Execution flow handler for one ATS vendor."""

    vendor: str
    allow_target_open: bool = True
    allow_submit_gate: bool = True
    allow_guarded_submit: bool = True
    guarded_submit_mode: str | None = None

    def supports_target_open(self) -> bool:
        """Return whether target-open is enabled for this handler."""

        return self.allow_target_open

    def supports_submit_gate(self) -> bool:
        """Return whether submit-gate is enabled for this handler."""

        return self.allow_submit_gate

    def supports_guarded_submit(self) -> bool:
        """Return whether guarded submit execution is enabled for this handler."""

        return self.allow_guarded_submit

    def submission_mode(self) -> str:
        """Return vendor-specific guarded submit mode identifier."""

        if self.guarded_submit_mode:
            return self.guarded_submit_mode
        return f"{self.vendor}_guarded_submit"

    def guarded_submit_plan(self) -> dict[str, object]:
        """Return deterministic guarded-submit strategy for this vendor."""

        return guarded_submit_plan_for_site(self.vendor)

    def required_fields(self) -> list[str]:
        """Return required fields used during submit-gate evaluation."""

        return required_fields_for_site(self.vendor)

    def overlay_entries(self, mappings: list[FieldMapping]) -> list[DraftSiteFieldPlanEntryRead]:
        """Build deterministic overlay entries for this vendor."""

        return build_overlay_entries(site_vendor=self.vendor, mappings=mappings)

    def target_open_resolutions(self, mappings: list[FieldMapping]) -> list[DraftResolvedFieldRead]:
        """Build deterministic target-open field resolutions for this vendor."""

        return build_target_open_resolutions(mappings)

    def submit_gate_signals(self, mappings: list[FieldMapping]):
        """Build deterministic submit-gate signals for this vendor."""

        return build_submit_gate_signals(
            required_fields=self.required_fields(),
            mappings=mappings,
        )


_VENDOR_HANDLERS: dict[str, ExecutionVendorHandler] = {
    "greenhouse": ExecutionVendorHandler(
        vendor="greenhouse",
        guarded_submit_mode="greenhouse_guarded_submit",
    ),
    "lever": ExecutionVendorHandler(
        vendor="lever",
        guarded_submit_mode="lever_guarded_submit",
    ),
    "workday": ExecutionVendorHandler(
        vendor="workday",
        guarded_submit_mode="workday_guarded_submit",
    ),
}


def get_vendor_execution_handler(site_vendor: str) -> ExecutionVendorHandler | None:
    """Return a registered execution handler for a vendor, if available."""

    return _VENDOR_HANDLERS.get(site_vendor.strip().lower())
