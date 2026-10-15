# Installs the runner watchdog on the desktop: every five minutes, one cycle of runner_watch.py, and a
# Windows toast when a runner of the organisation is offline or a run waits too long for one. Run it
# from a normal PowerShell window of the owner's session: scheduled tasks created from a sandboxed
# or elevated shell can be private to that shell.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File report\install-runner-watch.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File report\install-runner-watch.ps1 -Remove
#
# The scripts are copied to $Destination, so the task does not depend on which branch a checkout is
# on; run this again after runner_watch.py changes. The task uses the owner's `gh` login; nothing
# else is stored. State lives in $Destination\runner-watch.json.
param(
    [string]$Organisation = 'naindev-contrast3d',
    [string]$Destination = 'D:\Development\Tools\ci-train\watch',
    [string]$Notify = 'D:\Development\Tools\workstation-maintenance\notify.ps1',
    # Runners that are offline on purpose and must never raise a toast.
    [string[]]$Ignore = @(),
    [int]$OfflineMinutes = 10,
    [int]$QueueMinutes = 15,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$taskName = 'CI runner watch'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$taskName' removed. Files in $Destination are left in place."
    exit 0
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'gh is not installed' }
gh auth status *> $null
if ($LASTEXITCODE -ne 0) { throw 'gh is not signed in: run gh auth login' }
if (-not (Test-Path $Notify)) { throw "notify.ps1 not found at $Notify (pass -Notify)" }

# The real pythonw.exe, not the `pyw` launcher (see train\install.ps1 for why).
$python = (py -3 -c "import sys;print(sys.executable)")
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found at $pythonw" }

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
foreach ($file in 'runner_watch.py', 'ci_report.py') {
    Copy-Item -Force (Join-Path $PSScriptRoot $file) (Join-Path $Destination $file)
}
$script = Join-Path $Destination 'runner_watch.py'
$state = Join-Path $Destination 'runner-watch.json'

$arguments = "`"$script`" --org $Organisation --notify `"$Notify`" --state `"$state`"" +
    " --offline-min $OfflineMinutes --queue-min $QueueMinutes"
foreach ($name in $Ignore) { $arguments += " --ignore `"$name`"" }

# pythonw: no console window flashing every five minutes. No -WorkingDirectory (see the train).
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Toast when a runner of $Organisation is offline for $OfflineMinutes min or a run is queued for $QueueMinutes min." `
    -Force | Out-Null

Write-Host "Task '$taskName' installed: every 5 minutes."
Write-Host "  State:   $state"
Write-Host "  Try now: py -3 `"$script`" --org $Organisation --state `"$state`""
Write-Host "  Remove:  report\install-runner-watch.ps1 -Remove"
