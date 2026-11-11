"""Working together as the owner meets it: the setting, the commands and the app's screens,
with the real mail code talking to the Gmail-like test server."""
import imaplib
import logging
import time

import pytest

from imap_server import PASSWORD, USER, ImapServer
from paperbot import app as app_mod
from paperbot import cli
from paperbot import config as config_mod
from paperbot import mail as mail_mod
from paperbot import sync as sync_mod
from paperbot.app import (Busy, LockLost, SyncError, check_sharing, make_sync, single_instance,
                          start_sharing, stop_sharing, together)
from paperbot.config import Config, ConfigError
from paperbot.db import DB
from paperbot.sync import SUBJECTS, NoSync, Sync

FOLDER = "PaperBot-sync"


@pytest.fixture
def mailbox(monkeypatch):
    """A Gmail-like mailbox that the app's own mail code reaches (without TLS)."""
    server = ImapServer(gmail=True)

    def connect(self):
        imap = imaplib.IMAP4("127.0.0.1", server.port, timeout=10)
        imap.login(self.cfg.mail_username, self.password)
        return imap

    make = app_mod.make_sync

    def make_quick(*args, **kw):                 # the server is right here: no need to wait
        sync = make(*args, **kw)
        if isinstance(sync, Sync):
            sync._lease_options = {"settle_s": 0.05, "poll_s": 0.05}
        return sync

    monkeypatch.setattr(mail_mod.ImapSmtpMail, "connect", connect)
    monkeypatch.setattr(app_mod, "make_sync", make_quick)
    monkeypatch.setenv("PAPERBOT_MAIL_PASSWORD", PASSWORD)
    yield server
    server.stop()


@pytest.fixture
def computers(tmp_path, monkeypatch):
    """use("OFFICE-PC") -> that computer's settings; from then on the code runs "on" it."""
    now = {"name": "OFFICE-PC"}
    monkeypatch.setattr(sync_mod, "computer_name", lambda: now["name"])

    def use(name: str) -> Config:
        home = tmp_path / name
        path = home / "config.toml"
        if not path.exists():
            home.mkdir()
            path.write_text(f'[owner]\nname = "Priya"\n[mail]\nusername = "{USER}"\n',
                            encoding="utf-8")
        now["name"] = name
        monkeypatch.setenv("PAPERBOT_HOME", str(home))
        return config_mod.load(path)
    return use


def records(cfg: Config) -> DB:
    return DB(cfg.data_dir / "paperbot.db")


def part(cfg: Config):
    """That computer's part in working together, as the app builds it."""
    return app_mod.make_sync(cfg, records(cfg))


# ---- the setting ---------------------------------------------------------------------------
def test_a_computer_can_share_only_with_a_mailbox_and_unless_switched_off(tmp_path, monkeypatch):
    saved = {"mail:me@gmail.com": "app-password"}
    monkeypatch.setattr(app_mod, "get_secret", saved.get)
    db = DB(tmp_path / "data" / "paperbot.db")
    cfg = Config(mail_username="me@gmail.com", path=tmp_path / "config.toml", vpn_enabled=False)
    sync = make_sync(cfg, db)
    assert isinstance(sync, Sync) and not sync.joined and sync.vpn is False
    assert sync.lock_path == tmp_path / "data" / "sync.lock"
    assert sync.backup_path == tmp_path / "data" / "paperbot.before-sharing.db"
    assert sync.store.folder == FOLDER and sync.store.address == "me@gmail.com"

    assert isinstance(make_sync(Config(path=cfg.path), db), NoSync)              # no mailbox
    other = Config(mail_username="other@gmail.com", path=cfg.path)
    assert isinstance(make_sync(other, db), NoSync)                              # no password
    assert isinstance(make_sync(other, db, password="typed"), Sync)
    cfg.sync_enabled = False                                                      # [sync] enabled
    assert isinstance(make_sync(cfg, db), NoSync)
    assert isinstance(make_sync(cfg, db, even_if_off=True), Sync)


