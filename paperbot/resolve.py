"""Work out which paper an item refers to (DOI, arXiv id, title, link) using public metadata APIs."""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

import requests

from .config import VERSION
from .netsafe import is_public_http_url
from .parse import DOI_RE, clean_doi

log = logging.getLogger(__name__)
CROSSREF = "https://api.crossref.org/works"
S2 = "https://api.semanticscholar.org/graph/v1/paper"
ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


@dataclass
class Paper:
    doi: str | None = None
    arxiv_id: str | None = None
    landing_url: str | None = None
    title: str | None = None
    authors: str | None = None
    year: int | None = None


class ResolveError(Exception):
    """reason is shown to the requester; permanent=False means 'try again next run'."""

    def __init__(self, reason: str, permanent: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent


def make_session(contact_email: str = "") -> requests.Session:
    s = requests.Session()
    ua = f"PaperBot/{VERSION}"
    if contact_email:
        ua += f" (mailto:{contact_email})"
    s.headers["User-Agent"] = ua
    return s


def norm_title(t: str) -> str:
    t = re.sub(r"<[^>]+>", " ", t or "")          # Crossref titles may contain <i> tags
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t.lower())).strip()


def title_similarity(a: str, b: str) -> float:
    """Character-level similarity, capped by word-order similarity, so that
    'Attention is all you need' does not match 'Is attention all you need?'."""
    na, nb = norm_title(a), norm_title(b)
    chars = SequenceMatcher(None, na, nb).ratio()
    words = SequenceMatcher(None, na.split(), nb.split()).ratio()
    # one typo in a 6-word title still gives words ~0.83; allow that, punish reordering
    return chars if words >= 0.8 else min(chars, words)


def _authors_crossref(authors) -> str | None:
    if not authors:
        return None
    first = authors[0].get("family") or authors[0].get("name") or ""
    return f"{first} et al." if len(authors) > 1 else first or None


