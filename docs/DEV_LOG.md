# JobBot Dev Log

## 2026-04-18

### Operational Discipline
- Enforced periodic checkpoint commits after validated milestones.
- Kept repository hygiene strict: temporary/artifact outputs remain ignored (`graphify-out/`, `manual_validation/`, and ad-hoc generated files).
- Continued full-suite validation before every milestone commit.

### Graphify (used as intended)
- Ran `graphify update .` after structural code updates.
- Reviewed and captured `God Nodes` and `Surprising Connections` from `graphify-out/GRAPH_REPORT.md`.
- Latest observed God Nodes:
  - BrowserProfileType
  - CandidateProfile
  - Job
  - BrowserProfile
  - ApplicationState
- Latest observed Surprising Connections:
  - `src/jobbot/db/base.py` inferred dependency on `src/jobbot/db/bootstrap.py`
  - `alembic/env.py` inferred dependency on `src/jobbot/db/base.py`
  - `src/jobbot/scoring/__init__.py` inferred dependencies on browser schemas

### Milestones Completed (same day checkpoints)
- `59cb218`: Workday execution support + prompt registry baseline.
- `26da030`: PRD dedup layers 2-4 with deterministic fuzzy guardrails.
- `2cb92c0`: Scoring model-pass telemetry with prompt-version checks.
- `64106ef`: Enrichment model-pass telemetry with prompt-version checks.
- `0ce9be3`: Added rolling dev log with graphify + checkpoint discipline.
- `bfe7449`: Prompt registry/replay compatibility API surfaces (`/api/model-calls/prompts`, `/api/model-calls/replay-compatibility`).
- `ee03698`: Replay-capable job materialization endpoints for enrichment/scoring with prompt guardrails.
- `33759f3`: CLI replay parity for `enrich-job` and `score-job` with prompt-version pass-through.
- `ad9a239`: CLI prompt-governance commands for registry listing and replay compatibility checks.
- `f41c181`: durable auto-apply queue orchestration + automation-safe draft bootstrap reuse.
- `9688df5`: strict unattended auto-apply success policy (reject simulated submit fallback outcomes).
- `96c97f1`: stale-lease recovery and reclaim observability for auto-apply queue runs.
- `dd68ad7`: candidate-scoped auto-apply queue summary telemetry (API + CLI).
- `672b103`: failed-item requeue recovery controls (service/API/CLI + regressions).
- `04fe793`: targeted failed-item requeue reliability hardening (missing-ID visibility + no silent limit truncation).
- `bc9bdcd`: synced build/dev status docs after queue reliability checkpoint.
- `8faa498`: queue operation controls (`pause`/`resume`/`cancel`) + paused-runner safety.
- `888fdd2`: candidate-scoped queue-run concurrency guardrails (`queue_runner_already_active`).
- `32dab76`: queue pressure telemetry (aging + recent failure-rate window) for summary reads.
- `6f63d1f`: actionable runner-lease conflict diagnostics (API 409 detail + summary lease visibility).
- `ab106fb`: top failure-code remediation templates in queue summaries (route + CLI hints).
- `ca2a8f1`: queue summary SLO alert/severity classification (`ok`/`warning`/`critical`).
- `7636edc`: runner lease ownership diagnostics (host/pid) across summary + conflict payloads.

### Validation Baseline
- Full suite green at latest checkpoint: `245 passed`.

### Graphify Snapshot Update (latest)
- Re-ran `graphify update .` after runner lease ownership diagnostics updates.
- Current graph totals: 4526 nodes, 79592 edges, 269 communities.
- God Nodes remain stable around execution/profile/job domain entities.

### Next Intended Discipline
- Keep dev log updates synchronized with each structural milestone commit.
- Continue Graphify refresh after structural updates and summarize key graph signals here.
- Preserve commit cadence: implement -> focused tests -> full suite -> graphify update -> commit.

### End-Of-Day Final Review Sync
- Confirmed git baseline is clean at `7636edc` with no unstaged/staged drift.
- Re-ran full suite in repo `.venv`: `245 passed in 88.90s`.
- Confirmed latest graph snapshot remains current: `4526 nodes`, `79592 edges`, `269 communities`.

## 2026-04-19

### Git Sync Snapshot
- Branch: `main`
- HEAD: `a55cf20`
- Working tree remains intentionally dirty with active implementation files (auto-apply admission policy, preparation pipeline, API/CLI surfaces, and tests).
- Latest focused validation completed on auto-apply APIs: `31 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py -q`).

