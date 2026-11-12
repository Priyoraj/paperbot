"""SQLite state: who is approved, what was asked for, and where each paper stands."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
  email TEXT PRIMARY KEY,
  name TEXT,
  status TEXT NOT NULL,              -- approved | pending | denied
  token TEXT,                        -- reference code in the approval email to the owner
  prompted_at TEXT,
  first_seen TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY,
  message_id TEXT UNIQUE NOT NULL,
  sender TEXT NOT NULL,
  sender_name TEXT,
  subject TEXT,
  refs TEXT,                         -- References header, so replies stay in the thread
  status TEXT NOT NULL,              -- held | queued | done | denied | ignored
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  raw TEXT NOT NULL,                 -- what the requester wrote
  kind TEXT NOT NULL,                -- doi | arxiv | url | title
  value TEXT NOT NULL,
  doi TEXT, arxiv_id TEXT, landing_url TEXT,
  title TEXT, authors TEXT, year INTEGER,
  status TEXT NOT NULL,              -- new | resolved | fetched | manual | failed | sent
  pdf_path TEXT, source TEXT, version TEXT,
  reason TEXT,                       -- shown to the requester when not delivered
  reported INTEGER NOT NULL DEFAULT 0,
  owner_notified INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT, sent_at TEXT, updated_at TEXT,
  host TEXT                          -- the computer that holds pdf_path
);
CREATE TABLE IF NOT EXISTS seen(message_id TEXT PRIMARY KEY, kind TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS downloads(      -- every PDF fetched through the institute's access
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  source TEXT NOT NULL,                    -- publisher | manual | attach | try
  requester TEXT,
  uid TEXT                                 -- so two computers' counts can be put together
);
CREATE TABLE IF NOT EXISTS alerts(key TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS computers(      -- the computers that share these records
  node TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  vpn INTEGER NOT NULL DEFAULT 0,          -- does it fetch through the institute VPN?
  seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_meta(key TEXT PRIMARY KEY, value TEXT);   -- this computer only
CREATE TABLE IF NOT EXISTS toasted(key TEXT PRIMARY KEY, at TEXT);        -- this computer only:
                                           -- papers its Windows notifications have announced
"""

# The records computers share when they work together (see sync.py): table -> its key.
# sync_meta and toasted are deliberately not here.
SHARED_TABLES = {"people": "email", "requests": "id", "items": "id", "seen": "message_id",
                 "downloads": "id", "alerts": "key", "computers": "node"}
STATE_FORMAT = 1    # raise when a change means older versions can't use the shared records

