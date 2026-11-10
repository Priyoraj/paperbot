# Handoff: PaperBot on two computers at once ("working together")

Working notes for whoever continues this branch (`claude/adoring-cerf-l8zmxs`). **Delete this
file before merging to `main`**; the user-facing explanation belongs in `README.md`.

## 1. What was asked

The owner runs PaperBot on two Windows computers (office PC + laptop) that read the **same
Gmail mailbox**. Today each copy keeps its own SQLite records (`data/paperbot.db`), so with
automatic mode on in both:

- both answer every request (colleagues get two replies);
- each counts only its own institute downloads (together they can pass the daily safe limit);
- approvals are per computer;
- both may dial the institute VPN with the same account at once.

Request: *"I want paperbot to work together in both my computers simultaneously in sync."*
No new accounts or servers. An earlier thread proposed using the mailbox itself as the shared
place, and warned that the VPN usually allows one connection per account.

Facts already confirmed with the owner (and from Google's docs):

- Gmail does **not** log one device out when another connects (up to 15 IMAP clients).
  The real problem is only the missing coordination between PaperBot copies.
- Changing the Google password revokes the app password (then the login fails on both).

## 2. Design in one paragraph

The mailbox gets one extra folder (Gmail label) **`PaperBot-sync`**. It holds (a) one small
message with **all of PaperBot's records** (a gzipped JSON snapshot of the shared SQLite
tables) and (b) short-lived **"I'm working" lock messages**. A computer that has work to do
takes the lock, loads the newest records, does the work on its local database, puts the new
records back and releases the lock. So the two computers behave like one PaperBot with one set
of records, taking turns - which also guarantees only one of them uses the VPN at a time.
A check that finds nothing to do never takes the lock and writes nothing.

Why this and not the alternatives:

| option | verdict |
|---|---|
| Move request emails out of the inbox as the "claim" | changes the owner's inbox; two simultaneous IMAP MOVEs are not provably exclusive on Gmail |
| Event log of every change in the mailbox | every DB write would need an event with stable keys; many messages |
| Sync `paperbot.db` with OneDrive/Drive | corrupts when both write |
| Main + backup (only one works at a time) | simpler, but the owner asked for both working together |
| **Lock + whole-records snapshot (chosen)** | pipeline code stays as it is (still plain SQLite); every screen is in sync for free; few messages |

## 3. Protocol (the part that must be right)

All messages in the folder carry `X-PaperBot: PaperBot` (so the inbox reader ignores them even
if a filter moves them) and `X-PaperBot-Sync: <kind>`, `X-PaperBot-Id: <unique hex>`.
Kinds: `lock`, `beat`, `state`, `probe`.

### 3.1 Store interface (`paperbot/sync.py`)

```python
Entry(uid:int, when:float, kind:str, ident:str, session:str, node:str, version:int, digest:str)
Store.entries() -> list[Entry] | None      # None = folder doesn't exist; re-SELECTs each call
Store.add(kind, *, session="", node="", version=0, digest="", payload=None) -> str  # ident
Store.payload(uid) -> bytes
Store.remove(uids) -> None
Store.clone() / Store.close()               # clone = separate connection (heartbeat thread)
```

`when` = IMAP INTERNALDATE (server clock) so the two PCs' clocks don't matter. `uid` is the
IMAP UID: strictly increasing in arrival order. Those two server guarantees plus
"a listing made after my APPEND returned shows everything appended before it" are all the
lock relies on. Implementations: `ImapStore` (real), `FakeStore` (tests, `tests/fakes.py`).

### 3.2 Lock ("lease")

Constants: `LEASE_S=900`, `BEAT_S=240`, `SAFETY_S=120`, `SETTLE_S=3`, `POLL_S=8`.

- A **session** holds the lock iff its `lock` message exists and
  `max(when of its lock/beat messages) + LEASE_S > now`. Among live sessions the one with the
  **lowest `lock` uid** is the holder.
- **Acquire**: list; if somebody else looks live (judged by the local clock - only a hint)
  wait/poll. Otherwise append my `lock`, sleep `SETTLE_S`, list again, take `now` = `when` of
  my own message (server time), compute the holder. If it's me: delete expired sessions'
  messages and orphan beats, go. If not: delete my message and wait/poll until `wait_s` is
  over, then raise `Busy("PaperBot on <name> is working right now…")`.
