param([switch]$StopService)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$MarkerPath = Join-Path $ProjectRoot 'data\report_viewer.json'
if (Test-Path -LiteralPath $MarkerPath) {
    $Marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Marker.project_root -ne $ProjectRoot) { throw "Viewer marker belongs to another project." }
    $Owner = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$Marker.process_id)"
    # PowerShell 7 may deserialize ISO timestamps as DateTime; compare instants,
    # preserving fractional seconds, rather than their different string formats.
    $RecordedCreatedAt = if ($Marker.created_at -is [datetime]) {
        $Marker.created_at.ToUniversalTime()
    } else {
        [datetime]::Parse([string]$Marker.created_at, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    }
    if ($Owner -and $Owner.CreationDate.ToUniversalTime() -eq $RecordedCreatedAt -and $Owner.CommandLine -match 'src\.main\s+serve-reports') {
        Stop-Process -Id $Owner.ProcessId
        Write-Host "Project report viewer stopped."
    } elseif ($Owner) {
        throw "Process identity changed; refusing to stop a different process."
    }
    Remove-Item -LiteralPath $MarkerPath
} else {
    Write-Host "No viewer started by start_project.ps1 was recorded."
}
if ($StopService) {
    $ServiceEnv = Join-Path $ProjectRoot 'external\wechat-query-skill\services\wechat-download-api\.env'
    $ComposeFile = Join-Path $ProjectRoot 'config\wechat-service.compose.yaml'
    docker compose --env-file $ServiceEnv -f $ComposeFile stop wechat-api
    if ($LASTEXITCODE -ne 0) { throw "Service stop failed." }
    Write-Host "Project service stopped; persisted data was retained."
}
