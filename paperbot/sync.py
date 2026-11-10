"""Working together: several computers answering the same mailbox as one PaperBot.

Each computer still works on its own SQLite file. What makes them one is a folder in the
mailbox (a Gmail label) that holds

  * one message with all the shared records (people, requests, papers, download counts), and
  * a short-lived "I'm working" message - the lock.

A computer with work to do takes the lock, loads the newest records, works, puts the records
back and lets go. So requests are answered once, limits count for all computers together, and
only one of them is on the institute VPN at a time. A check that finds nothing to do writes
nothing.

The lock needs only three things from the mail server: message numbers (UIDs) grow in the
order messages arrive, each message gets the server's own arrival time, and a listing made
after "saved" shows everything saved before it.
"""
from __future__ import annotations

import email
import gzip
import imaplib
import json
import logging
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable, Protocol

from .config import APP_NAME
from .db import DB, SHARED_TABLES, STATE_FORMAT, now_iso, state_digest
from .mail import BOT_HEADER, MONTHS

log = logging.getLogger(__name__)

LEASE_S = 15 * 60    # a computer that stops renewing is taken to be gone after this long
BEAT_S = 4 * 60      # how often a working computer renews
SAFETY_S = 2 * 60    # a computer stops relying on its lock this long before it really ends
SETTLE_S = 3         # pause between saying "I'd like to work" and reading who came first
POLL_S = 8           # how often to look again while another computer is working
KEEP_STATES = 2      # the newest records message plus one spare


class Busy(RuntimeError):
    """Another PaperBot run - on this computer or another one - is working right now."""


class LockLost(RuntimeError):
    """This computer's turn ended under it (asleep or offline too long). Stop; send nothing."""


class SyncError(RuntimeError):
    """The shared records in the mailbox can't be used."""


@contextmanager
def file_lock(path: Path, wait_s: float = 0,
              busy: str = "PaperBot is already busy - try again in a minute"):
    """One holder at a time on this computer."""
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
                    raise Busy(busy)
                time.sleep(1)
        yield
    finally:
        fh.close()


# ---- what the shared folder holds ------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    uid: int             # the server's message number: grows in arrival order
    when: float          # the server's arrival time (seconds since 1970)
    kind: str            # lock | beat | state | probe
    ident: str           # unique per message
    session: str = ""    # lock, beat: one working turn
    node: str = ""       # the computer that wrote it
    version: int = 0     # state: counts up with every save
    digest: str = ""     # state: fingerprint of the records inside


class Store(Protocol):
    def entries(self) -> list[Entry] | None: ...     # None: the folder doesn't exist
    def add(self, kind: str, *, session: str = "", node: str = "", version: int = 0,
            digest: str = "", payload: bytes | None = None) -> str: ...
    def payload(self, uid: int) -> bytes: ...
    def remove(self, uids: list[int]) -> None: ...
    def clone(self) -> "Store": ...                   # a second connection (other thread)
    def close(self) -> None: ...


def node_name(node: str) -> str:
    """'OFFICE-PC-3fa2c1' -> 'OFFICE-PC'"""
    return node.rsplit("-", 1)[0] if "-" in node else node


def computer_name() -> str:
    """What Windows calls this computer."""
    import platform
    return platform.node() or "this computer"


