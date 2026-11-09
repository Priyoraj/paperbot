"""People (approvals, trust settings) and Requests (history with per-paper details)."""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from .. import config as config_mod
from .base import BODY, STRONG, clip, f, fill_tree, make_tree
from .home import text_colors

STATUS_PEOPLE = {"pending": "Waiting for your OK", "approved": "Approved",
                 "denied": "Blocked"}
STATUS_REQUEST = {"held": "Waiting for your OK", "queued": "In progress", "done": "Answered",
                  "denied": "Blocked sender", "ignored": "Ignored"}
STATUS_ITEM = {"new": "looking it up", "resolved": "queued for download",
               "fetched": "downloaded, about to be sent", "sent": "sent",
               "manual": "needs you (Needs you tab)", "failed": "not sent"}


def when(iso: str | None) -> str:
    if not iso:
        return ""
    t = datetime.fromisoformat(iso)
    return ("today " if t.date() == datetime.now().date() else t.strftime("%b %d ")) + \
        t.strftime("%H:%M")


class PeopleTab:
    key, title, always_refresh = "people", "People", False

    def __init__(self, app, nb):
        self.app = app
        px, pal = app.px, app.pal
        self.frame = ttk.Frame(nb, padding=px(18))

        card = ttk.Frame(self.frame, style="Card.TFrame", padding=px(16))
        card.pack(fill="x")
        self.ask = tk.BooleanVar(value=app.cfg.require_approval)
        ttk.Checkbutton(card, text="Ask me before sending papers to someone new",
                        style="Switch.TCheckbutton", variable=self.ask,
                        command=self.toggle_ask).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(card, foreground=pal["muted"], wraplength=px(900), justify="left",
                  text="When on, the first request from a new address waits until you approve "
                       "it - here, or by replying \"yes\" to the email PaperBot sends you. When "
                       "off, anyone who emails a request gets the papers.").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(px(4), px(10)))
        trust = ttk.Frame(card)
        trust.grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(trust, text="Always trust addresses at").pack(side="left")
        self.domains = ttk.Entry(trust, width=36)
        self.domains.pack(side="left", padx=px(10))
        self.domains.bind("<Return>", lambda e: self.save_domains())
        ttk.Button(trust, text="Save", command=self.save_domains).pack(side="left")
        ttk.Label(trust, text="e.g.  university.edu  (several: separate with commas)",
                  foreground=pal["muted"]).pack(side="left", padx=px(12))

        ttk.Label(self.frame, text="People who have asked for papers", font=f(STRONG)).pack(
            anchor="w", pady=(px(16), px(6)))
        box, self.tree = make_tree(self.frame, [("email", "Email", 280, True),
                                                ("name", "Name", 200, True),
                                                ("status", "Status", 190, False),
                                                ("since", "First request", 150, False)])
        box.pack(fill="both", expand=True)
        row = ttk.Frame(self.frame)
        row.pack(fill="x", pady=(px(10), 0))
        ttk.Button(row, text="Approve", style="Accent.TButton", command=self.approve).pack(
            side="left")
        ttk.Button(row, text="Block", command=self.block).pack(side="left", padx=px(8))
        ttk.Button(row, text="Forget", command=self.forget).pack(side="left")
        ttk.Label(row, text="Approving answers their waiting requests straight away.",
                  foreground=pal["muted"]).pack(side="left", padx=px(14))
        self._sig = None

    def refresh(self) -> None:
        cfg = self.app.cfg
        self.ask.set(cfg.require_approval)
        try:
            editing = self.frame.focus_get() is self.domains
        except (KeyError, tk.TclError):  # focus inside a combobox popdown
            editing = False
        if not editing:
            self.domains.delete(0, "end")
            self.domains.insert(0, ", ".join(cfg.auto_approve_domains))
        people = sorted(self.app.db.people(),
                        key=lambda p: ({"pending": 0, "approved": 1, "denied": 2}[p["status"]],
                                       p["email"]))
        sig = [tuple(p) for p in people]
        if sig == self._sig:
            return
        self._sig = sig
        fill_tree(self.tree, [(p["email"], (p["email"], p["name"] or "",
                                            STATUS_PEOPLE.get(p["status"], p["status"]),
                                            when(p["first_seen"]))) for p in people])

    def _selected(self) -> list[str]:
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("PaperBot", "Select someone in the list first.",
                                parent=self.app.root)
        return sel

    def toggle_ask(self) -> None:
        config_mod.set_value("access", "require_approval", bool(self.ask.get()), self.app.cfg.path)
        self.app.reload_config()
        self.app.set_status("New people now " + ("need your OK." if self.ask.get()
                                                 else "get papers without asking you."))

    def save_domains(self) -> None:
        doms = [d.strip().lower().lstrip("@") for d in self.domains.get().replace(";", ",")
                .split(",") if d.strip()]
        config_mod.set_value("access", "auto_approve_domains", doms, self.app.cfg.path)
        self.app.reload_config()
        self.app.set_status("Saved: " + (", ".join(doms) if doms else "no always-trusted domains"))

    def approve(self) -> None:
        emails = self._selected()
        if not emails:
            return
        app = self.app

        def work():
            bot = app.bot()
            released = sum(bot.decide(e, approve=True) for e in emails)
            if released:
                app.say("Approved - answering their waiting requests now…")
                bot.run_once()
            return released

        app.run_task(f"Approving {', '.join(emails)}…", work, lock=True,
                     done=lambda n: app.set_status(
                         "Approved." + (f" {n} waiting request(s) answered." if n else "")))

    def block(self) -> None:
        emails = self._selected()
        if emails and messagebox.askyesno(
                "PaperBot", "Block " + ", ".join(emails) + "?\nTheir requests will be ignored "
                "from now on.", parent=self.app.root):
            self.app.run_task("Blocking…", lambda: [self.app.bot().decide(e, approve=False)
                                                    for e in emails])

    def forget(self) -> None:
        emails = self._selected()
        if emails and messagebox.askyesno(
                "PaperBot", "Forget " + ", ".join(emails) + "?\nTheir next request will be "
                "treated as first-time.", parent=self.app.root):
            for e in emails:
                self.app.db.forget_person(e)
            self._sig = None
            self.app.refresh_all()


