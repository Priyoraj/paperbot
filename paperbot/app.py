"""Shared runtime for the command line and the desktop app: secrets, locking, logging,
building the bot, working together with other computers, setup checks and one-off test
fetches."""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from .config import Config, ConfigError
# One "busy" for both cases: another PaperBot run on this computer, or PaperBot on another one.
from .sync import Busy, LockLost, SyncError, file_lock  # noqa: F401 - used by the cli and app

KEYRING_SERVICE = "PaperBot"
log = logging.getLogger("paperbot")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---- secrets (Windows Credential Manager via keyring) -------------------------------
def get_secret(key: str) -> str | None:
    env = os.environ.get("PAPERBOT_" + key.split(":")[0].upper().replace("-", "_") + "_PASSWORD")
    if env:
        return env
    import keyring
    return keyring.get_password(KEYRING_SERVICE, key)


def set_secret(key: str, value: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, key, value)


def has_mail_password(cfg: Config) -> bool:
    return bool(cfg.mail_username and get_secret(f"mail:{cfg.mail_username}"))


def mail_password(cfg: Config) -> str:
    pw = get_secret(f"mail:{cfg.mail_username}") if cfg.mail_username else None
    if not pw:
        raise ConfigError("no mail password saved yet (Settings > Mail password)")
    return pw


def vpn_credentials(cfg: Config):
    import keyring
    user = keyring.get_password(KEYRING_SERVICE, f"vpn-user:{cfg.vpn_name}")
    pw = keyring.get_password(KEYRING_SERVICE, f"vpn:{cfg.vpn_name}")
    return (user, pw) if user and pw else None


def make_vpn(cfg: Config):
    """The VPN as used for real: dial with the sign-in saved in Windows (Settings > VPN) and
    wait until Windows actually routes traffic through it."""
    from .vpn import Vpn, local_source_ip, ras_dial_saved
    on_windows = sys.platform == "win32"
    return Vpn(cfg.vpn_name, cfg.vpn_connect_timeout_s, credentials=lambda: vpn_credentials(cfg),
               dialer=ras_dial_saved if on_windows else None,
               source_ip=local_source_ip if on_windows else None)


