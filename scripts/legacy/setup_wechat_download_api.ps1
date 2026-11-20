param(
    [string]$InstallDir = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "external\wechat-query-skill"),
    [string]$RepoUrl = "https://github.com/adennng/wechat-query-skill.git",
    [string]$SiteUrl = "http://localhost:5000",
    [string]$DebianMirror = "deb.debian.org",
    [int]$RateLimitGlobal = 120,
    [int]$RateLimitPerIp = 120,
    [int]$RateLimitArticleInterval = 1,
    [switch]$NoStart,
    [switch]$Update
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

Require-Command git
Require-Command docker

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $InstallDir) -Force | Out-Null
    git clone --depth 1 $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) { throw "Git clone failed." }
} elseif (Test-Path (Join-Path $InstallDir ".git")) {
    if ($Update) {
        git -C $InstallDir pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw "Git update failed." }
    }
} else {
    throw "InstallDir exists but is not a git checkout: $InstallDir"
}

$ServiceDir = Join-Path $InstallDir "services\wechat-download-api"
$EnvExample = Join-Path $ServiceDir "env.example"
$EnvPath = Join-Path $ServiceDir ".env"

if (-not (Test-Path $EnvPath)) {
    Copy-Item $EnvExample $EnvPath
}

$EnvText = Get-Content -Path $EnvPath -Encoding utf8 -Raw
if ($EnvText -match "(?m)^SITE_URL=") {
    $EnvText = $EnvText -replace "(?m)^SITE_URL=.*$", "SITE_URL=$SiteUrl"
} else {
    $EnvText = $EnvText.TrimEnd() + "`nSITE_URL=$SiteUrl`n"
}
if ($EnvText -match "(?m)^DEBIAN_MIRROR=") {
    $EnvText = $EnvText -replace "(?m)^DEBIAN_MIRROR=.*$", "DEBIAN_MIRROR=$DebianMirror"
} else {
    $EnvText = $EnvText.TrimEnd() + "`nDEBIAN_MIRROR=$DebianMirror`n"
}
if ($EnvText -match "(?m)^RATE_LIMIT_GLOBAL=") {
    $EnvText = $EnvText -replace "(?m)^RATE_LIMIT_GLOBAL=.*$", "RATE_LIMIT_GLOBAL=$RateLimitGlobal"
} else {
    $EnvText = $EnvText.TrimEnd() + "`nRATE_LIMIT_GLOBAL=$RateLimitGlobal`n"
}
if ($EnvText -match "(?m)^RATE_LIMIT_PER_IP=") {
    $EnvText = $EnvText -replace "(?m)^RATE_LIMIT_PER_IP=.*$", "RATE_LIMIT_PER_IP=$RateLimitPerIp"
} else {
    $EnvText = $EnvText.TrimEnd() + "`nRATE_LIMIT_PER_IP=$RateLimitPerIp`n"
}
if ($EnvText -match "(?m)^RATE_LIMIT_ARTICLE_INTERVAL=") {
    $EnvText = $EnvText -replace "(?m)^RATE_LIMIT_ARTICLE_INTERVAL=.*$", "RATE_LIMIT_ARTICLE_INTERVAL=$RateLimitArticleInterval"
} else {
    $EnvText = $EnvText.TrimEnd() + "`nRATE_LIMIT_ARTICLE_INTERVAL=$RateLimitArticleInterval`n"
}
Set-Content -Path $EnvPath -Value $EnvText -Encoding utf8

if (-not $NoStart) {
    $PreviousServiceDir = $env:WDA_SERVICE_DIR
    $env:WDA_SERVICE_DIR = $ServiceDir
    try {
        $ComposeFile = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "config\wechat-service.compose.yaml"
        docker compose --env-file $EnvPath -f $ComposeFile up -d --build
        if ($LASTEXITCODE -ne 0) { throw "Docker build/start failed. Start Docker Desktop and inspect the error above." }
    } finally {
        $env:WDA_SERVICE_DIR = $PreviousServiceDir
    }
}

Write-Host ""
Write-Host "wechat-download-api is prepared at: $ServiceDir"
Write-Host "Next steps:"
Write-Host "1. Start Docker Desktop if it is not running."
Write-Host "2. Visit $SiteUrl/login.html and scan the QR code with a WeChat official-account admin account."
Write-Host "3. Set config/settings.yaml -> wechat_download_api.enabled: true."
Write-Host "4. Run: .\.venv\Scripts\python.exe -B -m src.main validate-accounts --limit 3"
