# Registers a Windows scheduled task that runs `paperbot run` every few minutes while you're
# logged in. Run from PowerShell:   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
param([int]$Minutes = 10)

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Not found: $pythonw - create the virtual environment first (see README)." }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m paperbot run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
# Interactive = runs only while you're logged in, as you (needed for your saved VPN sign-in).
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName "PaperBot" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "PaperBot: answers paper requests by email ($root)" -Force | Out-Null
Write-Host "PaperBot will check mail every $Minutes minutes. Logs: $root\data\logs\paperbot.log"
Write-Host "Remove with: powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1"
