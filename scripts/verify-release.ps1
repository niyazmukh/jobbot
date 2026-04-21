param(
    [string]$PythonExe = "python",
    [int]$PytestRepeat = 2,
    [string]$ValidationArtifactPath = "",
    [string]$RollbackTargetRef = "origin/main",
    [switch]$RequireCleanTree = $true
)

$ErrorActionPreference = "Stop"

function Invoke-ExternalCommand {
    param(
        [string]$Name,
        [scriptblock]$Script
    )

    $startedAt = [DateTimeOffset]::UtcNow
    $commandOutput = @(& $Script 2>&1)
    $finishedAt = [DateTimeOffset]::UtcNow
    $exitCode = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }
    $status = if ($exitCode -eq 0) { "passed" } else { "failed" }

    foreach ($line in $commandOutput) {
        Write-Host $line
    }

    return [ordered]@{
        name = $Name
        status = $status
        exit_code = $exitCode
        started_at = $startedAt.ToString("o")
        finished_at = $finishedAt.ToString("o")
        output = @($commandOutput | ForEach-Object { "$_" })
    }
}

function Resolve-GitRefOrNull {
    param(
        [string]$RefName
    )

    try {
        return (git rev-parse $RefName 2>$null).Trim()
    } catch {
        return $null
    }
}

$repoRoot = (Get-Location).Path
$startedAt = [DateTimeOffset]::UtcNow
$branchName = (git branch --show-current).Trim()
$candidateSha = (git rev-parse HEAD).Trim()
$dirtyEntries = @(git status --porcelain)
$isDirty = $dirtyEntries.Count -gt 0

if ($RequireCleanTree -and $isDirty) {
    throw "release_verification_requires_clean_git_tree"
}

$rollbackTargetSha = Resolve-GitRefOrNull -RefName $RollbackTargetRef
if (-not $rollbackTargetSha) {
    $rollbackTargetSha = Resolve-GitRefOrNull -RefName "main"
}

New-Item -ItemType Directory -Force ".pytest_tmp\run" | Out-Null

$verificationFailed = $false
$commands = @()
$commands += @{
    name = "lint"
    command = "$PythonExe -m ruff check ."
}
$commands += @{
    name = "test"
    command = "$PythonExe -m pytest -q"
    repeat = $PytestRepeat
}

$results = @()
$results += Invoke-ExternalCommand -Name "lint" -Script {
    & $PythonExe -m ruff check .
}
if ($results[-1].status -ne "passed") {
    $verificationFailed = $true
}

for ($index = 1; $index -le $PytestRepeat; $index++) {
    $results += Invoke-ExternalCommand -Name "test-$index" -Script {
        & $PythonExe -m pytest -q
    }
    if ($results[-1].status -ne "passed") {
        $verificationFailed = $true
    }
}

$finishedAt = [DateTimeOffset]::UtcNow
$artifact = [ordered]@{
    release_branch = $branchName
    candidate_commit_sha = $candidateSha
    verification_started_at = $startedAt.ToString("o")
    verification_finished_at = $finishedAt.ToString("o")
    git_tree_clean = (-not $isDirty)
    rollback_target_ref = if ($rollbackTargetSha) { $RollbackTargetRef } else { $null }
    rollback_target_sha = $rollbackTargetSha
    validation_commands = $commands
    validation_results = $results
    operational_smoke_validation = @{
        status = "not_run"
        canary_validation_outcome = "pending_manual_confirmation"
    }
    sign_off = @{
        security = "pending"
        engineering = if ($verificationFailed) { "failed" } else { "passed" }
        operations = "pending"
    }
}

$artifactJson = $artifact | ConvertTo-Json -Depth 6
if ($ValidationArtifactPath) {
    $artifactDir = Split-Path -Parent $ValidationArtifactPath
    if ($artifactDir) {
        New-Item -ItemType Directory -Force $artifactDir | Out-Null
    }
    Set-Content -Path $ValidationArtifactPath -Value $artifactJson -Encoding utf8
}

if ($verificationFailed) {
    throw "release_verification_failed"
}

Write-Host "Release verification complete"
Write-Host "Branch: $branchName"
Write-Host "Candidate SHA: $candidateSha"
Write-Host "Rollback target SHA: $rollbackTargetSha"
if ($ValidationArtifactPath) {
    Write-Host "Validation artifact: $ValidationArtifactPath"
}
