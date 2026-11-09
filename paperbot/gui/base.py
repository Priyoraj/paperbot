"""Shared pieces of the desktop app: theme, background work, friendly errors, widgets."""
from __future__ import annotations

import ctypes
import imaplib
import queue
import smtplib
import socket
import sys
import threading
import tkinter as tk
import traceback
from tkinter import font as tkfont
from tkinter import ttk

from ..app import Busy
from ..config import ConfigError

PALETTES = {
    "light": {"ok": "#0f7b0f", "bad": "#c42b1c", "warn": "#9d5d00", "muted": "#5f5f5f",
              "accent": "#005fb8"},
    "dark": {"ok": "#6ccb5f", "bad": "#ff99a4", "warn": "#fce100", "muted": "#a0a0a0",
             "accent": "#60cdff"},
}


def make_dpi_aware() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def windows_prefers_dark() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def apply_theme(root: tk.Tk) -> dict:
    mode = "dark" if windows_prefers_dark() else "light"
    try:
        import sv_ttk
        sv_ttk.set_theme(mode)
    except Exception:  # noqa: BLE001 - fall back to the stock theme
        ttk.Style(root).theme_use("vista" if sys.platform == "win32" else "clam")
    style = ttk.Style(root)
    line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    style.configure("Treeview", rowheight=line + 12)
    pal = dict(PALETTES[mode])
    pal["mode"] = mode
    return pal


def font(name: str, fallback_size: int, bold: bool = False):
    """Sun Valley named fonts when the theme provides them, Segoe UI otherwise."""
    try:
        tkfont.nametofont(name)
        return name
    except tk.TclError:
        return ("Segoe UI", fallback_size, "bold" if bold else "normal")


BODY = ("SunValleyBodyFont", 10, False)
TITLE = ("SunValleyTitleFont", 20, True)
SUBTITLE = ("SunValleySubtitleFont", 14, True)
STRONG = ("SunValleyBodyStrongFont", 10, True)
CAPTION = ("SunValleyCaptionFont", 9, False)
DISPLAY = ("SunValleyTitleLargeFont", 24, True)


def f(spec):
    return font(*spec)


def friendly(exc: BaseException) -> str:
    """Turn exceptions into sentences for people who don't read tracebacks."""
    from ..vpn import VpnError
    if isinstance(exc, ConfigError):
        return f"Please finish setup first: {exc}"
    if isinstance(exc, Busy):
        return str(exc)
    if isinstance(exc, VpnError):
        return f"VPN problem: {exc}"
    if isinstance(exc, (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError)):
        return ("The mail server rejected the login. Gmail needs an *app password* (not your "
                "normal password): Settings > Mail password > Set.")
    if isinstance(exc, (socket.gaierror, ConnectionError, TimeoutError, socket.timeout)):
        return f"Couldn't reach the server - are you online? ({exc})"
    return f"{exc.__class__.__name__}: {exc}"


class Bg:
    """Run slow work on a thread; deliver results back on the Tk thread."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        root.after(100, self._poll)

    def run(self, fn, done=None, fail=None) -> None:
        def work():
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001 - report everything to the UI
                traceback.print_exc()
                self.q.put((fail, exc))
            else:
                self.q.put((done, result))
        threading.Thread(target=work, daemon=True).start()

    def call(self, fn, *args) -> None:
        """Schedule fn(*args) on the Tk thread (safe to call from workers)."""
        self.q.put((lambda a: fn(*a), args))

    def _poll(self) -> None:
        try:
            while True:
                cb, arg = self.q.get_nowait()
                if cb:
                    try:
                        cb(arg)
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


class ScrollFrame(ttk.Frame):
    """A vertically scrolling container; put widgets in .body."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self.body.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        style = ttk.Style(self)
        bg = style.lookup("TFrame", "background") or style.lookup(".", "background")
        if bg:
            self.canvas.configure(background=bg)

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


def make_tree(parent, columns: list[tuple[str, str, int, bool]], height: int = 10):
    """columns: (id, heading, width, stretch). Returns (frame, tree)."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                        height=height, selectmode="extended")
    for cid, heading, width, stretch in columns:
        tree.heading(cid, text=heading, anchor="w")
        tree.column(cid, width=width, stretch=stretch, anchor="w")
    bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    return frame, tree


def fill_tree(tree: ttk.Treeview, rows: list[tuple[str, tuple]]) -> None:
    """Replace rows (iid, values), keeping the selection when the same iids come back."""
    selected = set(tree.selection())
    tree.delete(*tree.get_children())
    for iid, values in rows:
        tree.insert("", "end", iid=iid, values=values)
    keep = [i for i in selected if tree.exists(i)]
    if keep:
        tree.selection_set(keep)


def clip(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"
