"""Schemas for deterministic execution bootstrap workflows."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DraftApplicationAttemptRead(BaseModel):
    """Read model for a draft application attempt bootstrap."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    event_id: int
    job_id: int
    candidate_profile_slug: str
    application_state: str
    attempt_mode: str
    browser_profile_key: str | None = None
    session_health: str | None = None
    attempt_result: str | None = None
    failure_code: str | None = None
    submit_confidence: float | None = None
    notes: str | None = None
    readiness_state: str
    ready: bool
    reasons: list[str]
    created_application: bool
    started_at: datetime


class DraftExecutionOverviewRead(BaseModel):
    """Read model for one operator-facing draft execution overview row."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    job_id: int
    candidate_profile_slug: str
    company_name: str | None = None
    job_title: str
    site_vendor: str | None = None
    application_state: str
    readiness_state: str
    ready: bool
    attempt_mode: str
    attempt_result: str | None = None
    failure_code: str | None = None
    failure_classification: str | None = None
    submit_confidence: float | None = None
    browser_profile_key: str | None = None
    session_health: str | None = None
    latest_event_type: str | None = None
    latest_event_message: str | None = None
    submit_interaction_mode: str | None = None
    submit_interaction_status: str | None = None
    submit_interaction_clicked: bool | None = None
    submit_interaction_selector: str | None = None
    submit_interaction_confirmation_count: int | None = None
    submit_troubleshoot_event_route: str | None = None
    submit_troubleshoot_artifact_route: str | None = None
    submit_remediation_message: str | None = None
    submit_remediation_primary_route: str | None = None
    submit_remediation_primary_label: str | None = None
    submit_remediation_secondary_route: str | None = None
    submit_remediation_secondary_label: str | None = None
    submit_remediation_retry_route: str | None = None
    submit_remediation_retry_label: str | None = None
    attempt_route: str
    replay_route: str
    primary_action_route: str
    primary_action_label: str
    latest_artifact_route: str | None = None
    latest_artifact_label: str | None = None
    visual_evidence_route: str | None = None
    visual_evidence_label: str | None = None
    artifact_count: int = 0
    screenshot_count: int = 0
    html_snapshot_count: int = 0
    model_io_count: int = 0
    generated_document_count: int = 0
    answer_pack_count: int = 0
    reasons: list[str]
    started_at: datetime


class DraftExecutionEventRead(BaseModel):
    """Read model for one persisted execution event."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    event_type: str
    message: str
    created_at: datetime
    payload: dict
    artifact_routes: list[str] = []


class DraftExecutionArtifactRead(BaseModel):
    """Read model for one persisted execution artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: int
    artifact_type: str
    path: str
    size_bytes: int | None = None
    created_at: datetime
    inspect_route: str
    raw_route: str | None = None
    launch_route: str | None = None
    launch_label: str | None = None
    launch_target: str | None = None


class DraftExecutionArtifactDetailRead(BaseModel):
    """Read model for one execution artifact with safe preview content."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: int
    attempt_id: int | None = None
    artifact_type: str
    path: str
    size_bytes: int | None = None
    created_at: datetime
    exists: bool
    raw_route: str | None = None
    launch_route: str | None = None
    launch_label: str | None = None
    launch_target: str | None = None
    preview_kind: str
    preview_text: str | None = None
    preview_truncated: bool = False


class DraftExecutionReplayAssetRead(BaseModel):
    """Read model for one replay-relevant execution asset."""

    model_config = ConfigDict(extra="forbid")

    label: str
    artifact_id: int | None = None
    artifact_type: str | None = None
    path: str | None = None
    exists: bool = False
    inspect_route: str | None = None
    raw_route: str | None = None
    launch_route: str | None = None
    launch_label: str | None = None
    launch_target: str | None = None
    openable_locally: bool = False
    open_hint: str | None = None


