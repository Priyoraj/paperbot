"""Shared by all tests: nothing reaches the real desktop, and the app's window for the tests
that need one."""
import time

import pytest


@pytest.fixture(autouse=True)
def toasts(monkeypatch):
    """For every test: no real Windows notification pops up, Windows isn't asked anything and
    nothing is written to the registry. What would have been shown is collected here:
    [(headline, lines, page)]."""
    from paperbot import notify
    shown = []
    monkeypatch.setattr(notify, "show_toast",
                        lambda title, lines, page="needs": shown.append((title, lines, page)))
    monkeypatch.setattr(notify, "register", lambda: None)
    monkeypatch.setattr(notify, "windows_setting", lambda: "Enabled")
    return shown


@pytest.fixture
def window(tmp_path, monkeypatch):
    """The app (paperbot.gui.window.App), hidden, with nothing that reaches outside: no
    scheduled tasks, no VPN, no mailbox. Dialogs it would show are collected in app.said."""
    tk = pytest.importorskip("tkinter")
    monkeypatch.setenv("PAPERBOT_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[owner]\nname = "Priya"\n[mail]\nusername = '
                                          '"p@example.com"\n', encoding="utf-8")
    from paperbot import winsvc
    from paperbot.gui import settings as gui_settings
    from paperbot.gui import window as gui_main
    from paperbot.sync import NoSync
    monkeypatch.setattr(winsvc, "status", lambda task=winsvc.TASK: winsvc.TaskStatus(False))
    monkeypatch.setattr(gui_main, "vpn_signin_saved", lambda cfg: True)
    monkeypatch.setattr(gui_main, "make_sync", lambda cfg, db: NoSync())
    monkeypatch.setattr(gui_settings, "vpn_profiles", lambda: ["Institute VPN"])
    said = []
    monkeypatch.setattr(gui_main.messagebox, "showinfo",
                        lambda title, text, **kw: said.append(text))
    root = app = problem = None
    for _ in range(4):          # on Windows a new Tk now and then fails to start, or to build
        try:                    # its first widgets, when many came and went before: try again
            root = tk.Tk()
            root.withdraw()
            app = gui_main.App(root)
            break
        except tk.TclError as exc:
            problem = exc
            if root is not None:
                try:
                    root.destroy()
                except tk.TclError:
                    pass
            root = app = None
            time.sleep(0.5)
    if app is None:
        pytest.skip(f"no display ({problem})")

    def pump(until, seconds: float = 5.0) -> None:
        """Let the window work (timers, results from background threads) until `until()`."""
        deadline = time.monotonic() + seconds
        while not until() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.05)

    app.said, app.pump = said, pump
    try:
        yield app
    finally:
        root.destroy()
