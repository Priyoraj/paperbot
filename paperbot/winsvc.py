"""Automatic mode = a Windows scheduled task that runs `paperbot run` every few minutes while
you're logged in (needed for your saved VPN sign-in), and one minute after you sign in to
Windows. A second task can open the app itself, minimised to the taskbar, after sign-in.
Both are controlled from the app's Home page."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import home_dir

TASK = "PaperBot"                # the automatic mail check
APP_TASK = "PaperBot app"        # the app window, opened after sign-in
SIGNIN_DELAY = "PT1M"    # both start this long after you sign in to Windows
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class TaskStatus:
    installed: bool
    state: str = ""                  # Ready | Running | Disabled
    last_run: datetime | None = None
    next_run: datetime | None = None
    last_result: int | None = None

    @property
    def on(self) -> bool:
        return self.installed and self.state != "Disabled"


def _ps(script: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                           script], capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW)


def _q(s: str) -> str:
    """PowerShell single-quoted string."""
    return "'" + str(s).replace("'", "''") + "'"


def _time(s: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    return t if t.year > 2000 else None  # Windows reports 1999-11-30 for "never"


def status(task: str = TASK) -> TaskStatus:
    cp = _ps(f"""$t = Get-ScheduledTask -TaskName {_q(task)} -ErrorAction SilentlyContinue
if (-not $t) {{ 'NONE' }} else {{
  $i = $t | Get-ScheduledTaskInfo
  $l = if ($i.LastRunTime) {{ $i.LastRunTime.ToString('s') }} else {{ '' }}
  $n = if ($i.NextRunTime) {{ $i.NextRunTime.ToString('s') }} else {{ '' }}
  '{{0}}|{{1}}|{{2}}|{{3}}' -f $t.State, $l, $n, $i.LastTaskResult }}""")
    out = cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else "NONE"
    if out == "NONE" or "|" not in out:
        return TaskStatus(installed=False)
    state, last, nxt, result = (out.split("|") + ["", "", "", ""])[:4]
    return TaskStatus(True, state, _time(last), _time(nxt),
                      int(result) if result.strip().lstrip("-").isdigit() else None)


def pythonw() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def install(minutes: int = 10) -> None:
    """Turn automatic mode on (re-installing replaces any older task). Checks run every
    `minutes`, and also one minute after you sign in to Windows, so mail is picked up soon
    after the computer starts instead of at the next 10-minute slot."""
    root = home_dir()
    script = f"""
$user = "$env:USERDOMAIN\\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute {_q(pythonw())} -Argument '-m paperbot run' -WorkingDirectory {_q(root)}
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes {int(minutes)})
$signin = New-ScheduledTaskTrigger -AtLogOn -User $user
$signin.Delay = '{SIGNIN_DELAY}'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive
Register-ScheduledTask -TaskName {_q(TASK)} -Action $action -Trigger @($every, $signin) -Settings $settings -Principal $principal -Description 'PaperBot: answers paper requests by email' -Force | Out-Null
'OK'"""
    cp = _ps(script, timeout=120)
    if "OK" not in cp.stdout:
        raise RuntimeError("Windows didn't accept the scheduled task: " +
                           (cp.stderr.strip().splitlines() or ["unknown error"])[-1])


def uninstall(task: str = TASK) -> None:
    """Turn automatic mode off."""
    cp = _ps(f"Unregister-ScheduledTask -TaskName {_q(task)} -Confirm:$false; 'OK'")
    if "OK" not in cp.stdout and "No MSFT_ScheduledTask objects found" not in cp.stderr:
        raise RuntimeError((cp.stderr.strip().splitlines() or ["unknown error"])[-1])


# ---- the app itself, opened when Windows starts --------------------------------------
def app_start_status() -> TaskStatus:
    return status(APP_TASK)


def install_app_start() -> None:
    """Open the app, minimised to the taskbar, one minute after every Windows sign-in. (After
    sign-in rather than at power-on: a window needs someone signed in to show it to.)"""
    script = f"""
$user = "$env:USERDOMAIN\\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute {_q(pythonw())} -Argument '-m paperbot.gui --minimized' -WorkingDirectory {_q(home_dir())}
$signin = New-ScheduledTaskTrigger -AtLogOn -User $user
$signin.Delay = '{SIGNIN_DELAY}'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 4
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive
Register-ScheduledTask -TaskName {_q(APP_TASK)} -Action $action -Trigger $signin -Settings $settings -Principal $principal -Description 'PaperBot: opens the app in the taskbar after you sign in' -Force | Out-Null
'OK'"""
    cp = _ps(script, timeout=120)
    if "OK" not in cp.stdout:
        raise RuntimeError("Windows didn't accept the scheduled task: " +
                           (cp.stderr.strip().splitlines() or ["unknown error"])[-1])


def uninstall_app_start() -> None:
    uninstall(APP_TASK)
