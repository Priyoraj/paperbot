"""Turn a request email into a list of things to fetch: DOIs, arXiv ids, links and titles."""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import EmailMessage
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
ARXIV_NEW = r"\d{4}\.\d{4,5}(?:v\d+)?"
ARXIV_OLD = r"[a-z][a-z\-]+(?:\.[a-z]{2})?/\d{7}(?:v\d+)?"
ARXIV_TEXT_RE = re.compile(rf"\barxiv\s*[:.]?\s*({ARXIV_NEW}|{ARXIV_OLD})", re.I)
ARXIV_URL_RE = re.compile(rf"arxiv\.org/(?:abs|pdf|html)/({ARXIV_NEW}|{ARXIV_OLD})", re.I)
QUOTED_RE = re.compile(r"[\"“”]([^\"“”\n]{12,300})[\"“”]")
PREFIXED_RE = re.compile(r"^\s*(?:paper\s+)?title\s*[:\-–]\s*(.{8,300})$", re.I)
BULLET_RE = re.compile(r"^\s*(?:[-*•]|\(?\d{1,2}[.):])\s+(.{8,300})$")
TRAILING_JUNK = ".,;:)]}>'\""

# Lines that are clearly not paper titles (greetings, sign-offs, chit-chat).
CHATTER_RE = re.compile(
    r"^(hi|hello|hey|dear|thanks|thank you|regards|best|cheers|sincerely|warm regards|"
    r"kind regards|could you|can you|would you|please|pls|i need|i want|i am|i'm|"
    r"sorry|hope|also|and |the following|following|here|this|these|sent from)\b", re.I)
REPLY_CUTOFF_RE = re.compile(
    r"^(on .{4,120} wrote:|-{2,}\s*original message\s*-{2,}|from: .+|sent from my .+|-- ?)$",
    re.I)


@dataclass(frozen=True)
class RequestItem:
    kind: str    # doi | arxiv | url | title
    value: str   # normalised DOI / arXiv id / URL / title
    raw: str     # what the requester wrote, for the reply


def clean_doi(doi: str) -> str:
    doi = unquote(doi).strip()
    doi = re.sub(r"[?#].*$", "", doi)
    # strip trailing punctuation, but keep a closing bracket that has a partner
    while doi and doi[-1] in TRAILING_JUNK:
        if doi[-1] == ")" and doi.count("(") >= doi.count(")"):
            break
        doi = doi[:-1]
    for suffix in ("/full", "/abstract", "/pdf", "/epdf", "/pdfdirect", ".pdf"):
        if doi.lower().endswith(suffix):
            doi = doi[: -len(suffix)]
    return doi.lower()


def _clean_url(url: str) -> str:
    while url and url[-1] in TRAILING_JUNK:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def item_from_url(url: str) -> RequestItem:
    """Map a link to the most specific identifier we can read straight from it."""
    m = ARXIV_URL_RE.search(url)
    if m:
        return RequestItem("arxiv", re.sub(r"v\d+$", "", m.group(1)), url)
    path = unquote(urlparse(url).path)
    host = (urlparse(url).hostname or "").lower()
    m = DOI_RE.search(path) or DOI_RE.search(unquote(url))
    if m:
        return RequestItem("doi", clean_doi(m.group(1)), url)
    m = re.match(r"/articles/([a-z0-9\-.]+)$", path, re.I)
    if host.endswith("nature.com") and m:
        return RequestItem("doi", f"10.1038/{m.group(1).lower()}", url)
    m = re.match(r"/publication/\d+_(.+)$", path)
    if host.endswith("researchgate.net") and m:
        return RequestItem("title", m.group(1).replace("_", " ").strip(), url)
    return RequestItem("url", url, url)