def test_the_setting_is_read_from_the_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[mail]\nusername = "me@gmail.com"\n', encoding="utf-8")
    cfg = config_mod.load(path)
    assert cfg.sync_enabled is True and cfg.sync_folder == FOLDER
    config_mod.set_value("sync", "enabled", False, path)
    config_mod.set_value("sync", "folder", "  Mine  ", path)
    cfg = config_mod.load(path)
    assert cfg.sync_enabled is False and cfg.sync_folder == "Mine"
    config_mod.set_value("sync", "folder", "inbox", path)
    with pytest.raises(ConfigError, match="must not be the folder PaperBot reads"):
        config_mod.load(path)


# ---- start, join, stop -------------------------------------------------------------------------
def test_start_on_one_computer_and_the_other_joins_by_itself(mailbox, computers):
    pc = computers("OFFICE-PC")
    assert together(pc, records(pc)).summary.startswith("Not in use")
    assert "Started" in start_sharing(pc)
    assert mailbox.subjects(FOLDER) == [SUBJECTS["state"]]
    here = together(pc, records(pc))
    assert here.joined and here.others == [] and here.synced_at is not None
    assert "No other computer has joined yet" in here.summary

    laptop = computers("LAPTOP")
    records(laptop).add_person("ravi@uni.edu", "Ravi", "approved")    # it had history of its own
    part(laptop).refresh()                                            # e.g. the app was opened
    here = together(laptop, records(laptop))
    assert here.joined and here.others == ["OFFICE-PC"]
    assert here.summary == "On: working together with PaperBot on OFFICE-PC."
    assert (laptop.data_dir / "paperbot.before-sharing.db").exists()

    pc = computers("OFFICE-PC")
    part(pc).refresh()
    assert together(pc, records(pc)).others == ["LAPTOP"]
    assert records(pc).person("ravi@uni.edu")["status"] == "approved"
    assert "works together with PaperBot on LAPTOP" in start_sharing(pc)   # pressing it again

    laptop = computers("LAPTOP")
    said = stop_sharing(laptop)
    assert "PaperBot on OFFICE-PC no longer knows" in said and "one computer only" in said
    laptop = config_mod.load(laptop.path)
    assert laptop.sync_enabled is False and isinstance(make_sync(laptop, records(laptop)), NoSync)
    here = together(laptop, records(laptop))
    assert not here.joined and here.summary.startswith("Off on this computer")
    assert records(laptop).person("ravi@uni.edu")                     # it keeps what it knew

    pc = computers("OFFICE-PC")
    part(pc).refresh()
    assert together(pc, records(pc)).others == []
    assert stop_sharing(pc) == "This computer keeps its own records again."
    assert mailbox.subjects(FOLDER) == []                             # the last one out clears up
    assert mailbox.subjects("[Gmail]/Bin") == []


def test_start_needs_a_mailbox_that_passes_the_check(mailbox, computers, monkeypatch):
    pc = computers("OFFICE-PC")
    assert check_sharing(pc).startswith("Your mailbox passed")
    assert mailbox.subjects(FOLDER) == []
    mailbox.dates_from_header = True                # a server PaperBot can't take turns on
    with pytest.raises(SyncError, match="own clock"):
        start_sharing(pc)
    assert not together(pc, records(pc)).joined and mailbox.subjects(FOLDER) == []


# ---- the commands --------------------------------------------------------------------------------
@pytest.fixture
def paperbot(monkeypatch, capsys):
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)

    def run(*words: str) -> str:
        cli.main(list(words))
        return capsys.readouterr().out
    return run


def test_the_sync_commands(mailbox, computers, paperbot):
    pc = computers("OFFICE-PC")
    out = paperbot("sync", "status")
    assert "Not in use" in out and "paperbot sync on" in out
    assert "Your mailbox passed" in paperbot("sync", "check")
    assert "Started" in paperbot("sync", "on")
    out = paperbot("sync", "status")
    assert "No other computer has joined yet" in out and "Records last exchanged" in out
    assert "More than one computer: On." in paperbot("status")
    assert "keeps its own records again" in paperbot("sync", "off")
    assert config_mod.load(pc.path).sync_enabled is False
    assert "Off on this computer" in paperbot("sync", "status")
    assert "Started" in paperbot("sync", "on")                        # ...and back on
    assert config_mod.load(pc.path).sync_enabled is True


