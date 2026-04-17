"""Enum types used across persistence and orchestration."""

from enum import Enum


class TruthTier(str, Enum):
    OBSERVED = "observed"
    INFERENCE = "inference"
    EXTENSION = "extension"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApplicationState(str, Enum):
    DISCOVERED = "discovered"
    ENRICHED = "enriched"
    SCORED = "scored"
    PREPARED = "prepared"
    REVIEW = "review"
    APPLIED = "applied"
    FAILED = "failed"
    IGNORED = "ignored"


class ApplicationMode(str, Enum):
    DRAFT = "draft"
    GUARDED_SUBMIT = "guarded_submit"
    ASSIST = "assist"


class AttemptResult(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    ABORTED = "aborted"


class ArtifactType(str, Enum):
    SCREENSHOT = "screenshot"
    TRACE = "trace"
    HTML_SNAPSHOT = "html_snapshot"
    GENERATED_DOCUMENT = "generated_document"
    MODEL_IO = "model_io"
    ANSWER_PACK = "answer_pack"


class SessionHealth(str, Enum):
    HEALTHY = "healthy"
    LOGIN_REQUIRED = "login_required"
    CHECKPOINTED = "checkpointed"
    RATE_LIMITED = "rate_limited"
    SUSPECTED_FLAGGED = "suspected_flagged"


class BrowserProfileType(str, Enum):
    DISCOVERY = "discovery"
    APPLICATION = "application"
