"""Windows notifications for papers that need the owner; the app opening when Windows starts;
one window however often the app is started. (conftest.py keeps every test away from the real
desktop: what would have been shown lands in the `toasts` list.)"""
import secrets
import subprocess
import sys
from xml.dom import minidom

import pytest

from paperbot import notify, winsvc
from paperbot.config import Config
from paperbot.db import DB, SHARED_TABLES
from paperbot.notify import NotificationsOff, announce, claim_new, page_from, toast_xml
from paperbot.notify import show_toast as really_show     # the real ones, before conftest's
from paperbot.notify import windows_setting as really_ask  # stand-ins take their place
from paperbot.pipeline import Bot
from paperbot.publisher import FetchResult
from paperbot.sync import Busy, NoSync
from test_pipeline import (OWNER, FakeMail, FakeOA, FakePublisher, FakeResolver, FakeVpn,
                           email_msg)


def waiting(db: DB, n: int = 1, kind: str = "manual", who: str = "Asha Rao") -> None:
    """n papers on the Needs-you list: blocked by the publisher, or too large to email."""
    rid = db.add_request(f"<{secrets.token_hex(4)}@uni.edu>", "asha@uni.edu", who,
                         "Paper request", "", "queued")
    for i in range(n):
        item = db.add_item(rid, f"10.1000/{kind}{i}", "doi", f"10.1000/{kind}{i}")
        if kind == "manual":
            db.update_item(item, status="manual", title=f"Guarded paper {i}", reported=1,
                           reason="the publisher's site needs a person to download it")
        else:
            db.update_item(item, status="failed", title=f"Huge paper {i}", reported=1,
                           pdf_path="C:/pdfs/huge.pdf", owner_notified=1,
                           reason="too large to email (40 MB); Priya will share it another way")


# ---- what is announced, and how often ---------------------------------------------------------
def test_a_paper_that_newly_needs_the_owner_is_announced_once(tmp_path, toasts):
    cfg, db = Config(), DB(tmp_path / "paperbot.db")
    assert announce(cfg, db) == 0 and toasts == []
    waiting(db)
    assert announce(cfg, db) == 1
    (title, lines, page), = toasts
    assert title == "A paper needs you" and page == "needs"
    assert lines[0] == 'Asha Rao asked for "Guarded paper 0".'
    assert "Click" in lines[1]
    assert announce(cfg, db) == 0                        # the next check says nothing new
    app_side = DB(tmp_path / "paperbot.db")              # ...nor does the open app
    assert claim_new(cfg, app_side) == [] and len(toasts) == 1
    waiting(db)                                          # another one comes in later
    assert announce(cfg, db) == 1 and len(toasts) == 2


def test_several_at_once_make_one_notification(tmp_path, toasts):
    db = DB(tmp_path / "paperbot.db")
    waiting(db, 3)
    assert announce(Config(), db) == 3
    (title, lines, _), = toasts
    assert title == "3 papers need you"
    assert lines[0] == '"Guarded paper 0" and 2 more.' and "download them" in lines[1]


def test_a_file_too_large_to_email_is_announced_too(tmp_path, toasts):
    db = DB(tmp_path / "paperbot.db")
    waiting(db, kind="big", who="")
    assert announce(Config(), db) == 1
    (title, lines, _), = toasts
    assert title == "A paper is too large to email"
    assert lines[0] == '"Huge paper 0" for asha@uni.edu.' and "share it yourself" in lines[1]
    waiting(db, kind="manual")                           # mixed kinds, in one go
    waiting(db, kind="big")
    assert announce(Config(), db) == 2 and toasts[-1][0] == "2 papers need you"
    assert "too large to email" in toasts[-1][1][1]


def test_switched_off_in_settings_it_says_nothing(tmp_path, toasts):
    db = DB(tmp_path / "paperbot.db")
    waiting(db)
    assert announce(Config(notify_desktop=False), db) == 0 and toasts == []
    assert announce(Config(), db) == 1                   # switched on later: still told, once


