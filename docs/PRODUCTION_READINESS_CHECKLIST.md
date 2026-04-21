# Production Readiness Checklist

Date: 2026-04-21
Release branch: release/2026-04-21-prod-readiness

## Security
- [x] Remove hardcoded secrets from tracked files.
- [x] Ensure `.env` remains ignored.
- [x] Ignore local PII baseline artifacts not required for runtime.
- [ ] Rotate any previously exposed provider keys and verify revocation.

## Quality Gates
- [x] CI workflow present under `.github/workflows/ci.yml`.
- [x] CI runs the shared release verification entrypoint (`scripts/verify-release.ps1`) on push and pull request.
- [x] Windows included in CI matrix to catch platform regressions.
- [ ] CI green on this exact release branch commit.
- [ ] Release validation artifact captured for the exact candidate SHA being promoted.

## Test Stability
- [x] Quarantine Windows temp ACL instability by forcing repo-local pytest base temp.
- [x] Keep pytest temp path ignored in git.
- [ ] Confirm no intermittent temp cleanup failures in CI.
- [ ] Keep repeated `pytest -q` passes green through the shared verification script.

## Product And Architecture Decisions
- [x] Product/repo-facing name finalized as `JobBot`.
- [x] LinkedIn discovery sequencing finalized (secondary until ATS parity and production guardrails).
- [x] Artifact addressing approach finalized (attempt-addressed first).
- [x] State persistence strategy finalized (validated string columns at app boundary).

## Operations
- [x] Live-test operator runbook present.
- [x] Production operations guide present.
- [x] Deterministic preflight and queue control surfaces available.
- [ ] Validate live canary run in target environment.

## Rollback
- [x] Release branch created for isolated promotion.
- [x] Release commit SHA recorded in the release validation artifact contract.
- [ ] Tag release candidate after full regression success.
- [ ] Keep rollback target as previous known-good `main` SHA.

## Final Sign-Off
- Security: Pending key rotation confirmation.
- Engineering: Pending CI green on release commit.
- Operations: Pending canary smoke validation in target environment.
- Release Decision: Pending all above checks.

## Exact-SHA Verification Procedure

Run from repository root:

```powershell
pwsh -File scripts/verify-release.ps1 `
  -PythonExe .venv\Scripts\python.exe `
  -ValidationArtifactPath manual_validation/release-validation.json
```

Do not mark engineering validation complete unless the resulting artifact SHA matches the release commit under review.
