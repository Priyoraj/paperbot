"""One pass of the bot: read inbox -> approvals -> find papers -> VPN fetch -> reply -> VPN off."""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .db import DB, now_iso
from .mail import (BOT_HEADER, Incoming, MailClient, build_message, is_from_bot,
                   pack_attachments, reply_subject, sender_auth_failed)
from .parse import message_text, parse_request, strip_quoted
from .resolve import Paper, ResolveError, Resolver
from .sources import PUBLISHED, Candidate, OpenAccess
from .vpn import Vpn, VpnError, vpn_session

log = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"\[ref:([0-9a-f]{8})\]", re.I)
YES_RE = re.compile(r"^(yes|y|yep|yeah|approve[d]?|ok|okay|sure|allow|accept|colleague|haan?)\b",
                    re.I)
NO_RE = re.compile(r"^(no|n|nope|deny|denied|block|reject|ignore|nahi|nah)\b", re.I)
MAX_PROMPTS_PER_DAY = 10
WAIT_FOR_VPN = timedelta(hours=24)   # after this, settle for a preprint instead of waiting
DAY_LIMIT = "today's download limit has been reached - please send this one again tomorrow"
PERSON_LIMIT = ("you've reached today's limit of {n} paper{s} - please send this one again "
                "tomorrow")

HELP_TEXT = """I couldn't find a paper in your email. Please send one paper per line as any of:

  10.1145/3292500.3330701                      (a DOI - best)
  https://link.springer.com/article/...        (a link to the paper's page)
  arXiv:1706.03762
  "Attention Is All You Need"                  (an exact title, in quotes)

with "{keyword}" in the subject."""


def paper_from_row(row) -> Paper:
    return Paper(doi=row["doi"], arxiv_id=row["arxiv_id"], landing_url=row["landing_url"],
                 title=row["title"], authors=row["authors"], year=row["year"])


def pdf_filename(row) -> str:
    """'Lee 2020 - First words of the title.pdf'"""
    head = " ".join(filter(None, [
        row["authors"].replace(" et al.", "").split()[-1] if row["authors"] else "",
        str(row["year"]) if row["year"] else ""]))
    title = " ".join(re.sub(r"<[^>]+>", "", row["title"] or "").split()[:8])
    name = " - ".join(filter(None, [head, title])) or row["doi"] or row["arxiv_id"] or "paper"
    name = re.sub(r"[^\w\-. ]+", "", name.replace("/", "_")).strip()
    return (name[:110] or "paper") + ".pdf"


def describe(row) -> str:
    if row["title"]:
        who = f"{row['authors']} " if row["authors"] else ""
        year = f"({row['year']}) " if row["year"] else ""
        return f'{who}{year}"{re.sub(r"<[^>]+>", "", row["title"])}"'.strip()
    return row["raw"]


