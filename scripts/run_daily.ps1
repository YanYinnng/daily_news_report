param(
    [switch]$SkipCatchUp,
    [switch]$NoEmail,
    [switch]$ReminderDryRun,
    [switch]$ReminderForce
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot
$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("task_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    $Python = "python"
}

if ($SkipCatchUp) {
    "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') catch-up skipped ====" | Add-Content -Path $LogFile -Encoding UTF8
    $CatchUpExitCode = 0
} else {
    $CatchUpArgs = @("-B", "-m", "src.main", "catch-up")
    if ($NoEmail) {
        $CatchUpArgs += "--no-email"
    }

    "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') catch-up start ====" | Add-Content -Path $LogFile -Encoding UTF8
    & $Python @CatchUpArgs *>&1 | Tee-Object -FilePath $LogFile -Append
    $CatchUpExitCode = $LASTEXITCODE
    "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') catch-up exit $CatchUpExitCode ====" | Add-Content -Path $LogFile -Encoding UTF8
}

$ReminderArgs = @("-B", "-m", "src.main", "send-login-reminder")
if ($ReminderDryRun) {
    $ReminderArgs += "--dry-run"
}
if ($ReminderForce) {
    $ReminderArgs += "--force"
}

"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') login reminder start ====" | Add-Content -Path $LogFile -Encoding UTF8
& $Python @ReminderArgs *>&1 | Tee-Object -FilePath $LogFile -Append
$ReminderExitCode = $LASTEXITCODE
"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') login reminder exit $ReminderExitCode ====" | Add-Content -Path $LogFile -Encoding UTF8

if ($CatchUpExitCode -ne 0) {
    exit $CatchUpExitCode
}
exit $ReminderExitCode
