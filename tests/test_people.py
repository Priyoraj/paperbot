"""Blocking people: someone on the list, or an address that hasn't written yet."""
from paperbot.db import DB
from paperbot.gui.people import read_addresses
from paperbot.pipeline import Bot
from test_pipeline import (OWNER, FakeMail, FakeOA, FakeResolver, FakeVpn, email_msg,
                           world)  # noqa: F401 - `world` is a fixture


def test_addresses_are_read_however_they_are_typed():
    assert read_addresses(" Ravi@Else.org ") == ([("", "ravi@else.org")], [])
    assert read_addresses('Asha Rao <asha@uni.edu>; "Kumar, Ravi" <ravi@else.org>\n'
                          "mailto:x@y.co") == (
        [("Asha Rao", "asha@uni.edu"), ("Kumar, Ravi", "ravi@else.org"), ("", "x@y.co")], [])
    # A name with a comma, as some mail programs write it - and the same person twice.
    assert read_addresses("Rao, Asha <asha@uni.edu>, ASHA@uni.edu") == (
        [("Rao, Asha", "asha@uni.edu")], [])
    # What isn't an address is said, never guessed at or silently dropped.
    assert read_addresses("ravi@college, nobody, a@b.org") == (
        [("", "a@b.org")], ["ravi@college", "nobody"])
    assert read_addresses("ravi") == ([], ["ravi"])
    assert read_addresses("  ") == read_addresses("") == ([], [])


def test_someone_blocked_beforehand_is_ignored_from_the_first_request(world):
    w = world
    db = w.bot.db
    assert w.bot.decide(" Spam@Else.org", approve=False, name="Spam Person") == 0
    person = db.person("spam@else.org")
    assert person["status"] == "denied" and person["name"] == "Spam Person"

    w.mail.inbox.append(email_msg("Spam Person <spam@else.org>", "Paper request", "10.1000/oa"))
    w.run()
    assert w.mail.sent == []                   # no paper - and no "approve?" email to the owner
    assert len(db.requests_with_status("denied", "spam@else.org")) == 1

    w.bot.forget("spam@else.org")              # the block is lifted: they count as new again
    w.mail.inbox.append(email_msg("Spam Person <spam@else.org>", "Paper request", "10.1000/oa"))
    w.run()
    assert [str(m["Subject"])[:18] for m in w.mail.to(OWNER)] == ["[PaperBot] Approve"]
    assert w.mail.to("spam@else.org") == []


def test_blocking_someone_who_is_waiting_drops_their_requests(world):
    w = world
    w.mail.inbox.append(email_msg("Ravi <ravi@else.org>", "Paper request", "10.1000/oa"))
    w.run()
    assert w.bot.db.person("ravi@else.org")["status"] == "pending"
    assert w.bot.decide("ravi@else.org", approve=False, name="ignored: he is known already") == 1
    assert w.bot.db.person("ravi@else.org")["name"] == "Ravi"
    w.run()
    assert w.mail.to("ravi@else.org") == []    # nothing is sent to him, now or later


def test_an_address_is_blocked_from_the_people_page(window, monkeypatch):
    from tkinter import messagebox
    app, tab, asked = window, window.tabs["people"], []
    monkeypatch.setattr(messagebox, "askyesno",
                        lambda title, text, **kw: asked.append(text) or True)

    def bot():                                 # as app.bot(), minus mailbox, VPN and browser
        return Bot(app.cfg, DB(app.cfg.data_dir / "paperbot.db"), FakeMail(), FakeResolver({}),
                   FakeOA(), FakeVpn(), lambda: None)

    monkeypatch.setattr(app, "bot", bot)
    app.show("people")

    def type_in(text):
        tab.block_entry.delete(0, "end")
        tab.block_entry.insert(0, text)
        tab.block_address()

    type_in("someone, I don't remember")
    assert "doesn't look like an email address" in app.said[-1]
    type_in("")
    assert "Type the email address" in app.said[-1]
    type_in("p@example.com")                   # the owner's own mailbox
    assert "your own address" in app.said[-1]
    assert not asked and app.db.people() == [] and len(app.said) == 3

    type_in("Spam Person <Spam@Else.org>")
    app.pump(lambda: app.db.person("spam@else.org") is not None and not app._busy)
    assert len(asked) == 1 and "Block spam@else.org?" in asked[0]
    person = app.db.person("spam@else.org")
    assert person["status"] == "denied" and person["name"] == "Spam Person"
    assert tab.block_entry.get() == "" and "Blocked: spam@else.org" in app.status.cget("text")
    app.refresh_all()
    assert tab.tree.item("spam@else.org")["values"][:3] == ["spam@else.org", "Spam Person",
                                                            "Blocked"]
