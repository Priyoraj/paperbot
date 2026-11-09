# Registers a Windows scheduled task that runs `paperbot run` every few minutes while you're
# logged in, and one minute after you sign in to Windows.
# Run from PowerShell:   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
param([int]$Minutes = 10)

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Not found: $pythonw - create the virtual environment first (see README)." }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m paperbot run" -WorkingDirectory $root
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$signin = New-ScheduledTaskTrigger -AtLogOn -User $user
$signin.Delay = 'PT1M'   # first check one minute after you sign in to Windows
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
# Interactive = runs only while you're logged in, as you (needed for your saved VPN sign-in).
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive

Register-ScheduledTask -TaskName "PaperBot" -Action $action -Trigger @($every, $signin) `
    -Settings $settings -Principal $principal `
    -Description "PaperBot: answers paper requests by email ($root)" -Force | Out-Null
Write-Host "PaperBot will check mail 1 minute after you sign in to Windows and every $Minutes minutes."
Write-Host "Logs: $root\data\logs\paperbot.log"
Write-Host "Remove with: powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1"
