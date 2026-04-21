# Release Validation Artifacts

This directory defines the exact-SHA validation evidence expected before production promotion.

## Verification entrypoint

Run the shared verification script from the repository root:

```powershell
pwsh -File scripts/verify-release.ps1 `
  -PythonExe .venv\Scripts\python.exe `
  -ValidationArtifactPath manual_validation/release-validation.json
```

CI uses the same script with `-PythonExe python`.

## Artifact contract

Each validation artifact records:

- `release_branch`
- `candidate_commit_sha`
- verification start/end timestamps
- clean/dirty git tree state
- rollback target ref and SHA
- lint/test command contract
- pass/fail results for each verification command
- operational smoke validation placeholder
- sign-off placeholders

Engineering validation is only considered complete when the artifact SHA matches the release candidate commit being promoted.

## Manual completion

The script captures engineering validation automatically. After canary execution, update the operational fields in a copied artifact or companion release note with:

- canary smoke outcome
- rollback target confirmation
- security sign-off
- operations sign-off