# ---- the lock ------------------------------------------------------------------------------
class Lease:
    """"One computer works at a time", kept as messages in the shared folder.

    A turn (session) holds the lock while its `lock` message exists and its newest message
    is younger than the lease. Of the turns alive, the one whose `lock` message arrived
    first has it."""

    def __init__(self, store: Store, node: str, clock=time, lease_s: float = LEASE_S,
                 safety_s: float = SAFETY_S, settle_s: float = SETTLE_S, poll_s: float = POLL_S,
                 remember: Callable[[str | None], None] = lambda session: None):
        self.store, self.node, self.clock = store, node, clock
        self.lease_s, self.safety_s = lease_s, safety_s
        self.settle_s, self.poll_s = settle_s, poll_s
        self.remember = remember           # note the turn locally, to clear up after a crash
        self.session: str | None = None
        self.other = ""                    # who had it when we didn't get it
        self._skew = 0.0                   # server clock minus this computer's clock
        self._valid_wall = self._valid_mono = 0.0
        self._lost = False
        self._mutex = threading.Lock()     # beats come from two threads
        self._beat_store: Store | None = None
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    # -- who has it ---------------------------------------------------------------------
    def _turns(self, entries: list[Entry]) -> dict[str, tuple[int, float, str]]:
        """session -> (number of its lock message, time of its newest message, node)"""
        out: dict[str, tuple[int, float, str]] = {}
        for e in entries:
            if e.kind == "lock" and (e.session not in out or e.uid < out[e.session][0]):
                out[e.session] = (e.uid, e.when, e.node)
        for e in entries:
            if e.kind in ("lock", "beat") and e.session in out:
                uid, newest, node = out[e.session]
                out[e.session] = (uid, max(newest, e.when), node)
        return out

    def holder(self, entries: list[Entry], now: float) -> tuple[str, str] | None:
        """(session, node) of the turn that has the lock at server time `now`."""
        alive = [(uid, session, node) for session, (uid, newest, node)
                 in self._turns(entries).items() if newest + self.lease_s > now]
        if not alive:
            return None
        _, session, node = min(alive)
        return session, node

    # -- taking it ------------------------------------------------------------------------
    def acquire(self, wait_s: float = 0, stale: str | None = None) -> list[Entry]:
        """Take the lock or raise Busy after wait_s. `stale`: a turn this computer started
        and never finished (it crashed): cleared away first. Returns the folder's contents."""
        deadline = self.clock.monotonic() + wait_s
        first = True
        while True:
            entries = self.store.entries() or []
            if stale:
                gone = [e.uid for e in entries if e.session == stale]
                if gone:
                    self.store.remove(gone)
                    entries = [e for e in entries if e.session != stale]
                stale = None
            # The first try always asks the server (its clock decides); after that this
            # computer's clock, corrected by what the server said, is a good enough hint.
            if first or self.holder(entries, self.clock.time() + self._skew) is None:
                got = self._attempt()
                if got is not None:
                    return got
            else:
                held = self.holder(entries, self.clock.time() + self._skew)
                self.other = node_name(held[1]) if held else self.other
            first = False
            left = deadline - self.clock.monotonic()
            if left <= 0:
                raise Busy(f"PaperBot on {self.other or 'another computer'} is working right "
                           "now - try again in a minute")
            self.clock.sleep(min(self.poll_s, left))

    def _attempt(self) -> list[Entry] | None:
        session = secrets.token_hex(8)
        self.remember(session)
        wall, mono = self.clock.time(), self.clock.monotonic()
        self.store.add("lock", session=session, node=self.node)
        self.clock.sleep(self.settle_s)
        entries = self.store.entries() or []
        mine = [e for e in entries if e.kind == "lock" and e.session == session]
        if not mine:
            raise SyncError("the mailbox didn't keep PaperBot's \"I'm working\" message")
        now = max(e.when for e in mine)                 # the server's clock, not ours
        self._skew = now - self.clock.time()
        turns = self._turns(entries)
        alive = {s for s, (_, newest, _) in turns.items() if newest + self.lease_s > now}
        winner = self.holder(entries, now)
        if winner is None or winner[0] != session:
            self.store.remove([e.uid for e in mine])
            self.remember(None)
            self.other = node_name(winner[1]) if winner else ""
            return None
        # Ours. Clear away turns that ran out, and renewals whose lock message is gone.
        dead = [e.uid for e in entries if e.kind in ("lock", "beat") and e.session not in alive]
        if dead:
            self.store.remove(dead)
        self.session, self._lost = session, False
        self._valid_wall = wall + self.lease_s - self.safety_s
        self._valid_mono = mono + self.lease_s - self.safety_s
        return [e for e in entries if e.uid not in dead]

    # -- keeping it -----------------------------------------------------------------------
    def valid(self) -> bool:
        """Can this computer still rely on having the lock? Both clocks must agree: a
        computer that slept fails the wall-clock test however little it ran."""
        return (self.session is not None and not self._lost
                and self.clock.time() < self._valid_wall
                and self.clock.monotonic() < self._valid_mono)

    def beat(self) -> None:
        """Renew. Never after the lock has run out here: that could revive a turn another
        computer has already taken over from."""
        with self._mutex:
            if self.session is None or self._lost:
                return
            if not self.valid():
                self._lost = True
                return
            wall, mono = self.clock.time(), self.clock.monotonic()
            if self._beat_store is None:
                self._beat_store = self.store.clone()
            store = self._beat_store
            store.add("beat", session=self.session, node=self.node)
            entries = store.entries() or []
            if not any(e.kind == "lock" and e.session == self.session for e in entries):
                self._lost = True                       # another computer took over
                return
            self._valid_wall = wall + self.lease_s - self.safety_s
            self._valid_mono = mono + self.lease_s - self.safety_s
            old = sorted(e.uid for e in entries
                         if e.kind == "beat" and e.session == self.session)[:-1]
            if old:
                store.remove(old)

    def start_heartbeat(self, every: float = BEAT_S) -> None:
        self._stop = stop = threading.Event()

        def run() -> None:
            wait = every
            while not stop.wait(wait):
                try:
                    self.beat()
                    wait = every
                except Exception as exc:  # noqa: BLE001 - offline for a moment: try sooner
                    log.debug("couldn't renew the working-together lock: %s", exc)
                    wait = min(30, every)

        self._thread = threading.Thread(target=run, daemon=True, name="paperbot-heartbeat")
        self._thread.start()

    # -- giving it back ---------------------------------------------------------------------
    def release(self, also: Callable[[list[Entry]], list[int]] = lambda entries: []) -> None:
        """Let go. `also` may name more messages to delete in the same sweep."""
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        with self._mutex:
            session, self.session = self.session, None
            if self._beat_store is not None:
                self._beat_store.close()
                self._beat_store = None
        if session is None:
            return
        entries = self.store.entries() or []
        uids = [e.uid for e in entries
                if e.kind in ("lock", "beat") and e.session == session] + also(entries)
        if uids:
            self.store.remove(uids)
        self.remember(None)


