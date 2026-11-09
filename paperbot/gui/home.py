"""Home: setup checklist, today's numbers, automatic mode, recent activity."""
from __future__ import annotations

import re
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from .. import winsvc
from ..app import has_mail_password
from .base import BODY, DISPLAY, STRONG, SUBTITLE, f

LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d):\d\d,\d+ (\w+) ([\w.]+): (.*)$")


def text_colors(pal: dict) -> tuple[str, str]:
    return ("#2b2b2b", "#e6e6e6") if pal["mode"] == "dark" else ("#ffffff", "#1a1a1a")


def read_activity(path, limit: int = 80) -> list[tuple[str, str, str]]:
    """[(when, level, message)] from the end of the log file."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 96_000))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    today = datetime.now().date().isoformat()
    out = []
    for line in lines:
        m = LINE_RE.match(line)
        if not m or m.group(3) == "DEBUG":
            continue
        day, hhmm, level, _, msg = m.groups()
        when = hhmm if day == today else datetime.fromisoformat(day).strftime("%b %d ") + hhmm
        out.append((when, level, msg))
    return out[-limit:]


class HomeTab:
    key, title, always_refresh = "home", "Home", False

    def __init__(self, app, nb):
        self.app = app
        px, pal = app.px, app.pal
        self.frame = ttk.Frame(nb, padding=px(18))

        # -- setup checklist (hidden once everything is done)
        self.setup = ttk.Frame(self.frame, style="Card.TFrame", padding=px(16))
        ttk.Label(self.setup, text="Finish setting up", font=f(SUBTITLE)).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, px(6)))
        self.steps = []
        for i, text in enumerate(["Your name and the mailbox colleagues write to",
                                  "Mail app password saved",
                                  "Institute VPN sign-in saved in Windows",
                                  "Automatic mode turned on (below)"], 1):
            mark = ttk.Label(self.setup, text="○", width=3, font=f(STRONG))
            mark.grid(row=i, column=0, sticky="w")
            ttk.Label(self.setup, text=text).grid(row=i, column=1, sticky="w", pady=px(2))
            self.steps.append(mark)
        self.setup.columnconfigure(1, weight=1)
        ttk.Button(self.setup, text="Open Settings", style="Accent.TButton",
                   command=lambda: app.show("settings")).grid(row=1, column=2, rowspan=2,
                                                               sticky="e")

        # -- numbers
        self.cards = ttk.Frame(self.frame)
        self.cards.pack(fill="x")
        self.stats, self.captions = {}, {}
        for i, (key, label, target) in enumerate([
                ("pending", "people waiting for your OK", "people"),
                ("needs", "papers that need you", "needs"),
                ("sent", "papers sent today", "requests"),
                ("vpn", "institute downloads today", "settings")]):
            card = ttk.Frame(self.cards, style="Card.TFrame", padding=(px(16), px(12)))
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else px(12), 0))
            self.cards.columnconfigure(i, weight=1, uniform="cards")
            num = ttk.Label(card, text="–", font=f(DISPLAY))
            num.pack(anchor="w")
            cap = ttk.Label(card, text=label, foreground=pal["muted"])
            cap.pack(anchor="w")
            for w in (card, num, cap):
                w.bind("<Button-1>", lambda e, t=target: app.show(t))
                w.configure(cursor="hand2")
            self.stats[key] = num
            self.captions[key] = cap

        # -- automatic mode
        auto = ttk.Frame(self.frame, style="Card.TFrame", padding=px(16))
        auto.pack(fill="x", pady=(px(14), 0))
        left = ttk.Frame(auto)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="Automatic mode", font=f(SUBTITLE)).pack(anchor="w")
        self.auto_text = ttk.Label(left, text="Checking…", justify="left",
                                   wraplength=px(720))
        self.auto_text.pack(anchor="w", pady=(px(4), 0))
        self.auto_btn = ttk.Button(auto, text="Turn on", style="Accent.TButton", width=12,
                                   command=self.toggle_auto)
        self.auto_btn.pack(side="right")

        # -- check now
        row = ttk.Frame(self.frame)
        row.pack(fill="x", pady=(px(14), px(4)))
        ttk.Button(row, text="Check mail now", command=self.check_now).pack(side="left")
        ttk.Label(row, text="reads new requests, fetches the papers and replies right away",
                  foreground=pal["muted"]).pack(side="left", padx=px(12))

        # -- activity
        ttk.Label(self.frame, text="Recent activity", font=f(STRONG)).pack(
            anchor="w", pady=(px(10), px(4)))
        box = ttk.Frame(self.frame)
        box.pack(fill="both", expand=True)
        bg, fg = text_colors(pal)
        self.log = tk.Text(box, height=8, wrap="word", relief="flat", borderwidth=0,
                           background=bg, foreground=fg, font=f(BODY),
                           padx=px(12), pady=px(8), cursor="arrow")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=bar.set, state="disabled")
        self.log.tag_configure("time", foreground=pal["muted"])
        self.log.tag_configure("WARNING", foreground=pal["warn"])
        self.log.tag_configure("ERROR", foreground=pal["bad"])
        self.log.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self._last_log = None

    # ------------------------------------------------------------------------------
    def refresh(self) -> None:
        app, db, cfg, pal = self.app, self.app.db, self.app.cfg, self.app.pal
        task = app.task
        done = [bool(cfg.owner_name and cfg.mail_username), has_mail_password(cfg),
                app.vpn_saved, bool(task and task.on)]
        for mark, ok in zip(self.steps, done):
            mark.configure(text="✓" if ok else ("…" if ok is None else "○"),
                           foreground=pal["ok"] if ok else pal["muted"])
        if all(done):
            self.setup.pack_forget()
        elif not self.setup.winfo_manager():
            self.setup.pack(fill="x", pady=(0, app.px(14)), before=self.cards)

        people = db.people()
        self.stats["pending"].configure(text=str(sum(p["status"] == "pending" for p in people)))
        self.stats["needs"].configure(text=str(len(db.owner_todo())))
        self.stats["sent"].configure(text=str(db.sent_today()))
        used, ceiling, share = db.downloads_today(), cfg.daily_ceiling, cfg.colleague_ceiling
        if used >= ceiling:
            color, note = pal["bad"], "today's safe limit is reached"
        elif used >= share:
            color, note = pal["warn"], (f"colleagues' share used up; {ceiling - used} left "
                                        "for you")
        else:
            color, note = "", f"institute downloads today · safe limit {ceiling}"
        self.stats["vpn"].configure(text=f"{used}/{ceiling}", foreground=color)
        self.captions["vpn"].configure(text=note, foreground=color or pal["muted"])

        if task is None:
            self.auto_text.configure(text="Checking…")
        elif task.on:
            last = task.last_run.strftime("%H:%M") if task.last_run else "not yet"
            nxt = task.next_run.strftime("%H:%M") if task.next_run else "soon"
            problem = task.last_result not in (None, 0, 267009, 267011)  # running / never run
            self.auto_text.configure(
                text=f"On - PaperBot checks your mail every {cfg.poll_minutes} minutes while "
                     f"you're logged in, even when this window is closed.\nLast check: {last}"
                     f"{' (had a problem - see Recent activity)' if problem else ''}"
                     f"  ·  next: {nxt}")
            self.auto_btn.configure(text="Turn off", style="TButton")
        else:
            self.auto_text.configure(
                text="Off - requests are only handled when you press \"Check mail now\".\n"
                     "Turn it on to answer requests automatically while you're logged in.")
            self.auto_btn.configure(text="Turn on", style="Accent.TButton")

        entries = read_activity(cfg.data_dir / "logs" / "paperbot.log")
        if entries != self._last_log:
            self._last_log = entries
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            if not entries:
                self.log.insert("end", "Nothing yet. Activity from checks and fetches shows up "
                                       "here.", "time")
            for when, level, msg in entries:
                self.log.insert("end", f"{when}  ", "time")
                self.log.insert("end", msg + "\n", level)
            self.log.see("end")
            self.log.configure(state="disabled")

    # ------------------------------------------------------------------------------
    def _ready(self) -> bool:
        cfg = self.app.cfg
        if not (cfg.owner_name and cfg.mail_username) or not has_mail_password(cfg):
            messagebox.showinfo("PaperBot", "First fill in your details and save the mail "
                                            "password in Settings.", parent=self.app.root)
            self.app.show("settings")
            return False
        return True

    def toggle_auto(self) -> None:
        app = self.app
        if app.task and app.task.on:
            app.run_task("Turning automatic mode off…", winsvc.uninstall,
                         done=lambda _: (app.refresh_task_status(),
                                         app.set_status("Automatic mode is off.")))
        elif self._ready():
            app.run_task("Turning automatic mode on…",
                         lambda: winsvc.install(app.cfg.poll_minutes),
                         done=lambda _: (app.refresh_task_status(),
                                         app.set_status("Automatic mode is on.")))

    def check_now(self) -> None:
        if self._ready():
            self.app.run_task("Checking mail and answering requests…",
                              lambda: self.app.bot().run_once(), lock=True,
                              done=lambda _: self.app.set_status(
                                  "Mail checked - see Recent activity."))
