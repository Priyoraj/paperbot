# Working together: PaperBot on several computers

Design notes for `paperbot/sync.py`. The user-facing explanation is in the
[README](../README.md#using-paperbot-on-two-computers).

## The problem

One owner, several Windows computers (say an office PC and a laptop), all reading the **same
mailbox**. Each copy keeps its records in its own SQLite file (`data/paperbot.db`). Left alone,
with automatic mode on everywhere:

- every copy answers every request (colleagues get two replies);
- each counts only its own institute downloads (together they can pass the daily safe limit);
- approvals are per computer;
- two copies may dial the institute VPN with the same account at once.

The mail server is not the problem: Gmail lets many IMAP clients read one mailbox at a time.
What is missing is coordination between the copies. It has to work with no new account and no
server of our own.

## The design in one paragraph

The mailbox gets one extra folder (a Gmail label), `PaperBot-sync`. It holds (a) one small
message with **all of PaperBot's records** (a gzipped JSON snapshot of the shared SQLite
tables) and (b) short-lived **"I'm working" messages**: the lock. A computer that has work to
do takes the lock, loads the newest records, does the work on its local database, puts the
records back and releases the lock. The computers behave like one PaperBot with one set of
records, taking turns, which also means only one of them is on the VPN at a time. A check that
finds nothing to do never takes the lock and writes nothing.

| alternative | why not |
|---|---|
| Move request emails out of the inbox as the "claim" | changes the owner's inbox; two simultaneous IMAP MOVEs are not provably exclusive on Gmail |
| A log of every change, kept in the mailbox | every database write would need an event with stable keys; many messages |
| Sync `paperbot.db` with OneDrive or Drive | corrupts when two computers write |
| Main + backup (only one ever works) | simpler, but the owner wanted both working |
| **Lock + whole-records snapshot** | the pipeline stays plain SQLite; every screen is in step for free; few messages |

## The folder

Everything in the folder carries `X-PaperBot: PaperBot` (so the inbox reader skips it even if
a filter moves it), `X-PaperBot-Sync: <kind>` and `X-PaperBot-Id: <unique hex>`.
Kinds: `lock`, `beat`, `state`, `probe`.

```python
Entry(uid, when, kind, ident, session, node, version, digest)
Store.entries() -> list[Entry] | None      # None: the folder doesn't exist; lists afresh each call
Store.add(kind, *, session="", node="", version=0, digest="", payload=None) -> ident
Store.payload(uid) -> bytes
Store.remove(uids)
Store.clone() / Store.close()               # clone: a separate connection (heartbeat thread)
```

`uid` is the IMAP UID and `when` the IMAP INTERNALDATE. The lock relies on three things the
server guarantees: UIDs grow in arrival order; INTERNALDATE is the server's own clock (so the
computers' clocks don't matter); and a listing made after an APPEND returned shows everything
appended before it. `ImapStore` is the real folder, `tests/fakes.py:FakeStore` the stand-in.

Gmail specifics in `ImapStore`: deleting from a label only takes the label off, so PaperBot's
own messages are moved to the Bin (`\Trash` special-use folder, `UID MOVE`, falling back to
`UID COPY`) and then expunged **there by the UIDs the server reported in `COPYUID`** - never a
bare `EXPUNGE` in the Bin, which would also delete whatever the owner's mail app had marked
there. If the Bin is hidden from IMAP the messages stay in All Mail and a warning is logged.

## The lock ("lease")

Constants: `LEASE_S=900`, `BEAT_S=240`, `SAFETY_S=120`, `SETTLE_S=3`, `POLL_S=8`.

- A **turn** (session) holds the lock while its `lock` message exists and
  `max(when of its lock and beat messages) + LEASE_S > now`. Of the turns alive, the one with
  the **lowest `lock` uid** has it.
- **Taking it**: list. If another turn looks alive (judged by the local clock, corrected by
  what the server last said: only a hint), wait and poll. Otherwise append my `lock`, sleep
  `SETTLE_S`, list again, take `now` from my own message (server time) and work out the
  holder. Mine: delete run-out turns' messages and orphaned beats, go. Not mine: delete my
  message, wait and poll until `wait_s` is over, then raise `Busy("PaperBot on <name> is
  working right now…")`.
- **Fencing**: locally the lock is trusted until `t0 + LEASE_S - SAFETY_S`, where `t0` is the
  local time *before* the last successful lock or beat append, checked on both the wall clock
  and the monotonic clock (a laptop that slept fails the wall-clock test). `Sync.guard()`
  raises `LockLost` otherwise. The pipeline calls it at the start of each unit of work (each
  incoming email, each publisher fetch, each reply), never in the middle of one.
- **Heartbeat**: a daemon thread appends a `beat` every `BEAT_S` over its own connection,
  checks that my `lock` message still exists (a taker deletes it) and removes my older beats.
  It never beats once the lock has run out locally. `guard(renew=True)` beats at once: used
  right before dialling the VPN, in case the mail server can't be reached through it.
- **Release**: stop the thread, delete my lock and beat messages, prune old `state` messages
  (the newest two stay).
- **Crash recovery**: the turn's id is written to the local `sync_meta` when it starts and
  cleared when it ends; a leftover id is deleted from the mailbox at the next start (safe
  because the local file lock `data/sync.lock` allows one turn per computer). Another computer
  simply waits out `LEASE_S`.

## The records

