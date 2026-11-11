"""The shared folder in a real mailbox (paperbot.sync.ImapStore), spoken to over IMAP.

These run against the small server in imap_server.py: as an ordinary mail server, as one that
answers a little differently, and as Gmail (labels, All Mail, the Bin). To run the tests that
don't look inside the server against a real one as well, set

    PAPERBOT_TEST_IMAP=host:port:user:password[:plain]

(":plain" = no TLS, for a throwaway local server). They use a folder with a random name and
delete it afterwards.
"""
import imaplib
import logging
import os
import secrets
import time

import pytest

from imap_server import ALL_MAIL, BIN, PASSWORD, USER, ImapServer
from paperbot import sync as sync_mod
from paperbot.db import DB, state_digest
from paperbot.sync import SUBJECTS, Busy, ImapStore, Sync, SyncError

FOLDER = "PaperBot-sync"
QUICK = {"settle_s": 0.05, "poll_s": 0.05}     # the test server is on this computer: no waiting
REAL = os.environ.get("PAPERBOT_TEST_IMAP")
FLAVOURS = {
    "plain": dict(),
    "plain, answers differently, no MOVE": dict(uid_last=True, caps="IMAP4rev1 UIDPLUS"),
    "plain, bare minimum": dict(caps="IMAP4rev1"),
    "gmail": dict(gmail=True),
    "gmail without MOVE": dict(gmail=True, caps="IMAP4rev1 UIDPLUS"),
}


def connector(server):
    def connect():
        imap = imaplib.IMAP4("127.0.0.1", server.port, timeout=10)
        imap.login(USER, PASSWORD)
        return imap
    return connect


def real_connector():
    parts = REAL.split(":")
    plain = parts[-1] == "plain"
    if plain:
        parts.pop()
    host, port, user, password = parts[0], int(parts[1]), parts[2], ":".join(parts[3:])

    def connect():
        imap = (imaplib.IMAP4 if plain else imaplib.IMAP4_SSL)(host, port, timeout=60)
        imap.login(user, password)
        return imap
    return connect, user


@pytest.fixture
def servers():
    made = []

    def make(**kw):
        made.append(ImapServer(**kw))
        return made[-1]
    yield make
    for server in made:
        server.stop()


@pytest.fixture(params=list(FLAVOURS) + (["a real server"] if REAL else []))
def box(request, servers):
    """(the folder, the test server behind it - None when it is a real one)"""
    if request.param == "a real server":
        connect, user = real_connector()
        folder = f"PaperBot-test-{secrets.token_hex(4)}"
        store = ImapStore(connect, folder, user)
        yield store, None
        empty_and_delete(store, connect)
        return
    server = servers(**FLAVOURS[request.param])
    store = ImapStore(connector(server), FOLDER, USER)
    yield store, server
    store.close()


def empty_and_delete(store: ImapStore, connect) -> None:
    """Take a test folder away again. Its messages first: on Gmail, deleting a label leaves
    the mail that was under it in All Mail."""
    left = store.entries()
    if left:
        store.remove([e.uid for e in left])
    store.close()
    if left is not None:
        imap = connect()
        imap.delete(store._quoted)
        imap.logout()


def computer(tmp_path, store, name, **kw):
    home = tmp_path / name
    return Sync(DB(home / "paperbot.db"), store, name, home / "sync.lock", heartbeat=False,
                lease=QUICK, backup_path=home / "paperbot.before-sharing.db", **kw)


# ---- the folder ----------------------------------------------------------------------------
def test_nothing_is_there_until_the_first_save(box):
    store, _ = box
    assert store.entries() is None                           # not even the folder
    records = b"\x00records\xff" * 400
    before = time.time()
    ident = store.add("state", node="OFFICE-PC-ab12cd", version=7, digest="d" * 32,
                      payload=records)
    (e,) = store.entries()
    assert (e.kind, e.ident, e.node, e.version, e.digest, e.session) == (
        "state", ident, "OFFICE-PC-ab12cd", 7, "d" * 32, "")
    assert before - 120 < e.when < time.time() + 120         # stamped by the server's clock
    assert store.payload(e.uid) == records