class RequestsTab:
    key, title, always_refresh = "requests", "Requests", False

    def __init__(self, app, nb):
        self.app = app
        px = app.px
        self.frame = ttk.Frame(nb, padding=px(18))
        ttk.Label(self.frame, text="Every request PaperBot has received (newest first). Select "
                                   "one to see what happened to each paper.",
                  foreground=app.pal["muted"]).pack(anchor="w", pady=(0, px(8)))
        box, self.tree = make_tree(self.frame, [("when", "Received", 140, False),
                                                ("from", "From", 260, True),
                                                ("papers", "Papers", 280, True),
                                                ("status", "Status", 190, False)], height=12)
        self.tree.configure(selectmode="browse")
        box.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        bg, fg = text_colors(app.pal)
        self.details = tk.Text(self.frame, height=8, wrap="word", relief="flat", borderwidth=0,
                               background=bg, foreground=fg, font=f(BODY),
                               padx=px(12), pady=px(8))
        self.details.tag_configure("muted", foreground=app.pal["muted"])
        self.details.tag_configure("ok", foreground=app.pal["ok"])
        self.details.tag_configure("bad", foreground=app.pal["bad"])
        self.details.pack(fill="x", pady=(px(10), 0))
        self.details.configure(state="disabled")
        self._sig = None

    def refresh(self) -> None:
        rows = self.app.db.recent_requests()
        sig = [tuple(r) for r in rows]
        if sig == self._sig:
            return
        self._sig = sig
        out = []
        for r in rows:
            n, sent, bad = r["n_items"] or 0, r["n_sent"] or 0, r["n_problem"] or 0
            papers = f"{n} paper{'s' if n != 1 else ''}"
            if sent:
                papers += f" · {sent} sent"
            if bad:
                papers += f" · {bad} not sent"
            status = STATUS_REQUEST.get(r["status"], r["status"])
            if r["status"] == "ignored" and r["note"]:
                status += f" ({r['note']})"
            who = f"{r['sender_name']} <{r['sender']}>" if r["sender_name"] else r["sender"]
            out.append((str(r["id"]), (when(r["created_at"]), who, papers, status)))
        fill_tree(self.tree, out)
        self.show_details()

    def show_details(self) -> None:
        from ..pipeline import describe
        sel = self.tree.selection()
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        if not sel:
            self.details.insert("end", "Select a request above.", "muted")
        else:
            req = self.app.db.request(int(sel[0]))
            self.details.insert("end", f"Subject: {req['subject']}\n", "muted")
            items = [i for i in self.app.db.items_for(req["id"]) if i["status"] != "dropped"]
            if not items:
                self.details.insert("end", "No papers were found in this email.", "muted")
            for n, it in enumerate(items, 1):
                status = STATUS_ITEM.get(it["status"], it["status"])
                tag = "ok" if it["status"] == "sent" else (
                    "bad" if it["status"] in ("failed", "manual") else "muted")
                self.details.insert("end", f"{n}. {clip(describe(it), 110)}  -  ")
                extra = ""
                if it["status"] == "sent" and it["source"]:
                    extra = f" ({it['source'].replace('_', ' ')})"
                elif it["status"] == "failed" and it["reason"]:
                    extra = f": {it['reason']}"
                self.details.insert("end", status + extra + "\n", tag)
        self.details.configure(state="disabled")
