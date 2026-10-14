# Installs the merge train on the desktop: every two minutes, one cycle over the repositories listed
# in repos.json (see automerge.py). Run it from a normal PowerShell window of the owner's session:
# scheduled tasks created from a sandboxed or elevated shell can be private to that shell.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File train\install.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File train\install.ps1 -Remove
#
# The script is copied to $Destination, so the task does not depend on which branch a checkout is
# on (the crm-platform train learned this the hard way). Run this again after automerge.py changes.
# repos.json is created from repos.example.json only when it does not exist; it is never overwritten.
# The task uses the owner's `gh` login; nothing else is stored.
param(
    [string]$Destination = 'D:\Development\Tools\ci-train',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$taskName = 'CI train'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$taskName' removed. Files in $Destination are left in place."
    exit 0
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'gh is not installed' }
gh auth status *> $null
if ($LASTEXITCODE -ne 0) { throw 'gh is not signed in: run gh auth login' }

# The real pythonw.exe, not the `pyw` launcher: under the task scheduler the launcher exited with
# code 2 and wrote nothing (crm-platform, 30/09/2026), while the interpreter itself runs fine.
$python = (py -3 -c "import sys;print(sys.executable)")
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found at $pythonw" }

$source = $PSScriptRoot
$script = Join-Path $Destination 'automerge.py'
$config = Join-Path $Destination 'repos.json'
$log = Join-Path $Destination 'train.log'

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Copy-Item -Force (Join-Path $source 'automerge.py') $script
if (-not (Test-Path $config)) {
    Copy-Item (Join-Path $source 'repos.example.json') $config
    Write-Host "Created $config from the example. Review the list of repositories."
}

# pythonw: no console window flashing every two minutes; the script writes its own log.
# No -WorkingDirectory: with it the task failed with 0x8007010B on this desktop (30/09/2026),
# although the directory exists; every path below is absolute anyway.
$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "`"$script`" --config `"$config`" --log `"$log`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
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
    -Description 'Merges pull requests labelled automerge whose gate is green and whose head contains main.' `
    -Force | Out-Null

Write-Host "Task '$taskName' installed: every 2 minutes."
Write-Host "  Configuration: $config"
Write-Host "  Log:           $log"
Write-Host "  Try without merging:  py -3 `"$script`" --config `"$config`" --dry-run"
Write-Host "  Remove:               train\install.ps1 -Remove"