### Graph Sync Update
- Ran `graphify update .` after admission-policy and runtime changes.
- Updated graph totals: `4742 nodes`, `80971 edges`, `278 communities`.
- God Nodes (top 5):
  - `BrowserProfileType` (2625)
  - `CandidateProfile` (2176)
  - `Job` (1948)
  - `BrowserProfile` (1856)
  - `ReviewStatus` (1855)
- Surprising Connections (latest):
  - `src/jobbot/db/base.py` inferred links to `src/jobbot/db/bootstrap.py` (base/bootstrap coupling signals).
  - `src/jobbot/execution/worker_runtime.py` inferred links to `QueueRunnerAlreadyActiveError` in `src/jobbot/execution/auto_apply.py`.

### Logs Sync Note
- Synchronized build/log narrative with the new deterministic auto-apply admission gate milestone and updated graph diagnostics.

### Verified Submit + Canary Milestone
- Implemented verified-submit contract in auto-apply queue runs:
  - Success now requires submit-stage interaction evidence plus confirmation markers.
  - Unverified submit outcomes are persisted as failed queue outcomes and routed to manual review (`auto_apply_submit_unverified_review`).
- Added canary constraints for unattended runs:
  - Candidate-scoped verified-submit budget limits (1h/24h) are enforced.
  - Vendor allowlist gate blocks non-canary ATS vendors for unattended drains.
  - Preflight now surfaces `canary_submit_budget` status and blocking reason codes.
- Added one-command canary CLI operation:
  - `run-auto-apply-canary` executes preflight -> bounded drain -> KPI snapshot in one deterministic flow.
- Added verification KPI surfacing in queue summary:
  - `verified_submit_count_1h`
  - `verified_submit_count_24h`
  - `unverified_submit_count_24h`
- Validation: `.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py -q` -> `33 passed`.

### Recovery Automation Iteration
- Implemented actionable-only failed-item requeue controls:
  - Service: `requeue_failed_auto_apply_items(..., actionable_only, cooldown_seconds)` now retries only actionable failure classes when requested.
  - Cooldown guard prevents immediate requeue of recently failed actionable items (default settings-driven fallback).
- Added API requeue query controls:
  - `actionable_only`
  - `cooldown_seconds`
- Added CLI parity for deterministic operator recovery:
  - `requeue-auto-apply-failed --actionable-only --cooldown-seconds <n>`
- Added config knob:
  - `JOBBOT_AUTO_APPLY_REQUEUE_ACTIONABLE_COOLDOWN_SECONDS`
- Validation:
  - Focused requeue regressions: `4 passed`
  - Full auto-apply API suite: `34 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py -q`).

### Functional KPI Iteration
- Implemented functional KPI telemetry for auto-apply readiness summaries:
  - submit quality rates: `verified_submit_rate_24h`, `unverified_submit_ratio_24h`
  - blocker trend: `blocker_counts_24h`, `top_blocker_code_24h`, `top_blocker_count_24h`
- Added deterministic tie-break behavior for top blocker selection (count desc, key asc) to stabilize trend outputs.
- Extended fleet summary CSV export with KPI columns for external analytics consumers.
- Extended CLI summary display to include KPI rate/trend lines.
- Validation:
  - Full auto-apply API suite: `35 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py -q`).

### Conditional Polling Iteration
- Implemented lineage-driven conditional-get behavior on fleet summary endpoints:
  - `GET /api/auto-apply/summaries`
  - `GET /api/auto-apply/summaries/export`
- Added request validator support:
  - Standard `If-None-Match` (including quoted and weak ETag forms)
  - Explicit `X-Snapshot-Lineage-Id`
- Added stable `ETag` emission derived from snapshot lineage IDs.
- Added `304 Not Modified` responses with snapshot/freshness headers when lineage is unchanged.
- Kept normal `200` payload responses when lineage changes.
- Validation:
  - Focused conditional snapshot tests: `3 passed`
  - Full auto-apply API suite: `38 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py`).

### Fleet Summary CLI Parity Iteration
- Implemented full CLI parity for fleet auto-apply summaries:
  - `list-auto-apply-summaries` with sort/filter/include-empty/limit/cursor controls.
  - `export-auto-apply-summaries --output-file ...` with KPI-rich CSV output matching fleet export fields.
- Preserved cursor guardrail behavior in CLI (`cursor_requires_candidate_asc_sort`) to match API semantics.
- Added CLI regression tests for:
  - cursor-based paging behavior in fleet summary listing
  - CSV export file generation and core field output
- Validation:
  - CLI history test suite: `7 passed` (`.venv\\Scripts\\python -m pytest tests/test_cli_execution_history.py`).

