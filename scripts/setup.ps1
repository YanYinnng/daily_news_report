param([switch]$ConfigOnly)
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

# Bootstrap only missing local configuration; never replace personal files.
New-Item -ItemType Directory -Path (Join-Path $ProjectRoot 'data') -Force | Out-Null
$Templates = @{
    'config/accounts.yaml' = 'config/accounts.example.yaml'
    'config/themes.yaml' = 'config/themes.example.yaml'
    'config/public_feeds.json' = 'config/public_feeds.example.json'
    'data/wechat_download_api_accounts.json' = 'config/identity.example.json'
    '.env' = '.env.example'
}
foreach ($Target in $Templates.Keys) {
    if (-not (Test-Path -LiteralPath $Target)) {
        Copy-Item -LiteralPath $Templates[$Target] -Destination $Target
    }
}
if ($ConfigOnly) {
    Write-Host 'Local configuration initialized. Existing files were preserved.'
    return
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt -c requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
& ".\.venv\Scripts\python.exe" -m pip check
if ($LASTEXITCODE -ne 0) { throw "Dependency verification failed." }

Write-Host "Setup complete. Python is at .\.venv\Scripts\python.exe"