class DraftExecutionReplayBundleRead(BaseModel):
    """Read model for one replay-oriented execution bundle."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    job_id: int
    candidate_profile_slug: str
    job_title: str
    company_name: str | None = None
    site_vendor: str | None = None
    application_state: str
    attempt_result: str | None = None
    failure_code: str | None = None
    latest_event_type: str | None = None
    startup_dir: str | None = None
    target_url: str | None = None
    assets: list[DraftExecutionReplayAssetRead]
    recommended_actions: list[str]


class DraftExecutionDashboardRead(BaseModel):
    """Read model for one candidate-scoped execution dashboard."""

    model_config = ConfigDict(extra="forbid")

    candidate_profile_slug: str
    total_attempts: int
    blocked_attempts: int
    manual_review_blocked_attempts: int
    extension_review_blocked_attempts: int
    pending_attempts: int
    review_state_attempts: int
    replay_ready_attempts: int
    remediation_history_count: int
    remediation_history_limit: int
    blocked_failure_counts: dict[str, int]
    blocked_failure_classification_counts: dict[str, int]
    linkedin_guarded_stop_reason_counts: dict[str, int]
    recent_attempts: list[DraftExecutionOverviewRead]
    blocked_recent_attempts: list[DraftExecutionOverviewRead]
    recommended_actions: list[str]


class DraftExecutionAttemptDetailRead(BaseModel):
    """Read model for one drill-down execution attempt detail."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    job_id: int
    candidate_profile_slug: str
    company_name: str | None = None
    job_title: str
    site_vendor: str | None = None
    application_state: str
    readiness_state: str
    ready: bool
    attempt_mode: str
    attempt_result: str | None = None
    failure_code: str | None = None
    failure_classification: str | None = None
    submit_confidence: float | None = None
    browser_profile_key: str | None = None
    session_health: str | None = None
    notes: str | None = None
    submit_interaction_mode: str | None = None
    submit_interaction_status: str | None = None
    submit_interaction_clicked: bool | None = None
    submit_interaction_selector: str | None = None
    submit_interaction_confirmation_count: int | None = None
    submit_troubleshoot_event_route: str | None = None
    submit_troubleshoot_artifact_route: str | None = None
    submit_remediation_message: str | None = None
    submit_remediation_primary_route: str | None = None
    submit_remediation_primary_label: str | None = None
    submit_remediation_secondary_route: str | None = None
    submit_remediation_secondary_label: str | None = None
    submit_remediation_retry_route: str | None = None
    submit_remediation_retry_label: str | None = None
    reasons: list[str]
    started_at: datetime
    events: list[DraftExecutionEventRead]
    artifacts: list[DraftExecutionArtifactRead]


class DraftExecutionStartupRead(BaseModel):
    """Read model for a staged draft execution startup bundle."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    event_id: int
    job_id: int
    candidate_profile_slug: str
    browser_profile_key: str | None = None
    readiness_state: str
    target_url: str
    startup_dir: str
    prepared_document_count: int
    prepared_answer_count: int
    startup_artifact_ids: list[int]
    started_at: datetime


class DraftFieldPlanEntryRead(BaseModel):
    """Read model for one persisted draft field mapping plan entry."""

    model_config = ConfigDict(extra="forbid")

    field_mapping_id: int
    field_key: str
    inferred_type: str | None = None
    confidence: float | None = None
    answer_id: int | None = None
    truth_tier: str | None = None
    chosen_answer: str | None = None
    answer_source: str | None = None


class DraftFieldPlanRead(BaseModel):
    """Read model for a persisted draft field-plan bundle."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    event_id: int
    job_id: int
    candidate_profile_slug: str
    field_count: int
    artifact_id: int
    artifact_path: str
    entries: list[DraftFieldPlanEntryRead]


class DraftSiteFieldPlanEntryRead(BaseModel):
    """Read model for one site-aware field overlay entry."""

    model_config = ConfigDict(extra="forbid")

    field_mapping_id: int
    field_key: str
    site_vendor: str
    selector_candidates: list[str]
    confidence_gate: float
    manual_review_required: bool = False


class DraftSiteFieldPlanRead(BaseModel):
    """Read model for a site-aware execution overlay."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    event_id: int
    job_id: int
    candidate_profile_slug: str
    site_vendor: str
    entry_count: int
    artifact_id: int
    artifact_path: str
    entries: list[DraftSiteFieldPlanEntryRead]


class DraftResolvedFieldRead(BaseModel):
    """Read model for one resolved site field entry."""

    model_config = ConfigDict(extra="forbid")

    field_mapping_id: int
    field_key: str
    resolved_selector: str | None = None
    resolution_status: str
    confidence_gate: float
    manual_review_required: bool = False


class DraftTargetOpenRead(BaseModel):
    """Read model for a non-submitting page-open and field-resolution pass."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    event_id: int
    job_id: int
    candidate_profile_slug: str
    site_vendor: str
    browser_profile_key: str
    target_url: str
    capture_method: str
    capture_error: str | None = None
    opened_page_artifact_id: int
    resolution_artifact_id: int
    screenshot_artifact_id: int | None = None
    trace_artifact_id: int | None = None
    resolved_count: int
    unresolved_count: int
    entries: list[DraftResolvedFieldRead]