- **Validity (fencing)**: locally valid until `t0 + LEASE_S - SAFETY_S`, where `t0` is the local
  time *before* the last successful lock/beat append - checked on both the wall clock and the
  monotonic clock (a laptop that slept fails the wall-clock check). `Sync.guard()` raises
  `LockLost` when not valid. The pipeline calls it at the start of each unit of work (each
  incoming email, each publisher fetch, each reply), never in the middle of one.
- **Heartbeat**: a daemon thread appends a `beat` every `BEAT_S` over its own connection,
  then checks that my `lock` message still exists (a taker deletes it) and removes my older
  beats. It never beats once locally expired (no zombie renewals). `guard(renew=True)` beats
  synchronously - used right before dialing the VPN, in case IMAP is unreachable through it.
- **Release**: stop the thread, delete my lock/beat messages, prune old `state` messages
  (keep the newest two).
- **Crash recovery**: the session id is written to local `sync_meta` at acquire and cleared at
  release; a leftover id is deleted from the mailbox at the next acquire (safe because a local
  file lock `data/sync.lock` allows one session per computer). Another computer simply waits
  out `LEASE_S`.

### 3.3 Records snapshot

- Shared tables: `people, requests, items, seen, downloads, alerts, computers`.
  Local-only: `sync_meta` (joined flag, node id, digests, version, session).
- Snapshot = `gzip(json({"format": 1, "tables": {name: {"cols": [...], "rows": [[...]]}}}))`
  attached to a `state` message with `X-PaperBot-Version` (counter) and `X-PaperBot-Hash`
  (sha256 of the canonical JSON). A reader takes the highest `(version, uid)` whose hash
  verifies, falling back to the previous one. `format` newer than the program knows =>
  `SyncError` ("update PaperBot on this computer") - never write with an older format.
- Local meta: `synced_remote` (hash of the snapshot last synced with), `synced_local` (hash of
  the local records right after), `version`.
- **Reconcile** at session start (lock held), with `clean = local_hash == synced_local`:
  1. no snapshot in the mailbox -> push mine at the next checkpoint (first sharer / self-heal);
  2. `remote.hash == local_hash` -> mark synced;
  3. `remote.hash == synced_remote` -> nobody else wrote; local is ahead, pushed at checkpoint;
  4. `clean` and `remote.version > version` -> import remote (fast-forward);
  5. otherwise **merge** (first join, or this computer was cut off mid-run and the other one
     carried on): base = the side with the higher version (remote on a tie);
     people/seen/alerts/computers: union, base wins; downloads: union by `(at, source,
     requester)`; requests matched by `message_id`: other-only requests are appended with new
     ids; for requests on both sides the item (matched by position) that is further along wins
     (`new < resolved < manual/failed/dropped < fetched < sent`) so a reply that was really
     sent is never forgotten.
- **Checkpoint** (push if `local_hash != synced_remote`): after intake, and when the session
  ends. Lock-free **refresh** (start of every check, app start, every 5 min in the app):
  import the newest snapshot only when local is clean and the local file lock is free.

### 3.4 Turning it on

- Config `[sync] enabled = true` (default) means "this computer may share"; `folder =
  "PaperBot-sync"`.
- Nothing is written to the mailbox until the owner presses **Start** on one computer
  (Settings > *More than one computer*, or `paperbot sync on`). Start first runs a round-trip
  self-test on the real mailbox (add/list/read/remove a `probe`), then pushes the records.
- Every other computer with the same mailbox **joins by itself** at its next check or app
  start (it sees a `state` message), after saving `data/paperbot.before-sharing.db`.
- `enabled = false` (Stop on this computer / `paperbot sync off`) = this copy ignores the
  shared records; the owner is warned to keep automatic mode on only one computer then.
- Fail-safe: once joined, if the folder can't be read the run fails instead of answering
  uncoordinated.

## 4. Code changes, file by file