def test_messages_are_numbered_in_the_order_they_arrive(box):
    store, _ = box
    idents = [store.add("lock", session=f"s{n}", node="PC-1") for n in range(3)]
    entries = store.entries()
    assert [e.ident for e in entries] == idents
    assert [e.uid for e in entries] == sorted({e.uid for e in entries})
    assert [(e.kind, e.session, e.node) for e in entries] == [
        ("lock", f"s{n}", "PC-1") for n in range(3)]


def test_removing_takes_exactly_the_messages_named(box):
    store, _ = box
    for n in range(4):
        store.add("beat", session=f"s{n}", node="PC-1")
    first = store.entries()
    store.remove([first[0].uid, first[2].uid])
    assert [e.session for e in store.entries()] == ["s1", "s3"]
    store.remove([first[0].uid])                             # gone already: nothing happens
    store.remove([])
    assert [e.session for e in store.entries()] == ["s1", "s3"]


def test_another_connection_sees_the_same_folder(box):
    store, _ = box
    other = store.clone()
    try:
        store.add("lock", session="a", node="PC-1")
        assert [e.session for e in other.entries()] == ["a"]
        other.add("lock", session="b", node="LAPTOP-2")
        assert [e.session for e in store.entries()] == ["a", "b"]
        other.remove([e.uid for e in other.entries() if e.session == "a"])
        assert [e.session for e in store.entries()] == ["b"]
    finally:
        other.close()


def test_mail_that_is_not_paperbots_is_left_out_and_left_alone(servers):
    server = servers(gmail=True)
    store = ImapStore(connector(server), FOLDER, USER)
    store.add("lock", session="a", node="PC-1")
    server.put(FOLDER, "Something the owner filed under this label by hand")
    entries = store.entries()
    assert [e.session for e in entries] == ["a"]
    store.remove([e.uid for e in entries])
    assert server.subjects(FOLDER) == ["Something the owner filed under this label by hand"]


# ---- Gmail: only the Bin really deletes ------------------------------------------------------
@pytest.mark.parametrize("flavour", ["gmail", "gmail without MOVE"])
def test_on_gmail_removed_messages_really_go_and_nothing_else_does(servers, flavour):
    server = servers(**FLAVOURS[flavour])
    server.put(BIN, "Something the owner threw away")
    marked = server.put(BIN, "Thrown away and marked for deletion in a mail app")
    with server.lock:                                        # ...but not expunged yet
        server.folders[BIN].deleted.update(
            uid for uid, mail in server.folders[BIN].mails.items() if mail is marked)
    server.put("INBOX", "A colleague's paper request")
    store = ImapStore(connector(server), FOLDER, USER)
    for n in range(3):
        store.add("lock", session=f"s{n}", node="PC-1")
    keep = store.add("state", node="PC-1", version=1, digest="d" * 32, payload=b"records")
    assert len(server.subjects(ALL_MAIL)) == 5               # Gmail keeps everything there too

    store.remove([e.uid for e in store.entries() if e.kind == "lock"])

    assert [e.ident for e in store.entries()] == [keep]
    assert server.subjects(BIN) == ["Something the owner threw away",
                                    "Thrown away and marked for deletion in a mail app"]
    assert sorted(server.subjects(ALL_MAIL)) == sorted(["A colleague's paper request",
                                                        SUBJECTS["state"]])
    assert server.subjects("INBOX") == ["A colleague's paper request"]


def test_on_gmail_with_the_bin_hidden_it_says_so_once(servers, caplog, monkeypatch):
    monkeypatch.setattr(sync_mod, "_warned", set())
    server = servers(gmail=True, bin_visible=False)
    store = ImapStore(connector(server), FOLDER, USER)
    for n in range(2):
        store.add("lock", session=f"s{n}", node="PC-1")
    with caplog.at_level(logging.WARNING, logger="paperbot.sync"):
        for e in store.entries():
            store.remove([e.uid])
    assert store.entries() == []                             # off the label all the same
    assert len(server.subjects(ALL_MAIL)) == 2               # but they pile up in All Mail
    assert len([r for r in caplog.records if "Bin" in r.getMessage()]) == 1


