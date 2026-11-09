"""Settings: every config.toml value as a form, passwords, and setup tests."""
from __future__ import annotations

import datetime as dt
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from .. import config as config_mod
from .. import winsvc
from ..config import RECOMMENDED_LIMITS
from ..app import (has_mail_password, make_vpn, set_secret, setup_checks, vpn_credentials,
                   vpn_profiles)
from .base import STRONG, SUBTITLE, ScrollFrame, f

# (section, key, label, kind, help); kind: str | int | float | bool | kw | choice:a,b | vpnname
SECTIONS = [
    ("Daily safety limits", [("@limits",)]),
    ("You", [
        ("owner", "name", "Your name", "str", "Signs the replies, e.g. your first name"),
        ("owner", "email", "Your email for approval questions", "str",
         "Leave blank to use the mailbox below"),
    ]),
    ("Mailbox colleagues write to", [
        ("mail", "username", "Email address", "str",
         "PaperBot reads requests here and replies from it (Gmail works out of the box)"),
        ("@mailpw",),
        ("mail", "subject_keywords", "Subject that marks a request", "kw",
         "Emails whose subject contains this are treated as paper requests"),
        ("mail", "signature", "Line under each reply", "str", ""),
    ]),
    ("Institute VPN", [
        ("vpn", "enabled", "Use the institute VPN for paywalled papers", "bool", ""),
        ("vpn", "name", "Windows VPN connection", "vpnname", ""),
        ("@vpnpw",),
        ("vpn", "allowed_hours", "Allowed hours", "str",
         "00:00-24:00 = any time; 22:00-07:00 = overnight only"),
        ("vpn", "disconnect_when_done", "Turn the VPN off when done (only if PaperBot turned "
                                        "it on)", "bool", ""),
    ]),
    ("Downloads", [
        ("fetch", "contact_email", "Contact email for open-access lookups", "str",
         "Unpaywall needs one; more free copies, less VPN use"),
        ("fetch", "browser", "Browser", "choice:msedge,chrome", "msedge = Microsoft Edge"),
        ("fetch", "headless", "Hide the browser window while fetching", "bool", ""),
        ("schedule", "poll_minutes", "Automatic mode checks mail every (minutes)", "int", ""),
        ("fetch", "keep_pdfs_days", "Keep downloaded PDFs for (days)", "int", ""),
    ]),
    ("Advanced mail settings", [
        ("mail", "imap_host", "IMAP server", "str", ""),
        ("mail", "imap_port", "IMAP port", "int", ""),
        ("mail", "smtp_host", "SMTP server", "str", ""),
        ("mail", "smtp_port", "SMTP port", "int", ""),
        ("mail", "mailbox", "Folder to read", "str", ""),
        ("mail", "lookback_days", "Look back (days)", "int", ""),
        ("access", "max_items_per_email", "Papers per email", "int", ""),
    ]),
]

# (section, key, label, lowest, highest, riskier_when) - riskier_when: "up" / "down" / None
LIMIT_FIELDS = [
    ("access", "max_papers_per_day", "Daily ceiling: institute downloads per day, yours "
                                     "included", 1, 500, "up"),
    ("access", "own_reserve_per_day", "Kept for your own downloads", 0, 500, None),
    ("access", "max_papers_per_hour", "At most per hour", 1, 100, "up"),
    ("access", "max_papers_per_person_per_day", "Per colleague per day", 1, 100, "up"),
    ("fetch", "delay_between_publisher_requests_s", "Seconds between downloads", 5, 600,
     "down"),
]
LIMIT_KEYS = {(s, k) for s, k, *_ in LIMIT_FIELDS}


def current_value(cfg, section: str, key: str):
    return getattr(cfg, config_mod._MAP[(section, key)])


