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

"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') catch-up start ====" | Add-Content -Path $LogFile -Encoding UTF8
& $Python -B -m src.main catch-up *>&1 | Tee-Object -FilePath $LogFile -Append
$ExitCode = $LASTEXITCODE
"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') catch-up exit $ExitCode ====" | Add-Content -Path $LogFile -Encoding UTF8
exit $ExitCode
