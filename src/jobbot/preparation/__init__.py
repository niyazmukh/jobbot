"""Preparation services for generated documents and answer packs."""

from jobbot.preparation.read_models import (
    PreparedAnswerRead,
    PreparedDocumentRead,
    PreparedJobRead,
    get_prepared_job_read,
)
from jobbot.preparation.schemas import PreparedAnswerPlan, PreparedClaim, PreparedJobSummary
from jobbot.preparation.service import prepare_job_for_candidate

__all__ = [
    "PreparedAnswerRead",
    "PreparedAnswerPlan",
    "PreparedClaim",
    "PreparedDocumentRead",
    "PreparedJobRead",
    "PreparedJobSummary",
    "get_prepared_job_read",
    "prepare_job_for_candidate",
]
