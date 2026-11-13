"""Share: hand colleagues the request page (or plain-email instructions)."""
from __future__ import annotations

import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import sharepage
from ..config import ConfigError
from .base import STRONG, SUBTITLE, f, scroll_page, wrap_to_width


class ShareTab:
    key, title, always_refresh = "share", "Share with colleagues", False

    def __init__(self, app, nb):
        self.app = app
        px, pal = app.px, app.pal
        self.frame, page = scroll_page(nb, px(22))

        def words(parent, text, **look):
            """A paragraph that wraps to the width of the page."""
            label = ttk.Label(parent, text=text, justify="left", **look)
            wrap_to_width(label, most=px(1000))
            return label

        words(page, "Let colleagues ask without writing emails by hand",
              font=f(SUBTITLE)).pack(fill="x")
        words(page, "Send them the request page: a single file they open in any browser, on "
                    "Windows, Mac or phone, with nothing to install. They paste DOIs, links or "
                    "titles. The page looks each one up so they can confirm the right paper, "
                    "then opens their Gmail or Outlook with the request filled in. PaperBot "
                    "answers it like any other request.",
              foreground=pal["muted"]).pack(fill="x", pady=(px(6), px(14)))

        card = ttk.Frame(page, style="Card.TFrame", padding=px(16))
        card.pack(fill="x")
        self.dest = words(card, "", font=f(STRONG))
        self.dest.pack(fill="x")
        row = ttk.Frame(card)
        row.pack(fill="x", pady=(px(12), 0))
        self.b_save = ttk.Button(row, text="Save the request page…", style="Accent.TButton",
                                 command=self.save_page)
        self.b_save.pack(side="left")
        self.b_preview = ttk.Button(row, text="Preview it", command=self.preview)
        self.b_preview.pack(side="left", padx=px(8))
        self.b_invite = ttk.Button(row, text="Copy an invitation message",
                                   command=self.copy_invite)
        self.b_invite.pack(side="left")

        ttk.Label(page, text="Or by plain email", font=f(STRONG)).pack(
            anchor="w", pady=(px(22), px(4)))
        self.plain = words(page, "")
        self.plain.pack(fill="x")
        words(page, "Tip: re-save the page if you change your name, the mailbox or the request "
                    "subject in Settings.", foreground=pal["muted"]).pack(fill="x",
                                                                         pady=(px(18), 0))

        words(page, "Colleague with institute access of their own?",
              font=f(SUBTITLE)).pack(fill="x", pady=(px(28), px(4)))
        words(page, "They can run their own PaperBot. Save the installer, send it to them, and "
                    "they unzip it and double-click \"Install PaperBot\". It contains only the "
                    "program: none of your settings, passwords, requests or PDFs.",
              foreground=pal["muted"]).pack(fill="x")
        ttk.Button(page, text="Save the PaperBot installer (zip)…",
                   command=self.save_installer).pack(anchor="w", pady=(px(10), 0))

    def save_installer(self) -> None:
        from ..packaging import make_zip
        desktop = Path.home() / "Desktop"
        path = filedialog.asksaveasfilename(
            parent=self.app.root, title="Save the PaperBot installer",
            initialdir=str(desktop if desktop.exists() else Path.home()),
            initialfile="PaperBot installer.zip", defaultextension=".zip",
            filetypes=[("Zip archive", "*.zip")])
        if path:
            make_zip(Path(path))
            self.app.set_status(f"Saved {path}.")

    def refresh(self) -> None:
        try:
            c = sharepage.page_config(self.app.cfg)
        except ConfigError:
            self.dest.configure(text="Fill in the mailbox address in Settings first.",
                                foreground=self.app.pal["bad"])
            for b in (self.b_save, self.b_preview, self.b_invite):
                b.configure(state="disabled")
            self.plain.configure(text="")
            return
        self.dest.configure(text=f"Requests go to {c['to']} with the subject \"{c['subject']}\".",
                            foreground="")
        for b in (self.b_save, self.b_preview, self.b_invite):
            b.configure(state="normal")
        self.plain.configure(text=f"Colleagues can also write to {c['to']} themselves: subject "
                                  f"\"{c['subject']}\", one DOI, link or title per line. Titles "
                                  "work best in quotes.")

    def save_page(self) -> None:
        cfg = self.app.cfg
        desktop = Path.home() / "Desktop"
        path = filedialog.asksaveasfilename(
            parent=self.app.root, title="Save the request page",
            initialdir=str(desktop if desktop.exists() else Path.home()),
            initialfile=sharepage.file_name(cfg), defaultextension=".html",
            filetypes=[("Web page", "*.html")])
        if path:
            sharepage.save(cfg, Path(path))
            self.app.set_status(f"Saved {path}. Send it to your colleagues as an attachment.")

    def preview(self) -> None:
        cfg = self.app.cfg
        out = cfg.data_dir / "share"
        out.mkdir(parents=True, exist_ok=True)
        path = sharepage.save(cfg, out / sharepage.file_name(cfg))
        webbrowser.open(path.resolve().as_uri())

    def copy_invite(self) -> None:
        root = self.app.root
        root.clipboard_clear()
        root.clipboard_append(sharepage.invitation(self.app.cfg))
        messagebox.showinfo("PaperBot", "Invitation copied. Paste it into an email or chat and "
                                        "attach the saved request page.", parent=root)
