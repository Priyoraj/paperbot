"""PaperBot desktop app: the main window."""
from __future__ import annotations

import logging
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .. import config as config_mod
from .. import winsvc
from ..app import build_bot, setup_logging, single_instance, vpn_signin_saved
from ..db import DB
from .base import Bg, STRONG, TITLE, apply_theme, f, friendly, make_dpi_aware

log = logging.getLogger("paperbot.gui")
ASSETS = Path(__file__).resolve().parent.parent / "assets"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.pal = apply_theme(root)
        self.scale = root.winfo_fpixels("1i") / 96
        self.bg = Bg(root)
        config_mod.init_config()  # first start: config.toml from the template
        self.cfg = config_mod.load()
        setup_logging(self.cfg, console=False)
        self.db = DB(self.cfg.data_dir / "paperbot.db")
        self.task: winsvc.TaskStatus | None = None
        self.vpn_saved: bool | None = None
        self._busy = 0
        root.title("PaperBot")
        root.geometry(f"{self.px(1120)}x{self.px(740)}")
        root.minsize(self.px(960), self.px(640))
        icon = ASSETS / "paperbot.png"
        if icon.exists():
            self._icon = tk.PhotoImage(file=str(icon))
            root.iconphoto(True, self._icon)
        self._build()
        self.refresh_all()
        self.refresh_task_status()
        self.refresh_vpn_status()
        root.after(4000, self._tick)
        root.after(30000, self._slow_tick)

    def px(self, n: float) -> int:
        return int(n * self.scale)

    # ---- layout -----------------------------------------------------------------
    def _build(self) -> None:
        from .home import HomeTab
        from .people import PeopleTab, RequestsTab
        from .settings import SettingsTab
        from .share import ShareTab
        from .work import NeedsYouTab, TryTab

        outer = ttk.Frame(self.root, padding=(self.px(22), self.px(14), self.px(22), self.px(8)))
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="PaperBot", font=f(TITLE)).pack(side="left")
        ttk.Label(header, text="answers your colleagues' paper requests",
                  foreground=self.pal["muted"]).pack(side="left", padx=(self.px(12), 0),
                                                     pady=(self.px(8), 0))
        self.mode_label = ttk.Label(header, text="", font=f(STRONG))
        self.mode_label.pack(side="right", pady=(self.px(8), 0))

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(self.px(10), self.px(6)))
        self.tabs = {}
        for cls in (HomeTab, NeedsYouTab, PeopleTab, RequestsTab, TryTab, SettingsTab, ShareTab):
            tab = cls(self, self.nb)
            self.tabs[cls.key] = tab
            self.nb.add(tab.frame, text=tab.title)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh_all())

        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text="Ready.", foreground=self.pal["muted"])
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=self.px(180))

    def show(self, key: str) -> None:
        self.nb.select(self.tabs[key].frame)

    # ---- status & background work ----------------------------------------------
    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def say(self, text: str) -> None:
        """Thread-safe status update for workers."""
        self.bg.call(self.set_status, text)

    def run_task(self, label: str, fn, done=None, lock: bool = False) -> None:
        """Run fn() in the background with a busy indicator; errors become dialogs.
        lock=True waits for (and blocks) the automatic background check."""
        self._busy += 1
        self.set_status(label)
        self.progress.pack(side="right")
        self.progress.start(12)

        def work():
            if lock:
                with single_instance(self.cfg, wait_s=180):
                    return fn()
            return fn()

        def finish():
            self._busy -= 1
            if not self._busy:
                self.progress.stop()
                self.progress.pack_forget()
            self.refresh_all()

        def ok(result):
            finish()
            if done:
                done(result)
            elif self._busy == 0:
                self.set_status("Done.")

        def bad(exc):
            finish()
            self.set_status("Something went wrong - see the message.")
            log.error("%s failed: %s", label, exc)
            messagebox.showerror("PaperBot", friendly(exc), parent=self.root)

        self.bg.run(work, ok, bad)

    def bot(self):
        return build_bot(self.cfg)

    def reload_config(self) -> None:
        self.cfg = config_mod.load()
        self.refresh_all()

    # ---- refreshing ---------------------------------------------------------------
    def refresh_all(self) -> None:
        current = self.nb.select()
        for tab in self.tabs.values():
            if tab.always_refresh or str(tab.frame) == current:
                try:
                    tab.refresh()
                except Exception:  # noqa: BLE001 - e.g. database busy for a moment
                    log.exception("refresh of %s failed", tab.key)
        todo = len(self.db.owner_todo())
        pending = sum(1 for p in self.db.people() if p["status"] == "pending")
        self.nb.tab(self.tabs["needs"].frame, text=f"Needs you ({todo})" if todo else "Needs you")
        self.nb.tab(self.tabs["people"].frame,
                    text=f"People ({pending} waiting)" if pending else "People")
        on = self.task is not None and self.task.on
        self.mode_label.configure(
            text="●  Automatic mode ON" if on else "○  Automatic mode OFF",
            foreground=self.pal["ok"] if on else self.pal["muted"])

    def refresh_task_status(self) -> None:
        def got(st):
            self.task = st
            self.refresh_all()
        self.bg.run(winsvc.status, got)

    def refresh_vpn_status(self) -> None:
        def got(saved):
            self.vpn_saved = saved
            self.refresh_all()
        self.bg.run(lambda: vpn_signin_saved(self.cfg), got)

    def _tick(self) -> None:
        if not self._busy:
            self.refresh_all()
        self.root.after(4000, self._tick)

    def _slow_tick(self) -> None:
        self.refresh_task_status()
        self.root.after(30000, self._slow_tick)


def main() -> None:
    make_dpi_aware()
    if sys.platform == "win32":  # own taskbar entry + icon instead of "pythonw"
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PaperBot.Desktop")
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    try:
        App(root)
    except Exception as exc:  # noqa: BLE001 - show startup problems instead of vanishing
        messagebox.showerror("PaperBot couldn't start", friendly(exc))
        raise
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