# ---- the records ----------------------------------------------------------------------------
def pack_state(state: dict) -> bytes:
    return gzip.compress(json.dumps(state, separators=(",", ":")).encode("utf-8"), mtime=0)


def unpack_state(blob: bytes) -> dict:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def _rows(state: dict, table: str) -> list[dict]:
    t = state.get("tables", {}).get(table) or {"cols": [], "rows": []}
    return [dict(zip(t["cols"], row)) for row in t["rows"]]


def _table(rows: list[dict], key: str) -> dict:
    cols = sorted({c for r in rows for c in r})
    rows = sorted(rows, key=lambda r: r[key])
    return {"cols": cols, "rows": [[r.get(c) for c in cols] for r in rows]}


ITEM_RANK = {"new": 0, "resolved": 1, "manual": 2, "failed": 2, "dropped": 2, "fetched": 3,
             "sent": 4}
REQUEST_RANK = {"held": 0, "queued": 1, "done": 2}


def merge_states(base: dict, other: dict) -> dict:
    """Fold `other` into `base`: used when a computer first joins, and when one was cut off
    in the middle of its turn while another carried on. `base` wins where both changed the
    same thing, except that work which really happened is never lost: a paper that was sent
    stays sent, a download stays counted, the newest decision about a person stands."""
    out = {name: _rows(base, name) for name in SHARED_TABLES}

    def stamp(p: dict) -> str:
        return max(p.get("decided_at") or "", p.get("prompted_at") or "",
                   p.get("first_seen") or "")

    people = {p["email"]: p for p in out["people"]}
    for p in _rows(other, "people"):
        if p["email"] not in people or stamp(p) > stamp(people[p["email"]]):
            people[p["email"]] = p
    out["people"] = list(people.values())

    requests = {r["message_id"]: r for r in out["requests"]}
    ours_by_request: dict[int, list[dict]] = {}
    for it in out["items"]:
        ours_by_request.setdefault(it["request_id"], []).append(it)
    theirs_by_request: dict[int, list[dict]] = {}
    for it in sorted(_rows(other, "items"), key=lambda i: i["id"]):
        theirs_by_request.setdefault(it["request_id"], []).append(it)
    next_request = max([r["id"] for r in out["requests"]], default=0) + 1
    next_item = max([i["id"] for i in out["items"]], default=0) + 1
    for r in sorted(_rows(other, "requests"), key=lambda r: r["id"]):
        theirs = theirs_by_request.get(r["id"], [])
        mine = requests.get(r["message_id"])
        if mine is None:                     # only the other side knows this email
            out["requests"].append({**r, "id": next_request})
            for it in theirs:
                out["items"].append({**it, "id": next_item, "request_id": next_request})
                next_item += 1
            next_request += 1
            continue
        changed = False
        ours = sorted(ours_by_request.get(mine["id"], []), key=lambda i: i["id"])
        for a, b in zip(ours, theirs):       # the same email parses into the same list
            if (a["raw"], a["kind"]) == (b["raw"], b["kind"]) and \
                    ITEM_RANK.get(b["status"], 0) > ITEM_RANK.get(a["status"], 0):
                a.update({k: v for k, v in b.items() if k not in ("id", "request_id")})
                changed = True
        if REQUEST_RANK.get(r["status"], -1) > REQUEST_RANK.get(mine["status"], 9):
            mine["status"], mine["note"] = r["status"], r.get("note") or mine.get("note")
            changed = True
        if changed and mine["status"] in ("queued", "done"):
            open_ = any(i["status"] in ("new", "resolved", "fetched", "manual")
                        or (i["status"] == "failed" and not i["reported"]) for i in ours)
            mine["status"] = "queued" if open_ else "done"

    seen = {s["message_id"] for s in out["seen"]}
    out["seen"] += [s for s in _rows(other, "seen") if s["message_id"] not in seen]

    counted = {d["uid"] for d in out["downloads"]}
    next_download = max([d["id"] for d in out["downloads"]], default=0) + 1
    for d in _rows(other, "downloads"):
        if d["uid"] not in counted:
            out["downloads"].append({**d, "id": next_download})
            next_download += 1

    alerts = {a["key"]: a for a in out["alerts"]}
    for a in _rows(other, "alerts"):
        if a["key"] not in alerts or (a["at"] or "") > (alerts[a["key"]]["at"] or ""):
            alerts[a["key"]] = a
    out["alerts"] = list(alerts.values())

    computers = {c["node"]: c for c in out["computers"]}
    for c in _rows(other, "computers"):
        if c["node"] not in computers or c["seen"] > computers[c["node"]]["seen"]:
            computers[c["node"]] = c
    out["computers"] = list(computers.values())

    return {"format": STATE_FORMAT,
            "tables": {name: _table(out[name], key) for name, key in SHARED_TABLES.items()}}