def _fmt(value) -> str:
    """20.0 -> '20'; anything else unchanged as text."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class SettingsTab:
    key, title, always_refresh = "settings", "Settings", False

    def __init__(self, app, nb):
        self.app = app
        px, pal = app.px, app.pal
        self.frame = ttk.Frame(nb)
        top = ttk.Frame(self.frame, padding=(px(18), px(14), px(18), px(6)))
        top.pack(fill="x")
        ttk.Button(top, text="Save changes", style="Accent.TButton", command=self.save).pack(
            side="left")
        ttk.Button(top, text="Check everything", command=self.check_all).pack(side="left",
                                                                               padx=px(8))
        ttk.Button(top, text="Test the VPN", command=self.test_vpn).pack(side="left")
        self.saved_label = ttk.Label(top, text="", foreground=pal["muted"])
        self.saved_label.pack(side="left", padx=px(14))

        scroll = ScrollFrame(self.frame)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        self.vars: dict[tuple[str, str], tuple[str, tk.Variable]] = {}
        row = 0
        for title, fields in SECTIONS:
            ttk.Label(body, text=title, font=f(SUBTITLE)).grid(
                row=row, column=0, columnspan=3, sticky="w", padx=px(18),
                pady=(px(18 if row else 6), px(6)))
            row += 1
            for spec in fields:
                if spec[0] == "@limits":
                    row = self._limits_block(body, row)
                    continue
                if spec[0] == "@mailpw":
                    row = self._password_row(body, row, mail=True)
                    continue
                if spec[0] == "@vpnpw":
                    row = self._password_row(body, row, mail=False)
                    continue
                section, key, label, kind, hint = spec
                row = self._field(body, row, section, key, label, kind, hint)
        body.columnconfigure(2, weight=1)  # hints take the spare width, fields stay compact
        self.load_values()

    # ---- form building ------------------------------------------------------------
    def _field(self, body, row, section, key, label, kind, hint) -> int:
        px = self.app.px
        if kind == "bool":
            var = tk.BooleanVar()
            ttk.Checkbutton(body, text=label, variable=var).grid(
                row=row, column=0, columnspan=2, sticky="w", padx=px(18), pady=px(3))
        else:
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=px(18),
                                              pady=px(3))
            var = tk.StringVar()
            if kind.startswith("choice:"):
                w = ttk.Combobox(body, textvariable=var, values=kind[7:].split(","),
                                 state="readonly", width=20)
            elif kind == "vpnname":
                w = ttk.Combobox(body, textvariable=var, width=30)
                self.vpn_combo = w
                self.app.bg.run(vpn_profiles, lambda names: w.configure(values=names))
            else:
                w = ttk.Entry(body, textvariable=var,
                              width=12 if kind in ("int", "float") else 46)
            w.grid(row=row, column=1, sticky="w", padx=px(8), pady=px(3))
        if hint:
            ttk.Label(body, text=hint, foreground=self.app.pal["muted"]).grid(
                row=row, column=2, sticky="w", padx=(px(4), px(18)))
        self.vars[(section, key)] = (kind, var)
        return row + 1

    def _limits_block(self, body, row) -> int:
        px, pal = self.app.px, self.app.pal
        r = RECOMMENDED_LIMITS
        rec = tk.BooleanVar()
        self.vars[("access", "use_recommended_limits")] = ("bool", rec)
        line = ttk.Frame(body)
        line.grid(row=row, column=0, columnspan=3, sticky="w", padx=px(18), pady=(px(2), px(4)))
        ttk.Checkbutton(line, text="Use the recommended safe limits", style="Switch.TCheckbutton",
                        variable=rec, command=lambda: self._sync_limits(user_toggled=True)).pack(
            side="left")
        self.limit_mode = ttk.Label(line, text="", foreground=pal["muted"])
        self.limit_mode.pack(side="left", padx=px(14))
        row += 1
        ttk.Label(body, foreground=pal["muted"], wraplength=px(1000), justify="left", text=(
            f"Recommended: {r['max_papers_per_day']} downloads a day through the institute in "
            f"total, {r['own_reserve_per_day']} of them kept for your own use; at most "
            f"{r['max_papers_per_hour']} an hour and {r['max_papers_per_person_per_day']} per "
            f"colleague, {r['delay_between_publisher_requests_s']} seconds apart. Publishers don't "
            "publish their thresholds, but libraries report blocks after e.g. 100 articles in 40 "
            "minutes or 25 in 5 minutes, and these values stay far below that. Free copies don't "
            "count. When a limit is reached, colleagues are asked to send the paper again "
            "tomorrow.")).grid(row=row, column=0, columnspan=3, sticky="w", padx=px(18),
                               pady=(0, px(8)))
        row += 1
        self.limit_spins = {}
        for section, key, label, lo, hi, _ in LIMIT_FIELDS:
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=px(18),
                                              pady=px(3))
            var = tk.StringVar()
            spin = ttk.Spinbox(body, from_=lo, to=hi, increment=1, textvariable=var, width=8)
            spin.grid(row=row, column=1, sticky="w", padx=px(8), pady=px(3))
            self.vars[(section, key)] = ("float" if key.endswith("_s") else "int", var)
            self.limit_spins[(section, key)] = spin
            var.trace_add("write", lambda *a: self._update_limit_summary())
            row += 1
        self.limit_summary = ttk.Label(body, text="", font=f(STRONG), wraplength=px(1000),
                                       justify="left")
        self.limit_summary.grid(row=row, column=0, columnspan=3, sticky="w", padx=px(18),
                                pady=(px(8), 0))
        row += 1
        self.limit_warning = ttk.Label(body, text="", foreground=pal["warn"],
                                       wraplength=px(1000), justify="left")
        self.limit_warning.grid(row=row, column=0, columnspan=3, sticky="w", padx=px(18))
        row += 1
        ttk.Button(body, text="Back to recommended", command=self._reset_limits).grid(
            row=row, column=0, sticky="w", padx=px(18), pady=(px(8), 0))
        return row + 1

    def _sync_limits(self, user_toggled: bool = False) -> None:
        """Recommended on: show the recommended numbers, locked. Off: editable custom numbers."""
        rec = self.vars[("access", "use_recommended_limits")][1].get()
        for (section, key), spin in self.limit_spins.items():
            var = self.vars[(section, key)][1]
            if rec:
                var.set(_fmt(RECOMMENDED_LIMITS[key]))
                spin.state(["disabled"])
            else:
                if user_toggled:  # start from the custom values saved in config.toml
                    var.set(_fmt(current_value(self.app.cfg, section, key)))
                spin.state(["!disabled"])
        self.limit_mode.configure(
            text="(numbers below are locked; switch off to choose your own)" if rec
            else "(your own numbers: use the arrows, then Save changes)")
        self._update_limit_summary()

    def _reset_limits(self) -> None:
        self.vars[("access", "use_recommended_limits")][1].set(True)
        self._sync_limits()
        self.saved_label.configure(text="Recommended limits selected - press Save changes.")

    def _update_limit_summary(self) -> None:
        if not hasattr(self, "limit_warning"):
            return  # still building the form
        get = lambda section, key: self.vars[(section, key)][1].get()  # noqa: E731
        try:
            day = int(get("access", "max_papers_per_day"))
            keep = min(int(get("access", "own_reserve_per_day")), day)
            hour = int(get("access", "max_papers_per_hour"))
            person = int(get("access", "max_papers_per_person_per_day"))
            delay = float(get("fetch", "delay_between_publisher_requests_s"))
        except ValueError:
            self.limit_summary.configure(text="Please use whole numbers.")
            self.limit_warning.configure(text="")
            return
        self.limit_summary.configure(
            text=f"Colleagues can get up to {day - keep} papers a day between them ({person} "
                 f"each at most), and {keep} stay free for you. Never more than {hour} downloads "
                 f"in an hour, {delay:g} seconds apart.")
        r = RECOMMENDED_LIMITS
        if day - keep <= 0:
            warn = ("Nothing is left for colleagues: every paper that needs the institute's "
                    "access will get \"please try again tomorrow\".")
        elif (day > r["max_papers_per_day"] or hour > r["max_papers_per_hour"]
              or person > r["max_papers_per_person_per_day"]
              or delay < r["delay_between_publisher_requests_s"]):
            warn = ("Above the recommended limits. More downloads per day or per hour make it "
                    "likelier that a publisher flags the institute's VPN address, which can "
                    "block access for everyone at the institute.")
        else:
            warn = ""
        self.limit_warning.configure(text=warn)

    def _password_row(self, body, row, mail: bool) -> int:
        px, pal = self.app.px, self.app.pal
        ttk.Label(body, text="Mail password" if mail else "VPN sign-in").grid(
            row=row, column=0, sticky="w", padx=px(18), pady=px(3))
        cell = ttk.Frame(body)
        cell.grid(row=row, column=1, columnspan=2, sticky="w", padx=px(8))
        status = ttk.Label(cell, text="", font=f(STRONG))
        status.pack(side="left")
        if mail:
            ttk.Button(cell, text="Set…", command=self.set_mail_password).pack(
                side="left", padx=px(10))
            link = ttk.Label(cell, text="How do I get a Gmail app password?",
                             foreground=pal["accent"], cursor="hand2")
            link.pack(side="left")
            link.bind("<Button-1>", lambda e: webbrowser.open(
                "https://myaccount.google.com/apppasswords"))
            self.mail_status = status
        else:
            self.vpn_btn = ttk.Button(cell, text="Save a VPN password for PaperBot…",
                                      command=self.set_vpn_password)
            self.vpn_btn.pack(side="left", padx=px(10))
            self.vpn_status = status
        return row + 1

    # ---- values -------------------------------------------------------------------
    def load_values(self) -> None:
        cfg = self.app.cfg
        for (section, key), (kind, var) in self.vars.items():
            value = current_value(cfg, section, key)
            if kind == "kw":
                value = value[0] if value else ""
            var.set(value if kind == "bool" else _fmt(value))
        self._sync_limits()
        self.refresh()

    def refresh(self) -> None:
        cfg, pal, app = self.app.cfg, self.app.pal, self.app
        ok = has_mail_password(cfg)
        self.mail_status.configure(
            text="✓ saved" if ok else ("✗ not set" if cfg.mail_username else
                                       "– save the email address first"),
            foreground=pal["ok"] if ok else pal["bad"])
        saved = app.vpn_saved
        self.vpn_status.configure(
            text="checking…" if saved is None else (
                "✓ uses the password saved in Windows" if saved else "✗ no saved password"),
            foreground=pal["muted"] if saved is None else (pal["ok"] if saved else pal["bad"]))
        if saved and vpn_credentials(cfg) is None:
            self.vpn_btn.pack_forget()

    def save(self) -> None:
        cfg = self.app.cfg
        changes = {}
        use_rec = bool(self.vars[("access", "use_recommended_limits")][1].get())
        bounds = {(s, k): (lo, hi) for s, k, _, lo, hi, _ in LIMIT_FIELDS}
        try:
            for (section, key), (kind, var) in self.vars.items():
                if (section, key) in LIMIT_KEYS and use_rec:
                    continue  # keep the custom numbers on file for when the switch goes off
                raw = var.get()
                if kind == "bool":
                    value = bool(raw)
                elif kind == "int":
                    value = int(str(raw).strip())
                elif kind == "float":
                    value = float(str(raw).strip())
                elif kind == "kw":
                    value = [str(raw).strip()] if str(raw).strip() else ["paper request"]
                else:
                    value = str(raw).strip()
                if (section, key) == ("vpn", "allowed_hours"):
                    config_mod.hours_contain(value, dt.time(0, 0))  # raises if malformed
                if (section, key) in bounds and not (bounds[(section, key)][0] <= value
                                                     <= bounds[(section, key)][1]):
                    raise ValueError(f"{key.replace('_', ' ')} must be between "
                                     f"{bounds[(section, key)][0]} and "
                                     f"{bounds[(section, key)][1]}")
                old = current_value(cfg, section, key)
                if kind == "kw":
                    old = list(old)
                if value != old:
                    changes[(section, key)] = value
        except (ValueError, config_mod.ConfigError) as exc:
            messagebox.showerror("PaperBot", f"Please check the numbers and times: {exc}",
                                 parent=self.app.root)
            return
        for (section, key), value in changes.items():
            config_mod.set_value(section, key, value, cfg.path)
        self.app.reload_config()
        self.app.refresh_vpn_status()
        self.saved_label.configure(text=f"Saved {len(changes)} change(s)." if changes
                                   else "Nothing changed.")
        if ("schedule", "poll_minutes") in changes and self.app.task and self.app.task.on:
            self.app.run_task("Updating automatic mode…",
                              lambda: winsvc.install(self.app.cfg.poll_minutes),
                              done=lambda _: self.app.refresh_task_status())
        self.refresh()

    # ---- passwords ------------------------------------------------------------------
    def _ask_secret(self, title: str, prompt: str, need_user: bool = False):
        win = tk.Toplevel(self.app.root)
        win.title(title)
        win.transient(self.app.root)
        win.resizable(False, False)
        pad = ttk.Frame(win, padding=self.app.px(18))
        pad.pack(fill="both", expand=True)
        ttk.Label(pad, text=prompt, wraplength=self.app.px(420), justify="left").pack(anchor="w")
        user = None
        if need_user:
            ttk.Label(pad, text="Username").pack(anchor="w", pady=(self.app.px(10), 0))
            user = ttk.Entry(pad, width=40)
            user.pack(fill="x")
        ttk.Label(pad, text="Password").pack(anchor="w", pady=(self.app.px(10), 0))
        pw = ttk.Entry(pad, show="•", width=40)
        pw.pack(fill="x")
        result = {}

        def ok(_=None):
            result["pw"] = pw.get()
            result["user"] = user.get().strip() if user else ""
            win.destroy()

        row = ttk.Frame(pad)
        row.pack(fill="x", pady=(self.app.px(14), 0))
        ttk.Button(row, text="Save", style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right",
                                                                 padx=self.app.px(8))
        win.bind("<Return>", ok)
        (user or pw).focus_set()
        win.grab_set()
        self.app.root.wait_window(win)
        return result

    def set_mail_password(self) -> None:
        cfg = self.app.cfg
        typed = self.vars[("mail", "username")][1].get().strip()
        if not typed:
            messagebox.showinfo("PaperBot", "Fill in the email address first.",
                                parent=self.app.root)
            return
        if typed != cfg.mail_username:
            self.save()
            cfg = self.app.cfg
        res = self._ask_secret("Mail password",
                               f"App password for {cfg.mail_username}.\n\nFor Gmail: turn on "
                               "2-Step Verification, then create an app password (16 letters) at "
                               "myaccount.google.com/apppasswords. It's stored in Windows "
                               "Credential Manager, not in a file.")
        pw = (res.get("pw") or "").replace(" ", "")
        if not pw:
            return
        set_secret(f"mail:{cfg.mail_username}", pw)
        self.refresh()
        self.app.run_task("Testing the mail login…",
                          lambda: self.app.bot().mail.check_login(),
                          done=lambda _: messagebox.showinfo(
                              "PaperBot", "Saved - and the mail login works.",
                              parent=self.app.root))

    def set_vpn_password(self) -> None:
        cfg = self.app.cfg
        res = self._ask_secret("VPN sign-in", f"Only needed if Windows hasn't saved the password "
                                              f"for {cfg.vpn_name}.", need_user=True)
        if res.get("user") and res.get("pw"):
            set_secret(f"vpn-user:{cfg.vpn_name}", res["user"])
            set_secret(f"vpn:{cfg.vpn_name}", res["pw"])
            self.app.refresh_vpn_status()

    # ---- tests ----------------------------------------------------------------------
    def check_all(self) -> None:
        def show(results):
            lines = [f"{'✓' if ok else '✗'}  {name}" + (f" - {detail}" if detail else "")
                     for name, ok, detail in results]
            fn = messagebox.showinfo if all(ok for _, ok, _ in results) else \
                messagebox.showwarning
            fn("PaperBot setup", "\n\n".join(lines), parent=self.app.root)
        self.app.run_task("Checking your setup…", lambda: setup_checks(self.app.cfg), done=show)

    def test_vpn(self) -> None:
        from ..vpn import vpn_session
        cfg = self.app.cfg

        def work():
            t = time.monotonic()
            with vpn_session(make_vpn(cfg), cfg.vpn_disconnect_when_done) as already:
                took = time.monotonic() - t
            if already:
                return "The VPN was already on, so it was left as it is."
            return (f"The VPN works: it connected in {took:.0f} seconds, internet traffic went "
                    "through the institute, and it's off again.")

        self.app.run_task("Testing the VPN (on, check, off)…", work, lock=True,
                          done=lambda msg: messagebox.showinfo("PaperBot", msg,
                                                               parent=self.app.root))