def _year_crossref(rec) -> int | None:
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (rec.get(key) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None


class Resolver:
    def __init__(self, session: requests.Session | None = None, timeout: float = 30):
        self.http = session or make_session()
        self.timeout = timeout

    # ---- plumbing -----------------------------------------------------
    def _get(self, url: str, **kw) -> requests.Response:
        for attempt in range(3):
            try:
                r = self.http.get(url, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                raise ResolveError(f"lookup service unreachable ({exc.__class__.__name__})",
                                   permanent=False) from exc
            if r.status_code != 429 or attempt == 2:
                break
            retry = r.headers.get("Retry-After", "")
            time.sleep(min(float(retry), 10) if retry.isdigit() else 3 * (attempt + 1))
        if r.status_code == 429 or r.status_code >= 500:
            raise ResolveError(f"lookup service busy (HTTP {r.status_code})", permanent=False)
        return r

    # ---- entry point --------------------------------------------------
    def resolve(self, kind: str, value: str) -> Paper:
        if kind == "doi":
            return self.by_doi(value)
        if kind == "arxiv":
            return self.by_arxiv(value)
        if kind == "url":
            return self.by_url(value)
        if kind in ("title", "guess"):
            return self.by_title(value, threshold=0.95 if kind == "guess" else 0.92)
        raise ResolveError(f"unknown item type {kind}")

    # ---- DOI ----------------------------------------------------------
    def by_doi(self, doi: str) -> Paper:
        doi = clean_doi(doi)
        m = re.match(r"10\.48550/arxiv\.(.+)$", doi, re.I)
        if m:
            return self.by_arxiv(m.group(1))
        r = self._get(f"{CROSSREF}/{quote(doi, safe='/')}")
        if r.status_code == 200:
            rec = r.json()["message"]
            return Paper(doi=doi, title=(rec.get("title") or [None])[0],
                         authors=_authors_crossref(rec.get("author")), year=_year_crossref(rec))
        # Not a Crossref DOI: check the DOI exists at all (DataCite, mEDRA, ...).
        r = self._get(f"https://doi.org/api/handles/{quote(doi, safe='/')}")
        if r.status_code == 200 and r.json().get("responseCode") == 1:
            return Paper(doi=doi)
        raise ResolveError("this DOI doesn't exist (typo?)")

    # ---- arXiv --------------------------------------------------------
    def by_arxiv(self, arxiv_id: str) -> Paper:
        r = self._get(ARXIV_API, params={"id_list": arxiv_id, "max_results": 1})
        if r.status_code != 200:
            raise ResolveError(f"arXiv lookup failed (HTTP {r.status_code})", permanent=False)
        root = ET.fromstring(r.content)
        entry = root.find("a:entry", ATOM)
        if entry is None or entry.find("a:title", ATOM) is None or \
                "api/errors" in (entry.findtext("a:id", "", ATOM)):
            raise ResolveError("no arXiv paper with this id")
        names = [a.findtext("a:name", "", ATOM) for a in entry.findall("a:author", ATOM)]
        first = names[0].split()[-1] if names and names[0] else None
        published = entry.findtext("a:published", "", ATOM)
        return Paper(arxiv_id=arxiv_id,
                     doi=(entry.findtext("arxiv:doi", "", ATOM) or None),
                     title=re.sub(r"\s+", " ", entry.findtext("a:title", "", ATOM)).strip(),
                     authors=(f"{first} et al." if len(names) > 1 else first),
                     year=int(published[:4]) if published[:4].isdigit() else None)

    # ---- title --------------------------------------------------------
    def by_title(self, title: str, threshold: float = 0.92) -> Paper:
        r = self._get(CROSSREF, params={
            "query.bibliographic": title, "rows": 10,
            "select": "DOI,title,author,issued,published-print,published-online,type,"
                      "is-referenced-by-count"})
        matches: list[tuple[float, dict]] = []
        if r.status_code == 200:
            for rec in r.json()["message"].get("items", []):
                score = title_similarity(title, (rec.get("title") or [""])[0])
                # prefer the journal/proceedings record over preprint copies with the same title
                if rec.get("type") in ("posted-content", "peer-review"):
                    score -= 0.02
                if score >= threshold:
                    matches.append((score, rec))
        if len(matches) == 1:
            return self._crossref_paper(matches[0][1])
        # Nothing in Crossref (e.g. an arXiv-only preprint), or several records share this
        # title (reprints, mirrors, book chapters): Semantic Scholar knows the canonical paper.
        try:
            paper = self._s2_match(title, threshold)
        except ResolveError:
            if not matches:
                raise
            paper = None
        if paper:
            return paper
        if matches:  # still ambiguous: the most cited record is the canonical one
            best = max(matches, key=lambda m: (round(m[0], 2),
                                               m[1].get("is-referenced-by-count") or 0))
            return self._crossref_paper(best[1])
        raise ResolveError("couldn't identify a paper with this exact title - "
                           "please send the DOI or a link")

    @staticmethod
    def _crossref_paper(rec: dict) -> Paper:
        return Paper(doi=rec["DOI"].lower(), title=(rec.get("title") or [None])[0],
                     authors=_authors_crossref(rec.get("author")), year=_year_crossref(rec))

    def _s2_match(self, title: str, threshold: float) -> Paper | None:
        r = self._get(f"{S2}/search/match",
                      params={"query": title, "fields": "title,externalIds,year,authors"})
        if r.status_code != 200 or not r.json().get("data"):
            return None
        rec = r.json()["data"][0]
        if title_similarity(title, rec.get("title", "")) < threshold:
            return None
        ids = rec.get("externalIds") or {}
        names = [a.get("name", "") for a in rec.get("authors") or []]
        first = names[0].split()[-1] if names and names[0] else None
        paper = Paper(doi=(ids.get("DOI") or "").lower() or None, arxiv_id=ids.get("ArXiv"),
                      title=rec.get("title"),
                      authors=(f"{first} et al." if len(names) > 1 else first),
                      year=rec.get("year"))
        return paper if (paper.doi or paper.arxiv_id) else None

    # ---- link ---------------------------------------------------------
    def by_url(self, url: str) -> Paper:
        if not is_public_http_url(url):
            raise ResolveError("this link isn't a public web address, so it was skipped")
        host = (urlparse(url).hostname or "").lower()
        m = re.search(r"/pii/(S?[0-9X]{15,17})", url, re.I)
        if host.endswith("sciencedirect.com") and m:
            r = self._get(CROSSREF, params={"filter": f"alternative-id:{m.group(1)}", "rows": 1})
            if r.status_code == 200 and r.json()["message"].get("items"):
                return self.by_doi(r.json()["message"]["items"][0]["DOI"])
        meta = self._page_meta(url)
        doi = meta.get("doi")
        if doi:
            try:
                paper = self.by_doi(doi)
                paper.landing_url = url
                return paper
            except ResolveError as exc:
                if not exc.permanent:
                    raise
        if meta.get("title"):
            try:
                paper = self.by_title(meta["title"], threshold=0.93)
                paper.landing_url = url
                return paper
            except ResolveError:
                pass
        # Unknown page: the publisher step will open it directly and look for a PDF link.
        return Paper(landing_url=url, title=meta.get("title"))

    def _page_meta(self, url: str) -> dict[str, str]:
        """Read <meta> tags of a landing page. Redirects are followed by hand so that every
        hop is checked: a public link must not bounce us into a private network."""
        try:
            for _ in range(6):
                if not is_public_http_url(url):
                    return {}
                r = self.http.get(url, timeout=self.timeout, stream=True, allow_redirects=False,
                                  headers={"Accept": "text/html"})
                if r.is_redirect and r.headers.get("Location"):
                    url = requests.compat.urljoin(url, r.headers["Location"])
                    r.close()
                    continue
                body = r.raw.read(2_000_000, decode_content=True)
                r.close()
                break
            else:
                return {}
        except requests.RequestException:
            return {}
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
            return {}
        return parse_meta(body.decode(r.encoding or "utf-8", errors="replace"))


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        name = (a.get("name") or a.get("property") or "").lower()
        content = a.get("content", "").strip()
        if not content:
            return
        if name in ("citation_doi", "prism.doi", "dc.identifier", "bepress_citation_doi") and \
                "doi" not in self.meta:
            m = DOI_RE.search(content)
            if m:
                self.meta["doi"] = clean_doi(m.group(1))
        elif name in ("citation_title", "dc.title") and "title" not in self.meta:
            self.meta["title"] = content
        elif name == "citation_pdf_url" and "pdf" not in self.meta:
            self.meta["pdf"] = content


def parse_meta(html: str) -> dict[str, str]:
    p = _MetaParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed pages are common; take what we got
        pass
    return p.meta
