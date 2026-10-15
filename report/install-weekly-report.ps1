# Installs the weekly CI report on the desktop: every Monday at 08:00 it writes the report of the
# organisation to a file and raises a Windows toast only when the week deviates. Run it from a normal
# PowerShell window of the owner's session: scheduled tasks created from a sandboxed or elevated shell
# can be private to that shell.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File report\install-weekly-report.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File report\install-weekly-report.ps1 -Remove
#
# The scripts are copied to $Destination, so the task does not depend on which branch a checkout is on;
# run this again after weekly_report.py changes. The task uses the owner's `gh` login; nothing else is
# stored. Reports land in $Destination\reports as ci-report-<date>.md, with a copy as latest.md.
param(
    [string]$Organisation = 'naindev-contrast3d',
    [string]$Destination = 'D:\Development\Tools\ci-train\weekly',
    [string]$Notify = 'D:\Development\Tools\workstation-maintenance\notify.ps1',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$taskName = 'CI weekly report'

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
foreach ($file in 'weekly_report.py', 'runner_watch.py', 'ci_report.py') {
    Copy-Item -Force (Join-Path $PSScriptRoot $file) (Join-Path $Destination $file)
}
$script = Join-Path $Destination 'weekly_report.py'
$reports = Join-Path $Destination 'reports'

$arguments = "`"$script`" --org $Organisation --out-dir `"$reports`" --notify `"$Notify`""
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 8:00
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
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
    -Description "Writes the CI report of $Organisation to a file every Monday and toasts when the week deviates." `
    -Force | Out-Null

Write-Host "Task '$taskName' installed: every Monday at 08:00 (or at the next start if the desktop was off)."
Write-Host "  Reports: $reports"
Write-Host "  Try now: py -3 `"$script`" --org $Organisation --out-dir `"$reports`""
Write-Host "  Remove:  report\install-weekly-report.ps1 -Remove"
