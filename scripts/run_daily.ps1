param(
    [switch]$SkipCatchUp,
    [switch]$NoEmail,
    [switch]$ReminderDryRun,
    [switch]$ReminderForce,
    [string]$TaskName = "WeChatDailyDigestNoon"
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "Run scripts/setup.ps1 first." }
$RunnerArgs = @("-B", "-m", "src.task_runner")
if ($SkipCatchUp) { $RunnerArgs += "--skip-catch-up" }
if ($NoEmail) { $RunnerArgs += "--no-email" }
if ($ReminderDryRun) { $RunnerArgs += "--reminder-dry-run" }
if ($ReminderForce) { $RunnerArgs += "--reminder-force" }
& $Python @RunnerArgs
$RunnerExitCode = $LASTEXITCODE
if (-not $SkipCatchUp -and -not $NoEmail) {
    try {
        . (Join-Path $PSScriptRoot 'task_schedule.ps1')
        Sync-DigestTaskSchedule -ProjectRoot $ProjectRoot -TaskName $TaskName
    } catch {
        Write-Warning "Could not defer completed-day checks: $($_.Exception.Message)"
        if ($RunnerExitCode -eq 0) { $RunnerExitCode = 1 }
    }
}
exit $RunnerExitCode