- Shared tables: `people, requests, items, seen, downloads, alerts, computers`.
  Local only: `sync_meta` (joined flag, this computer's id, fingerprints, version, turn id).
- Snapshot: `gzip(json({"format": 1, "tables": {name: {"cols": [...], "rows": [[...]]}}}))`,
  attached to a `state` message with `X-PaperBot-Version` (a counter) and `X-PaperBot-Hash`
  (fingerprint of the canonical JSON). A reader takes the highest `(version, uid)` whose
  fingerprint checks out and falls back to the one before. A `format` newer than the program
  knows raises `SyncError` ("update PaperBot on this computer"): it never writes over records
  it doesn't understand.
- Local bookkeeping: `synced_remote` (fingerprint of the snapshot last exchanged),
  `synced_local` (fingerprint of the local records right after), `version`.
- **Reconcile** at the start of a turn (lock held), with `clean = local == synced_local`:
  1. no snapshot in the mailbox: push mine when the turn ends (first to share, or self-heal);
  2. `remote == local`: mark as exchanged;
  3. `remote == synced_remote`: nobody else wrote; local is ahead and is pushed;
  4. `clean` and `remote.version > version`: import the remote (fast-forward);
  5. otherwise **merge** (first join, or this computer was cut off mid-turn while another
     carried on). Base = the side with the higher version (the remote on a tie).
     people: the newest decision stands; seen, alerts, computers: union; downloads: union by
     their `uid` (rows from before 0.2 get one derived from their contents, so a database that
     began as a copy of another doesn't count a download twice); requests are matched by
     `message_id`: requests only one side knows are added under new ids, and for requests both
     know, the paper (matched by position) that is further along wins
     (`new < resolved < manual/failed/dropped < fetched < sent`), so a reply that really went
     out is never forgotten.
- **Checkpoint** (push if the records differ from `synced_remote`): after intake, and when the
  turn ends. **Refresh** (no lock): at the start of every check, when the app starts and every
  5 minutes in the app; imports the newest snapshot only when local is clean and the local
  file lock is free.

## Turning it on and off

- `[sync] enabled = true` (the default) means "this computer may share"; `folder` is the
  folder's name.
- Nothing is written to the mailbox until the owner presses **Start** on one computer
  (*Settings → More than one computer*, or `paperbot sync on`). Start first runs a round-trip
  self-test on the real mailbox (save, list, read back and delete a `probe` message), then
  pushes the records. The probe is dated 2001 on purpose: if the server reports that date back
  as the arrival time, it isn't using its own clock, and Start refuses.
- Every other computer with the same mailbox **joins by itself** at its next check or when
  the app starts (it sees a `state` message), after saving `data/paperbot.before-sharing.db`.
- *Stop on this computer* / `paperbot sync off`: the computer signs out of the shared list
  (the last one out deletes the `state` messages) and `enabled` becomes `false`: this copy
  then ignores shared records.
- Fail-safe: once joined, a run fails rather than answer uncoordinated if the folder can't be
  read.

Where each piece lives: `sync.py` (store, lease, merge, `Sync`, `NoSync`), `db.py`
(export/import, fingerprints, `sync_meta`, `computers`), `pipeline.py` (turns and guards),
`app.py` (`make_sync`, `start_sharing`, `stop_sharing`, `check_sharing`, `together`), `cli.py`
(`paperbot sync …`), `gui/` (Settings section, Home line, background refresh).

## Tests

- `tests/test_sync.py`, `tests/test_two_computers.py`: lock, snapshot, merge and two-computer
  end-to-end runs on a fake folder and fake clocks (sleeping laptops, power cuts, wrong clocks).
- `tests/test_imap_store.py`: the real `ImapStore` over a real socket against
  `tests/imap_server.py`, as an ordinary server and as Gmail (labels, All Mail, Bin), including
  "nothing else in the Bin is ever touched". Set `PAPERBOT_TEST_IMAP` to run them against a
  real server too.
- `tests/test_together.py`: start, join, stop, the `paperbot sync` commands and the app's
  screens, with the app's own mail code talking to the Gmail-like test server.

## Checked against Gmail (November 2026)

Run on a real Gmail mailbox, in the real folder (the self-test) and in a throwaway label (the
`test_imap_store.py` checks that don't look inside the server):

- Gmail advertises `UIDPLUS`, `MOVE`, `SPECIAL-USE` and `X-GM-EXT-1`. The Bin is the folder
  flagged `\Trash` (`[Gmail]/Trash` or `[Gmail]/Bin`, depending on the account's language).
- An appended message's INTERNALDATE is Gmail's own clock: the probe dated 2001 comes back
  stamped with the time it was saved.
- `UID MOVE` to the Bin answers with `COPYUID`, and expunging those UIDs there removes the
  messages for good: afterwards a search of All Mail and of the Bin finds none of them.
- A second connection sees an append as soon as it has returned.
- Logging in takes about 1 s, a command 0.3 to 1 s, a whole turn about 13 s (3 s of that is
  the settle wait). Once, after a burst of activity, the same sequence ran four times slower
  for a few minutes: everything still passed. A command that hangs for 20 s or more before
  it is retried gets a line in the activity log.

Still to be seen on two real computers: both with automatic mode on, one request, one reply.

## Known limits

- Settings (limits, approval on/off, trusted domains) stay **per computer**. Sharing them
  through the snapshot is a natural next step.
- The lock covers PaperBot's own use of the VPN only. If the owner is connected by hand on one
  computer while the other fetches, the institute may refuse or drop one of them. Workaround:
  untick "Use the institute VPN" on one computer; it then leaves paywalled papers to the other
  (for up to 24 hours, then falls back to a preprint as before).
- A downloaded PDF lives only on the computer that fetched it (`items.host`). The other one
  downloads it again if it has to finish that request.
- Two computers with the same Windows name are told apart internally, but the screens show the
  same name for both.