def test_each_computer_keeps_its_own_note_of_what_it_announced(tmp_path, toasts):
    cfg, pc, laptop = Config(), DB(tmp_path / "pc.db"), DB(tmp_path / "laptop.db")
    waiting(pc)
    assert announce(cfg, pc) == 1
    assert "toasted" not in SHARED_TABLES and "toasted" not in pc.export_state()["tables"]
    laptop.import_state(pc.export_state())               # the laptop takes in the PC's records
    assert announce(cfg, laptop) == 1                    # ...and tells you there too, once
    assert announce(cfg, laptop) == 0
    pc.import_state(laptop.export_state())               # records going round: no repeats
    assert announce(cfg, pc) == 0 and len(toasts) == 2


def test_old_notes_are_cleared_away(tmp_path):
    db = DB(tmp_path / "paperbot.db")
    assert db.claim_toast("recent") and db.claim_toast("ancient")
    db.conn.execute("UPDATE toasted SET at='2020-01-01T00:00:00' WHERE key='ancient'")
    db.forget_old_toasts()
    assert not db.claim_toast("recent") and db.claim_toast("ancient")


# ---- the automatic check ---------------------------------------------------------------------
def bot_that_notifies(tmp_path, sync=None):
    cfg = Config(owner_name="Priya", owner_email=OWNER, mail_username=OWNER,
                 path=tmp_path / "config.toml", auto_approve_domains=["college.edu"])
    mail, vpn = FakeMail(), FakeVpn()
    papers = {"10.1000/captcha": dict(doi="10.1000/captcha", title="Guarded paper", year=2019)}
    pub = FakePublisher({"10.1000/captcha": FetchResult("blocked", reason="captcha")}, vpn)
    return Bot(cfg, DB(tmp_path / "t.db"), mail, FakeResolver(papers), FakeOA(), vpn,
               lambda: pub, sync=sync, notifier=notify.announce), mail


def test_the_automatic_check_announces_a_paper_the_publisher_blocked(tmp_path, toasts):
    bot, mail = bot_that_notifies(tmp_path)
    bot.run_once()
    assert toasts == []                                  # nothing waiting: nothing said
    mail.inbox.append(email_msg("Ravi Kumar <ravi@college.edu>", "Paper request",
                                "10.1000/captcha"))
    bot.run_once()
    (title, lines, _), = toasts
    assert title == "A paper needs you" and lines[0] == 'Ravi Kumar asked for "Guarded paper".'
    assert len([m for m in mail.to(OWNER) if "need you" in m["Subject"]]) == 1   # and the email
    bot.run_once()
    assert len(toasts) == 1


def test_a_check_that_steps_aside_still_announces_what_came_in(tmp_path, toasts):
    class OtherComputerAtWork(NoSync):
        def refresh(self, wait_s=0):
            raise Busy("PaperBot on LAPTOP is working right now - try again in a minute")

    bot, _ = bot_that_notifies(tmp_path, OtherComputerAtWork())
    waiting(bot.db)                                      # e.g. picked up from the laptop earlier
    with pytest.raises(Busy):
        bot.run_once()
    assert [t[0] for t in toasts] == ["A paper needs you"]


def test_a_notification_that_fails_never_breaks_a_check(tmp_path, monkeypatch, caplog):
    def broken(title, lines, page="needs"):
        raise RuntimeError("Windows said no")

    monkeypatch.setattr(notify, "show_toast", broken)
    bot, mail = bot_that_notifies(tmp_path)
    mail.inbox.append(email_msg("Ravi Kumar <ravi@college.edu>", "Paper request",
                                "10.1000/captcha"))
    bot.run_once()                                       # no exception
    assert "couldn't show the Windows notification: Windows said no" in caplog.text
    assert len(mail.to("ravi@college.edu")) == 1         # the requester was answered as usual


# ---- the notification itself -------------------------------------------------------------------
def test_the_notification_is_well_formed_whatever_a_title_contains():
    xml = toast_xml('A "paper" <needs> you & me', ["Line <1>", "Line & 2", "a third: left out"])
    doc = minidom.parseString(xml)
    toast = doc.documentElement
    assert toast.getAttribute("launch") == "paperbot:needs"
    assert toast.getAttribute("activationType") == "protocol"
    assert [t.firstChild.data for t in doc.getElementsByTagName("text")] == [
        'A "paper" <needs> you & me', "Line <1>", "Line & 2"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows notifications")
def test_showing_hands_the_notification_to_windows(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw["env"]))
        return subprocess.CompletedProcess(cmd, 0, "SETTING Enabled\nOK\n", "")

    monkeypatch.setattr(notify.subprocess, "run", run)
    really_show("A paper needs you", ['Asha asked for "X".'])
    (cmd, env), = calls
    assert cmd[0] == "powershell" and "ToastNotificationManager" in cmd[-1]
    assert env["PAPERBOT_APP_ID"] == "PaperBot.Desktop"
    assert "<text>A paper needs you</text>" in env["PAPERBOT_TOAST"]
    assert really_ask() == "Enabled"
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "", "something\nAccess is denied.\n"))
    with pytest.raises(RuntimeError, match="Access is denied"):
        really_show("A paper needs you", [])
    assert really_ask() == ""                            # couldn't be asked: nothing claimed