def test_forget_goes_through_a_turn_so_the_other_computer_hears_of_it(mailbox, computers,
                                                                      paperbot):
    pc = computers("OFFICE-PC")
    records(pc).add_person("asha@uni.edu", "Asha", "approved")
    start_sharing(pc)
    laptop = computers("LAPTOP")
    part(laptop).refresh()
    assert records(laptop).person("asha@uni.edu")
    assert "Forgot asha@uni.edu" in paperbot("forget", "asha@uni.edu")   # on the laptop
    pc = computers("OFFICE-PC")
    part(pc).refresh()
    assert records(pc).person("asha@uni.edu") is None


def test_an_automatic_check_steps_aside_while_the_other_computer_works(mailbox, computers,
                                                                       monkeypatch, caplog):
    monkeypatch.setattr(cli, "WAIT_FOR_OTHER_S", 0)
    pc = computers("OFFICE-PC")
    start_sharing(pc)
    laptop = computers("LAPTOP")
    turn = part(laptop)
    turn.refresh()
    mailbox.put("INBOX", "Paper request", sender="Asha <asha@uni.edu>", body="10.1000/x")
    pc = computers("OFFICE-PC")
    with caplog.at_level(logging.INFO, logger="paperbot"):
        with turn.session():                         # the laptop is in the middle of its turn
            cli.check_once(pc)                       # ...when the PC's automatic check comes round
        assert "skipped this check: PaperBot on LAPTOP is working right now" in caplog.text
        assert records(pc).recent_requests() == []   # it took nothing on
        with single_instance(pc):                    # the app on the PC itself is busy
            cli.check_once(pc)
        assert "another PaperBot run is in progress on this computer" in caplog.text


# ---- the app (the `window` fixture is in conftest.py) ---------------------------------------------
def test_the_app_shows_where_working_together_stands(window):
    app = window
    home, settings = app.tabs["home"], app.tabs["settings"]
    app.show("settings")
    app.refresh_all()
    assert settings.together_status.cget("text").startswith("Not in use")
    assert not settings.b_together_start.instate(["disabled"])
    assert settings.b_together_stop.instate(["disabled"])
    assert home.together_text.winfo_manager() == ""            # nothing about it on Home

    # The background check on this computer finds PaperBot on LAPTOP sharing, and joins.
    app.db.set_meta(node="abc123", joined="1", synced_at="2026-11-10T12:40:00")
    app.db.touch_computer("THIS-PC-abc123", "THIS-PC", True)
    app.db.touch_computer("LAPTOP-ffffff", "LAPTOP", False)
    app.refresh_all()
    assert "works together with PaperBot on LAPTOP" in app.status.cget("text")
    assert "working together with PaperBot on LAPTOP" in settings.together_status.cget("text")
    assert "12:40" in settings.together_status.cget("text")
    assert settings.b_together_start.instate(["disabled"])
    assert not settings.b_together_stop.instate(["disabled"])
    app.show("home")
    app.refresh_all()
    assert "working together with PaperBot on LAPTOP" in home.together_text.cget("text")
    deadline = time.monotonic() + 3
    while not app.said and time.monotonic() < deadline:        # the notice comes a moment later
        app.root.update()
        time.sleep(0.05)
    assert len(app.said) == 1 and "PaperBot on LAPTOP shares its records" in app.said[0]
    app.refresh_all()
    assert len(app.said) == 1                                  # said once

    app.sync_problem = "PaperBot on LAPTOP is newer than the one on this computer"
    app.refresh_all()
    assert "has stopped: PaperBot on LAPTOP is newer" in home.together_text.cget("text")


def test_pressing_start_reports_once_not_twice(window):
    app = window
    app.started_here = True                    # the Start button was pressed in this window
    app.db.set_meta(node="abc123", joined="1")
    app.db.touch_computer("THIS-PC-abc123", "THIS-PC", True)
    app.refresh_all()
    for _ in range(10):
        app.root.update()
        time.sleep(0.05)
    assert app.said == [] and app.started_here is False


def test_problems_are_put_in_plain_words():
    from paperbot.gui.base import friendly
    assert friendly(Busy("PaperBot on LAPTOP is working right now - try again in a minute")) \
        .startswith("PaperBot on LAPTOP is working")
    assert "asleep or offline" in friendly(LockLost("turn ran out"))
    assert "update PaperBot here" in friendly(SyncError("update PaperBot here"))