| file | change |
|---|---|
| `paperbot/sync.py` (new) | `Entry`, `Busy`, `LockLost`, `SyncError`, `file_lock`, `Lease`, `merge_states`, `Sync`, `NoSync`, `ImapStore` |
| `paperbot/db.py` | `sync_meta` + `computers` tables, `items.host` column (migration), `export_state/import_state/state_digest`, `meta/set_meta`, `has_work`, `backup_to`, `vpn_elsewhere` |
| `paperbot/pipeline.py` | `Bot(..., sync=None)`; `run_once(wait_s)` = refresh -> fetch inbox -> only if new mail or `has_work()`: session{intake, checkpoint, process, deliver, notify}; guards; `decide/attach/give_up/run_manual` wrapped in sessions; new `forget`, `mark_shared`; VPN-less computer leaves paywalled papers to one that has VPN; alert keys per computer |
| `paperbot/mail.py` | `ImapSmtpMail.connect()` reused by `ImapStore` |
| `paperbot/app.py` | `single_instance` built on `file_lock`; `make_sync`; `build_bot` wires it; `fetch_one` VPN part in a session; `setup_checks` line for shared records |
| `paperbot/cli.py` | `paperbot sync status|on|off|check`; `run`/`watch` wait up to 60 s for the other computer; `approve/deny/forget` under the local lock |
| `paperbot/config.py`, `config.example.toml` | `[sync] enabled`, `folder`; version 0.2.0 |
| `paperbot/gui/*` | Settings section + buttons; People *Block/Forget* and Needs-you *I've shared it* go through the bot (session); background refresh at start and every 5 min; "Show the file" names the computer that has it |
| `tests/fakes.py`, `tests/test_sync.py` (new) | fake mailbox store + clock; lock, snapshot, merge and two-computer end-to-end tests |
| `tests/test_imap_store.py` (new) | runs `ImapStore` against a real IMAP server only when `PAPERBOT_TEST_IMAP` is set |
| `README.md` | "Using PaperBot on two computers" |

## 5. Status

See the checklist at the end of this file (kept up to date as work lands).

## 6. How to test

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q            # fakes only: no mail, no VPN
```

Real IMAP server (Dovecot on Linux, no Gmail needed) - see `tests/test_imap_store.py` for the
`PAPERBOT_TEST_IMAP=host:port:user:password[:plain]` variable. Dovecot 2.3 was installed in the
cloud container with `apt-get install dovecot-imapd`; the throwaway config used is recorded in
section 8.

**Not verifiable without the owner's mailbox** (do these on the Windows PC):

1. `paperbot sync check` - the probe round trip against real Gmail. Confirms: label creation,
   APPEND, INTERNALDATE = server time, header fetch, delete via `[Gmail]/Trash` + expunge.
2. Start sharing on the PC, open the app on the laptop: it should say it joined; People /
   Requests / Needs you match on both.
3. Automatic mode on both; send one "Paper request" email: exactly one reply.
4. In Gmail, label `PaperBot-sync` should hold 1-2 "PaperBot records" messages and no pile of
   lock messages; nothing new in the Inbox; Trash not filling up with sync messages.

## 7. Known limits / follow-ups

- Settings (limits, approval on/off, trusted domains) stay **per computer**; set them the same
  on both. Sharing them through the snapshot is a natural next step.
- If the owner is connected to the institute VPN by hand on one computer while the other one
  fetches a paper, the institute may still refuse or drop one of them: PaperBot's lock only
  covers PaperBot's own use. Workaround: untick "Use the institute VPN" on one computer; it
  then leaves paywalled papers to the other (for up to 24 h, then falls back as before).
- A downloaded PDF lives only on the computer that fetched it (`items.host`). The other
  computer re-downloads if it has to finish that request.
- Gmail specifics that are coded but untested here: the Trash move (`\Trash` special-use
  folder, `UID MOVE`, `COPYUID`), and that Gmail assigns INTERNALDATE at APPEND time.

## 8. Scratch notes

(filled in as work proceeds)

## 9. Checklist

- [ ] `db.py`: shared-state export/import/digest, meta, `has_work`, migration
- [ ] `sync.py`: lease, merge, `Sync`, `NoSync`
- [ ] `tests/fakes.py` + `tests/test_sync.py`
- [ ] `pipeline.py` integration
- [ ] `ImapStore` + real-server test
- [ ] `app.py` / `cli.py` / `config.py`
- [ ] GUI
- [ ] README
- [ ] Verified on the owner's Gmail (section 6)