# ---- the network ---------------------------------------------------------------------------
def test_a_dropped_connection_is_opened_again(servers):
    server = servers()
    store = ImapStore(connector(server), FOLDER, USER)
    store.add("lock", session="a", node="PC-1")
    assert len(store.entries()) == 1
    server.drop_connections()
    assert len(store.entries()) == 1                         # carried on by itself
    server.drop_connections()
    store.add("lock", session="b", node="PC-1")
    server.drop_connections()
    store.remove([e.uid for e in store.entries()])
    assert store.entries() == [] and server.logins >= 3


def test_a_long_silence_from_the_server_gets_a_line_in_the_activity_log(servers, caplog,
                                                                        monkeypatch):
    server = servers()
    store = ImapStore(connector(server), FOLDER, USER)
    store.add("lock", session="a", node="PC-1")
    with caplog.at_level(logging.INFO, logger="paperbot.sync"):
        server.drop_connections()
        assert len(store.entries()) == 1                     # a quick drop: not worth a line
        assert "didn't answer" not in caplog.text
        monkeypatch.setattr(sync_mod, "SLOW_S", 0)           # in real life: 20 seconds of silence
        server.drop_connections()
        assert len(store.entries()) == 1
        assert "the mail server didn't answer for" in caplog.text and "trying again" in caplog.text


def test_an_unreachable_server_is_an_error_not_an_empty_folder(servers):
    server = servers()
    store = ImapStore(connector(server), FOLDER, USER)
    store.add("state", node="PC-1", version=1, digest="d" * 32, payload=b"records")
    store.close()
    server.stop()
    with pytest.raises(OSError):
        store.entries()


# ---- working together through it ---------------------------------------------------------------
def test_the_mailbox_check_passes_and_leaves_an_empty_folder(box, tmp_path):
    store, _ = box
    pc = computer(tmp_path, store, "OFFICE-PC")
    assert "can hold PaperBot's shared records" in pc.selftest()
    assert store.entries() == []


def test_the_mailbox_check_catches_a_server_that_does_not_keep_time(servers, tmp_path):
    server = servers(dates_from_header=True)      # arrival time = whatever the message says
    pc = computer(tmp_path, ImapStore(connector(server), FOLDER, USER), "PC")
    with pytest.raises(SyncError, match="own clock"):
        pc.selftest()
    assert server.subjects(FOLDER) == []                     # it tidied up all the same


def test_two_computers_work_together_through_the_folder(box, tmp_path):
    store, server = box
    pc = computer(tmp_path, store, "OFFICE-PC", vpn=True)
    laptop = computer(tmp_path, store.clone(), "LAPTOP")
    pc.db.add_person("asha@uni.edu", "Asha", "approved")
    pc.start()
    laptop.refresh()                                         # joins by itself
    assert laptop.joined and laptop.db.person("asha@uni.edu")["status"] == "approved"
    with laptop.session():
        laptop.db.add_person("ravi@uni.edu", "Ravi", "pending")
        with pytest.raises(Busy, match="PaperBot on LAPTOP is working"):
            with pc.session():
                raise AssertionError("two computers working at once")
        laptop.guard(renew=True)                             # renewing has its own connection
    pc.refresh()
    assert pc.db.person("ravi@uni.edu")["status"] == "pending"
    assert state_digest(pc.db.export_state()) == state_digest(laptop.db.export_state())
    assert sorted(pc.others() + laptop.others()) == ["LAPTOP", "OFFICE-PC"]
    assert [e.kind for e in store.entries()] == ["state", "state"]   # newest + a spare; no locks
    if server is not None and server.gmail:
        assert server.subjects(BIN) == []                    # nothing left lying in the Bin
        assert server.subjects(ALL_MAIL) == [SUBJECTS["state"]] * 2
    pc.leave()
    laptop.leave()                                           # the last one out clears the records
    assert store.entries() == []
