# Production Operations

This guide covers the minimum operator actions needed to run or halt JobBot safely during production validation.

## Safely pause live execution

To stop new queue processing for one candidate:

```powershell
python -m jobbot.cli.main control-auto-apply-queue --candidate-profile <candidate-slug> --operation pause --queue-id <id>
```

To stop active continuous work for one candidate:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/auto-apply/<candidate-slug>/worker/stop?join_timeout_seconds=2"
```

Use both when you need an immediate operational freeze: stop the worker first, then pause queued items that should not resume automatically.

## Identify active workers and runner leases

- Worker runtime status:
  - `GET /api/auto-apply/<candidate-slug>/worker/status`
  - `GET /api/auto-apply/workers`
- Queue summary lease diagnostics:
  - `GET /api/auto-apply/<candidate-slug>/summary`
  - Check `runner_lease_active`, `runner_lease_expires_at`, `runner_lease_remaining_seconds`, `runner_lease_owner_host`, and `runner_lease_owner_pid`

If a bounded drain or worker start returns `queue_runner_already_active`, inspect the reported owner host/pid before retrying.

## Stop live activity without corrupting queue state

Preferred shutdown order:

1. Stop the continuous worker.
2. Confirm `active=false` from worker status.
3. Inspect the candidate summary for active runner lease fields.
4. If the lease is still active, wait for expiry or investigate the owner process before starting a new run.
5. Pause queue items when you need to preserve state for manual review.

Do not cancel queued items unless they are intentionally abandoned.

## Telemetry that indicates health

Healthy system signals:

- preflight returns `allow_run=true`
- worker heartbeat updates while active
- `runner_lease_active` matches expected in-flight work
- queue pressure ages remain below warning thresholds
- recent failure-rate stays below warning thresholds
- canary verified/unverified submit metrics behave as expected

Idle but healthy:

- `active=false`
- queue empty or intentionally paused
- no stale runner lease
- no critical SLO alerts in the queue summary

Needs intervention:

- repeated `auto_apply_preflight_failed`
- repeated `queue_runner_already_active`
- critical SLO alerts
- rising unverified submit ratio
- selector probe degradation or admission policy blockers
