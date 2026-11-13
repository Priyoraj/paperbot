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

from ..app import Busy, LockLost, SyncError
from ..config import ConfigError
from ..notify import NotificationsOff
from .look import text_scale


def make_dpi_aware() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def work_area(root: tk.Tk) -> tuple[int, int, int, int]:
    """(left, top, width, height) of the screen without the taskbar, in pixels."""
    if sys.platform == "win32":
        try:
            from ctypes import wintypes
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except (AttributeError, OSError):
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def fitted(width: int, height: int, min_width: int, min_height: int,
           area: tuple[int, int, int, int], title_bar: int) -> tuple[int, int, int, int, int, int]:
    """(width, height, x, y, min_width, min_height) for a window that would like width x
    height on a screen whose usable part is `area` (left, top, width, height): as wanted, but
    never larger than there is room for, and centred. On a small or heavily scaled display
    the window's lower edge would otherwise end up behind the taskbar."""
    left, top, room_w, room_h = area
    frame = title_bar // 3                 # the edges Windows adds left and right, together
    width = max(320, min(width, room_w - frame))
    height = max(240, min(height, room_h - title_bar - 8))
    return (width, height, left + max(0, (room_w - width - frame) // 2),
            top + max(0, (room_h - height - title_bar) // 2),
            min(min_width, width), min(min_height, height))


def fit_window(root: tk.Tk, width: int, height: int, min_width: int, min_height: int) -> None:
    title_bar = int(48 * root.winfo_fpixels("1i") / 96)      # the frame Windows adds on top
    w, h, x, y, min_w, min_h = fitted(width, height, min_width, min_height, work_area(root),
                                      title_bar)
    root.minsize(min_w, min_h)                               # first: it limits the size
    root.geometry(f"{w}x{h}+{x}+{y}")


def font(name: str, fallback_size: int, bold: bool = False):
    """The named fonts of look.py (the text-size setting changes those, and with them every
    widget at once); Segoe UI if one is somehow missing."""
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
    if isinstance(exc, (Busy, NotificationsOff)):
        return str(exc)
    if isinstance(exc, LockLost):
        return ("PaperBot stopped before it finished: this computer was asleep or offline for "
                "too long, and PaperBot on your other computer may have carried on. Nothing is "
                "done twice this way. Please try again.")
    if isinstance(exc, SyncError):
        return f"Working together with your other computer isn't possible right now: {exc}"
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


def page_height(wanted: int, room: int, fill: bool) -> tuple[int, bool]:
    """(how tall a ScrollFrame's content is made, whether its scrollbar shows) when the
    content asks for `wanted` pixels and `room` are in view. fill: content that fits is
    stretched over the whole room, so that a list in it can use what is spare."""
    return (max(wanted, room) if fill else wanted), wanted > room


class ScrollFrame(ttk.Frame):
    """A vertically scrolling container; put widgets in .body.

    fill=True makes a page that behaves like an ordinary one while everything fits (the
    content takes the whole room, no scrollbar) and scrolls once it doesn't - large text on a
    small screen. The scrollbar's place is kept free either way, so showing it never changes
    the width the content is laid out in."""

    def __init__(self, parent, fill: bool = False, **kw):
        super().__init__(parent, **kw)
        self.fill = fill
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.body.bind("<Configure>", lambda e: self._fit())
        self.canvas.bind("<Configure>", lambda e: self._fit())
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        style = ttk.Style(self)
        bg = style.lookup("TFrame", "background") or style.lookup(".", "background")
        if bg:
            self.canvas.configure(background=bg)

    def _fit(self) -> None:
        width, room = self.canvas.winfo_width(), self.canvas.winfo_height()
        height, scrolls = page_height(self.body.winfo_reqheight(), room, self.fill)
        self.columnconfigure(1, minsize=self.bar.winfo_reqwidth())
        self.canvas.itemconfigure(self._win, width=width, height=height)
        self.canvas.configure(scrollregion=(0, 0, width, height))
        if scrolls or not self.fill:
            self.bar.grid()
        else:
            self.bar.grid_remove()
            self.canvas.yview_moveto(0)

    def _wheel(self, event) -> None:
        widget = event.widget
        if not isinstance(widget, str) and widget.winfo_class() in ("Treeview", "Text",
                                                                     "Listbox"):
            return                         # a list or text area under the mouse scrolls itself
        first, last = self.canvas.yview()
        if first > 0 or last < 1:          # only when something is out of view
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


def scroll_page(parent, padding) -> tuple[ScrollFrame, ttk.Frame]:
    """A page for the notebook that scrolls when its content doesn't fit.
    Returns (the page, the frame to put its content in)."""
    page = ScrollFrame(parent, fill=True)
    inside = ttk.Frame(page.body, padding=padding)
    inside.pack(fill="both", expand=True)
    return page, inside


def wrap_to_width(label, least: int = 160, most: int | None = None) -> None:
    """Let a label wrap its text to the width it is given, instead of running out of the
    window. For a label that is given the room left in its row or grid cell: packed with
    fill="x" (and expand=True beside other things), or gridded sticky="ew" in a column that
    stretches. `most`: lines no longer than this, however wide the window is."""
    def fit(event) -> None:
        width = max(least, event.width if most is None else min(event.width, most))
        if abs(width - int(str(label.cget("wraplength")) or 0)) > 2:
            label.configure(wraplength=width)
    label.bind("<Configure>", fit)


def make_tree(parent, columns: list[tuple[str, str, int, bool]], height: int = 5):
    """columns: (id, heading, width, stretch); widths are for normal text on an unscaled
    display and grow with both. `height`: the fewest rows to show - lists are packed to take
    the room that is spare. Returns (frame, tree)."""
    frame = ttk.Frame(parent)
    grow = text_scale(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                        height=height, selectmode="extended")
    least = int(40 * grow)
    for cid, heading, width, stretch in columns:
        tree.heading(cid, text=heading, anchor="w")
        tree.column(cid, width=int(width * grow), minwidth=least, stretch=stretch, anchor="w")

    def share_out(event) -> None:
        """Give the columns that may stretch the width that is there, in proportion: more
        when there is room to spare, less when the text is large and the list is not as
        wide as its columns would like (it would otherwise cut the last one off)."""
        fixed = sum(int(w * grow) for _, _, w, stretch in columns if not stretch)
        flexible = sum(w for _, _, w, stretch in columns if stretch)
        room = event.width - fixed - 4
        if flexible and room > 0:
            for cid, _, width, stretch in columns:
                if stretch:
                    tree.column(cid, width=max(least, int(room * width / flexible)))

    tree.bind("<Configure>", share_out)
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
