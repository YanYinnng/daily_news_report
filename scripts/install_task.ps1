param(
    [string]$TaskName = "WeChatDailyDigest",
    [string]$ProjectRoot = "",
    [switch]$IncludeStartupTrigger
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

$RunScript = Join-Path $ProjectRoot "scripts\run_daily.ps1"
if (-not (Test-Path $RunScript)) {
    throw "Cannot find run script: $RunScript"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`""

$Triggers = @(
    (New-ScheduledTaskTrigger -Daily -At 12:00),
    (New-ScheduledTaskTrigger -AtLogOn)
)

if ($IncludeStartupTrigger) {
    $Triggers += New-ScheduledTaskTrigger -AtStartup
}

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Triggers `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Generate and email the WeChat official account daily digest. Missed noon runs are caught up on next logon."

try {
    Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
    Write-Host "Scheduled task '$TaskName' installed."
    Write-Host "Triggers: daily at 12:00 and at logon."
    if ($IncludeStartupTrigger) {
        Write-Host "Startup trigger included."
    }
} catch {
    Write-Warning "Register-ScheduledTask failed: $($_.Exception.Message)"
    Write-Warning "Falling back to a user-level daily task plus a Startup-folder catch-up command."

    $TaskRun = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$RunScript`""
    $NoonTaskName = "${TaskName}Noon"
    schtasks.exe /Create /TN $NoonTaskName /TR $TaskRun /SC DAILY /ST 12:00 /F /RL LIMITED | Out-Host

    $FallbackSettings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -WakeToRun `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    Set-ScheduledTask -TaskName $NoonTaskName -Settings $FallbackSettings | Out-Null

    $StartupFolder = [Environment]::GetFolderPath("Startup")
    $StartupCmd = Join-Path $StartupFolder "${TaskName}CatchUp.cmd"
    $EncodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("& '$RunScript'"))
    @(
        "@echo off",
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $EncodedCommand"
    ) | Set-Content -Path $StartupCmd -Encoding ASCII

    Write-Host "Scheduled task '$NoonTaskName' installed for daily 12:00."
    Write-Host "Startup catch-up command installed at '$StartupCmd'."
}
