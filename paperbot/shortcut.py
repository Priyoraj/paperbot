"""Desktop and Start-menu shortcuts for the PaperBot app.  `python -m paperbot.shortcut`"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import home_dir
from .winsvc import NO_WINDOW, _q, pythonw

ICON = Path(__file__).resolve().parent / "assets" / "paperbot.ico"


def create_shortcuts(desktop: bool = True, start_menu: bool = True) -> list[str]:
    folders = [f for f, on in (("Desktop", desktop), ("Programs", start_menu)) if on]
    script = f"""
$ws = New-Object -ComObject WScript.Shell
foreach ($folder in @({', '.join(_q(f) for f in folders)})) {{
  $dir = [Environment]::GetFolderPath($folder)
  $s = $ws.CreateShortcut((Join-Path $dir 'PaperBot.lnk'))
  $s.TargetPath = {_q(pythonw())}
  $s.Arguments = '-m paperbot.gui'
  $s.WorkingDirectory = {_q(home_dir())}
  $s.IconLocation = {_q(ICON)}
  $s.Description = 'PaperBot - answers paper requests'
  $s.Save()
  Join-Path $dir 'PaperBot.lnk'
}}"""
    cp = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                         script], capture_output=True, text=True, timeout=60,
                        creationflags=NO_WINDOW)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "could not create the shortcuts")
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


if __name__ == "__main__":
    for path in create_shortcuts():
        print("Shortcut:", path)
    sys.exit(0)
