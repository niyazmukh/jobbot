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
- `in-progress`: targeted failed-item requeue reliability hardening (missing-ID visibility + no silent limit truncation).

### Validation Baseline
- Full suite green at latest checkpoint: `238 passed`.

### Graphify Snapshot Update (latest)
- Re-ran `graphify update .` after prompt API/schema changes.
- Current graph totals: 3770 nodes, 72764 edges, 213 communities.
- God Nodes remain stable around execution/profile/job domain entities.

### Next Intended Discipline
- Keep dev log updates synchronized with each structural milestone commit.
- Continue Graphify refresh after structural updates and summarize key graph signals here.
- Preserve commit cadence: implement -> focused tests -> full suite -> graphify update -> commit.