ITEM_FIELDS = {
    "doi", "arxiv_id", "landing_url", "title", "authors", "year", "status", "pdf_path",
    "source", "version", "reason", "reported", "owner_notified", "fetched_at", "sent_at",
    "host",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def state_digest(state: dict) -> str:
    """Fingerprint of exported records: equal records give equal fingerprints. (32 hex
    digits: it travels in a mail header, which must stay on one line.)"""
    blob = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()[:32]


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # timeout: the app and the background check share this file
        self.conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if "host" not in self._columns("items"):       # database from before 0.2
            self.conn.execute("ALTER TABLE items ADD COLUMN host TEXT")
        if "uid" not in self._columns("downloads"):
            self.conn.execute("ALTER TABLE downloads ADD COLUMN uid TEXT")
        # Downloads from before 0.2 get a tag that comes out the same on every computer: a
        # database that began as a copy of another one (a second computer set up from the
        # first) must not count the same download twice when the two are put together.
        for row in self.conn.execute(
                "SELECT id, at, source, requester FROM downloads WHERE uid IS NULL").fetchall():
            basis = "|".join(str(row[k]) for k in ("id", "at", "source", "requester"))
            self.conn.execute("UPDATE downloads SET uid=? WHERE id=?",
                              (hashlib.sha256(basis.encode()).hexdigest()[:16], row["id"]))
        self.conn.commit()

    def _columns(self, table: str) -> list[str]:
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]

    def close(self) -> None:
        self.conn.close()

    def _x(self, sql: str, args=()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    # ---- messages already handled -----------------------------------
    def is_seen(self, message_id: str) -> bool:
        if self.conn.execute("SELECT 1 FROM seen WHERE message_id=?", (message_id,)).fetchone():
            return True
        return bool(self.conn.execute(
            "SELECT 1 FROM requests WHERE message_id=?", (message_id,)).fetchone())

    def mark_seen(self, message_id: str, kind: str) -> None:
        self._x("INSERT OR IGNORE INTO seen VALUES(?,?,?)", (message_id, kind, now_iso()))

    # ---- people -----------------------------------------------------
    def person(self, email: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM people WHERE email=?", (email.lower(),)).fetchone()

    def person_by_token(self, token: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM people WHERE token=?", (token.lower(),)).fetchone()

    def people(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM people ORDER BY status, email").fetchall()

    def add_person(self, email: str, name: str, status: str, token: str | None = None) -> None:
        self._x("INSERT OR IGNORE INTO people(email,name,status,token,first_seen,decided_at)"
                " VALUES(?,?,?,?,?,?)",
                (email.lower(), name, status, token, now_iso(),
                 None if status == "pending" else now_iso()))

    def set_person_status(self, email: str, status: str) -> None:
        email = email.lower()
        if not self.person(email):
            self.add_person(email, "", status)
        self._x("UPDATE people SET status=?, decided_at=?, token=NULL WHERE email=?",
                (status, now_iso(), email))

    def set_prompted(self, email: str, token: str) -> None:
        self._x("UPDATE people SET token=?, prompted_at=? WHERE email=?",
                (token.lower(), now_iso(), email.lower()))

    def forget_person(self, email: str) -> None:
        self._x("DELETE FROM people WHERE email=?", (email.lower(),))

    # ---- requests ---------------------------------------------------
    def add_request(self, message_id, sender, sender_name, subject, refs, status, note=None) -> int:
        cur = self._x("INSERT INTO requests(message_id,sender,sender_name,subject,refs,status,note,"
                      "created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (message_id, sender.lower(), sender_name, subject, refs, status, note,
                       now_iso()))
        return cur.lastrowid

    def request(self, request_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()

    def requests_with_status(self, status: str, sender: str | None = None) -> list[sqlite3.Row]:
        if sender:
            return self.conn.execute("SELECT * FROM requests WHERE status=? AND sender=? ORDER BY id",
                                     (status, sender.lower())).fetchall()
        return self.conn.execute("SELECT * FROM requests WHERE status=? ORDER BY id",
                                 (status,)).fetchall()

    def set_request_status(self, request_id: int, status: str, note: str | None = None) -> None:
        self._x("UPDATE requests SET status=?, note=COALESCE(?, note) WHERE id=?",
                (status, note, request_id))

    def release_held(self, sender: str, status: str) -> int:
        cur = self._x("UPDATE requests SET status=? WHERE status='held' AND sender=?",
                      (status, sender.lower()))
        return cur.rowcount

    # ---- items ------------------------------------------------------
    def add_item(self, request_id: int, raw: str, kind: str, value: str) -> int:
        cur = self._x("INSERT INTO items(request_id,raw,kind,value,status,updated_at)"
                      " VALUES(?,?,?,?,'new',?)", (request_id, raw, kind, value, now_iso()))
        return cur.lastrowid

    def item(self, item_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def items_for(self, request_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM items WHERE request_id=? ORDER BY id",
                                 (request_id,)).fetchall()

    def active_items(self, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
        """Items of queued requests in the given states, oldest request first."""
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT items.*, requests.sender AS sender, requests.created_at AS req_created"
            f" FROM items JOIN requests"
            f" ON items.request_id = requests.id WHERE requests.status='queued'"
            f" AND items.status IN ({marks}) ORDER BY items.request_id, items.id",
            statuses).fetchall()

    def update_item(self, item_id: int, **fields) -> None:
        bad = set(fields) - ITEM_FIELDS
        if bad:
            raise ValueError(f"unknown item fields {bad}")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))

    def cached_pdf(self, doi: str | None, arxiv_id: str | None) -> sqlite3.Row | None:
        """A previously fetched copy of the same paper whose file still exists."""
        for col, val in (("doi", doi), ("arxiv_id", arxiv_id)):
            if not val:
                continue
            for row in self.conn.execute(
                    f"SELECT * FROM items WHERE lower({col})=lower(?) AND pdf_path IS NOT NULL"
                    " ORDER BY fetched_at DESC", (val,)):
                if Path(row["pdf_path"]).exists():
                    return row
        return None

    def delivered_today(self, sender: str) -> int:
        day = datetime.now().date().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM items JOIN requests ON items.request_id=requests.id"
            " WHERE requests.sender=? AND items.fetched_at >= ?", (sender.lower(), day)
        ).fetchone()[0]

    # ---- institute downloads (the daily safety ceiling) -----------------
    def log_download(self, source: str, requester: str | None) -> None:
        self._x("INSERT INTO downloads(at, source, requester, uid) VALUES(?,?,?,?)",
                (now_iso(), source, (requester or "").lower(), secrets.token_hex(8)))

    def downloads_today(self) -> int:
        day = datetime.now().date().isoformat()
        return self.conn.execute("SELECT COUNT(*) FROM downloads WHERE at >= ?",
                                 (day,)).fetchone()[0]

    def downloads_last_hour(self) -> int:
        since = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        return self.conn.execute("SELECT COUNT(*) FROM downloads WHERE at >= ?",
                                 (since,)).fetchone()[0]

    def publisher_fetches_today(self) -> int:
        day = datetime.now().date().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE source='publisher' AND fetched_at >= ?", (day,)
        ).fetchone()[0]

    def old_pdf_paths(self, days: int) -> list[str]:
        """Files only referenced by items delivered more than `days` ago."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT DISTINCT pdf_path FROM items WHERE pdf_path IS NOT NULL AND pdf_path NOT IN"
            " (SELECT pdf_path FROM items WHERE pdf_path IS NOT NULL AND"
            "  (status NOT IN ('sent','failed') OR fetched_at >= ?))", (cutoff,)).fetchall()
        return [r[0] for r in rows]

    def needs_owner(self) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """(CAPTCHA'd papers, papers too large to email) the owner hasn't been told about."""
        base = ("SELECT items.*, requests.sender AS sender FROM items JOIN requests"
                " ON items.request_id=requests.id WHERE items.owner_notified=0 AND ")
        manual = self.conn.execute(base + "items.status='manual' AND items.pdf_path IS NULL"
                                   " ORDER BY items.id").fetchall()
        big = self.conn.execute(base + "items.status='failed' AND items.pdf_path IS NOT NULL"
                                " AND items.reason LIKE 'too large%' ORDER BY items.id").fetchall()
        return manual, big

    # ---- for the desktop app -----------------------------------------
    def sent_today(self) -> int:
        day = datetime.now().date().isoformat()
        return self.conn.execute("SELECT COUNT(*) FROM items WHERE sent_at >= ?",
                                 (day,)).fetchone()[0]

    def recent_requests(self, limit: int = 300) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, COUNT(i.id) AS n_items, SUM(i.status='sent') AS n_sent,"
            " SUM(i.status IN ('failed','manual')) AS n_problem FROM requests r"
            " LEFT JOIN items i ON i.request_id=r.id AND i.status != 'dropped'"
            " GROUP BY r.id ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()

    def owner_todo(self) -> list[sqlite3.Row]:
        """Papers waiting for the owner: blocked ones to fetch by hand, and files too large
        to email (until dismissed, owner_notified=2)."""
        return self.conn.execute(
            "SELECT items.*, requests.sender AS sender, requests.sender_name AS sender_name"
            " FROM items JOIN requests ON items.request_id=requests.id WHERE"
            " (items.status='manual' AND items.pdf_path IS NULL AND requests.status='queued')"
            " OR (items.status='failed' AND items.pdf_path IS NOT NULL"
            "     AND items.reason LIKE 'too large%' AND items.owner_notified < 2)"
            " ORDER BY items.id").fetchall()

    def prompts_today(self) -> int:
        day = datetime.now().date().isoformat()
        return self.conn.execute("SELECT COUNT(*) FROM people WHERE prompted_at >= ?",
                                 (day,)).fetchone()[0]

    def items_with_status(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT items.*, requests.sender AS sender, requests.sender_name AS sender_name"
            " FROM items JOIN requests ON items.request_id=requests.id"
            " WHERE items.status=? AND requests.status='queued' ORDER BY items.id",
            (status,)).fetchall()

    # ---- owner alerts (rate-limited) --------------------------------
    def alert_due(self, key: str, every: timedelta) -> bool:
        row = self.conn.execute("SELECT at FROM alerts WHERE key=?", (key,)).fetchone()
        if row and datetime.now() - datetime.fromisoformat(row[0]) < every:
            return False
        self._x("INSERT OR REPLACE INTO alerts VALUES(?,?)", (key, now_iso()))
        return True

    # ---- Windows notifications shown on this computer (see notify.py) -----
    def claim_toast(self, key: str) -> bool:
        """True for the first PaperBot process on this computer that asks about this paper:
        the background check and the open app must not both announce it. (The app asks every
        few seconds, so the usual "announced already" answer only reads.)"""
        if self.conn.execute("SELECT 1 FROM toasted WHERE key=?", (key,)).fetchone():
            return False
        return self._x("INSERT OR IGNORE INTO toasted VALUES(?,?)", (key, now_iso())).rowcount == 1

    def forget_old_toasts(self, days: int = 180) -> None:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        self._x("DELETE FROM toasted WHERE at < ?", (cutoff,))

    # ---- is there anything to do? ---------------------------------------
    def has_work(self) -> bool:
        """True when a run would find, fetch, send or report something (new mail aside)."""
        if self.conn.execute(
                "SELECT 1 FROM items JOIN requests ON items.request_id=requests.id"
                " WHERE requests.status='queued' AND (items.status IN ('new','resolved','fetched')"
                " OR (items.status IN ('failed','manual') AND items.reported=0)) LIMIT 1").fetchone():
            return True
        if self.conn.execute(      # nothing to send at all: the requester still gets a reply
                "SELECT 1 FROM requests r WHERE r.status='queued' AND NOT EXISTS (SELECT 1 FROM"
                " items i WHERE i.request_id=r.id AND i.status!='dropped') LIMIT 1").fetchone():
            return True
        manual, big = self.needs_owner()
        return bool(manual or big)

    # ---- working together with other computers (see sync.py) --------------
    def meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, **values: str | None) -> None:
        for key, value in values.items():
            if value is None:
                self.conn.execute("DELETE FROM sync_meta WHERE key=?", (key,))
            else:
                self.conn.execute("INSERT OR REPLACE INTO sync_meta VALUES(?,?)",
                                  (key, str(value)))
        self.conn.commit()

    def export_state(self) -> dict:
        """All shared records, in a form that is the same on every computer."""
        tables = {}
        for name, key in SHARED_TABLES.items():
            cols = sorted(self._columns(name))
            rows = self.conn.execute(
                f"SELECT {', '.join(cols)} FROM {name} ORDER BY {key}").fetchall()
            tables[name] = {"cols": cols, "rows": [list(r) for r in rows]}
        return {"format": STATE_FORMAT, "tables": tables}

    def import_state(self, state: dict) -> None:
        """Replace the shared records in one step: the app sees the old or the new ones."""
        try:
            for name in reversed(list(SHARED_TABLES)):
                self.conn.execute(f"DELETE FROM {name}")
            for name in SHARED_TABLES:
                table = state.get("tables", {}).get(name)
                if not table or not table["rows"]:
                    continue
                known = set(self._columns(name))
                keep = [i for i, c in enumerate(table["cols"]) if c in known]
                cols = [table["cols"][i] for i in keep]
                self.conn.executemany(
                    f"INSERT INTO {name}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                    [[row[i] for i in keep] for row in table["rows"]])
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def backup_to(self, path: Path | str) -> None:
        dest = sqlite3.connect(str(path))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()

    def computers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM computers ORDER BY name, node").fetchall()

    def touch_computer(self, node: str, name: str, vpn: bool,
                       every: timedelta = timedelta(days=1)) -> None:
        """Keep this computer's line in the shared list fresh (rewritten at most once a day,
        so it doesn't make every run look like a change)."""
        row = self.conn.execute("SELECT * FROM computers WHERE node=?", (node,)).fetchone()
        if row and row["name"] == name and bool(row["vpn"]) == bool(vpn) and \
                datetime.now() - datetime.fromisoformat(row["seen"]) < every:
            return
        self._x("INSERT OR REPLACE INTO computers(node, name, vpn, seen) VALUES(?,?,?,?)",
                (node, name, int(bool(vpn)), now_iso()))

    def forget_computer(self, node: str) -> None:
        self._x("DELETE FROM computers WHERE node=?", (node,))

    def vpn_elsewhere(self, node: str, within: timedelta = timedelta(days=14)) -> bool:
        """Does another computer sharing these records fetch through the institute VPN?"""
        since = (datetime.now() - within).isoformat(timespec="seconds")
        return bool(self.conn.execute(
            "SELECT 1 FROM computers WHERE node != ? AND vpn = 1 AND seen >= ? LIMIT 1",
            (node, since)).fetchone())
