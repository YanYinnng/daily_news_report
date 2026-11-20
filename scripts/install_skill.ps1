# Installs only the four files managed by this project and a local root pointer.
param([string]$CodexDir = '')
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($CodexDir)) {
    $CodexDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
}
$Source = Join-Path $ProjectRoot 'skills\wechat-public-articles'
$Destination = Join-Path ([IO.Path]::GetFullPath($CodexDir)) 'skills\wechat-public-articles'
$Files = @('SKILL.md', 'agents\openai.yaml', 'references\local-reader.md', 'scripts\wechat_articles.py')
foreach ($File in $Files) {
    if (-not (Test-Path -LiteralPath (Join-Path $Source $File) -PathType Leaf)) { throw "Missing skill source: $File" }
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'src\article_reader.py') -PathType Leaf)) { throw 'Missing project reader.' }

$Locator = Join-Path $Destination 'project-root.txt'
$Changes = @($Files | Where-Object {
    $Current = Join-Path $Destination $_
    (Test-Path -LiteralPath $Current) -and ((Get-FileHash -LiteralPath $Current).Hash -ne (Get-FileHash -LiteralPath (Join-Path $Source $_)).Hash)
})
if ((Test-Path -LiteralPath $Locator) -and ((Get-Content -LiteralPath $Locator -Raw -Encoding UTF8).Trim() -ne $ProjectRoot)) {
    $Changes += 'project-root.txt'
}
if ($Changes.Count) {
    $Backup = Join-Path $ProjectRoot ('data\backups\skills\wechat-public-articles_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
    foreach ($File in $Changes) {
        $BackupFile = Join-Path $Backup $File
        New-Item -ItemType Directory -Path (Split-Path -Parent $BackupFile) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $Destination $File) -Destination $BackupFile
    }
    Write-Host "Previous skill files backed up to $Backup."
}
foreach ($File in $Files) {
    $Target = Join-Path $Destination $File
    New-Item -ItemType Directory -Path (Split-Path -Parent $Target) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Source $File) -Destination $Target -Force
}
[IO.File]::WriteAllText($Locator, $ProjectRoot, [Text.UTF8Encoding]::new($false))
Write-Host "Installed wechat-public-articles at $Destination. Reads are delegated to the project; no tasks started."
