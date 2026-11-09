"""The browser for papers that need a person.

A browser that is remote-controlled (as PaperBot's automatic fetching is) can never pass
"verify you are human" checks: they detect the remote control and keep asking, even after
a person solves them. So for the human step PaperBot opens an *ordinary* Edge/Chrome window
that nothing controls:

* its own profile, separate from your everyday browser;
* traffic through PaperBot's IPv4 relay, so publishers see the institute address over the VPN
  (and the IP doesn't flip between IPv4 and IPv6, which also makes such checks loop);
* PDFs download straight into a folder PaperBot watches; it picks the file up and emails it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from .ipv4proxy import IPv4Proxy
from .sources import is_pdf

log = logging.getLogger(__name__)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
EXE = {"msedge": ("msedge.exe", r"Microsoft\Edge\Application\msedge.exe"),
       "chrome": ("chrome.exe", r"Google\Chrome\Application\chrome.exe")}


class ManualControl:
    """Lets the app say 'skip this paper' or 'stop' while PaperBot waits for a download."""

    def __init__(self):
        self.skip = threading.Event()
        self.stop = threading.Event()


def find_browser(channel: str) -> str:
    exe, rel = EXE.get(channel, EXE["msedge"])
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                    path = winreg.QueryValue(k, None)
                if path and Path(path).exists():
                    return path
            except OSError:
                continue
    except ImportError:
        pass
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("LOCALAPPDATA")):
        if base and (Path(base) / rel).exists():
            return str(Path(base) / rel)
    raise FileNotFoundError(f"{exe} not found - pick another browser in Settings")


class HumanBrowser:
    """Use as a context manager around a batch; capture() once per paper."""

    def __init__(self, browser: str, data_dir: Path, max_pdf_bytes: int = 60_000_000,
                 control: ManualControl | None = None, say=lambda msg: None,
                 timeout_s: float = 900):
        self.exe = find_browser(browser)
        self.profile = Path(data_dir) / "manual-browser"
        self.inbox = Path(data_dir) / "manual-inbox"
        self.max_pdf_bytes = max_pdf_bytes
        self.control = control or ManualControl()
        self.say = say
        self.timeout_s = timeout_s
        self._proxy: IPv4Proxy | None = None
        self._proc: subprocess.Popen | None = None
        self._taken: set[str] = set()      # hashes of PDFs already used in this session
        self._seen: set[str] = set()

    # ---- lifecycle -----------------------------------------------------------------
    def __enter__(self):
        self._close_browser()              # a leftover window would use a dead relay port
        self.inbox.mkdir(parents=True, exist_ok=True)
        for f in self.inbox.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
        self._write_prefs()
        self._proxy = IPv4Proxy().start()
        return self

    def __exit__(self, *exc):
        self._close_browser()
        if self._proxy:
            self._proxy.stop()

    def _write_prefs(self) -> None:
        """Downloads go straight to the watched folder; PDFs download instead of opening."""
        prefs_file = self.profile / "Default" / "Preferences"
        prefs_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prefs = {}
        prefs.setdefault("download", {}).update(
            {"default_directory": str(self.inbox), "prompt_for_download": False,
             "directory_upgrade": True})
        prefs.setdefault("savefile", {})["default_directory"] = str(self.inbox)
        prefs.setdefault("plugins", {})["always_open_pdf_externally"] = True
        prefs.setdefault("profile", {}).update({"exit_type": "Normal", "exited_cleanly": True})
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")

    def _open(self, url: str) -> None:
        args = [self.exe, f"--user-data-dir={self.profile}",
                f"--proxy-server={self._proxy.url}", "--no-first-run",
                "--no-default-browser-check", url]
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(args)            # starts our own browser instance
        else:
            subprocess.Popen(args)                          # new tab in the running instance

    def _our_pids(self) -> list[int]:
        token = str(self.profile).replace("'", "''")
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like "
             f"'*--user-data-dir={token}*' -and $_.CommandLine -notlike '*--type=*' }} | "
             f"ForEach-Object {{ $_.ProcessId }}"],
            capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        return [int(x) for x in cp.stdout.split() if x.strip().isdigit()]

    def _close_browser(self) -> None:
        """Close only the browser instance using PaperBot's own profile."""
        pids = self._our_pids()
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True,
                           creationflags=NO_WINDOW)
        if pids:
            time.sleep(3)
            for pid in self._our_pids():
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                               creationflags=NO_WINDOW)
        self._proc = None

    # ---- one paper -------------------------------------------------------------------
    def capture(self, paper, start_url: str | None) -> bytes | None:
        """Open the paper and wait until a new PDF lands in the watched folder.
        Returns None if skipped, stopped, timed out, or the browser was closed."""
        start = start_url or paper.landing_url or (
            f"https://doi.org/{paper.doi}" if paper.doi else None)
        if not start or self.control.stop.is_set():
            return None
        self.control.skip.clear()
        self._seen.update(p.name for p in self.inbox.glob("*"))
        self._open(start)
        deadline = time.monotonic() + self.timeout_s
        sizes: dict[str, int] = {}
        while time.monotonic() < deadline:
            if self.control.stop.is_set() or self.control.skip.is_set():
                return None
            if self._proc is not None and self._proc.poll() is not None and \
                    not self._our_pids():
                self.control.stop.set()   # the person closed the browser: they're done
                return None
            for f in sorted(self.inbox.glob("*.pdf"), key=lambda p: p.name):
                if f.name in self._seen:
                    continue
                try:
                    size = f.stat().st_size
                except OSError:                 # renamed/removed mid-download
                    continue
                if sizes.get(f.name) != size:   # still being written: check again shortly
                    sizes[f.name] = size
                    continue
                self._seen.add(f.name)
                data = f.read_bytes()
                digest = hashlib.sha1(data).hexdigest()
                if not is_pdf(data) or len(data) > self.max_pdf_bytes or digest in self._taken:
                    continue
                self._taken.add(digest)
                self.say(f"Got {f.name} ({len(data) // 1024} KB).")
                return data
            time.sleep(0.7)
        return None
