"""Shared runtime for the command line and the desktop app: secrets, locking, logging,
building the bot, setup checks and one-off test fetches."""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Config, ConfigError

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


class Busy(RuntimeError):
    """Another PaperBot run (e.g. the automatic background check) is working right now."""


@contextmanager
def single_instance(cfg: Config, wait_s: float = 0):
    """Only one PaperBot run at a time: the background check, the app and the command line
    must not answer the same request twice."""
    path = cfg.data_dir / "paperbot.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy("PaperBot is already busy (probably the automatic mail check) - "
                               "try again in a minute")
                time.sleep(1)
        yield
    finally:
        fh.close()


# ---- building the bot -------------------------------------------------------------
def build_bot(cfg: Config, need_mail: bool = True):
    from .db import DB
    from .mail import ImapSmtpMail
    from .pipeline import Bot
    from .publisher import PublisherFetcher
    from .resolve import Resolver, make_session
    from .sources import OpenAccess

    max_pdf = int(cfg.max_pdf_mb * 1024 * 1024)
    session = make_session(cfg.contact_email)
    mail = ImapSmtpMail(cfg, mail_password(cfg) if need_mail else "")
    return Bot(cfg, DB(cfg.data_dir / "paperbot.db"), mail, Resolver(session),
               OpenAccess(session, cfg.contact_email, max_pdf), make_vpn(cfg),
               lambda: PublisherFetcher(cfg.browser, cfg.headless, max_pdf,
                                        cfg.download_spacing_s))


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
    say("Connecting the VPN and trying the publisher...")
    with vpn_session(make_vpn(cfg), cfg.vpn_disconnect_when_done):
        with PublisherFetcher(cfg.browser, cfg.headless, max_pdf, 0) as pub:
            res = pub.fetch(paper)
    if res.data:
        from .db import DB
        db = DB(cfg.data_dir / "paperbot.db")
        db.log_download("try", cfg.owner_address)  # counts toward today's safe ceiling
        path = out / "publisher.pdf"
        path.write_bytes(res.data)
        say(f"Saved {len(res.data) // 1024} KB from the publisher -> {path}")
        say(f"Institute downloads today: {db.downloads_today()} of {cfg.daily_ceiling}.")
        return path
    say(f"Publisher: {res.status} - {res.reason}")
    return None