@pytest.mark.skipif(sys.platform != "win32", reason="Windows notifications")
def test_windows_set_to_show_no_notifications_is_said_not_swallowed(monkeypatch):
    # Windows takes a notification without complaint even when it will never show it.
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "SETTING DisabledForUser\n", ""))
    assert really_ask() == "DisabledForUser"
    with pytest.raises(NotificationsOff, match="switched off on this computer") as off:
        really_show("A paper needs you", [])
    assert off.value.setting == "DisabledForUser" and "Windows Settings > System" in str(off.value)
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "SETTING DisabledForApplication\n", ""))
    with pytest.raises(NotificationsOff, match="from PaperBot are switched off"):
        really_show("A paper needs you", [])


def test_the_app_says_so_when_windows_shows_no_notifications(window):
    app, home, settings = window, window.tabs["home"], window.tabs["settings"]
    app.pump(lambda: app.notify_setting is not None)     # it asked Windows when it started
    app.show("settings")
    app.refresh_all()
    assert settings.notify_status.cget("text") == "✓ Windows will show them"
    assert home.notify_text.winfo_manager() == ""        # nothing to warn about on Home
    app.notify_setting = "DisabledForUser"               # as on a PC with notifications off
    app.refresh_all()
    assert settings.notify_status.cget("text").startswith(
        "✗ Windows notifications are switched off on this computer")
    app.show("home")
    app.refresh_all()
    assert "PaperBot can't show you a notification" in home.notify_text.cget("text")
    assert home.notify_text.winfo_manager() == "pack"
    app.cfg.notify_desktop = False                       # not wanted anyway: no warning
    app.refresh_all()
    assert home.notify_text.winfo_manager() == ""


def test_links_in_notifications_name_a_page_of_the_app():
    assert page_from("paperbot:needs") == page_from("paperbot://needs/") == "needs"
    assert page_from("PaperBot:People") == "people" and page_from("settings") == "settings"
    assert page_from("paperbot:delete-everything") == page_from("") == page_from(None) == ""
    assert page_from("https://example.com/needs") == ""


def test_a_link_starts_this_app_whichever_folder_it_is_opened_from():
    from paperbot import notify
    entries = notify.registration()
    (command,) = [values[""] for key, values in entries.items()
                  if key.endswith(r"\shell\open\command")]
    # -I: Python doesn't look for "paperbot" in the folder a link happens to be opened from
    # (a web page or a document can open one). The link itself stays one quoted argument.
    assert command.startswith('"') and command.endswith('" -I -m paperbot.gui --show "%1"')
    assert entries[rf"Software\Classes\{notify.SCHEME}"]["URL Protocol"] == ""
    assert entries[rf"Software\Classes\AppUserModelId\{notify.APP_ID}"]["DisplayName"] == "PaperBot"


# ---- opening with Windows ---------------------------------------------------------------------
def test_the_app_can_be_set_to_open_after_sign_in(monkeypatch):
    scripts = []

    def fake_ps(script, timeout=60):
        scripts.append(script)
        return subprocess.CompletedProcess([], 0, "OK\n", "")

    monkeypatch.setattr(winsvc, "_ps", fake_ps)
    winsvc.install_app_start()
    script = scripts.pop()
    assert "-Argument '-m paperbot.gui --minimized'" in script     # in the taskbar, not in the way
    assert "-AtLogOn -User $user" in script and "$signin.Delay = 'PT1M'" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script      # never closed after 3 days
    assert "-TaskName 'PaperBot app'" in script and "-m paperbot run" not in script
    winsvc.uninstall_app_start()
    assert "Unregister-ScheduledTask -TaskName 'PaperBot app'" in scripts.pop()
    winsvc.uninstall()                                             # automatic mode: untouched
    assert "Unregister-ScheduledTask -TaskName 'PaperBot'" in scripts.pop()
    winsvc.app_start_status()
    assert "Get-ScheduledTask -TaskName 'PaperBot app'" in scripts.pop()
    winsvc.status()
    assert "Get-ScheduledTask -TaskName 'PaperBot' " in scripts.pop()


