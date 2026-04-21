# Live Test Operator Runbook

This runbook covers a deterministic live-test loop for auto-apply:
preflight -> bounded drain -> worker supervise -> remediation loop.

## Scope
- Candidate-scoped auto-apply operations
- Browser-backed guarded submit only (no simulated fallback accepted)
- Local-first operation using existing API and CLI surfaces

## Prerequisites
- Database initialized and candidate profile imported.
- At least one application browser profile exists and is healthy.
- Candidate has ready-to-apply jobs.
- Python environment active in repository root.

## Pre-Merge And Release Verification
Before a live test, validate the exact candidate SHA with the shared verification entrypoint:

```powershell
pwsh -File scripts/verify-release.ps1 `
  -PythonExe .venv\Scripts\python.exe `
  -ValidationArtifactPath manual_validation/release-validation.json
```

Required signal:
- git tree is clean for the candidate SHA
- lint passes
- repeated `pytest -q` passes
- rollback target SHA is captured in the artifact

Do not promote a commit that lacks a matching validation artifact.

## Readiness And Feasibility
A live test is feasible when all of the following are true:
- Preflight returns allow_run=true for the candidate and browser profile.
- Queue has at least one queued item.
- Browser profile readiness policy allows application execution.

Use either API or CLI to verify:

### API readiness check
PowerShell example:

Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/api/auto-apply/<candidate-slug>/preflight?browser_profile_key=<profile-key>"

Expected signal:
- allow_run is true
- blocked_reason_codes is empty

### CLI readiness check
python -m jobbot.cli.main check-auto-apply-preflight --candidate-profile <candidate-slug> --browser-profile-key <profile-key>

Expected signal:
- Allow run: True
- no blocked reasons

### Optional preflight calibration knobs
Use these environment variables to tune selector-health preflight sensitivity per environment:

- JOBBOT_AUTO_APPLY_SELECTOR_PROBE_WINDOW (default 20)
- JOBBOT_AUTO_APPLY_SELECTOR_PROBE_MIN_SAMPLE (default 4)
- JOBBOT_AUTO_APPLY_SELECTOR_PROBE_FAILURE_RATE_WARNING (default 0.30)
- JOBBOT_AUTO_APPLY_SELECTOR_PROBE_FAILURE_RATE_CRITICAL (default 0.50)

Example conservative tuning for noisy/staging environments:

JOBBOT_AUTO_APPLY_SELECTOR_PROBE_WINDOW=30
JOBBOT_AUTO_APPLY_SELECTOR_PROBE_MIN_SAMPLE=6
JOBBOT_AUTO_APPLY_SELECTOR_PROBE_FAILURE_RATE_WARNING=0.40
JOBBOT_AUTO_APPLY_SELECTOR_PROBE_FAILURE_RATE_CRITICAL=0.65

## Step 1: Build Candidate Queue
Use ready jobs and enqueue selected IDs.

python -m jobbot.cli.main list-ready-to-apply --candidate-profile <candidate-slug> --limit 50
python -m jobbot.cli.main enqueue-auto-apply --candidate-profile <candidate-slug> --job-id <job-id-1> --job-id <job-id-2> --priority 100 --max-attempts 3
python -m jobbot.cli.main list-auto-apply-queue --candidate-profile <candidate-slug> --limit 100

Expected signal:
- queued count increases
- list shows queued items with attempt_count 0

## Step 2: Bounded Drain (Smoke Pass)
Run one bounded pass first.

python -m jobbot.cli.main run-auto-apply-queue --candidate-profile <candidate-slug> --browser-profile-key <profile-key> --limit 5 --lease-seconds 300
python -m jobbot.cli.main show-auto-apply-summary --candidate-profile <candidate-slug>

Expected signal:
- processed_count > 0
- summary shows updated succeeded/failed/retry counts

If queue runner contention appears:
- Error contains queue_runner_already_active with lease owner details.
- Wait for lease expiry or stop the active worker before retry.

## Iterative LLM CV Writer Activation
The preparation pipeline supports a true iterative CV generation loop:
- Draft CV generation
- Reviewer/second-opinion quality control
- Final CV rewrite