def strip_quoted(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if REPLY_CUTOFF_RE.match(s):
            break
        if s.startswith(">"):
            continue
        out.append(line)
    return "\n".join(out)


def parse_text(text: str, limit: int = 10) -> list[RequestItem]:
    """Extract request items from plain text, in the order they appear."""
    found: list[RequestItem] = []
    seen: set[tuple[str, str]] = set()

    def add(item: RequestItem) -> None:
        key = (item.kind, item.value.lower())
        if key not in seen and len(found) < limit:
            seen.add(key)
            found.append(item)

    title_candidates: list[str] = []
    for line in text.splitlines():
        line_items: list[RequestItem] = []
        urls = [_clean_url(u) for u in URL_RE.findall(line)]
        for url in urls:
            line_items.append(item_from_url(url))
        rest = URL_RE.sub(" ", line)
        for m in ARXIV_TEXT_RE.finditer(rest):
            line_items.append(RequestItem("arxiv", re.sub(r"v\d+$", "", m.group(1)), m.group(0)))
        rest = ARXIV_TEXT_RE.sub(" ", rest)
        for m in DOI_RE.finditer(rest):
            line_items.append(RequestItem("doi", clean_doi(m.group(1)), m.group(1)))
        rest = DOI_RE.sub(" ", rest)
        for m in QUOTED_RE.finditer(rest):
            line_items.append(RequestItem("title", _tidy_title(m.group(1)), m.group(1)))
        if line_items:
            for it in line_items:
                add(it)
            continue
        bullet = BULLET_RE.match(line)
        m = PREFIXED_RE.match(bullet.group(1) if bullet else line) or bullet
        if m:
            add(RequestItem("title", _tidy_title(m.group(1)), m.group(1).strip()))
            continue
        title_candidates.append(line.strip())

    if not found:
        # No explicit markers anywhere: plausible free-standing lines become "guess" items.
        # A guess only survives if Crossref has a near-exact title match; failed guesses
        # are dropped quietly instead of being reported back as "not found".
        for line in title_candidates:
            if _looks_like_title(line):
                add(RequestItem("guess", _tidy_title(line), line))
    return found


def _tidy_title(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().strip(TRAILING_JUNK).strip()


def _looks_like_title(line: str) -> bool:
    words = line.split()
    if not (4 <= len(words) <= 40) or CHATTER_RE.match(line):
        return False
    if line.endswith(",") or "@" in line:
        return False
    letters = sum(c.isalpha() for c in line)
    return letters >= 0.6 * len(line.replace(" ", ""))


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "td"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.quote = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        if tag == "blockquote":
            self.quote += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")) and not self.quote:
                self.parts.append(f" {href} ")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self.skip = max(0, self.skip - 1)
        if tag == "blockquote":
            self.quote = max(0, self.quote - 1)
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip and not self.quote:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    return re.sub(r"[ \t\xa0]+", " ", "".join(p.parts))


def _content(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, KeyError):
        raw = part.get_payload(decode=True) or b""
        return raw.decode("utf-8", errors="replace")


def message_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return _content(part)
    part = msg.get_body(preferencelist=("html",))
    if part is not None:
        return html_to_text(_content(part))
    return ""


def subject_remainder(subject: str, keywords: list[str]) -> str:
    low = subject.lower()
    for kw in keywords:
        i = low.find(kw)
        if i >= 0:
            return subject[i + len(kw):].lstrip(" :-–—")
    return ""


def parse_request(msg: EmailMessage, keywords: list[str], limit: int) -> list[RequestItem]:
    """Items from the subject ("Paper request: <title or DOI>") and then the body."""
    subject = str(msg.get("Subject", ""))
    rest = subject_remainder(subject, keywords)
    items: list[RequestItem] = []
    if rest:
        items = [i for i in parse_text(rest, limit) if i.kind != "guess"]
        if not items and len(rest.split()) >= 2:
            items = [RequestItem("title", _tidy_title(rest), rest)]
    body = parse_text(strip_quoted(message_text(msg)), limit)
    if items and all(i.kind == "guess" for i in body):
        body = []  # the subject said what is wanted; don't guess titles from chit-chat
    keys = {(i.kind, i.value.lower()) for i in items}
    for i in body:
        if (i.kind, i.value.lower()) not in keys and len(items) < limit:
            items.append(i)
    return items
