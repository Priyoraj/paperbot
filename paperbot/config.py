"""Loading and editing config.toml."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

import tomlkit

APP_NAME = "PaperBot"
VERSION = "0.2.0"
SYNC_FOLDER = "PaperBot-sync"

# Recommended safe limits. Publishers don't publish their thresholds; libraries report blocks
# after e.g. 100 articles in 40 minutes, 25 accesses in 5 minutes, or caps of 100 a day. These
# values stay far below all of them, and every download is spaced out. "Institute downloads" are
# PDFs fetched through the institute's access (VPN); free open-access copies don't count.
RECOMMENDED_LIMITS = {
    "max_papers_per_day": 30,             # all institute downloads in a day, yours included
    "own_reserve_per_day": 10,            # kept for you; colleagues share the rest (20)
    "max_papers_per_hour": 10,
    "max_papers_per_person_per_day": 5,
    "delay_between_publisher_requests_s": 20,
}


def home_dir() -> Path:
    """Project folder holding config.toml and data/ (override with PAPERBOT_HOME)."""
    env = os.environ.get("PAPERBOT_HOME")
    return Path(env) if env else Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


@dataclass
class Config:
    owner_name: str = ""
    owner_email: str = ""
    mail_username: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    mailbox: str = "INBOX"
    subject_keywords: list[str] = field(default_factory=lambda: ["paper request"])
    lookback_days: int = 7
    signature: str = "(sent automatically by PaperBot)"
    require_approval: bool = True
    auto_approve_domains: list[str] = field(default_factory=list)
    use_recommended_limits: bool = True
    max_papers_per_person_per_day: int = 5
    max_papers_per_day: int = 30
    own_reserve_per_day: int = 10
    max_papers_per_hour: int = 10
    max_items_per_email: int = 10
    reply_to_denied: bool = False
    vpn_enabled: bool = True
    vpn_name: str = "Institute VPN"
    vpn_disconnect_when_done: bool = True
    vpn_connect_timeout_s: int = 90
    vpn_allowed_hours: str = "00:00-24:00"
    contact_email: str = ""
    browser: str = "msedge"
    headless: bool = True
    publisher_delay_s: float = 20
    max_pdf_mb: float = 60
    max_attachment_mb: float = 18
    keep_pdfs_days: int = 30
    poll_minutes: int = 10
    # Working together with PaperBot on other computers that answer the same mailbox (sync.py).
    # enabled = this computer may share its records; nothing is shared until the owner starts it.
    sync_enabled: bool = True
    sync_folder: str = SYNC_FOLDER
    # A Windows notification on this computer when a paper comes to need the owner (notify.py).
    notify_desktop: bool = True
    path: Path | None = None

    # ---- derived -------------------------------------------------------
    @property
    def owner_address(self) -> str:
        return (self.owner_email or self.mail_username).strip().lower()

    @property
    def data_dir(self) -> Path:
        base = self.path.parent if self.path else home_dir()
        return base / "data"

    # ---- effective safety limits (recommended unless the owner switched them off) ------
    def _limit(self, key: str, attr: str):
        return RECOMMENDED_LIMITS[key] if self.use_recommended_limits else getattr(self, attr)

    @property
    def daily_ceiling(self) -> int:
        return int(self._limit("max_papers_per_day", "max_papers_per_day"))

    @property
    def own_reserve(self) -> int:
        return min(int(self._limit("own_reserve_per_day", "own_reserve_per_day")),
                   self.daily_ceiling)

    @property
    def colleague_ceiling(self) -> int:
        return self.daily_ceiling - self.own_reserve

    @property
    def hourly_cap(self) -> int:
        return int(self._limit("max_papers_per_hour", "max_papers_per_hour"))

    @property
    def per_person_cap(self) -> int:
        return int(self._limit("max_papers_per_person_per_day", "max_papers_per_person_per_day"))

    @property
    def download_spacing_s(self) -> float:
        return float(self._limit("delay_between_publisher_requests_s", "publisher_delay_s"))

    def vpn_allowed_now(self, now: datetime | None = None) -> bool:
        return hours_contain(self.vpn_allowed_hours, (now or datetime.now()).time())

    def check_ready(self) -> None:
        """Raise ConfigError listing what still needs filling in."""
        missing = []
        if not self.mail_username:
            missing.append("[mail] username")
        if not self.owner_address:
            missing.append("[owner] email")
        if not self.subject_keywords:
            missing.append("[mail] subject_keywords")
        if missing:
            raise ConfigError("config.toml is missing: " + ", ".join(missing))


# section, key -> attribute name
_MAP = {
    ("owner", "name"): "owner_name",
    ("owner", "email"): "owner_email",
    ("mail", "username"): "mail_username",
    ("mail", "imap_host"): "imap_host",
    ("mail", "imap_port"): "imap_port",
    ("mail", "smtp_host"): "smtp_host",
    ("mail", "smtp_port"): "smtp_port",
    ("mail", "mailbox"): "mailbox",
    ("mail", "subject_keywords"): "subject_keywords",
    ("mail", "lookback_days"): "lookback_days",
    ("mail", "signature"): "signature",
    ("access", "require_approval"): "require_approval",
    ("access", "auto_approve_domains"): "auto_approve_domains",
    ("access", "use_recommended_limits"): "use_recommended_limits",
    ("access", "max_papers_per_person_per_day"): "max_papers_per_person_per_day",
    ("access", "max_papers_per_day"): "max_papers_per_day",
    ("access", "own_reserve_per_day"): "own_reserve_per_day",
    ("access", "max_papers_per_hour"): "max_papers_per_hour",
    ("access", "max_items_per_email"): "max_items_per_email",
    ("access", "reply_to_denied"): "reply_to_denied",
    ("vpn", "enabled"): "vpn_enabled",
    ("vpn", "name"): "vpn_name",
    ("vpn", "disconnect_when_done"): "vpn_disconnect_when_done",
    ("vpn", "connect_timeout_s"): "vpn_connect_timeout_s",
    ("vpn", "allowed_hours"): "vpn_allowed_hours",
    ("fetch", "contact_email"): "contact_email",
    ("fetch", "browser"): "browser",
    ("fetch", "headless"): "headless",
    ("fetch", "delay_between_publisher_requests_s"): "publisher_delay_s",
    ("fetch", "max_pdf_mb"): "max_pdf_mb",
    ("fetch", "max_attachment_mb"): "max_attachment_mb",
    ("fetch", "keep_pdfs_days"): "keep_pdfs_days",
    ("schedule", "poll_minutes"): "poll_minutes",
    ("sync", "enabled"): "sync_enabled",
    ("sync", "folder"): "sync_folder",
    ("notify", "desktop"): "notify_desktop",
}


def config_path() -> Path:
    return home_dir() / "config.toml"


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise ConfigError(f"{path} not found - run `paperbot init` first.")
    doc = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    cfg = Config(path=path)
    for (section, key), attr in _MAP.items():
        if key in doc.get(section, {}):
            setattr(cfg, attr, doc[section][key])
    cfg.subject_keywords = [k.lower() for k in cfg.subject_keywords if k.strip()]
    cfg.auto_approve_domains = [d.lower().lstrip("@") for d in cfg.auto_approve_domains]
    cfg.sync_folder = str(cfg.sync_folder).strip() or SYNC_FOLDER
    if cfg.sync_folder.lower() == str(cfg.mailbox).strip().lower():
        raise ConfigError("[sync] folder must not be the folder PaperBot reads requests from")
    hours_contain(cfg.vpn_allowed_hours, time(0, 0))  # validate early
    return cfg


def set_value(section: str, key: str, value, path: Path | None = None) -> None:
    """Change one setting in config.toml, keeping comments and layout."""
    if (section, key) not in _MAP:
        raise ConfigError(f"unknown setting [{section}] {key}")
    path = path or config_path()
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    if section not in doc:
        doc[section] = tomlkit.table()
    doc[section][key] = value
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def init_config() -> Path:
    path = config_path()
    if not path.exists():
        shutil.copyfile(home_dir() / "config.example.toml", path)
    return path


def hours_contain(spec: str, t: time) -> bool:
    """'HH:MM-HH:MM' window test; windows may wrap midnight ('22:00-07:00')."""
    try:
        start_s, end_s = (p.strip() for p in spec.split("-"))
        start = _parse_hhmm(start_s)
        end = _parse_hhmm(end_s)
    except ValueError as exc:
        raise ConfigError(f"bad [vpn] allowed_hours {spec!r}: use 'HH:MM-HH:MM'") from exc
    if start == end:
        return True
    if start < end:
        return start <= _minutes(t) < end
    return _minutes(t) >= start or _minutes(t) < end


def _parse_hhmm(s: str) -> int:
    h, m = s.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
        raise ValueError(s)
    return h * 60 + m


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute
