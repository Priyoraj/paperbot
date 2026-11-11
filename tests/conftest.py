"""Shared by all tests: the desktop app's window, opened on an empty PaperBot folder."""
import time

import pytest


@pytest.fixture
def window(tmp_path, monkeypatch):
    """The app (paperbot.gui.window.App), hidden, with nothing that reaches outside: no
    scheduled task, no VPN, no mailbox. Dialogs it would show are collected in app.said."""
    tk = pytest.importorskip("tkinter")
    monkeypatch.setenv("PAPERBOT_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[owner]\nname = "Priya"\n[mail]\nusername = '
                                          '"p@example.com"\n', encoding="utf-8")
    from paperbot import winsvc
    from paperbot.gui import settings as gui_settings
    from paperbot.gui import window as gui_main
    from paperbot.sync import NoSync
    monkeypatch.setattr(gui_main.winsvc, "status", lambda: winsvc.TaskStatus(False))
    monkeypatch.setattr(gui_main, "vpn_signin_saved", lambda cfg: True)
    monkeypatch.setattr(gui_main, "make_sync", lambda cfg, db: NoSync())
    monkeypatch.setattr(gui_settings, "vpn_profiles", lambda: ["Institute VPN"])
    said = []
    monkeypatch.setattr(gui_main.messagebox, "showinfo",
                        lambda title, text, **kw: said.append(text))
    root, problem = None, None
    for _ in range(4):          # on Windows a new Tk now and then fails to start: try again
        try:
            root = tk.Tk()
            break
        except tk.TclError as exc:
            problem = exc
            time.sleep(0.5)
    if root is None:
        pytest.skip(f"no display ({problem})")
    root.withdraw()
    try:
        app = gui_main.App(root)
        app.said = said
        yield app
    finally:
        root.destroy()