Required configuration:
- JOBBOT_LLM_CV_WRITER_ENABLED=true
- JOBBOT_LLM_PROVIDER=gemini|openai|anthropic
- Set the matching provider API key:
  - JOBBOT_GEMINI_API_KEY
  - JOBBOT_OPENAI_API_KEY
  - JOBBOT_ANTHROPIC_API_KEY

Optional tuning:
- JOBBOT_LLM_CV_WRITER_MODEL
- JOBBOT_LLM_CV_REVIEWER_MODEL
- JOBBOT_LLM_CV_WRITER_TEMPERATURE
- JOBBOT_LLM_CV_WRITER_MAX_TOKENS

Validation command:
- python -m jobbot.cli.main prepare-job --job-id <job-id> --candidate-profile <candidate-slug>

Expected generated-document metadata:
- generation_method=iterative_llm_cv_writer_v1

## Step 3: Continuous Worker Supervision
Use API for supervised worker start/stop and status.

### Start worker
POST /api/auto-apply/<candidate-slug>/worker/start
Query parameters:
- browser_profile_key=<profile-key>
- limit=10
- lease_seconds=300
- poll_seconds=30
- preflight_required=true

### Observe worker status
GET /api/auto-apply/<candidate-slug>/worker/status
GET /api/auto-apply/workers

Expected signal:
- active=true while running
- cycles_completed increments
- total_processed_count rises over time

### Stop worker
POST /api/auto-apply/<candidate-slug>/worker/stop?join_timeout_seconds=2

Expected signal:
- active=false

## Step 4: Remediation Loop
When failures accumulate:

### Review summary and top failure
python -m jobbot.cli.main show-auto-apply-summary --candidate-profile <candidate-slug>

### Requeue failed scope by IDs
python -m jobbot.cli.main requeue-auto-apply-failed --candidate-profile <candidate-slug> --queue-id <id-1> --queue-id <id-2>

### Pause or resume queue slices
python -m jobbot.cli.main control-auto-apply-queue --candidate-profile <candidate-slug> --operation pause --queue-id <id>
python -m jobbot.cli.main control-auto-apply-queue --candidate-profile <candidate-slug> --operation resume --queue-id <id>

Expected signal:
- requeue updates reflected in list and summary
- paused items carry operator pause marker and are skipped until resumed

## Rollback And Disable Procedure
When a live run must stop immediately:

1. Stop the candidate worker:
   - `POST /api/auto-apply/<candidate-slug>/worker/stop?join_timeout_seconds=2`
2. Confirm worker status shows `active=false`.
3. Inspect queue summary lease diagnostics:
   - `runner_lease_active`
   - `runner_lease_expires_at`
   - `runner_lease_owner_host`
   - `runner_lease_owner_pid`
4. Pause affected queue items if they should not resume automatically:
   - `python -m jobbot.cli.main control-auto-apply-queue --candidate-profile <candidate-slug> --operation pause --queue-id <id>`
5. Record the rollback target SHA from the release validation artifact before restoring the previous known-good deployment.

## Fleet Monitoring During Live Test
Use fleet summaries for operations tooling:
- GET /api/auto-apply/summaries
- GET /api/auto-apply/summaries/export

Useful headers:
- X-Snapshot-Lineage-Id
- X-Snapshot-Generated-At
- X-Snapshot-Max-Age-Seconds
- X-Next-Cursor (when candidate_asc pagination and more rows exist)

## Failure Triage Quick Guide
- auto_apply_preflight_failed:
  - Check blocked_reason_codes in preflight payload.
  - Resolve browser profile readiness, Playwright runtime, or selector-health issues.
- guarded_submit_simulation_not_allowed_in_auto_apply:
  - Ensure real browser execution path is available.
  - Do not rely on simulated probe fallback for unattended runs.
- queue_runner_already_active:
  - Avoid parallel drains for same candidate.
  - Use worker status and lease diagnostics to coordinate operators.

## Exit Criteria For A Successful Live Test
- Preflight allow_run=true throughout the run window.
- At least one bounded drain succeeds with non-zero processed_count.
- Worker mode can start, report heartbeat/cycles, and stop cleanly.
- Remediation loop can requeue and recover failed items deterministically.
