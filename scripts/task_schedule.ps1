# Shared by installation and the daily wrapper. No commands run when dot-sourced.
function Get-DigestSchedulePlan {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $TaskPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    Push-Location -LiteralPath $ProjectRoot
    try {
        $PlanText = & $TaskPython -B -m src.task_runner --schedule-plan
        $PlanExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($PlanExitCode -ne 0) { throw 'Cannot read report completion state for Windows scheduling.' }
    $JsonOptions = @{}
    # PowerShell 7 may turn ISO strings into DateTime values; 5.1 keeps text.
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
        $JsonOptions['DateKind'] = 'String'
    }
    $Plan = $PlanText | ConvertFrom-Json @JsonOptions
    $Plan.first_run_at = ([datetimeoffset]::Parse($Plan.first_run_at)).ToString('yyyy-MM-ddTHH:mm:sszzz')
    return $Plan
}

function New-DigestTaskTriggers {
    param(
        [Parameter(Mandatory = $true)]$Plan,
        [Parameter(Mandatory = $true)][string]$CurrentUser,
        [switch]$IncludeStartupTrigger
    )
    $Triggers = @()
    $SlotIndex = 0
    foreach ($Slot in $Plan.run_slots) {
        $At = [datetimeoffset]::Parse($Slot)
        $Trigger = New-ScheduledTaskTrigger -Daily -At $At.LocalDateTime
        # Keep the explicit configured offset instead of silently using PC time.
        $Trigger.StartBoundary = $At.ToString('yyyy-MM-ddTHH:mm:sszzz')
        $Trigger.Id = "DigestSlot$SlotIndex"
        $Triggers += $Trigger
        $SlotIndex++
    }
    $Logon = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    $FirstBoundary = ([datetimeoffset]::Parse($Plan.first_run_at)).ToString('yyyy-MM-ddTHH:mm:sszzz')
    $Logon.StartBoundary = $FirstBoundary
    $Logon.Id = 'DigestLogon'
    $Triggers += $Logon
    if ($IncludeStartupTrigger) {
        $Startup = New-ScheduledTaskTrigger -AtStartup
        $Startup.StartBoundary = $FirstBoundary
        $Startup.Id = 'DigestStartup'
        $Triggers += $Startup
    }
    return $Triggers
}

function Sync-DigestTaskSchedule {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$TaskName
    )
    $Plan = Get-DigestSchedulePlan -ProjectRoot $ProjectRoot
    # Failed / unsent reports retain every remaining retry slot. Never cancel
    # retries based on process exit code or generation alone.
    if (-not $Plan.complete) { return }
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $Task) { throw "Scheduled task '$TaskName' was not found." }
    $HasStartup = @($Task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }).Count -gt 0
    $Triggers = @(New-DigestTaskTriggers -Plan $Plan -CurrentUser $Task.Principal.UserId -IncludeStartupTrigger:$HasStartup)
    $Task.Triggers = $Triggers
    Set-ScheduledTask -InputObject $Task -ErrorAction Stop | Out-Null
    $Message = "All due reports delivered; remaining checks deferred until $($Plan.first_run_at)."
    $LogDir = Split-Path -Parent $Plan.log_path
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    [System.IO.File]::AppendAllText($Plan.log_path, "`n$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message`n", [System.Text.UTF8Encoding]::new($false))
    Write-Host $Message
}
