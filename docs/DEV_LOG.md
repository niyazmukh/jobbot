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
- `in-progress`: runner lease ownership diagnostics (host/pid) across summary + conflict payloads.

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