def test_the_switch_on_home_turns_opening_after_sign_in_on(window, monkeypatch):
    app, home, did = window, window.tabs["home"], []
    monkeypatch.setattr(winsvc, "install_app_start", lambda: did.append("on"))
    monkeypatch.setattr(winsvc, "uninstall_app_start", lambda: did.append("off"))
    app.pump(lambda: app.app_start is not None)          # it asked Windows when it started
    app.refresh_all()
    assert home.start_var.get() is False
    home.start_var.set(True)                             # the owner flips the switch
    monkeypatch.setattr(winsvc, "status", lambda task=winsvc.TASK: winsvc.TaskStatus(
        task == winsvc.APP_TASK, "Ready"))
    home.toggle_app_start()
    app.pump(lambda: did and app.app_start is not None)
    assert did == ["on"] and home.start_var.get() is True
    assert "a minute after you sign in" in app.status.cget("text")
    assert app.task is not None and not app.task.on      # automatic mode is a separate thing


# ---- one window ---------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="uses Windows named objects")
def test_a_second_start_finds_the_first_and_asks_it_forward(tmp_path):
    from paperbot.gui.instance import Instance
    name = f"test-{secrets.token_hex(6)}"
    first, second = Instance(tmp_path, name), Instance(tmp_path, name)
    try:
        assert first.first and not second.first
        assert first.asked() is None                     # nobody asked yet
        second.ask_to_show("needs")                      # a click on a notification
        assert first.asked() == "needs" and first.asked() is None
        second.ask_to_show()                             # the shortcut, clicked again
        assert first.asked() == ""
        assert Instance(tmp_path, f"other-{name}").first             # another install: its own
    finally:
        first.close()
        second.close()
    third = Instance(tmp_path, name)                     # the app was closed and started again
    assert third.first
    third.close()


def test_starting_again_never_opens_a_second_window(monkeypatch):
    from paperbot.gui import window as gui_main
    asked = []

    class AlreadyOpen:
        first = False

        def __init__(self, home):
            pass

        def ask_to_show(self, page=""):
            asked.append(page)

    monkeypatch.setattr(gui_main, "Instance", AlreadyOpen)
    monkeypatch.setattr(gui_main, "make_dpi_aware", lambda: None)
    monkeypatch.setattr(gui_main.tk, "Tk", lambda: pytest.fail("opened a second window"))
    gui_main.main(["--show", "paperbot:needs"])          # a click on a notification
    gui_main.main([])                                    # the shortcut
    gui_main.main(["--minimized"])                       # the sign-in task: leaves it be
    gui_main.main(["--show", "paperbot:needs", "--odd", "extras"])   # junk after a link: ignored
    assert asked == ["needs", "", "needs"]


def test_coming_forward_shows_the_page_that_was_asked_for(window, monkeypatch):
    app, did = window, []
    for name in ("deiconify", "lift", "focus_force"):
        monkeypatch.setattr(app.root, name, lambda name=name: did.append(name))
    monkeypatch.setattr(app.root, "attributes", lambda *a: did.append("topmost"))
    app.come_forward("needs")
    assert did[:2] == ["deiconify", "lift"]
    assert app.nb.select() == str(app.tabs["needs"].frame)
    app.come_forward("")                                 # no page named: stay where it was
    assert app.nb.select() == str(app.tabs["needs"].frame)


def test_the_open_app_announces_within_moments_and_only_once(window, toasts):
    app = window
    app.refresh_all()
    assert toasts == []
    waiting(app.db)                      # the background check just put a paper on the list
    app.refresh_all()                    # (the app looks every 4 seconds)
    app.pump(lambda: toasts)
    assert [t[0] for t in toasts] == ["A paper needs you"]
    app.refresh_all()
    app.pump(lambda: len(toasts) > 1, seconds=0.5)
    assert len(toasts) == 1
