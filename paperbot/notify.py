"""Windows notifications for papers that need the owner.

Whichever part of PaperBot notices first on this computer shows it: the automatic background
check when it ends, or the open app within a few seconds. Each computer keeps its own note of
what it has announced (the `toasted` table, which is not among the records computers share),
so with PaperBot on two computers each one tells you once.

Clicking a notification opens the app on its "Needs you" page: the notification carries the
link `paperbot:needs`, and register() tells Windows that such links start this app.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .config import APP_NAME, Config

log = logging.getLogger(__name__)
APP_ID = "PaperBot.Desktop"       # who the notification is from; also the app's taskbar identity
SCHEME = "paperbot"               # paperbot:needs = open the app on "Needs you"
PAGES = ("home", "needs", "people", "requests", "try", "settings", "share")
ICON = Path(__file__).resolve().parent / "assets" / "paperbot-256.png"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Both run in Windows PowerShell 5.1, which can reach the notification API without extra
# software. ASK: what does Windows say about showing PaperBot's notifications? SHOW: the same
# question first - Windows takes a notification without complaint even when it is set to show
# none - and then the notification, which arrives in an environment variable (nothing to quote).
ASK = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:PAPERBOT_APP_ID)
'SETTING ' + $notifier.Setting
"""
SHOW = ASK + r"""
if ("$($notifier.Setting)" -eq 'Enabled') {
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml($env:PAPERBOT_TOAST)
  $notifier.Show((New-Object Windows.UI.Notifications.ToastNotification $xml))
  'OK'
}
"""
WINDOWS_SETTINGS = "ms-settings:notifications"    # the page where the owner switches them on
WHY_NOT = {
    "DisabledForUser": "Windows notifications are switched off on this computer. Turn them on "
                       "in Windows Settings > System > Notifications.",
    "DisabledForApplication": "Notifications from PaperBot are switched off in Windows Settings "
                              "> System > Notifications.",
    "DisabledByGroupPolicy": "This computer's organisation settings don't allow notifications.",
    "DisabledByManifest": "Windows doesn't let PaperBot show notifications on this computer.",
}


class NotificationsOff(RuntimeError):
    """Windows is set not to show notifications: all of them, or PaperBot's."""

    def __init__(self, setting: str):
        self.setting = setting
        super().__init__(WHY_NOT.get(setting, f"Windows won't show notifications ({setting})."))


def page_from(link: str | None) -> str:
    """'paperbot:needs', 'paperbot://needs/' or 'needs' -> 'needs'; anything else -> ''."""
    page = (link or "").strip().lower()
    if page.startswith(SCHEME + ":"):
        page = page[len(SCHEME) + 1:]
    page = page.strip("/ ")
    return page if page in PAGES else ""


# ---- what to say ---------------------------------------------------------------------------
def paper_key(db, row) -> str:
    """Stays the same when computers put their records together (paper numbers can change)."""
    request = db.request(row["request_id"])
    basis = f"{request['message_id']}|{row['raw']}|{row['status']}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _name(row, limit: int = 70) -> str:
    title = " ".join((row["title"] or "").split())
    if not title:
        return row["raw"]
    return '"' + (title if len(title) <= limit else title[: limit - 1] + "…") + '"'


def wording(rows) -> tuple[str, list[str]]:
    """(headline, up to two lines) for papers that have newly come to need the owner."""
    manual = [r for r in rows if r["status"] == "manual"]
    first = rows[0]
    who = first["sender_name"] or first["sender"]
    if len(rows) == 1 and manual:
        return ("A paper needs you",
                [f"{who} asked for {_name(first)}.",
                 "The publisher wants a person to download it. Click to do it in PaperBot."])
    if len(rows) == 1:
        return ("A paper is too large to email",
                [f"{_name(first)} for {who}.", "Click to find the file and share it yourself."])
    rest = len(rows) - 1
    what = ("The publisher wants a person to download them." if len(manual) == len(rows) else
            "They need a download by hand, or are too large to email.")
    return (f"{len(rows)} papers need you",
            [f"{_name(first, 50)} and {rest} more.", what + " Click to open PaperBot."])


