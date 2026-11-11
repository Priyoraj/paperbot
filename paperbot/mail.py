"""Reading requests over IMAP and sending replies over SMTP."""
from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Callable, Iterable, Protocol

from .config import APP_NAME

log = logging.getLogger(__name__)
BOT_HEADER = "X-PaperBot"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


@dataclass
class Incoming:
    message_id: str
    sender: str          # lower-case address from the From header (never Reply-To)
    sender_name: str
    subject: str
    msg: EmailMessage

    @property
    def references(self) -> str:
        return " ".join(filter(None, [str(self.msg.get("References", "")).strip(),
                                       self.message_id])).strip()


class MailClient(Protocol):
    def fetch(self, since_days: int, wanted: Callable[[EmailMessage], bool]) -> list[Incoming]: ...
    def send(self, msg: EmailMessage) -> None: ...


def to_incoming(msg: EmailMessage) -> Incoming:
    name, addr = parseaddr(str(msg.get("From", "")))
    mid = str(msg.get("Message-ID", "")).strip()
    if not mid:  # rare, but keep de-duplication stable
        basis = f"{msg.get('Date')}|{addr}|{msg.get('Subject')}".encode()
        mid = f"<synthetic-{hashlib.sha1(basis).hexdigest()[:16]}@paperbot>"
    return Incoming(mid, addr.strip().lower(), name.strip(), str(msg.get("Subject", "")), msg)


def is_from_bot(msg: EmailMessage) -> bool:
    return msg.get(BOT_HEADER) is not None


def sender_auth_failed(msg: EmailMessage) -> bool:
    """True when the receiving server (e.g. Gmail) recorded a DMARC failure: a likely spoof."""
    return any("dmarc=fail" in str(h).lower() for h in msg.get_all("Authentication-Results", []))


def imap_date(d: date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


class ImapSmtpMail:
    def __init__(self, cfg, password: str):
        self.cfg = cfg
        self.password = password

    def connect(self) -> imaplib.IMAP4:
        """A logged-in IMAP connection; the caller logs out. Also used for the folder that
        holds the records several computers share (sync.ImapStore)."""
        cfg = self.cfg
        imap = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=60)
        try:
            imap.login(cfg.mail_username, self.password)
        except BaseException:
            try:
                imap.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                pass
            raise
        return imap

    # ---- receive ------------------------------------------------------
    def fetch(self, since_days: int, wanted: Callable[[EmailMessage], bool]) -> list[Incoming]:
        cfg = self.cfg
        out: list[Incoming] = []
        imap = self.connect()
        try:
            typ, _ = imap.select(f'"{cfg.mailbox}"', readonly=True)
            if typ != "OK":
                raise RuntimeError(f"cannot open mailbox {cfg.mailbox!r}")
            since = imap_date(date.today() - timedelta(days=since_days))
            typ, data = imap.uid("SEARCH", None, "SINCE", since)
            uids = data[0].split() if data and data[0] else []
            hits: list[bytes] = []
            for i in range(0, len(uids), 200):
                chunk = b",".join(uids[i:i + 200]).decode()
                typ, data = imap.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS "
                                     "(FROM SUBJECT MESSAGE-ID X-PAPERBOT)])")
                for uid, raw in _fetch_pairs(data):
                    hdr = email.message_from_bytes(raw, policy=policy.default)
                    if wanted(hdr):
                        hits.append(uid)
            for uid in hits:
                typ, data = imap.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
                for _, raw in _fetch_pairs(data):
                    out.append(to_incoming(email.message_from_bytes(raw, policy=policy.default)))
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001 - best effort
                pass
        return out

    # ---- send ---------------------------------------------------------
    def send(self, msg: EmailMessage) -> None:
        cfg = self.cfg
        ctx = ssl.create_default_context()
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=120, context=ctx)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=120)
            server.starttls(context=ctx)
        with server:
            server.login(cfg.mail_username, self.password)
            server.send_message(msg)
        log.info("sent %r to %s", msg["Subject"], msg["To"])

    def check_login(self) -> None:
        self.connect().logout()


def _fetch_pairs(data) -> Iterable[tuple[bytes, bytes]]:
    for part in data or []:
        if isinstance(part, tuple) and len(part) == 2:
            m = re.search(rb"UID (\d+)", part[0])
            if m:
                yield m.group(1), part[1]


def build_message(cfg, to: str, subject: str, body: str,
                  attachments: Iterable[tuple[str, bytes]] = (),
                  in_reply_to: str | None = None, references: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.owner_name or "", cfg.mail_username))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=(cfg.mail_username.split("@")[-1] or "paperbot"))
    msg[BOT_HEADER] = APP_NAME
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    for filename, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    return msg


def reply_subject(subject: str) -> str:
    subject = subject.strip() or "Paper request"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def pack_attachments(files: list[tuple[str, bytes]], max_bytes: int) -> list[list[tuple[str, bytes]]]:
    """Group attachments into emails no bigger than max_bytes each (one file may exceed it alone)."""
    groups: list[list[tuple[str, bytes]]] = []
    current: list[tuple[str, bytes]] = []
    size = 0
    for name, data in files:
        if current and size + len(data) > max_bytes:
            groups.append(current)
            current, size = [], 0
        current.append((name, data))
        size += len(data)
    if current:
        groups.append(current)
    return groups
