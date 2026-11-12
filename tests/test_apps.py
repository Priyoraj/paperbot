"""The desktop app and the colleague request page."""
import json
import re
import subprocess
import zipfile

import pytest

from paperbot import sharepage
from paperbot.config import Config, ConfigError
from paperbot.packaging import make_zip


def test_request_page_has_owner_details_safely_embedded():
    cfg = Config(owner_name='Priya </script><script>alert(1)', mail_username="p@example.com",
                 subject_keywords=["paper request"])
    html = sharepage.build(cfg)
    blob = re.search(r"/\*CONFIG\*/(.*?)/\*END\*/", html).group(1)
    assert "</script>" not in blob                      # can't break out of the script tag
    assert json.loads(blob.replace("<\\/", "</")) == {
        "to": "p@example.com", "name": "Priya </script><script>alert(1)",
        "subject": "Paper request"}
    assert sharepage.file_name(cfg).startswith("Request papers from Priya")
    assert "p@example.com" in sharepage.invitation(cfg)


def test_request_page_needs_a_mailbox():
    with pytest.raises(ConfigError):
        sharepage.build(Config())


def test_installer_zip_never_contains_private_files(tmp_path):
    root = tmp_path / "proj"
    (root / "paperbot" / "assets").mkdir(parents=True)
    (root / "paperbot" / "__init__.py").write_text("")
    (root / "paperbot" / "assets" / "paperbot.ico").write_bytes(b"ico")
    (root / "paperbot" / "__pycache__").mkdir()
    (root / "paperbot" / "__pycache__" / "x.pyc").write_bytes(b"")
    (root / "data" / "pdfs").mkdir(parents=True)
    (root / "data" / "paperbot.db").write_text("secret")
    (root / "config.toml").write_text("username = 'me'")
    (root / "pyproject.toml").write_text("")
    (root / "LICENSE").write_text("MIT License")
    (root / "Install PaperBot.cmd").write_text("")
    names = zipfile.ZipFile(make_zip(tmp_path / "out.zip", root)).namelist()
    assert "PaperBot/Install PaperBot.cmd" in names
    assert "PaperBot/LICENSE" in names
    assert "PaperBot/paperbot/assets/paperbot.ico" in names
    assert not [n for n in names if "config.toml" in n or "/data/" in n or "__pycache__" in n]


def test_automatic_mode_also_runs_a_minute_after_windows_sign_in(monkeypatch):
    from paperbot import winsvc
    scripts = []

    def fake_ps(script, timeout=60):
        scripts.append(script)
        return subprocess.CompletedProcess([], 0, "OK\n", "")

    monkeypatch.setattr(winsvc, "_ps", fake_ps)
    winsvc.install(10)
    (script,) = scripts
    assert "-RepetitionInterval (New-TimeSpan -Minutes 10)" in script
    assert "-AtLogOn -User $user" in script and "$signin.Delay = 'PT1M'" in script
    assert "-Trigger @($every, $signin)" in script


def test_installer_zip_gives_batch_files_windows_line_endings(tmp_path):
    root = tmp_path / "proj"
    (root / "paperbot").mkdir(parents=True)
    (root / "paperbot" / "__init__.py").write_text("")
    (root / "Install PaperBot.cmd").write_bytes(b"@echo off\ngoto :done\n:done\r\n")
    zf = zipfile.ZipFile(make_zip(tmp_path / "out.zip", root))
    assert zf.read("PaperBot/Install PaperBot.cmd") == b"@echo off\r\ngoto :done\r\n:done\r\n"


def test_app_window_opens_and_every_screen_refreshes(window):      # fixture: conftest.py
    app = window
    for key in app.tabs:
        app.show(key)
        app.refresh_all()
        app.root.update()
    assert app.cfg.owner_name == "Priya"
    assert "Automatic mode" in app.mode_label.cget("text")


def test_the_app_opens_even_when_its_icon_cannot_be_shown(window, monkeypatch, caplog):
    from paperbot.gui import window as gui_main

    def no_picture(**kw):
        raise gui_main.tk.TclError("couldn't read the picture")

    monkeypatch.setattr(gui_main.tk, "PhotoImage", no_picture)
    gui_main.set_icon(window.root)                       # nothing raised: the app goes on
    assert "couldn't set the window's icon" in caplog.text


def test_the_window_never_wants_more_room_than_the_screen_has():
    from paperbot.gui.base import fitted
    # A 1920x1080 screen at 150% scaling, taskbar taken off: the app's usual size (1680x1110
    # at that scaling) is taller than the screen, and its lower edge sat behind the taskbar.
    w, h, x, y, min_w, min_h = fitted(1680, 1110, 1440, 960, (0, 0, 1920, 1008), title_bar=72)
    assert (w, h) == (1680, 928) and y + 72 + h <= 1008          # title bar + window: in view
    assert x == 120 and (min_w, min_h) == (1440, 928)            # centred; can't be forced taller
    # Room to spare: exactly as asked for, in the middle (the taskbar may be on the left).
    assert fitted(800, 500, 600, 400, (60, 0, 1860, 1008), title_bar=48) == (
        800, 500, 60 + 530, 230, 600, 400)
    # A tiny screen: still a usable window, never a negative size.
    w, h, x, y, _, _ = fitted(1680, 1110, 1440, 960, (0, 0, 300, 200), title_bar=48)
    assert (w, h) == (320, 240) and x == 0 and y == 0