class DraftSubmitGateRead(BaseModel):
    """Read model for a guarded submit-confidence evaluation."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    event_id: int
    job_id: int
    candidate_profile_slug: str
    site_vendor: str
    application_state: str
    attempt_result: str | None = None
    failure_code: str | None = None
    confidence_score: float
    allow_submit: bool
    stop_reasons: list[str]
    required_fields: list[str]
    resolved_required_fields: list[str]
    manual_review_fields: list[str]
    artifact_id: int
    artifact_path: str


class DraftGuardedSubmitRead(BaseModel):
    """Read model for guarded submit execution outcomes."""

    model_config = ConfigDict(extra="forbid")

    application_id: int
    attempt_id: int
    event_id: int
    job_id: int
    candidate_profile_slug: str
    site_vendor: str
    application_state: str
    attempt_result: str
    failure_code: str | None = None
    confidence_score: float
    allow_submit: bool
    submission_mode: str
    target_url: str
    artifact_id: int
    artifact_path: str
    screenshot_artifact_id: int | None = None
    trace_artifact_id: int | None = None
    submitted_at: datetime


class DraftSubmitRemediationActionRead(BaseModel):
    """Read model for deterministic submit-remediation orchestration actions."""

    model_config = ConfigDict(extra="forbid")

    source_attempt_id: int
    application_id: int
    attempt_id: int
    job_id: int
    candidate_profile_slug: str
    remediation_action: str
    executed_steps: list[str]
    stop_reason: str | None = None
    failure_code: str | None = None
    failure_classification: str | None = None
    allow_submit: bool | None = None
    submit_confidence: float | None = None
    final_attempt_result: str | None = None
    final_failure_code: str | None = None
    final_failure_classification: str | None = None
    detail_route: str
    replay_route: str


class DraftSubmitRemediationFailureRead(BaseModel):
    """Read model for one failed submit-remediation replay in a bulk action."""

    model_config = ConfigDict(extra="forbid")

    source_attempt_id: int
    error_code: str


class DraftSubmitRemediationBatchRead(BaseModel):
    """Read model for dashboard-scoped bulk submit-remediation actions."""

    model_config = ConfigDict(extra="forbid")

    candidate_profile_slug: str
    requested_count: int
    remediated_count: int
    failed_count: int
    targeted_attempt_ids: list[int]
    results: list[DraftSubmitRemediationActionRead]
    failures: list[DraftSubmitRemediationFailureRead]


class DraftExecutionDashboardRemediationHistoryRead(BaseModel):
    """Read model for one persisted dashboard bulk-remediation history entry."""

    model_config = ConfigDict(extra="forbid")

    history_id: str
    created_at: str
    requested_count: int
    remediated_count: int
    failed_count: int
    failure_code: str | None = None
    failure_classification: str | None = None
    linkedin_stop_reason: str | None = None
    manual_review_only: bool = False
    max_submit_confidence: float | None = None
    sort_by: str
    descending: bool
    limit: int
    first_failure_attempt_id: int | None = None
    first_failure_code: str | None = None
    rerun_route: str


class DraftExecutionDashboardRemediationHistoryRetentionRead(BaseModel):
    """Read model for remediation-history retention/cleanup operations."""

    model_config = ConfigDict(extra="forbid")

    candidate_profile_slug: str
    configured_limit: int
    keep_limit: int
    before_count: int
    after_count: int
    removed_count: int


class AutoApplyQueueItemRead(BaseModel):
    """Read model for one durable auto-apply queue item."""

    model_config = ConfigDict(extra="forbid")

    queue_id: int
    candidate_profile_slug: str
    job_id: int
    status: str
    priority: int
    attempt_count: int
    max_attempts: int
    source_attempt_id: int | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    next_attempt_at: datetime | None = None
    lease_expires_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AutoApplyEnqueueRead(BaseModel):
    """Read model for queue enqueue outcomes."""

    model_config = ConfigDict(extra="forbid")

    candidate_profile_slug: str
    requested_job_ids: list[int]
    queued_count: int
    requeued_count: int
    skipped_count: int
    items: list[AutoApplyQueueItemRead]


class AutoApplyQueueRunRead(BaseModel):
    """Read model for one queue-run batch."""

    model_config = ConfigDict(extra="forbid")

    candidate_profile_slug: str
    requested_limit: int
    reclaimed_count: int
    processed_count: int
    succeeded_count: int
    failed_count: int
    retried_count: int
    items: list[AutoApplyQueueItemRead]


class AutoApplyQueueSummaryRead(BaseModel):
    """Read model for candidate-scoped auto-apply queue health summary."""

    model_config = ConfigDict(extra="forbid")

    candidate_profile_slug: str
    total_count: int
    queued_count: int
    paused_count: int
    running_count: int
    succeeded_count: int
    failed_count: int
    retry_scheduled_count: int
    stale_running_count: int
    next_attempt_at: datetime | None = None
    oldest_queued_age_seconds: int | None = None
    oldest_retry_scheduled_age_seconds: int | None = None
    recent_completed_count_1h: int = 0
    recent_failure_rate_1h: float | None = None
    runner_lease_active: bool = False
    runner_lease_expires_at: datetime | None = None
    runner_lease_remaining_seconds: int | None = None


class AutoApplyQueueRequeueRead(BaseModel):
    """Read model for failed-queue requeue operations."""

    model_config = ConfigDict(extra="forbid")

    candidate_profile_slug: str
    requested_queue_ids: list[int]
    missing_queue_ids: list[int]
    requeued_count: int
    skipped_count: int
    items: list[AutoApplyQueueItemRead]


class AutoApplyQueueControlRead(BaseModel):
    """Read model for auto-apply queue pause/resume/cancel operations."""

    model_config = ConfigDict(extra="forbid")

    candidate_profile_slug: str
    operation: str
    requested_queue_ids: list[int]
    missing_queue_ids: list[int]
    updated_count: int
    skipped_count: int
    items: list[AutoApplyQueueItemRead]


class DraftLinkedInQuestionRead(BaseModel):
    """Read model for one extracted LinkedIn question widget."""

    model_config = ConfigDict(extra="forbid")

    field_key: str
    question_text: str
    field_type: str
    confidence: float
    source: str
    assist_required: bool


class DraftLinkedInQuestionExtractionRead(BaseModel):
    """Read model for deterministic LinkedIn question extraction output."""

    model_config = ConfigDict(extra="forbid")

    question_count: int
    unknown_field_count: int
    assist_required: bool
    recommended_mode: str
    questions: list[DraftLinkedInQuestionRead]


class DraftLinkedInAssistFieldRead(BaseModel):
    """Read model for one LinkedIn assist-mode field-fill decision."""

    model_config = ConfigDict(extra="forbid")

    field_key: str
    question_text: str
    field_type: str
    confidence: float
    source: str
    action: str
    proposed_answer: str | None = None
    reason: str


class DraftLinkedInAssistPlanRead(BaseModel):
    """Read model for deterministic LinkedIn assist-plan output."""

    model_config = ConfigDict(extra="forbid")

    candidate_profile_slug: str | None = None
    question_count: int
    auto_fill_count: int
    assist_review_count: int
    blocked_auto_action_count: int
    recommended_mode: str
    fields: list[DraftLinkedInAssistFieldRead]
    recommended_actions: list[str]


class DraftLinkedInGuardedSubmitCriteriaRead(BaseModel):
    """Read model for LinkedIn guarded-submit eligibility criteria evaluation."""

    model_config = ConfigDict(extra="forbid")

    application_id: int | None = None
    attempt_id: int | None = None
    event_id: int | None = None
    artifact_id: int | None = None
    artifact_path: str | None = None
    profile_key: str
    candidate_profile_slug: str | None = None
    session_health: str
    session_requires_reauth: bool
    allow_session_automation: bool
    question_count: int
    assist_review_count: int
    blocked_auto_action_count: int
    recommended_mode: str
    min_auto_confidence: float
    allow_guarded_submit: bool
    stop_reasons: list[str]
    recommended_actions: list[str]