# ---- logging & locking ------------------------------------------------------------
def setup_logging(cfg: Config | None, verbose: bool = False, console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if cfg is not None:
        logdir = cfg.data_dir / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(logdir / "paperbot.log", maxBytes=2_000_000,
                                                  backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if console and sys.stderr is not None and (verbose or sys.stderr.isatty()):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        sh.setLevel(logging.INFO if verbose else logging.WARNING)
        root.addHandler(sh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@contextmanager
def single_instance(cfg: Config, wait_s: float = 0):
    """Only one PaperBot run at a time on this computer: the background check, the app and the
    command line must not answer the same request twice. (Between computers, the mailbox does
    this job: see sync.py.)"""
    with file_lock(cfg.data_dir / "paperbot.lock", wait_s,
                   busy="PaperBot is already busy (probably the automatic mail check) - "
                        "try again in a minute"):
        yield


# ---- building the bot -------------------------------------------------------------
def build_bot(cfg: Config, need_mail: bool = True):
    from . import notify
    from .db import DB
    from .mail import ImapSmtpMail
    from .pipeline import Bot
    from .publisher import PublisherFetcher
    from .resolve import Resolver, make_session
    from .sources import OpenAccess

    max_pdf = int(cfg.max_pdf_mb * 1024 * 1024)
    session = make_session(cfg.contact_email)
    password = mail_password(cfg) if need_mail else ""
    db = DB(cfg.data_dir / "paperbot.db")
    return Bot(cfg, db, ImapSmtpMail(cfg, password), Resolver(session),
               OpenAccess(session, cfg.contact_email, max_pdf), make_vpn(cfg),
               lambda: PublisherFetcher(cfg.browser, cfg.headless, max_pdf,
                                        cfg.download_spacing_s),
               sync=make_sync(cfg, db, password), notifier=notify.announce)


# ---- working together with PaperBot on other computers (sync.py) -------------------
def make_sync(cfg: Config, db, password: str | None = None, even_if_off: bool = False):
    """This computer's part in working together with other computers that answer the same
    mailbox - or a stand-in that shares nothing, while that is switched off on this computer
    ([sync] enabled = false) or there is no mailbox to share through yet."""
    from .mail import ImapSmtpMail
    from .sync import ImapStore, NoSync, Sync, computer_name
    if not cfg.mail_username or not (cfg.sync_enabled or even_if_off):
        return NoSync()
    password = password or get_secret(f"mail:{cfg.mail_username}")
    if not password:
        return NoSync()
    store = ImapStore(ImapSmtpMail(cfg, password).connect, cfg.sync_folder, cfg.mail_username)
    return Sync(db, store, computer_name(), cfg.data_dir / "sync.lock", vpn=cfg.vpn_enabled,
                backup_path=cfg.data_dir / "paperbot.before-sharing.db")


@dataclass
class Together:
    """Where this computer stands, read from its own records (no network needed)."""
    allowed: bool                     # [sync] enabled: this computer may share its records
    joined: bool                      # it uses the records kept in the mailbox
    others: list[str] = field(default_factory=list)   # the other computers' names
    synced_at: datetime | None = None                  # last exchange with the mailbox

    @property
    def summary(self) -> str:
        if not self.allowed:
            return ("Off on this computer: it keeps its own records. Keep automatic mode on "
                    "in one computer only.")
        if not self.joined:
            return "Not in use: PaperBot on this computer works on its own."
        if not self.others:
            return ("On. No other computer has joined yet: PaperBot on your other computer "
                    "joins by itself the next time it checks the mail.")
        return "On: working together with PaperBot on " + " and ".join(self.others) + "."


def together(cfg: Config, db) -> Together:
    tag = db.meta("node")
    joined = cfg.sync_enabled and db.meta("joined") == "1"
    others = [c["name"] for c in db.computers()
              if not (tag and c["node"].endswith("-" + tag))] if joined else []
    try:
        at = datetime.fromisoformat(db.meta("synced_at") or "") if joined else None
    except ValueError:
        at = None
    return Together(cfg.sync_enabled, joined, others, at)


def _open_sync(cfg: Config, db):
    from .sync import NoSync
    sync = make_sync(cfg, db, mail_password(cfg), even_if_off=True)
    if isinstance(sync, NoSync):
        raise ConfigError("fill in the mailbox address first (Settings)")
    return sync


def check_sharing(cfg: Config) -> str:
    """Try out, on the real mailbox, everything working together needs from it. Leaves
    nothing behind but the (empty) folder."""
    from .db import DB
    cfg.check_ready()
    db = DB(cfg.data_dir / "paperbot.db")
    try:
        return "Your mailbox passed: " + _open_sync(cfg, db).selftest() + "."
    finally:
        db.close()


def start_sharing(cfg: Config, wait_s: float = 60) -> str:
    """Start working together from this computer - or join, if PaperBot on another computer
    already started. Call it with the local lock held (single_instance)."""
    from .db import DB
    cfg.check_ready()
    if not cfg.sync_enabled:
        config_mod.set_value("sync", "enabled", True, cfg.path)
        cfg = config_mod.load(cfg.path)
    db = DB(cfg.data_dir / "paperbot.db")
    try:
        sync = _open_sync(cfg, db)
        sync.start(wait_s)
        others = sync.others()
    finally:
        db.close()
    if others:
        return ("This computer now works together with PaperBot on " + " and ".join(others)
                + ". Both answer your mailbox as one.")
    return ("Started. Now open PaperBot on your other computer (it needs version "
            f"{config_mod.VERSION} or newer): it joins by itself, and from then on the two "
            "answer your mailbox as one.")


def stop_sharing(cfg: Config) -> str:
    """This computer goes back to keeping its own records. Call it with the local lock held."""
    from .db import DB
    from .sync import NoSync, detach
    db = DB(cfg.data_dir / "paperbot.db")
    try:
        was = together(cfg, db)
        sync = make_sync(cfg, db, even_if_off=True)
        if isinstance(sync, NoSync):          # no mailbox to tell: tidy up here only
            detach(db)
        else:
            sync.leave()
    finally:
        db.close()
    if cfg.sync_enabled:
        config_mod.set_value("sync", "enabled", False, cfg.path)
    return ("This computer keeps its own records again."
            + (" PaperBot on " + " and ".join(was.others) + " no longer knows what this one "
               "does, so keep automatic mode on in one computer only - otherwise colleagues "
               "get every paper twice." if was.others else ""))


def manual_session_factory(cfg: Config, control=None, say=lambda msg: None):
    """An ordinary (not remote-controlled) browser window for papers that need a person."""
    from .humanbrowser import HumanBrowser
    return lambda: HumanBrowser(cfg.browser, cfg.data_dir, int(cfg.max_pdf_mb * 1024 * 1024),
                                control=control, say=say)


# ---- checks -------------------------------------------------------------------------
def vpn_profiles() -> list[str]:
    """Names of the Windows VPN connections of this user."""
    try:
        cp = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "(Get-VpnConnection).Name"], capture_output=True, text=True,
                            timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [n.strip() for n in cp.stdout.splitlines() if n.strip()]


def vpn_signin_saved(cfg: Config) -> bool:
    """Has Windows saved a password for this VPN connection (Settings > VPN > Advanced)?"""
    if sys.platform != "win32":
        return False
    from .vpn import saved_signin_present
    return saved_signin_present(cfg.vpn_name) or vpn_credentials(cfg) is not None


def browser_version(cfg: Config) -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(channel=cfg.browser, headless=True)
        version = b.version
        b.close()
    return f"{cfg.browser} {version}"


def setup_checks(cfg: Config) -> list[tuple[str, bool, str]]:
    """[(what, ok, detail)] - used by `paperbot check` and the app's Settings page."""
    results: list[tuple[str, bool, str]] = []

    def check(name, fn):
        try:
            results.append((name, True, fn() or ""))
        except Exception as exc:  # noqa: BLE001
            results.append((name, False, str(exc)))

    check("Your details", lambda: cfg.check_ready())
    check("Mail login", lambda: build_bot(cfg).mail.check_login() or cfg.mail_username)

    def vpn():
        if cfg.vpn_name not in vpn_profiles():
            raise ConfigError(f"no Windows VPN connection named {cfg.vpn_name!r}")
        if not vpn_signin_saved(cfg):
            raise ConfigError("Windows has no saved password for this VPN")
        return f"{cfg.vpn_name}, sign-in saved in Windows"

    check("Institute VPN", vpn)
    check("Browser", lambda: browser_version(cfg))

    def contact():
        if not cfg.contact_email:
            raise ConfigError("blank - fewer free copies are found (Unpaywall is skipped)")
        return cfg.contact_email

    check("Open-access contact email", contact)

    def more_computers():
        """Looks only; never writes to the mailbox."""
        from .db import DB
        from .sync import NoSync
        db = DB(cfg.data_dir / "paperbot.db")
        try:
            here = together(cfg, db)
            if not here.allowed:
                return "switched off on this computer (it keeps its own records)"
            sync = make_sync(cfg, db)
            if isinstance(sync, NoSync):
                raise ConfigError("needs the mailbox address and mail password first")
            try:
                entries = sync.store.entries()
            finally:
                sync.store.close()
            in_mailbox = any(e.kind == "state" for e in entries or [])
            if here.joined and not in_mailbox:
                return ("on; the shared records are missing from the mailbox and will be put "
                        "back at the next check")
            if here.joined:
                return ("working together with " + " and ".join(here.others) if here.others
                        else "on; no other computer has joined yet")
            if in_mailbox:
                return "PaperBot on another computer shares its records; this one joins at " \
                       "its next check"
            return "not in use"
        finally:
            db.close()

    check("More than one computer", more_computers)
    return results


# ---- one-off test fetch -------------------------------------------------------------
def fetch_one(cfg: Config, what: str, mode: str = "free", out_dir: Path | None = None,
              say=print) -> Path | None:
    """Fetch a single paper without email. mode: 'free' (open access only), 'free+vpn'
    (open access, then the publisher through the VPN) or 'vpn' (publisher only)."""
    from .parse import RequestItem, parse_text
    from .publisher import PublisherFetcher
    from .resolve import Resolver, make_session
    from .sources import OpenAccess
    from .vpn import vpn_session

    items = parse_text(what) or [RequestItem("title", what.strip(), what)]
    item = items[0]
    session = make_session(cfg.contact_email)
    max_pdf = int(cfg.max_pdf_mb * 1024 * 1024)
    paper = Resolver(session).resolve("title" if item.kind == "guess" else item.kind, item.value)
    say(f"Paper: {paper.title or paper.doi or paper.arxiv_id or paper.landing_url}"
        + (f" ({paper.authors}, {paper.year})" if paper.authors else "")
        + (f"\nDOI: {paper.doi}" if paper.doi else ""))
    out = Path(out_dir or cfg.data_dir / "test")
    out.mkdir(parents=True, exist_ok=True)
    if mode in ("free", "free+vpn"):
        oa = OpenAccess(session, cfg.contact_email, max_pdf)
        for c in oa.candidates(paper):
            say(f"Trying free copy: {c.source} ({c.version})")
            data = oa.download(c.url)
            if data:
                path = out / "open_access.pdf"
                path.write_bytes(data)
                say(f"Saved {len(data) // 1024} KB free copy -> {path}")
                return path
        say("No free copy found.")
        if mode == "free":
            return None
    from .db import DB
    db = DB(cfg.data_dir / "paperbot.db")
    try:
        sync = make_sync(cfg, db)
        # A turn of its own: only one computer is on the VPN at a time, and the download is
        # counted in the records all computers share.
        with sync.session(wait_s=120):
            sync.guard(renew=True)
            say("Connecting the VPN and trying the publisher...")
            with vpn_session(make_vpn(cfg), cfg.vpn_disconnect_when_done):
                with PublisherFetcher(cfg.browser, cfg.headless, max_pdf, 0) as pub:
                    res = pub.fetch(paper)
            if res.data:
                db.log_download("try", cfg.owner_address)  # counts toward today's safe ceiling
        if res.data:
            path = out / "publisher.pdf"
            path.write_bytes(res.data)
            say(f"Saved {len(res.data) // 1024} KB from the publisher -> {path}")
            say(f"Institute downloads today: {db.downloads_today()} of {cfg.daily_ceiling}.")
            return path
    finally:
        db.close()
    say(f"Publisher: {res.status} - {res.reason}")
    return None