def claim_new(cfg: Config, db, rows=None) -> list:
    """The papers on the Needs-you list that this computer hasn't announced yet - now marked as
    announced, so that only one of the background check and the app speaks up."""
    if not cfg.notify_desktop:
        return []
    return [r for r in (db.owner_todo() if rows is None else rows)
            if db.claim_toast(paper_key(db, r))]


def announce(cfg: Config, db, show=None) -> int:
    """Tell the owner about papers that have newly come to need them. Returns how many."""
    fresh = claim_new(cfg, db)
    if fresh:
        (show or show_toast)(*wording(fresh))
    return len(fresh)


# ---- showing it ------------------------------------------------------------------------------
def toast_xml(title: str, lines: list[str], page: str = "needs") -> str:
    texts = "".join(f"<text>{escape(t)}</text>" for t in [title, *lines[:2]])
    return (f'<toast activationType="protocol" launch={quoteattr(f"{SCHEME}:{page}")} '
            f'duration="long"><visual><binding template="ToastGeneric">{texts}</binding>'
            "</visual></toast>")


def _ask_windows(script: str, **more) -> tuple[str, subprocess.CompletedProcess]:
    """Run ASK or SHOW. Returns (what Windows says about PaperBot's notifications, the run)."""
    register()
    env = dict(os.environ, PAPERBOT_APP_ID=APP_ID, **more)
    cp = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                         "Bypass", "-Command", script], capture_output=True, text=True,
                        timeout=60, env=env, creationflags=NO_WINDOW)
    setting = next((line.split(None, 1)[1].strip() for line in cp.stdout.splitlines()
                    if line.startswith("SETTING ") and len(line.split()) > 1), "")
    return setting, cp


def windows_setting() -> str:
    """'Enabled' when Windows will show PaperBot's notifications. Otherwise why not, e.g.
    'DisabledForUser' = notifications are off for this Windows account (see WHY_NOT).
    '' when it can't be asked."""
    if sys.platform != "win32":
        return ""
    return _ask_windows(ASK)[0]


def show_toast(title: str, lines: list[str], page: str = "needs") -> None:
    """Show a Windows notification from PaperBot. Raises NotificationsOff when Windows is
    set not to show it, RuntimeError when it wouldn't take it."""
    if sys.platform != "win32":
        return
    setting, cp = _ask_windows(SHOW, PAPERBOT_TOAST=toast_xml(title, lines, page))
    if setting and setting != "Enabled":
        raise NotificationsOff(setting)
    if "OK" not in cp.stdout:
        raise RuntimeError((cp.stderr.strip().splitlines() or
                            ["Windows didn't show the notification"])[-1])


def registration() -> dict[str, dict[str, str]]:
    """What register() keeps under HKEY_CURRENT_USER: {key: {value name: value}}."""
    from .winsvc import pythonw
    return {
        rf"Software\Classes\AppUserModelId\{APP_ID}": {"DisplayName": APP_NAME,
                                                       "IconUri": str(ICON)},
        rf"Software\Classes\{SCHEME}": {"": f"URL:{APP_NAME}", "URL Protocol": ""},
        # -I: a link can be opened from anywhere - a web page, a document in some folder - and
        # Python must not go looking for "paperbot" in whichever folder that happens to be.
        rf"Software\Classes\{SCHEME}\shell\open\command": {
            "": f'"{pythonw()}" -I -m paperbot.gui --show "%1"'},
    }


def register() -> None:
    """Tell Windows (for this user only) whose notifications these are - name and icon - and
    that paperbot: links open this app. Only writes what isn't there yet or has changed."""
    if sys.platform != "win32":
        return
    import winreg
    for path, values in registration().items():
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as key:
            for name, value in values.items():
                try:
                    current = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    current = None
                if current != value:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
