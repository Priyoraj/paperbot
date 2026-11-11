"""Needs you (papers to fetch by hand / share yourself) and Try a paper (test fetch)."""
from __future__ import annotations

import os
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..app import fetch_one, manual_session_factory
from ..sync import computer_name, tidy_name
from .base import BODY, STRONG, clip, f, fill_tree, make_tree
from .home import text_colors


def over_limit_ok(app, n: int) -> bool:
    """Your own fetches count toward the safe daily ceiling too: ask before going past it."""
    used, ceiling = app.db.downloads_today(), app.cfg.daily_ceiling
    if used + n <= ceiling:
        return True
    return messagebox.askyesno(
        "PaperBot - daily safety limit",
        f"You've made {used} institute downloads today; the safe limit is {ceiling}.\n\n"
        f"This would take it to {used + n}. Going past the limit makes it likelier that a "
        "publisher flags the institute's VPN address.\n\nDownload anyway?",
        icon="warning", default="no", parent=app.root)


class NeedsYouTab:
    key, title, always_refresh = "needs", "Needs you", False

    def __init__(self, app, nb):
        self.app = app
        px, pal = app.px, app.pal
        self.frame = ttk.Frame(nb, padding=px(18))
        ttk.Label(self.frame, wraplength=px(980), justify="left", foreground=pal["muted"],
                  text="These papers couldn't be fetched automatically - usually because the "
                       "publisher blocks automated downloads. The requester has been told you'll "
                       "send them. Pick one and let PaperBot open it in a browser window "
                       "(it switches the VPN on and off for you).").pack(anchor="w")
        box, self.tree = make_tree(self.frame, [("id", "#", 60, False),
                                                ("paper", "Paper", 420, True),
                                                ("for", "For", 230, True),
                                                ("why", "Why", 260, True)], height=11)
        box.pack(fill="both", expand=True, pady=(px(10), 0))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        row = ttk.Frame(self.frame)
        row.pack(fill="x", pady=(px(10), 0))
        self.b_browser = ttk.Button(row, text="Get it in a browser window",
                                    style="Accent.TButton", command=self.get_in_browser)
        self.b_browser.pack(side="left")
        self.b_attach = ttk.Button(row, text="Attach a PDF I downloaded…", command=self.attach)
        self.b_attach.pack(side="left", padx=px(8))
        self.b_giveup = ttk.Button(row, text="Give up on it", command=self.give_up)
        self.b_giveup.pack(side="left")
        self.b_show = ttk.Button(row, text="Show the file", command=self.show_file)
        self.b_shared = ttk.Button(row, text="I've shared it", command=self.mark_shared)
        self.b_shared.pack(side="right")
        self.b_show.pack(side="right", padx=px(8))

        self.guide = ttk.Label(self.frame, text="", wraplength=px(980), justify="left",
                               font=f(STRONG), foreground=pal["accent"])
        self.guide.pack(anchor="w", pady=(px(12), 0))
        self.waiting = ttk.Frame(self.frame)          # shown while a browser window is open
        self.b_skip = ttk.Button(self.waiting, text="Skip this paper", command=self._skip)
        self.b_skip.pack(side="left")
        self.b_stop = ttk.Button(self.waiting, text="Stop", command=self._stop)
        self.b_stop.pack(side="left", padx=px(8))
        ttk.Label(self.waiting, foreground=pal["muted"],
                  text="The browser is an ordinary window (not remote-controlled), so "
                       "\"verify you are human\" checks work normally there.").pack(
            side="left", padx=px(10))
        self._control = None
        self._rows: dict[str, object] = {}
        self._sig = None
        self._update_buttons()

    def refresh(self) -> None:
        from ..pipeline import describe
        rows = self.app.db.owner_todo()
        sig = [tuple(r) for r in rows]
        if sig == self._sig:
            return
        self._sig = sig
        self._rows = {str(r["id"]): r for r in rows}
        out = []
        for r in rows:
            big = r["status"] == "failed"
            why = (r["reason"].split(";")[0] if big else
                   "the publisher blocks automated downloads")
            who = f"{r['sender_name']} <{r['sender']}>" if r["sender_name"] else r["sender"]
            out.append((str(r["id"]), (f"#{r['id']}", clip(describe(r), 80), who, why)))
        fill_tree(self.tree, out)
        self._update_buttons()

    def _update_buttons(self) -> None:
        sel = [self._rows[i] for i in self.tree.selection() if i in self._rows]
        manual = [r for r in sel if r["status"] == "manual"]
        big = [r for r in sel if r["status"] == "failed"]
        any_manual = any(r["status"] == "manual" for r in self._rows.values())
        idle = self._control is None                  # no browser session running
        self.b_browser.configure(state="normal" if idle and (manual or (not sel and any_manual))
                                 else "disabled",
                                 text="Get it in a browser window" if sel or not any_manual
                                 else "Get them all in a browser window")
        self.b_attach.configure(state="normal" if len(manual) == 1 else "disabled")
        self.b_giveup.configure(state="normal" if manual else "disabled")
        self.b_show.configure(state="normal" if len(big) == 1 else "disabled")
        self.b_shared.configure(state="normal" if big else "disabled")

    def _selected_manual(self) -> list[int]:
        return [int(i) for i in self.tree.selection()
                if i in self._rows and self._rows[i]["status"] == "manual"]

    def get_in_browser(self) -> None:
        app = self.app
        ids = self._selected_manual() or [int(i) for i, r in self._rows.items()
                                          if r["status"] == "manual"]
        if not ids:
            return
        if not over_limit_ok(app, len(ids)):
            return
        from ..humanbrowser import ManualControl
        self._control = control = ManualControl()
        self.guide.configure(text="Connecting the VPN and opening a browser window…")
        self.waiting.pack(anchor="w", pady=(app.px(8), 0))
        self.b_browser.configure(state="disabled")

        def tell(msg):  # progress from the worker, shown under the list
            app.bg.call(self.guide.configure, {"text": msg})

        def finished():
            self.waiting.pack_forget()
            self.guide.configure(text="")
            self._control = None
            self._update_buttons()

        def done(got):
            finished()
            app.set_status(f"{got} of {len(ids)} paper(s) downloaded and emailed.")

        def work():
            try:
                return app.bot().run_manual(manual_session_factory(app.cfg, control, tell),
                                            say=tell, item_ids=ids)
            finally:
                app.bg.call(finished)

        app.run_task("Waiting for you in the browser window…", work, lock=True, done=done)

    def _skip(self) -> None:
        if self._control:
            self._control.skip.set()

    def _stop(self) -> None:
        if self._control:
            self._control.stop.set()

    def attach(self) -> None:
        from ..sources import is_pdf
        ids = self._selected_manual()
        if len(ids) != 1:
            return
        path = filedialog.askopenfilename(parent=self.app.root, title="Choose the PDF",
                                          filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        data = Path(path).read_bytes()
        if not is_pdf(data):
            messagebox.showerror("PaperBot", "That file isn't a PDF.", parent=self.app.root)
            return
        self.app.run_task("Sending it to the requester…",
                          lambda: self.app.bot().attach(ids[0], data), lock=True,
                          done=lambda _: self.app.set_status("Sent."))

    def give_up(self) -> None:
        ids = self._selected_manual()
        if ids and messagebox.askyesno(
                "PaperBot", f"Give up on {len(ids)} paper(s)?\nThe requester gets a short "
                            "\"sorry, couldn't get this one\" email.", parent=self.app.root):
            self.app.run_task("Letting the requester know…",
                              lambda: self.app.bot().give_up(ids), lock=True)

    def show_file(self) -> None:
        sel = [self._rows[i] for i in self.tree.selection() if i in self._rows]
        if not (sel and sel[0]["pdf_path"]):
            return
        path = os.path.normpath(sel[0]["pdf_path"])
        if os.path.exists(path):
            subprocess.run(["explorer", "/select,", path])
            return
        # A downloaded file stays on the computer that fetched it.
        host, here = sel[0]["host"], computer_name()
        elsewhere = bool(host) and host not in (here, tidy_name(here))
        messagebox.showinfo(
            "PaperBot",
            (f"This file is on your other computer, {host}:\n\n{path}\n\nOpen PaperBot there "
             "and press \"Show the file\"." if elsewhere else
             f"The file isn't there any more:\n\n{path}"), parent=self.app.root)

    def mark_shared(self) -> None:
        ids = [int(i) for i in self.tree.selection()
               if i in self._rows and self._rows[i]["status"] == "failed"]
        if ids:
            self._sig = None
            self.app.run_task("Taking it off the list…",
                              lambda: self.app.bot().mark_shared(ids), lock=True)


class TryTab:
    key, title, always_refresh = "try", "Try a paper", False

    def __init__(self, app, nb):
        self.app = app
        px, pal = app.px, app.pal
        self.frame = ttk.Frame(nb, padding=px(18))
        ttk.Label(self.frame, foreground=pal["muted"], wraplength=px(980), justify="left",
                  text="Test PaperBot on one paper without any email. The PDF is saved on this "
                       "PC so you can check it.").pack(anchor="w")
        row = ttk.Frame(self.frame)
        row.pack(fill="x", pady=(px(12), 0))
        ttk.Label(row, text="DOI, link or exact title").pack(side="left")
        self.what = ttk.Entry(row)
        self.what.pack(side="left", fill="x", expand=True, padx=px(10))
        self.what.bind("<Return>", lambda e: self.fetch())
        self.go = ttk.Button(row, text="Fetch", style="Accent.TButton", command=self.fetch)
        self.go.pack(side="left")
        self.mode = tk.StringVar(value="free+vpn")
        modes = ttk.Frame(self.frame)
        modes.pack(fill="x", pady=(px(10), 0))
        for value, text in (("free", "Free copies only (no VPN)"),
                            ("free+vpn", "Free copy first, then the publisher through the VPN"),
                            ("vpn", "Publisher through the VPN only")):
            ttk.Radiobutton(modes, text=text, value=value, variable=self.mode).pack(
                side="left", padx=(0, px(18)))
        ttk.Label(self.frame, text="What happened", font=f(STRONG)).pack(
            anchor="w", pady=(px(16), px(4)))
        bg, fg = text_colors(pal)
        self.out = tk.Text(self.frame, height=12, wrap="word", relief="flat", borderwidth=0,
                           background=bg, foreground=fg, font=f(BODY),
                           padx=px(12), pady=px(8))
        self.out.pack(fill="both", expand=True)
        self.out.configure(state="disabled")
        row2 = ttk.Frame(self.frame)
        row2.pack(fill="x", pady=(px(10), 0))
        self.open_btn = ttk.Button(row2, text="Open the PDF", state="disabled",
                                   command=lambda: os.startfile(self._pdf))
        self.open_btn.pack(side="left")
        ttk.Button(row2, text="Open the test folder",
                   command=self.open_folder).pack(side="left", padx=px(8))
        self._pdf = None

    def refresh(self) -> None:
        pass

    def append(self, text: str) -> None:
        self.out.configure(state="normal")
        self.out.insert("end", text + "\n")
        self.out.see("end")
        self.out.configure(state="disabled")

    def fetch(self) -> None:
        what = self.what.get().strip()
        if not what:
            return
        if self.mode.get() != "free" and not over_limit_ok(self.app, 1):
            return
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        mode, cfg = self.mode.get(), self.app.cfg

        def done(path):
            self._pdf = str(path) if path else None
            self.open_btn.configure(state="normal" if path else "disabled")
            self.append("Done." if path else "No PDF this time.")
            self.app.set_status("Fetched." if path else "No PDF found.")

        self.app.run_task("Fetching…", lambda: fetch_one(
            cfg, what, mode, say=lambda m: self.app.bg.call(self.append, m)),
            lock=(mode != "free"), done=done)

    def open_folder(self) -> None:
        folder = self.app.cfg.data_dir / "test"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)
