"""The colleague request page: one self-contained HTML file with the owner's details baked
in. Colleagues open it in any browser (nothing to install), paste DOIs/links/titles, and it
prepares the request email in their own Gmail / Outlook / mail app."""
from __future__ import annotations

import json
from pathlib import Path

from .config import Config, ConfigError

TEMPLATE = Path(__file__).resolve().parent / "assets" / "request_page.html"


def page_config(cfg: Config) -> dict:
    if not cfg.mail_username:
        raise ConfigError("fill in the mailbox address in Settings first")
    keyword = cfg.subject_keywords[0] if cfg.subject_keywords else "paper request"
    return {"to": cfg.mail_username,
            "name": cfg.owner_name or cfg.mail_username.split("@")[0],
            "subject": keyword[:1].upper() + keyword[1:]}


def build(cfg: Config) -> str:
    blob = json.dumps(page_config(cfg), ensure_ascii=True).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8")
    start, end = html.index("/*CONFIG*/"), html.index("/*END*/")
    return html[:start] + "/*CONFIG*/" + blob + html[end:]


def save(cfg: Config, path: Path) -> Path:
    path = Path(path)
    path.write_text(build(cfg), encoding="utf-8")
    return path


def file_name(cfg: Config) -> str:
    name = "".join(c for c in page_config(cfg)["name"] if c.isalnum() or c in " ._-").strip()
    return f"Request papers from {name or 'me'}.html"


def invitation(cfg: Config) -> str:
    c = page_config(cfg)
    return (f"Hi! I've set up a quick way to request papers from me.\n\n"
            f"Open the attached page \"{file_name(cfg)}\" in your browser, paste DOIs, links "
            f"or titles, and press \"Send with Gmail\" (or Outlook). The PDFs come back to you "
            f"by email, usually within 15 minutes. The first time, I just confirm it's you.\n\n"
            f"Prefer plain email? Write to {c['to']} with the subject \"{c['subject']}\" and "
            f"one DOI or link per line.\n")
