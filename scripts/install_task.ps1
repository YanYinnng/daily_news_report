param(
    [string]$TaskName = "WeChatDailyDigestNoon",
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

. (Join-Path $PSScriptRoot 'task_schedule.ps1')
$Plan = Get-DigestSchedulePlan -ProjectRoot $ProjectRoot
$FirstSlot = [datetimeoffset]::Parse($Plan.first_run_at)
$RunTime = $FirstSlot.ToString('HH:mm')

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`" -TaskName `"$TaskName`"" `
    -WorkingDirectory $ProjectRoot

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Triggers = @(New-DigestTaskTriggers -Plan $Plan -CurrentUser $CurrentUser -IncludeStartupTrigger:$IncludeStartupTrigger)
$RunSlots = @($Plan.run_slots | ForEach-Object { ([datetimeoffset]::Parse($_)).ToString('HH:mm') })

# Explicit time triggers also cover a program that starts and then exits with
# an application error. RestartOnFailure alone concerns task/action startup.
# All triggers move to the next due day once every report is delivered.
# A sleeping laptop may resume hours later, so do not kill it at four hours.
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Triggers `
    -Settings $Settings `
    -Principal $Principal `
    -Description "WeChat daily digest at $RunTime; retry incomplete reports at $($RunSlots -join ', '). After all reports are delivered, defer every trigger until next day. Do not wake the PC; catch up after resume or logon."

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    $BackupDir = Join-Path $ProjectRoot 'data\backups\tasks'
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    $BackupPath = Join-Path $BackupDir ("{0}_{1}.xml" -f $TaskName, (Get-Date -Format 'yyyyMMdd_HHmmss'))
    Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $BackupPath -Encoding Unicode
}

try {
    if ($ExistingTask) {
        # Preserve the existing task identity/history; do not create a duplicate.
        $ExistingTask.Actions = @($Action)
        $ExistingTask.Triggers = $Triggers
        $ExistingTask.Settings = $Settings
        $ExistingTask.Principal = $Principal
        $ExistingTask.Description = $Task.Description
        Set-ScheduledTask -InputObject $ExistingTask -ErrorAction Stop | Out-Null
    } else {
        Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force -ErrorAction Stop | Out-Null
    }
    Write-Host "Scheduled task '$TaskName' installed."
    Write-Host "Daily slots while incomplete: $($RunSlots -join ', '); also catch up at logon."
    Write-Host "First active trigger: $($Plan.first_run_at). After delivery, stop remaining checks that day."
    Write-Host "No wake or sleep prevention."
    if ($IncludeStartupTrigger) {
        Write-Host "Startup trigger included."
    }
} catch {
    Write-Warning "Register-ScheduledTask failed: $($_.Exception.Message)"
    Write-Warning "Falling back to a user-level daily task plus a Startup-folder catch-up command."

    $TaskRun = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`" -TaskName `"$TaskName`""
    $NoonTaskName = $TaskName
    schtasks.exe /Create /TN $NoonTaskName /TR $TaskRun /SC DAILY /ST $RunTime /F /RL LIMITED | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Fallback scheduled task creation failed." }

    Set-ScheduledTask -TaskName $NoonTaskName -Settings $Settings -Trigger $Triggers -Action $Action -ErrorAction Stop | Out-Null

    $StartupFolder = [Environment]::GetFolderPath("Startup")
    $StartupCmd = Join-Path $StartupFolder "${TaskName}CatchUp.cmd"
    $EscapedRunScript = $RunScript.Replace("'", "''")
    $EscapedTaskName = $TaskName.Replace("'", "''")
    $EncodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("& '$EscapedRunScript' -TaskName '$EscapedTaskName'"))
    @(
        "@echo off",
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $EncodedCommand"
    ) | Set-Content -Path $StartupCmd -Encoding ASCII

    Write-Host "Scheduled task '$NoonTaskName' installed for daily $RunTime."
    Write-Host "Startup catch-up command installed at '$StartupCmd'."
}
