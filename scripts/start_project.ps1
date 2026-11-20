param(
    [switch]$CheckOnly,
    [switch]$SkipService,
    [switch]$LegacyService,
    [switch]$NoViewer
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "Run scripts/setup.ps1 first." }
if ($CheckOnly) {
    & $Python -B -m src.main doctor
    exit $LASTEXITCODE
}
if ($LegacyService -and -not $SkipService) {
    $ComposeFile = Join-Path $ProjectRoot "config\wechat-service.compose.yaml"
    $ServiceEnv = Join-Path $ProjectRoot "external\wechat-query-skill\services\wechat-download-api\.env"
    if (-not (Test-Path -LiteralPath $ServiceEnv)) { throw "Run scripts/legacy/setup_wechat_download_api.ps1 first." }
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw "Start Docker Desktop, wait until it is ready, then run this script again." }
    docker compose --env-file $ServiceEnv -f $ComposeFile up -d --no-build --wait --wait-timeout 60
    if ($LASTEXITCODE -ne 0) { throw "Service start failed. For a missing image, run scripts/legacy/setup_wechat_download_api.ps1." }
    Write-Host "Login: http://localhost:5000/login.html"
}
if (-not $NoViewer) {
    $Existing = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($Existing) {
        Write-Host "Port 8765 already has a listener; no duplicate viewer was started."
    } else {
        $LogDir = Join-Path $ProjectRoot "logs"
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        $ViewerProcess = Start-Process -FilePath $Python -ArgumentList '-B','-m','src.main','serve-reports','--host','127.0.0.1','--port','8765' -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'viewer.out.log') -RedirectStandardError (Join-Path $LogDir 'viewer.err.log')
        $Ready = $false
        for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
            Start-Sleep -Milliseconds 500
            try {
                $Response = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/reports' -UseBasicParsing -TimeoutSec 2
                if ($Response.StatusCode -eq 200) { $Ready = $true; break }
            } catch { }
            if ($ViewerProcess.HasExited) { break }
        }
        if (-not $Ready) { throw "Viewer did not become ready; inspect logs/viewer.err.log." }
        $Listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen
        $Owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($Listener.OwningProcess)"
        if ($Owner.ProcessId -ne $ViewerProcess.Id -and $Owner.ParentProcessId -ne $ViewerProcess.Id) {
            throw "Port 8765 was claimed by another process; viewer ownership could not be verified."
        }
        $DataDir = Join-Path $ProjectRoot 'data'
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
        @{ process_id = $Owner.ProcessId; created_at = $Owner.CreationDate.ToUniversalTime().ToString('o'); project_root = $ProjectRoot } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $DataDir 'report_viewer.json') -Encoding UTF8
    }
    Write-Host "Reports: http://127.0.0.1:8765"
}
Write-Host "No reports or emails were generated. Run doctor after QR login; enable scheduling separately."
