"""PaperBot desktop app: the main window."""
from __future__ import annotations

import argparse
import logging
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .. import config as config_mod
from .. import notify, winsvc
from ..app import (SyncError, Together, build_bot, make_sync, setup_logging, single_instance,
                   together, vpn_signin_saved)
from ..db import DB
from .base import Bg, STRONG, TITLE, apply_theme, f, fit_window, friendly, make_dpi_aware
from .instance import Instance

log = logging.getLogger("paperbot.gui")
ASSETS = Path(__file__).resolve().parent.parent / "assets"
SHARED_EVERY_MS = 5 * 60 * 1000    # how often to pick up what another computer did
LISTEN_EVERY_MS = 400              # how often to check whether a second start wants us forward


def set_icon(root: tk.Tk) -> None:
    """The window's own icon. PaperBot works without it, so trouble with the picture is only
    noted - it must never keep the app from opening."""
    icon = ASSETS / "paperbot.png"
    if not icon.exists():
        return
    try:
        root.paperbot_icon = tk.PhotoImage(master=root, file=str(icon))   # kept: Tk needs it
        root.iconphoto(True, root.paperbot_icon)
    except tk.TclError as exc:
        log.warning("couldn't set the window's icon: %s", exc)


class App:
    def __init__(self, root: tk.Tk, instance: Instance | None = None):
        self.root = root
        self.instance = instance
        self.pal = apply_theme(root)
        self.scale = root.winfo_fpixels("1i") / 96
        self.bg = Bg(root)
        config_mod.init_config()  # first start: config.toml from the template
        self.cfg = config_mod.load()
        setup_logging(self.cfg, console=False)
        self.db = DB(self.cfg.data_dir / "paperbot.db")
        self.task: winsvc.TaskStatus | None = None
        self.app_start: winsvc.TaskStatus | None = None   # "open the app after sign-in"
        self.notify_setting: str | None = None            # what Windows says: 'Enabled', ...
        self.vpn_saved: bool | None = None
        self._busy = 0
        # Working together with PaperBot on another computer (sync.py)
        self.together: Together | None = None
        self.sync_problem = ""            # e.g. "PaperBot on LAPTOP is newer than ..."
        self.started_here = False         # the owner pressed Start in this window
        self._picking_up = False
        root.title("PaperBot")
        fit_window(root, self.px(1120), self.px(740), self.px(960), self.px(640))
        set_icon(root)
        self._build()
        self.refresh_all()
        self.refresh_task_status()
        self.refresh_app_start()
        self.refresh_notify_setting()
        self.refresh_vpn_status()
        root.after(4000, self._tick)
        root.after(30000, self._slow_tick)
        root.after(1500, self._shared_tick)
        if instance is not None:
            root.after(LISTEN_EVERY_MS, self._listen)

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

    def come_forward(self, page: str = "") -> None:
        """The app was started a second time, or a notification was clicked: show this
        window (on `page`, if one was named) instead of opening another."""
        root = self.root
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)        # in front of whatever is there now...
        root.after(400, lambda: root.attributes("-topmost", False))   # ...without staying so
        try:
            root.focus_force()
        except tk.TclError:
            pass
        if page in self.tabs:
            self.show(page)
        self.refresh_all()

    def _listen(self) -> None:
        page = self.instance.asked()
        if page is not None:
            self.come_forward(page)
        self.root.after(LISTEN_EVERY_MS, self._listen)

    # ---- status & background work ----------------------------------------------
    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def say(self, text: str) -> None:
        """Thread-safe status update for workers."""
        self.bg.call(self.set_status, text)

    def run_task(self, label: str, fn, done=None, lock: bool = False, failed=None) -> None:
        """Run fn() in the background with a busy indicator; errors become dialogs (and
        failed(exc) is called, if given). lock=True waits for (and blocks) the automatic
        background check."""
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
            if failed:
                failed(exc)
            self.set_status("Something went wrong - see the message.")
            log.error("%s failed: %s", label, exc)
            messagebox.showerror("PaperBot", friendly(exc), parent=self.root)

        self.bg.run(work, ok, bad)

    def bot(self):
        return build_bot(self.cfg)

    def reload_config(self) -> None:
        self.cfg = config_mod.load()
        self.refresh_all()

    # ---- working together with PaperBot on another computer -------------------------
    def _note_together(self) -> None:
        """Where this computer stands; say so when it has just joined in by itself."""
        was, here = self.together, together(self.cfg, self.db)
        self.together = here
        if was is None or was.joined or not here.joined:
            return
        if self.started_here:             # the Start button reports on its own
            self.started_here = False
            return
        who = " and ".join(here.others) or "your other computer"
        text = (f"PaperBot on {who} shares its records through your mailbox, and this "
                "computer has joined in.\n\nFrom now on they answer your mailbox as one: every "
                "request is answered once, and people, requests and daily limits are the same "
                "on both.\n\nWhat this computer had recorded before was put together with the "
                "rest, and a copy from before is kept in "
                f"{self.cfg.data_dir / 'paperbot.before-sharing.db'}")
        self.set_status(f"This computer now works together with PaperBot on {who}.")
        self.root.after(300, lambda: messagebox.showinfo("PaperBot", text, parent=self.root))

    def refresh_shared(self) -> None:
        """Pick up what PaperBot on another computer did - and join in, the first time
        shared records turn up in the mailbox. Quietly: being offline is no news."""
        if self._picking_up or self._busy:
            return
        cfg = self.cfg
        self._picking_up = True

        def work():
            db = DB(cfg.data_dir / "paperbot.db")
            try:
                make_sync(cfg, db).refresh()
            finally:
                db.close()

        def ok(_):
            self._picking_up, self.sync_problem = False, ""
            self.refresh_all()

        def bad(exc):
            self._picking_up = False
            if isinstance(exc, SyncError):          # needs the owner: shown on Home
                if str(exc) != self.sync_problem:
                    log.error("working together: %s", exc)
                self.sync_problem = str(exc)
            else:                                    # offline, busy...: next time
                log.debug("couldn't look at the shared records: %s", exc)
            self.refresh_all()

        self.bg.run(work, ok, bad)

    def _shared_tick(self) -> None:
        self.refresh_shared()
        self.root.after(SHARED_EVERY_MS, self._shared_tick)

    # ---- refreshing ---------------------------------------------------------------
    def refresh_all(self) -> None:
        try:
            self._note_together()
        except Exception:  # noqa: BLE001 - e.g. database busy for a moment
            log.exception("couldn't read where working together stands")
        current = self.nb.select()
        for tab in self.tabs.values():
            if tab.always_refresh or str(tab.frame) == current:
                try:
                    tab.refresh()
                except Exception:  # noqa: BLE001 - e.g. database busy for a moment
                    log.exception("refresh of %s failed", tab.key)
        waiting = self.db.owner_todo()
        self._announce(waiting)
        todo = len(waiting)
        pending = sum(1 for p in self.db.people() if p["status"] == "pending")
        self.nb.tab(self.tabs["needs"].frame, text=f"Needs you ({todo})" if todo else "Needs you")
        self.nb.tab(self.tabs["people"].frame,
                    text=f"People ({pending} waiting)" if pending else "People")
        on = self.task is not None and self.task.on
        self.mode_label.configure(
            text="●  Automatic mode ON" if on else "○  Automatic mode OFF",
            foreground=self.pal["ok"] if on else self.pal["muted"])

    def _announce(self, waiting) -> None:
        """A Windows notification for papers that have newly come to need the owner. The
        background check does the same when it ends; whichever is first speaks, once."""
        try:
            fresh = notify.claim_new(self.cfg, self.db, waiting)
        except Exception:  # noqa: BLE001 - e.g. database busy for a moment
            log.exception("couldn't work out what to announce")
            return
        if fresh:
            title, lines = notify.wording(fresh)
            self.bg.run(lambda: notify.show_toast(title, lines), fail=lambda exc: log.warning(
                "couldn't show the Windows notification: %s", exc))

    def refresh_task_status(self) -> None:
        def got(st):
            self.task = st
            self.refresh_all()
        self.bg.run(winsvc.status, got)

    def refresh_app_start(self) -> None:
        def got(st):
            self.app_start = st
            self.refresh_all()
        self.bg.run(winsvc.app_start_status, got)

    @property
    def notifications_blocked(self) -> str:
        """Why PaperBot's notifications can't appear although the owner wants them - '' if
        they can, or if that isn't known (yet)."""
        if not self.cfg.notify_desktop or self.notify_setting in (None, "", "Enabled"):
            return ""
        return str(notify.NotificationsOff(self.notify_setting))

    def refresh_notify_setting(self) -> None:
        """Ask Windows whether it will show PaperBot's notifications at all."""
        if not self.cfg.notify_desktop:
            return

        def got(setting):
            self.notify_setting = setting
            self.refresh_all()
        self.bg.run(notify.windows_setting, got)

    def open_windows_notification_settings(self) -> None:
        import os
        try:
            os.startfile(notify.WINDOWS_SETTINGS)
        except (AttributeError, OSError) as exc:
            log.warning("couldn't open Windows' notification settings: %s", exc)

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
        if self.notifications_blocked:    # until the owner has switched them on in Windows
            self.refresh_notify_setting()
        self.root.after(30000, self._slow_tick)


def main(argv: list[str] | None = None) -> None:
    """`--minimized`: start in the taskbar (the sign-in task). `--show needs` or
    `--show paperbot:needs`: open on that page (a click on a notification)."""
    ap = argparse.ArgumentParser(prog="paperbot-app", add_help=False)
    ap.add_argument("--minimized", action="store_true")
    ap.add_argument("--show", default="")
    args, _ = ap.parse_known_args(argv)        # whatever else arrives with a link is ignored
    page = notify.page_from(args.show)
    make_dpi_aware()
    if sys.platform == "win32":  # own taskbar entry + icon instead of "pythonw"
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(notify.APP_ID)
        except (AttributeError, OSError):
            pass
    instance = Instance(config_mod.home_dir())
    if not instance.first:                     # PaperBot is open already: no second window
        if not args.minimized:                 # a person or a notification asked for it
            instance.ask_to_show(page)
        return
    root = tk.Tk()
    try:
        app = App(root, instance)
    except Exception as exc:  # noqa: BLE001 - show startup problems instead of vanishing
        messagebox.showerror("PaperBot couldn't start", friendly(exc))
        raise
    if page:
        app.show(page)
    if args.minimized:
        root.iconify()
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