# ---- putting it together -----------------------------------------------------------------
class NoSync:
    """PaperBot on its own: nothing is shared."""
    name = node = ""
    joined = False

    def refresh(self, wait_s: float = 0) -> None:
        pass

    @contextmanager
    def session(self, wait_s: float = 0):
        yield

    def checkpoint(self) -> None:
        pass

    def guard(self, renew: bool = False) -> None:
        pass


class Sync:
    """This computer's part in working together. Everything that changes the records or
    uses the VPN happens inside `with sync.session():`."""

    def __init__(self, db: DB, store: Store, name: str, lock_path: Path, vpn: bool = False,
                 clock=time, heartbeat: bool = True, backup_path: Path | None = None,
                 lease: dict | None = None):
        self.db, self.store, self.clock = db, store, clock
        self.name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "computer")[:40]
        tag = db.meta("node")
        if not tag:
            tag = secrets.token_hex(3)
            db.set_meta(node=tag)
        self.node = f"{self.name}-{tag}"
        self.lock_path, self.backup_path = Path(lock_path), backup_path
        self.vpn, self.heartbeat = vpn, heartbeat
        self._lease_options = lease or {}
        self._lease: Lease | None = None
        self._depth = 0
        self._remote_version = 0
        self._leaving = False

    # -- state of things ------------------------------------------------------------------
    @property
    def joined(self) -> bool:
        return self.db.meta("joined") == "1"

    def others(self) -> list[str]:
        """Names of the other computers sharing these records."""
        return [c["name"] for c in self.db.computers() if c["node"] != self.node]

    # -- between turns ------------------------------------------------------------------
    def refresh(self, wait_s: float = 0) -> None:
        """Pick up what another computer wrote. Takes no lock, changes nothing in the
        mailbox - except the first time this computer finds shared records: then it joins."""
        if self._depth:
            return
        try:
            try:
                with file_lock(self.lock_path):
                    entries = self.store.entries()
                    shared = bool(self._states(entries))
                    if self.joined:
                        if shared:
                            self._fast_forward(entries)
                        return
                    if not shared:
                        return
            except Busy:
                return                    # this computer is in the middle of a turn
            with self.session(wait_s):    # first contact: join under the lock
                pass
        finally:
            self.store.close()

    # -- a turn ---------------------------------------------------------------------------
    @contextmanager
    def session(self, wait_s: float = 0):
        if self._depth:                   # already in one, e.g. decide() inside run_once()
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        with file_lock(self.lock_path, wait_s):
            try:
                entries = self.store.entries()
                if not self.joined and not self._states(entries):
                    self._depth = 1       # nobody shares this mailbox's records
                    try:
                        yield
                    finally:
                        self._depth = 0
                    return
                lease = Lease(self.store, self.node, self.clock,
                              remember=lambda s: self.db.set_meta(session=s),
                              **self._lease_options)
                entries = lease.acquire(wait_s, stale=self.db.meta("session"))
                self._lease = lease
                try:
                    self._reconcile(entries)
                    if self.heartbeat:
                        lease.start_heartbeat()
                    self._depth = 1
                    done = False
                    try:
                        yield
                        done = True
                    finally:
                        self._depth = 0
                        try:
                            self._save()  # also after an error: what was done, was done
                        except LockLost:
                            if done:      # otherwise that is what stopped the work already
                                raise
                        except Exception:
                            if done:
                                raise
                            log.exception("couldn't save the shared records to the mailbox")
                finally:
                    self._lease = None
                    try:
                        lease.release(also=self._spare_states)
                    except Exception as exc:  # noqa: BLE001 - it frees itself in a while
                        log.warning("couldn't tell the mailbox this computer is done (%s); "
                                    "the other computers wait up to %d minutes", exc,
                                    lease.lease_s // 60)
            finally:
                self.store.close()

    def checkpoint(self) -> None:
        """Save the records now, so other computers see what this one has taken on."""
        if self._lease is not None:
            self._save()

    def guard(self, renew: bool = False) -> None:
        """Call before each piece of work that sends something or uses the VPN."""
        lease = self._lease
        if lease is None:
            return
        if renew:
            try:
                lease.beat()
            except Exception as exc:  # noqa: BLE001 - the validity check below decides
                log.debug("couldn't renew the working-together lock: %s", exc)
        if not lease.valid():
            raise LockLost("this computer's turn ran out (asleep or offline for too long), so "
                           "it stopped; another computer may have carried on")

    # -- starting and stopping ------------------------------------------------------------
    def start(self, wait_s: float = 60) -> None:
        """Start sharing from this computer (or join if another one already did)."""
        self.selftest()
        self.db.set_meta(joined="1")
        with self.session(wait_s):
            pass

    def leave(self, wait_s: float = 30) -> None:
        """This computer stops using the shared records. The last one out removes them."""
        if self.joined:
            try:
                with self.session(wait_s):
                    if self._lease is not None:
                        self._leaving = True          # the save must not sign us in again
                        self.db.forget_computer(self.node)
                        if not self.db.computers():   # nobody left: no save, no records
                            entries = self.store.entries() or []
                            self.store.remove([e.uid for e in entries if e.kind == "state"])
                            self.db.set_meta(
                                synced_remote=state_digest(self.db.export_state()))
            except Exception as exc:  # noqa: BLE001 - leaving must work offline too
                log.warning("couldn't tell the mailbox that this computer left: %s", exc)
            finally:
                self._leaving = False
        for c in self.db.computers():                 # on its own again: no one to wait for
            self.db.forget_computer(c["node"])
        self.db.set_meta(joined=None, synced_remote=None, synced_local=None, version=None,
                         session=None, synced_at=None)

    def selftest(self) -> str:
        """Save, list, read back and delete a test message in the shared folder."""
        token = secrets.token_bytes(48)
        try:
            ident = self.store.add("probe", node=self.node, payload=token)
            entries = self.store.entries() or []
            mine = [e for e in entries if e.ident == ident]
            if not mine:
                raise SyncError("the mailbox didn't list the test message PaperBot just saved")
            if self.store.payload(mine[0].uid) != token:
                raise SyncError("the test message came back changed")
            drift = mine[0].when - self.clock.time()
            self.store.remove([e.uid for e in mine])
            if any(e.ident == ident for e in self.store.entries() or []):
                raise SyncError("the mailbox didn't delete the test message")
        finally:
            self.store.close()
        note = "" if abs(drift) < 90 else \
            f"; this computer's clock is {abs(drift) / 60:.0f} min off (harmless)"
        return f"the mailbox can hold PaperBot's shared records{note}"

    # -- inside a turn ----------------------------------------------------------------------
    @staticmethod
    def _states(entries: list[Entry] | None) -> list[Entry]:
        """Records messages, newest first."""
        return sorted((e for e in entries or [] if e.kind == "state"),
                      key=lambda e: (e.version, e.uid), reverse=True)

    def _spare_states(self, entries: list[Entry]) -> list[int]:
        return [e.uid for e in self._states(entries)[KEEP_STATES:]]

    def _read(self, entries: list[Entry] | None) -> tuple[Entry, dict] | None:
        """The newest records message that arrives intact."""
        for e in self._states(entries):
            try:
                state = unpack_state(self.store.payload(e.uid))
            except Exception as exc:  # noqa: BLE001 - damaged: fall back to the one before
                log.warning("skipping a damaged records message in the mailbox: %s", exc)
                continue
            if int(state.get("format", 0)) > STATE_FORMAT:
                raise SyncError(f"PaperBot on {node_name(e.node)} is newer than the one on "
                                "this computer - update PaperBot here to keep working together")
            if state_digest(state) == e.digest:
                return e, state
            log.warning("skipping a damaged records message in the mailbox")
        return None

    def _mark_synced(self, entry: Entry, local: str) -> None:
        self.db.set_meta(joined="1", synced_remote=entry.digest, synced_local=local,
                         version=str(entry.version), synced_at=now_iso())

    def _fast_forward(self, entries: list[Entry] | None) -> None:
        states = self._states(entries)
        if not states or states[0].digest == self.db.meta("synced_remote"):
            return                                    # nothing new
        if state_digest(self.db.export_state()) != self.db.meta("synced_local"):
            return                                    # unsaved work here: needs a turn
        if states[0].version <= int(self.db.meta("version") or 0):
            return
        found = self._read(entries)
        if found:
            entry, state = found
            self.db.import_state(state)
            self._mark_synced(entry, state_digest(self.db.export_state()))
            log.info("picked up what PaperBot on %s did", node_name(entry.node))

    def _reconcile(self, entries: list[Entry]) -> None:
        """Lock held: bring this computer's records and the mailbox's together."""
        found = self._read(entries)
        local = self.db.export_state()
        mine = state_digest(local)
        if found is None:                 # first to share, or the records were deleted
            self._remote_version = 0
            self.db.set_meta(synced_remote="")
            return
        entry, state = found
        self._remote_version = entry.version
        version = int(self.db.meta("version") or 0)
        if entry.digest == mine:
            self._mark_synced(entry, mine)
        elif entry.digest == self.db.meta("synced_remote"):
            pass                          # nobody else wrote: this computer is simply ahead
        elif mine == self.db.meta("synced_local") and entry.version > version:
            self.db.import_state(state)
            self._mark_synced(entry, state_digest(self.db.export_state()))
            log.info("picked up what PaperBot on %s did", node_name(entry.node))
        else:
            first = self.db.meta("synced_local") is None
            if first and self.backup_path is not None:
                self.db.backup_to(self.backup_path)
            merged = merge_states(state, local) if entry.version >= version \
                else merge_states(local, state)
            self.db.import_state(merged)
            after = state_digest(self.db.export_state())
            if after == entry.digest:
                self._mark_synced(entry, after)
            else:                         # saved when the turn ends
                self.db.set_meta(joined="1", synced_remote=entry.digest,
                                 version=str(max(entry.version, version)))
            log.info("now working together with PaperBot on %s through the mailbox" if first
                     else "this computer and PaperBot on %s had both changed the records; "
                          "they were put together", node_name(entry.node))

    def _save(self) -> None:
        self.guard()
        if not self._leaving:
            self.db.touch_computer(self.node, self.name, self.vpn)
        state = self.db.export_state()
        digest = state_digest(state)
        if digest == self.db.meta("synced_remote"):
            if digest != self.db.meta("synced_local"):
                self.db.set_meta(synced_local=digest)
            return
        version = max(self._remote_version, int(self.db.meta("version") or 0)) + 1
        self.store.add("state", node=self.node, version=version, digest=digest,
                       payload=pack_state(state))
        self._remote_version = version
        self.db.set_meta(joined="1", synced_remote=digest, synced_local=digest,
                         version=str(version), synced_at=now_iso())
        log.debug("shared records saved to the mailbox (v%d)", version)


# ---- the real folder: IMAP ------------------------------------------------------------------
SYNC_HEADER = "X-PaperBot-Sync"
HEADERS = {"ident": "X-PaperBot-Id", "session": "X-PaperBot-Session", "node": "X-PaperBot-Node",
           "version": "X-PaperBot-Version", "digest": "X-PaperBot-Hash"}
FETCH_FIELDS = " ".join([SYNC_HEADER, *HEADERS.values()]).upper()
SUBJECTS = {"lock": "PaperBot is working on {name}",
            "beat": "PaperBot is still working on {name}",
            "state": "PaperBot records (keep this: it lets your computers work together)",
            "probe": "PaperBot test message"}
NOTE = ("PaperBot keeps this message in your mailbox so that the computers you run it on can "
        "work together.\nPlease don't delete, move or change it. PaperBot tidies up after "
        "itself.\n")
DATE_RE = re.compile(r'INTERNALDATE "\s?(\d{1,2})-(\w{3})-(\d{4}) (\d\d):(\d\d):(\d\d) '
                     r'([+-])(\d\d)(\d\d)"')


def build_entry(address: str, kind: str, ident: str, session: str = "", node: str = "",
                version: int = 0, digest: str = "", payload: bytes | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = msg["To"] = address
    msg["Subject"] = SUBJECTS.get(kind, "PaperBot").format(name=node_name(node))
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain="paperbot.invalid")
    msg[BOT_HEADER] = APP_NAME            # the inbox reader skips anything carrying this
    msg[SYNC_HEADER] = kind
    for field, value in (("ident", ident), ("session", session), ("node", node),
                         ("version", str(version) if version else ""), ("digest", digest)):
        if value:
            msg[HEADERS[field]] = value
    msg.set_content(NOTE)
    if payload is not None:
        msg.add_attachment(payload, maintype="application", subtype="octet-stream",
                           filename="paperbot-records.bin")
    return msg.as_bytes(policy=policy.SMTP)


def parse_internaldate(meta: bytes) -> float | None:
    m = DATE_RE.search(meta.decode("ascii", "replace"))
    if not m:
        return None
    day, mon, year, hh, mm, ss, sign, zh, zm = m.groups()
    try:
        month = MONTHS.index(mon.title()) + 1
    except ValueError:
        return None
    zone = timedelta(hours=int(zh), minutes=int(zm)) * (1 if sign == "+" else -1)
    return datetime(int(year), month, int(day), int(hh), int(mm), int(ss),
                    tzinfo=timezone(zone)).timestamp()


def fetch_parts(data) -> list[tuple[bytes, bytes]]:
    """imaplib FETCH result -> [(everything outside the literal, the literal)]. The server may
    put UID and INTERNALDATE before or after the literal; imaplib hands the part after it over
    as the next list element."""
    out = []
    data = list(data or [])
    for i, part in enumerate(data):
        if isinstance(part, tuple) and len(part) == 2:
            tail = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
            out.append((part[0] + b" " + tail, part[1]))
    return out


def parse_entries(data) -> list[Entry]:
    out = []
    for meta, raw in fetch_parts(data):
        hdr = email.message_from_bytes(raw, policy=policy.default)
        kind = str(hdr.get(SYNC_HEADER, "")).strip().lower()
        if not kind:
            continue                       # something else filed under this label: not ours
        uid = re.search(rb"UID (\d+)", meta)
        when = parse_internaldate(meta)
        if not uid or when is None:
            raise SyncError("the mail server left out a message's number or arrival time")
        get = lambda field: str(hdr.get(HEADERS[field], "")).strip()  # noqa: E731
        out.append(Entry(int(uid.group(1)), when, kind, get("ident"), get("session"),
                         get("node"), int(get("version") or 0), get("digest")))
    return sorted(out, key=lambda e: e.uid)


class ImapStore:
    """The shared folder in the real mailbox. One connection, opened when first needed; a
    dropped connection is reopened once. Not for use from two threads: clone() it."""

    def __init__(self, connect: Callable[[], imaplib.IMAP4], folder: str, address: str):
        self.connect, self.folder, self.address = connect, folder, address
        self._quoted = '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
        self._imap: imaplib.IMAP4 | None = None
        self._caps: set[str] = set()
        self._selected = False
        self._count = 0
        self._trash: str | None | bool = False      # False: not looked up yet

    def clone(self) -> "ImapStore":
        return ImapStore(self.connect, self.folder, self.address)

    def close(self) -> None:
        imap, self._imap, self._selected = self._imap, None, False
        if imap is not None:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001 - best effort
                pass

    # -- plumbing ---------------------------------------------------------------------------
    def _do(self, op):
        for attempt in (1, 2):
            try:
                if self._imap is None:
                    self._imap = self.connect()
                    typ, data = self._imap.capability()
                    self._caps = set((data[0] or b"").decode("ascii", "replace").upper().split())
                    self._selected = False
                return op(self._imap)
            except (imaplib.IMAP4.abort, OSError, EOFError):
                self.close()               # the connection dropped: once more on a fresh one
                if attempt == 2:
                    raise

    def _select(self, imap) -> bool:
        """Open the folder afresh (so the listing is current). False: it doesn't exist."""
        typ, data = imap.select(self._quoted)
        self._selected = typ == "OK"
        if self._selected:
            self._count = int((data[0] if data else None) or 0)
            return True
        typ, listed = imap.list('""', self._quoted)
        if typ == "OK" and any(listed or []):
            raise SyncError(f"couldn't open the mailbox folder {self.folder}")
        return False

    # -- the Store interface -----------------------------------------------------------------
    def entries(self) -> list[Entry] | None:
        def op(imap):
            if not self._select(imap):
                return None
            if not self._count:
                return []
            typ, data = imap.uid("FETCH", "1:*",
                                 f"(INTERNALDATE BODY.PEEK[HEADER.FIELDS ({FETCH_FIELDS})])")
            if typ != "OK":
                raise SyncError(f"couldn't read the mailbox folder {self.folder}")
            return parse_entries(data)
        return self._do(op)

    def add(self, kind: str, *, session: str = "", node: str = "", version: int = 0,
            digest: str = "", payload: bytes | None = None) -> str:
        ident = secrets.token_hex(8)
        raw = build_entry(self.address, kind, ident, session, node, version, digest, payload)

        def op(imap):
            typ, data = imap.append(self._quoted, "(\\Seen)", None, raw)
            if typ != "OK":                # most likely the folder isn't there yet
                imap.create(self._quoted)
                typ, data = imap.append(self._quoted, "(\\Seen)", None, raw)
            if typ != "OK":
                raise SyncError(f"couldn't save to the mailbox folder {self.folder}: "
                                f"{_text(data)}")
        self._do(op)
        return ident

    def payload(self, uid: int) -> bytes:
        def op(imap):
            if not self._selected and not self._select(imap):
                raise SyncError(f"the mailbox folder {self.folder} has gone")
            typ, data = imap.uid("FETCH", str(int(uid)), "(BODY.PEEK[])")
            for _, raw in fetch_parts(data if typ == "OK" else []):
                msg = email.message_from_bytes(raw, policy=policy.default)
                for part in msg.iter_attachments():
                    return part.get_payload(decode=True)
            raise SyncError("a PaperBot message in the mailbox has gone or lost its contents")
        return self._do(op)

    def remove(self, uids: list[int]) -> None:
        if not uids:
            return
        ids = ",".join(str(int(u)) for u in sorted(set(uids)))

        def op(imap):
            if not self._selected and not self._select(imap):
                return
            trash = self._trash_folder(imap)
            if trash and self._bin(imap, ids, trash):
                return
            imap.uid("STORE", ids, "+FLAGS.SILENT", "(\\Deleted)")
            if "UIDPLUS" in self._caps:
                imap.uid("EXPUNGE", ids)
            else:
                imap.expunge()
        self._do(op)

    # -- Gmail: deleting from a label only removes the label ---------------------------------
    def _trash_folder(self, imap) -> str | None:
        """Gmail keeps a message in All Mail when it is deleted from a label; only moving it
        to the Bin really removes it. Other servers delete on the spot."""
        if self._trash is False:
            self._trash = None
            if "X-GM-EXT-1" in self._caps:
                typ, data = imap.list()
                for line in data or []:
                    if isinstance(line, bytes) and re.search(rb"\\Trash\b", line, re.I):
                        m = re.search(rb'\)\s+(?:"[^"]*"|NIL)\s+(.+?)\s*$', line)
                        if m:
                            self._trash = m.group(1).decode("ascii", "replace")
                            break
        return self._trash or None

    def _bin(self, imap, ids: str, trash: str) -> bool:
        try:
            imap.response("COPYUID")                       # forget any older one
            if "MOVE" in self._caps and "MOVE" in imaplib.Commands:
                typ, _ = imap.uid("MOVE", ids, trash)
                if typ != "OK":
                    return False
            else:
                typ, _ = imap.uid("COPY", ids, trash)
                if typ != "OK":
                    return False
                imap.uid("STORE", ids, "+FLAGS.SILENT", "(\\Deleted)")
                imap.expunge()
            _, copied = imap.response("COPYUID")           # b'<validity> <from> <to>'
            where = (copied[0] or b"").split() if copied else []
            if len(where) == 3 and "UIDPLUS" in self._caps:
                # ...and out of the Bin, so it doesn't fill up with these for 30 days
                self._selected = False
                typ, _ = imap.select(trash)
                if typ == "OK":
                    binned = where[2].decode("ascii")
                    imap.uid("STORE", binned, "+FLAGS.SILENT", "(\\Deleted)")
                    imap.uid("EXPUNGE", binned)
            return True
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error as exc:
            log.debug("couldn't move PaperBot's own messages to the Bin: %s", exc)
            self._selected = False
            return False


def _text(data) -> str:
    return " ".join(d.decode("utf-8", "replace") if isinstance(d, bytes) else str(d)
                    for d in data or [] if d)