class Bot:
    def __init__(self, cfg: Config, db: DB, mail: MailClient, resolver: Resolver,
                 oa: OpenAccess, vpn: Vpn | None, publisher_factory, clock=datetime.now):
        self.cfg = cfg
        self.db = db
        self.mail = mail
        self.resolver = resolver
        self.oa = oa
        self.vpn = vpn
        self.publisher_factory = publisher_factory
        self.clock = clock
        self.pdf_dir = cfg.data_dir / "pdfs"
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self._oa_cache: dict[int, list[Candidate]] = {}
        self._oa_tried: dict[int, set[str]] = {}
        self._outcome: dict[int, tuple[str, str]] = {}  # item -> publisher status, reason

    # =================================================================
    def run_once(self) -> None:
        self.intake()
        self.process()
        self.deliver()
        self.notify_owner()
        self.cleanup()

    # ---- 1. inbox ----------------------------------------------------
    def _wanted(self, hdr) -> bool:
        if hdr.get(BOT_HEADER) is not None:
            return False
        subject = str(hdr.get("Subject", "")).lower()
        if "[paperbot]" in subject and TOKEN_RE.search(subject):
            return True
        return any(k in subject for k in self.cfg.subject_keywords)

    def intake(self) -> None:
        for inc in self.mail.fetch(self.cfg.lookback_days, self._wanted):
            if self.db.is_seen(inc.message_id):
                continue
            try:
                if is_from_bot(inc.msg):
                    self.db.mark_seen(inc.message_id, "own")
                    continue
                m = TOKEN_RE.search(inc.subject)
                if m and "[paperbot]" in inc.subject.lower():
                    self.handle_approval_reply(inc, m.group(1).lower())
                    self.db.mark_seen(inc.message_id, "approval")
                    continue
                self.intake_request(inc)
            except Exception:  # noqa: BLE001 - one odd email must not block the rest
                log.exception("could not handle message %s", inc.message_id)
                self.db.mark_seen(inc.message_id, "error")

    def access_for(self, sender: str, name: str) -> str:
        """'queued' (serve now), 'held' (ask owner first) or 'denied'."""
        cfg = self.cfg
        if sender == cfg.owner_address:
            return "queued"
        person = self.db.person(sender)
        if person and person["status"] == "approved":
            return "queued"
        if person and person["status"] == "denied":
            return "denied"
        domain = sender.rsplit("@", 1)[-1]
        trusted = any(domain == d or domain.endswith("." + d) for d in cfg.auto_approve_domains)
        if trusted or not cfg.require_approval:
            if person:
                self.db.set_person_status(sender, "approved")
            else:
                self.db.add_person(sender, name, "approved")
            return "queued"
        if not person:
            self.db.add_person(sender, name, "pending")
        return "held"

    def intake_request(self, inc: Incoming) -> None:
        cfg = self.cfg
        if not inc.sender or "@" not in inc.sender:
            self.db.mark_seen(inc.message_id, "no-sender")
            return
        if sender_auth_failed(inc.msg):
            log.warning("ignoring %s: sender failed DMARC (likely spoofed)", inc.sender)
            self.db.add_request(inc.message_id, inc.sender, inc.sender_name, inc.subject,
                                inc.references, "ignored", "failed sender authentication")
            return
        items = parse_request(inc.msg, cfg.subject_keywords, cfg.max_items_per_email)
        access = self.access_for(inc.sender, inc.sender_name)
        if not items:
            is_reply = inc.subject.strip().lower().startswith(("re:", "fwd:", "fw:"))
            self.db.add_request(inc.message_id, inc.sender, inc.sender_name, inc.subject,
                                inc.references, "done" if access == "queued" else access,
                                "no papers found in email")
            if access == "queued" and not is_reply:
                self._send(inc.sender, reply_subject(inc.subject),
                           self._greeting(inc.sender_name) + HELP_TEXT.format(
                               keyword=cfg.subject_keywords[0]) + self._sign(),
                           in_reply_to=inc.message_id, references=inc.references)
            return
        rid = self.db.add_request(inc.message_id, inc.sender, inc.sender_name, inc.subject,
                                  inc.references, access)
        for it in items:
            self.db.add_item(rid, it.raw, it.kind, it.value)
        log.info("request %d from %s: %d item(s), %s", rid, inc.sender, len(items), access)
        if access == "held":
            self.prompt_owner(inc, items)
        elif access == "denied" and cfg.reply_to_denied:
            self._send(inc.sender, reply_subject(inc.subject),
                       self._greeting(inc.sender_name) +
                       "Sorry, I'm not able to send papers to this address." + self._sign(),
                       in_reply_to=inc.message_id, references=inc.references)

    # ---- approvals ---------------------------------------------------
    def prompt_owner(self, inc: Incoming, items) -> None:
        person = self.db.person(inc.sender)
        if person["prompted_at"] and self.clock() - datetime.fromisoformat(
                person["prompted_at"]) < timedelta(hours=24):
            return  # already asked recently; the new request just waits with the old one
        if self.db.prompts_today() >= MAX_PROMPTS_PER_DAY:
            log.warning("approval prompt limit reached; %s stays pending", inc.sender)
            return
        token = person["token"] or secrets.token_hex(4)  # keep old emails' "yes" valid
        self.db.set_prompted(inc.sender, token)
        who = f"{inc.sender_name} <{inc.sender}>" if inc.sender_name else inc.sender
        listing = "\n".join(f"  {i}. {it.raw}" for i, it in enumerate(items, 1))
        body = (f"{who} is asking for papers for the first time.\n\n"
                f"  Subject: {inc.subject}\n{listing}\n\n"
                "Reply to this email with just one word:\n"
                "  yes  - they're a colleague: send these now, and future requests automatically\n"
                "  no   - ignore their requests from now on\n\n"
                f"(Or on your PC: paperbot approve {inc.sender}  /  paperbot deny {inc.sender})\n")
        self._send(self.cfg.owner_address,
                   f"[PaperBot] Approve {who}? [ref:{token}]", body)

    def handle_approval_reply(self, inc: Incoming, token: str) -> None:
        if inc.sender != self.cfg.owner_address:
            log.warning("approval reply from %s ignored (not the owner)", inc.sender)
            return
        person = self.db.person_by_token(token)
        if not person:
            log.info("approval reply for unknown/used ref %s", token)
            return
        text = strip_quoted(message_text(inc.msg))
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if YES_RE.match(first):
            self.decide(person["email"], approve=True, notify=True)
        elif NO_RE.match(first):
            self.decide(person["email"], approve=False, notify=True)
        else:
            self._send(self.cfg.owner_address, "[PaperBot] Didn't understand your reply",
                       f"Your reply about {person['email']} started with {first[:60]!r}.\n"
                       "Please reply to the original approval email with just 'yes' or 'no'.\n")

    def decide(self, email: str, approve: bool, notify: bool = False) -> int:
        """Approve/deny a person and release their held requests. Returns #requests released."""
        email = email.lower()
        self.db.set_person_status(email, "approved" if approve else "denied")
        released = self.db.release_held(email, "queued" if approve else "denied")
        if not approve and self.cfg.reply_to_denied:
            for req in self.db.requests_with_status("denied", email):
                if req["note"] != "decline sent":
                    self._send(email, reply_subject(req["subject"]),
                               self._greeting(req["sender_name"]) +
                               "Sorry, I'm not able to send papers to this address." + self._sign(),
                               in_reply_to=req["message_id"], references=req["refs"])
                    self.db.set_request_status(req["id"], "denied", "decline sent")
        if notify:
            what = (f"approved - {released} waiting request(s) will be answered now"
                    if approve else "denied - their requests will be ignored")
            self._send(self.cfg.owner_address, f"[PaperBot] {email} {what.split(' - ')[0]}",
                       f"{email} {what}.\n")
        log.info("%s %s (%d held requests released)", email,
                 "approved" if approve else "denied", released)
        return released

    # ---- 2. find and fetch -------------------------------------------
    def process(self) -> None:
        self._resolve_new()
        todo = self._within_person_limits(self.db.active_items(("resolved",)))
        todo = [r for r in todo if not self._from_cache(r)]
        todo = [r for r in todo if not self._try_open_access(r, published_only=True)]
        attempted = self._publisher_phase(todo)
        now = self.clock()
        for r in todo:
            r = self.db.item(r["id"])
            if r["status"] != "resolved":
                continue
            waited = now - datetime.fromisoformat(self._req_created(r)) >= WAIT_FOR_VPN
            no_vpn_route = not (r["doi"] or r["landing_url"]) or not self.cfg.vpn_enabled
            if r["id"] in attempted or waited or no_vpn_route:
                if not self._try_open_access(r, published_only=False):
                    self._give_up(r)

    def _req_created(self, row) -> str:
        return self.db.request(row["request_id"])["created_at"]

    def _resolve_new(self) -> None:
        for r in self.db.active_items(("new",)):
            try:
                p = self.resolver.resolve(r["kind"], r["value"])
            except ResolveError as exc:
                if not exc.permanent:
                    log.info("lookup of %r postponed: %s", r["value"], exc.reason)
                    continue
                status = "dropped" if r["kind"] == "guess" else "failed"
                self.db.update_item(r["id"], status=status, reason=exc.reason)
                continue
            self.db.update_item(r["id"], status="resolved", doi=p.doi, arxiv_id=p.arxiv_id,
                                landing_url=p.landing_url, title=p.title, authors=p.authors,
                                year=p.year)

    def _within_person_limits(self, rows) -> list:
        """At most per_person_cap papers per colleague per day; the rest are answered with
        "please send this one again tomorrow"."""
        cap = self.cfg.per_person_cap
        allowance: dict[str, int] = {}
        out = []
        for r in rows:
            sender = r["sender"]
            if sender == self.cfg.owner_address:
                out.append(r)
                continue
            if sender not in allowance:
                allowance[sender] = cap - self.db.delivered_today(sender)
            if allowance[sender] > 0:
                allowance[sender] -= 1
                out.append(r)
            else:
                self.db.update_item(r["id"], status="failed", reason=PERSON_LIMIT.format(
                    n=cap, s="" if cap == 1 else "s"))
        return out

    def _apply_ceiling(self, rows) -> list:
        """Split papers that need the institute's access into: fetch now, wait for a later run
        (hourly cap), or over today's safe ceiling (answered with "send it again tomorrow").
        Colleagues can't use the part of the ceiling kept for the owner."""
        cfg = self.cfg
        used_day, used_hour = self.db.downloads_today(), self.db.downloads_last_hour()
        now_rows, over = [], []
        for r in rows:
            limit = cfg.daily_ceiling if r["sender"] == cfg.owner_address \
                else cfg.colleague_ceiling
            if used_day + len(now_rows) >= limit:
                over.append(r)
            elif used_hour + len(now_rows) < cfg.hourly_cap:
                now_rows.append(r)
        for r in over:
            if not self._try_open_access(r, published_only=False):  # an author's copy is fine
                self.db.update_item(r["id"], status="failed", reason=DAY_LIMIT)
        if over:
            log.info("safe daily ceiling reached (%d): %d paper(s) answered with 'send it again "
                     "tomorrow'", cfg.daily_ceiling, len(over))
            self._alert_ceiling(len(over))
        waiting = len(rows) - len(now_rows) - len(over)
        if waiting:
            log.info("%d paper(s) wait for a later check: at most %d downloads an hour",
                     waiting, cfg.hourly_cap)
        return now_rows

    def _alert_ceiling(self, n: int) -> None:
        cfg = self.cfg
        if not self.db.alert_due(f"ceiling-{datetime.now().date()}", timedelta(hours=23)):
            return
        try:
            self._send(cfg.owner_address, "[PaperBot] Today's safe download limit is used up",
                       f"PaperBot has made {self.db.downloads_today()} downloads through the "
                       f"institute today. The safe ceiling is {cfg.daily_ceiling} a day, with "
                       f"{cfg.own_reserve} kept for you.\n\n{n} paper(s) weren't fetched; the "
                       "requesters were asked to send them again tomorrow.\n\nYou can change the "
                       "limits in PaperBot > Settings > Daily safety limits.\n")
        except Exception:  # noqa: BLE001 - the notice is a courtesy; never block replies
            log.exception("could not send the daily-limit notice")

    def _save(self, row, data: bytes, source: str, version: str) -> None:
        key = row["doi"] or row["arxiv_id"] or row["landing_url"] or str(row["id"])
        path = self.pdf_dir / f"{hashlib.sha1(key.lower().encode()).hexdigest()[:16]}.pdf"
        path.write_bytes(data)
        self.db.update_item(row["id"], status="fetched", pdf_path=str(path), source=source,
                            version=version, fetched_at=now_iso(), reason=None)
        log.info("item %d fetched from %s (%s, %d KB)", row["id"], source, version,
                 len(data) // 1024)

    def _from_cache(self, row) -> bool:
        hit = self.db.cached_pdf(row["doi"], row["arxiv_id"])
        if not hit or hit["id"] == row["id"]:
            return False
        self.db.update_item(row["id"], status="fetched", pdf_path=hit["pdf_path"],
                            source="cache", version=hit["version"], fetched_at=now_iso())
        return True

    def _try_open_access(self, row, published_only: bool) -> bool:
        if row["id"] not in self._oa_cache:
            self._oa_cache[row["id"]] = self.oa.candidates(paper_from_row(row))
        tried = self._oa_tried.setdefault(row["id"], set())
        wanted_arxiv = row["kind"] == "arxiv"
        for c in self._oa_cache[row["id"]]:
            is_wanted = c.version == PUBLISHED or (wanted_arxiv and c.source == "arxiv")
            if (published_only and not is_wanted) or c.url in tried:
                continue
            tried.add(c.url)
            data = self.oa.download(c.url)
            if data:
                self._save(row, data, c.source,
                           "publishedVersion" if wanted_arxiv and c.source == "arxiv"
                           else c.version)
                return True
        return False

    def _publisher_phase(self, rows) -> set[int]:
        """Fetch through the VPN. Returns ids of items that were actually tried."""
        cfg = self.cfg
        rows = [r for r in rows if r["doi"] or r["landing_url"]]
        if not rows or not cfg.vpn_enabled or self.vpn is None:
            return set()
        rows = self._apply_ceiling(rows)
        if not rows:
            return set()
        if not cfg.vpn_allowed_now(self.clock()):
            log.info("%d paper(s) wait for the VPN window %s", len(rows), cfg.vpn_allowed_hours)
            return set()
        attempted: set[int] = set()
        try:
            with vpn_session(self.vpn, cfg.vpn_disconnect_when_done):
                with self.publisher_factory() as pub:
                    for r in rows:
                        res = pub.fetch(paper_from_row(r))
                        attempted.add(r["id"])
                        if res.status == "ok" and res.data:
                            self._save(r, res.data, "publisher", PUBLISHED)
                            self.db.log_download("publisher", r["sender"])
                        else:
                            self._outcome[r["id"]] = (res.status, res.reason)
                            log.info("item %d: publisher %s (%s)", r["id"], res.status,
                                     res.reason)
        except VpnError as exc:
            log.error("%s", exc)
            if self.db.alert_due("vpn", timedelta(hours=6)):
                self._send(cfg.owner_address, "[PaperBot] VPN problem",
                           f"PaperBot couldn't connect '{cfg.vpn_name}':\n  {exc}\n\n"
                           "Papers that need the VPN will wait and be retried every run.\n")
        except Exception as exc:  # noqa: BLE001 - e.g. the browser failed to start
            log.exception("publisher phase failed")
            if self.db.alert_due("browser", timedelta(hours=6)):
                self._send(cfg.owner_address, "[PaperBot] Browser problem",
                           f"PaperBot couldn't use the browser ({cfg.browser}):\n  {exc}\n")
        return attempted

    def _give_up(self, row) -> None:
        status, reason = self._outcome.get(row["id"], ("", ""))
        owner = self.cfg.owner_name or "I"
        if status == "blocked":
            self.db.update_item(row["id"], status="manual",
                                reason=f"the publisher's site needs a person to download it; "
                                       f"{owner} will send it separately")
        elif status == "no_access":
            self.db.update_item(row["id"], status="failed",
                                reason="not available: the institute doesn't seem to subscribe "
                                       "to this journal, and there's no free copy")
        elif status == "failed":
            self.db.update_item(row["id"], status="failed",
                                reason=f"couldn't download it ({reason})")
        else:
            self.db.update_item(row["id"], status="failed",
                                reason="couldn't find a free copy, and the institute access "
                                       "isn't available right now")

    # ---- 3. replies --------------------------------------------------
    def deliver(self) -> None:
        for req in self.db.requests_with_status("queued"):
            try:
                self._deliver_request(req)
            except Exception:  # noqa: BLE001 - try again next run
                log.exception("sending reply for request %d failed", req["id"])

    def _deliver_request(self, req) -> None:
        max_bytes = int(self.cfg.max_attachment_mb * 1024 * 1024)
        items = self.db.items_for(req["id"])
        for it in items:
            if it["status"] != "fetched":
                continue
            path = Path(it["pdf_path"])
            if not path.exists():  # file vanished: fetch again next run
                self.db.update_item(it["id"], status="resolved", pdf_path=None)
            elif path.stat().st_size > max_bytes:  # too big for email -> owner shares it
                mb = path.stat().st_size / 1024 / 1024
                self.db.update_item(it["id"], status="failed", owner_notified=0,
                                    reason=f"too large to email ({mb:.0f} MB); "
                                           f"{self.cfg.owner_name or 'I'} will share it "
                                           "another way")
        items = self.db.items_for(req["id"])
        ready = [i for i in items if i["status"] == "fetched"]
        notes = [i for i in items if i["status"] in ("failed", "manual") and not i["reported"]]
        pending = [i for i in items if i["status"] in ("new", "resolved")]
        visible = [i for i in items if i["status"] != "dropped"]
        if not visible and not pending:
            if not req["subject"].strip().lower().startswith(("re:", "fwd:", "fw:")):
                self._send(req["sender"], reply_subject(req["subject"]),
                           self._greeting(req["sender_name"]) + HELP_TEXT.format(
                               keyword=self.cfg.subject_keywords[0]) + self._sign(),
                           in_reply_to=req["message_id"], references=req["refs"])
            self.db.set_request_status(req["id"], "done")
            return
        if not ready and not notes:
            return
        files, names = [], set()
        for it in ready:
            name = pdf_filename(it)
            while name.lower() in names:
                name = name[:-4] + "_.pdf"
            names.add(name.lower())
            files.append((name, Path(it["pdf_path"]).read_bytes()))
        lines = []
        for n, it in enumerate([i for i in visible if i in ready or i in notes], 1):
            if it in ready:
                extra = "" if it["version"] in (PUBLISHED, None) else \
                    " (author's manuscript version: the publisher's version wasn't available)"
                lines.append(f"  {n}. {describe(it)} - attached{extra}")
            else:
                lines.append(f"  {n}. {describe(it)} - {it['reason']}")
        intro = "Here are the papers you asked for:" if ready else \
            "About the papers you asked for:"
        body = self._greeting(req["sender_name"]) + intro + "\n\n" + "\n".join(lines)
        if pending:
            body += (f"\n\nStill working on {len(pending)} more; "
                     "they'll follow in a separate email.")
        body += self._sign()
        groups = pack_attachments(files, max_bytes) or [[]]
        for g, group in enumerate(groups, 1):
            text = body if g == 1 else f"Part {g} of {len(groups)} of the papers you asked for."
            subject = reply_subject(req["subject"]) + (
                f" ({g}/{len(groups)})" if len(groups) > 1 else "")
            self._send(req["sender"], subject, text, group, in_reply_to=req["message_id"],
                       references=req["refs"])
        for it in ready:
            self.db.update_item(it["id"], status="sent", sent_at=now_iso(), reported=1)
        for it in notes:
            self.db.update_item(it["id"], reported=1)
        left = [i for i in self.db.items_for(req["id"])
                if i["status"] in ("new", "resolved", "fetched", "manual")]
        if not left:
            self.db.set_request_status(req["id"], "done")

    # ---- 4. owner notifications --------------------------------------
    def notify_owner(self) -> None:
        manual, big = self.db.needs_owner()
        if not manual and not big:
            return
        parts = []
        if manual:
            lines = [f"  #{r['id']}  {describe(r)}  (for {r['sender']})\n        "
                     + ("https://doi.org/" + r["doi"] if r["doi"] else r["landing_url"] or "")
                     for r in manual]
            parts.append(
                "The publisher blocked automated download (or asked for a CAPTCHA) on these:\n\n"
                + "\n".join(lines) +
                "\n\nWhen you're at your PC, either:\n"
                "  paperbot manual           - a browser window opens for each one (VPN handled);\n"
                "                              solve any check and click the PDF/Download button\n"
                "  paperbot attach <id> <file.pdf>\n"
                "                            - if you downloaded it yourself in your own browser\n"
                "PaperBot then emails it to the requester.\n"
                "To give up on one:  paperbot giveup <id>   (the requester is told)")
        if big:
            lines = [f"  #{r['id']}  {describe(r)}  (for {r['sender']})\n        {r['pdf_path']}"
                     for r in big]
            parts.append("Too large to email - please share these yourself (e.g. Google Drive):"
                         "\n\n" + "\n".join(lines))
        self._send(self.cfg.owner_address,
                   f"[PaperBot] {len(manual) + len(big)} paper(s) need you",
                   "\n\n".join(parts) + "\n")
        for r in manual + big:
            self.db.update_item(r["id"], owner_notified=1)

    # ---- 5. manual help for CAPTCHA'd papers --------------------------
    def run_manual(self, manual_factory, say=print, item_ids: list[int] | None = None) -> int:
        rows = [r for r in self.db.items_with_status("manual") if r["pdf_path"] is None
                and (item_ids is None or r["id"] in item_ids)]
        if not rows:
            say("Nothing is waiting for you.")
            return 0
        got = 0
        with ExitStack() as stack:
            if self.cfg.vpn_enabled and self.vpn is not None:
                stack.enter_context(vpn_session(self.vpn, self.cfg.vpn_disconnect_when_done))
            session = stack.enter_context(manual_factory())
            for n, r in enumerate(rows, 1):
                if getattr(getattr(session, "control", None), "stop", None) and \
                        session.control.stop.is_set():
                    break
                say(f"[{n}/{len(rows)}] {describe(r)} (for {r['sender']}): a browser window "
                    "is open. Solve the check there if one appears, then download the PDF - "
                    "PaperBot picks it up.")
                data = session.capture(paper_from_row(r), r["landing_url"])
                if data:
                    self._save(r, data, "manual", PUBLISHED)
                    self.db.log_download("manual", r["sender"])
                    got += 1
                    self.deliver()          # send it now, not after the whole batch
                    say(f"[{n}/{len(rows)}] Got it - sent to {r['sender']}.")
                else:
                    say(f"[{n}/{len(rows)}] Skipped.")
        self.deliver()
        return got

    def attach(self, item_id: int, data: bytes) -> None:
        """The owner downloaded a waiting paper by hand: send it on."""
        row = self.db.item(item_id)
        if row is None or row["status"] != "manual":
            raise ValueError(f"#{item_id} isn't waiting for you (see `paperbot status`)")
        self._save(row, data, "manual", PUBLISHED)
        self.db.log_download("attach", self.db.request(row["request_id"])["sender"])
        self.deliver()

    def give_up(self, item_ids: list[int] | None) -> int:
        rows = self.db.items_with_status("manual")
        n = 0
        for r in rows:
            if item_ids is None or r["id"] in item_ids:
                self.db.update_item(r["id"], status="failed", reported=0,
                                    reason="sorry, I couldn't get this one")
                n += 1
        self.deliver()
        return n

    # ---- misc ----------------------------------------------------------
    def cleanup(self) -> None:
        for p in self.db.old_pdf_paths(self.cfg.keep_pdfs_days):
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass

    def _greeting(self, name: str | None) -> str:
        first = (name or "").split()[0] if (name or "").split() else "there"
        return f"Hi {first},\n\n"

    def _sign(self) -> str:
        sig = "\n\n" + (self.cfg.owner_name or "")
        if self.cfg.signature:
            sig += ("\n" if self.cfg.owner_name else "") + self.cfg.signature
        return sig.rstrip() + "\n"

    def _send(self, to: str, subject: str, body: str, attachments=(), in_reply_to=None,
              references=None) -> None:
        msg = build_message(self.cfg, to, subject, body, attachments, in_reply_to, references)
        self.mail.send(msg)