### Preflight Threshold Observability Iteration
- Added explicit effective configuration observability to auto-apply preflight output:
  - New check key: `effective_configuration`
  - Includes resolved selector thresholds, admission knobs, and canary limits/allowlist as emitted runtime values.
- Enhanced CLI preflight operator flow:
  - `check-auto-apply-preflight` now prints a machine-readable `Effective knobs` JSON line for calibration/audit loops.
- Added regression coverage:
  - Preflight override test now asserts selector threshold overrides are reflected in `effective_configuration` details.
- Validation:
  - Focused preflight API tests: `5 passed`
  - Full auto-apply API suite: `38 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py`)
  - CLI history suite: `7 passed` (`.venv\\Scripts\\python -m pytest tests/test_cli_execution_history.py`)

### Queue Summary Delta Marker Iteration
- Added deterministic per-candidate change markers to queue summaries:
  - New summary field: `summary_delta_marker` (stable hash of functional summary state).
  - Marker intentionally excludes volatile generation timestamp noise and changes only when material summary state changes.
- Surfaced marker across operations surfaces:
  - Candidate summary API (`/api/auto-apply/{candidate}/summary`)
  - Fleet summary JSON (`/api/auto-apply/summaries`)
  - Fleet summary CSV export (`/api/auto-apply/summaries/export`)
  - CLI list/export commands for fleet summaries
- Validation:
  - Full auto-apply API suite: `39 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py`)
  - CLI history suite: `7 passed` (`.venv\\Scripts\\python -m pytest tests/test_cli_execution_history.py`)

### Fleet Summary CLI JSON Output Iteration
- Added scriptable JSON output mode for fleet summaries:
  - `list-auto-apply-summaries --json-output`
  - Output includes query controls, `next_cursor`, and serialized summary rows for automation pipelines.
- Kept default table output unchanged for interactive operators.
- Validation:
  - CLI history suite: `8 passed` (`.venv\\Scripts\\python -m pytest tests/test_cli_execution_history.py`)

### Preflight Configuration Drift Warning Iteration
- Added non-blocking drift warning check to preflight output:
  - New check key: `configuration_drift`
  - Emits `drift_keys`, `defaults`, and `effective` values when runtime knobs diverge from conservative defaults.
- Drift check behavior:
  - `status=ok` when defaults are preserved.
  - `status=warning` with reason `preflight_configuration_drift_detected` when overrides are detected.
- Validation:
  - Focused preflight tests: `5 passed`
  - Full auto-apply API suite: `39 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py`)
  - CLI history suite: `8 passed` (`.venv\\Scripts\\python -m pytest tests/test_cli_execution_history.py`)

### Fleet Summary Changed-Candidate Hint Iteration
- Added optional marker-map diff hints for fleet summary polling:
  - Clients can provide `X-Prior-Summary-Markers` (JSON candidate->marker map).
  - API now responds with:
    - `X-Changed-Candidate-Count`
    - `X-Changed-Candidates`
  - Supported on both summary JSON and summary export endpoints.
- Preserved compatibility with existing lineage/conditional headers and response behavior.
- Validation:
  - Focused changed-candidate tests: `2 passed`
  - Full auto-apply API suite: `41 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py`)
  - CLI history suite: `8 passed` (`.venv\\Scripts\\python -m pytest tests/test_cli_execution_history.py`)

### Changed-Candidate Header Truncation Iteration
- Added bounded response metadata for large changed-candidate sets:
  - `X-Changed-Candidates` now returns a capped subset.
  - New headers:
    - `X-Changed-Candidates-Returned`
    - `X-Changed-Candidates-Truncated`
- Preserved full changed cardinality via `X-Changed-Candidate-Count`.
- Validation:
  - Focused changed-candidate + truncation tests: `3 passed`
  - Full auto-apply API suite: `42 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py`)
  - CLI history suite: `8 passed` (`.venv\\Scripts\\python -m pytest tests/test_cli_execution_history.py`)

### Fleet Summary Export Cursor Iteration
- Added cursor pagination to fleet summary CSV export endpoint:
  - `GET /api/auto-apply/summaries/export?...&sort_by=candidate_asc&cursor=<slug>&limit=<n>`
  - Added `X-Next-Cursor` support on export responses when additional rows remain.
- Added cursor guardrail parity:
  - Export now rejects cursor usage unless `sort_by=candidate_asc` (`cursor_requires_candidate_asc_sort`).
- Validation:
  - Focused export cursor tests: `2 passed`
  - Full auto-apply API suite: `44 passed` (`.venv\\Scripts\\python -m pytest tests/test_api_auto_apply.py`)
  - CLI history suite: `8 passed` (`.venv\\Scripts\\python -m pytest tests/test_cli_execution_history.py`)
