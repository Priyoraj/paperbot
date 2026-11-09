"""Open-access copies (no VPN needed): Unpaywall, Semantic Scholar, arXiv, Europe PMC."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .netsafe import is_public_http_url
from .resolve import Paper

log = logging.getLogger(__name__)

PUBLISHED = "publishedVersion"


@dataclass
class Candidate:
    url: str
    version: str      # publishedVersion | acceptedVersion | submittedVersion | unknown
    source: str       # unpaywall | semantic_scholar | arxiv | europepmc


class OpenAccess:
    def __init__(self, session: requests.Session, contact_email: str = "",
                 max_pdf_bytes: int = 60_000_000, timeout: float = 60):
        self.http = session
        self.contact_email = contact_email
        self.max_pdf_bytes = max_pdf_bytes
        self.timeout = timeout

    def candidates(self, paper: Paper) -> list[Candidate]:
        out: list[Candidate] = []
        if paper.doi:
            out += self._unpaywall(paper.doi)
            out += self._semantic_scholar(paper.doi, paper)
        if paper.arxiv_id:
            out.append(Candidate(f"https://arxiv.org/pdf/{paper.arxiv_id}", "submittedVersion",
                                 "arxiv"))
        seen, unique = set(), []
        for c in out:
            if c.url not in seen:
                seen.add(c.url)
                unique.append(c)
        return unique

    def _unpaywall(self, doi: str) -> list[Candidate]:
        if not self.contact_email:
            return []
        try:
            r = self.http.get(f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}",
                              params={"email": self.contact_email}, timeout=30)
        except requests.RequestException as exc:
            log.warning("unpaywall: %s", exc)
            return []
        if r.status_code != 200:
            return []
        out = []
        for loc in r.json().get("oa_locations") or []:
            if loc.get("url_for_pdf"):
                out.append(Candidate(loc["url_for_pdf"], loc.get("version") or "unknown",
                                     "unpaywall"))
        return out

    def _semantic_scholar(self, doi: str, paper: Paper) -> list[Candidate]:
        try:
            r = self.http.get(
                f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi, safe='/')}",
                params={"fields": "openAccessPdf,externalIds"}, timeout=30)
        except requests.RequestException as exc:
            log.warning("semantic scholar: %s", exc)
            return []
        if r.status_code != 200:
            return []
        data = r.json()
        ids = data.get("externalIds") or {}
        if ids.get("ArXiv") and not paper.arxiv_id:
            paper.arxiv_id = ids["ArXiv"]
        out = []
        pdf = data.get("openAccessPdf") or {}
        if pdf.get("url"):
            status = (pdf.get("status") or "").upper()
            version = PUBLISHED if status in ("GOLD", "HYBRID", "BRONZE") else "unknown"
            out.append(Candidate(pdf["url"], version, "semantic_scholar"))
        if ids.get("PubMedCentral"):
            out.append(Candidate(
                f"https://europepmc.org/articles/PMC{ids['PubMedCentral']}?pdf=render",
                PUBLISHED, "europepmc"))
        return out

    def download(self, url: str) -> bytes | None:
        """Plain HTTP download; returns PDF bytes or None (never raises for HTTP problems)."""
        return download_pdf(self.http, url, self.max_pdf_bytes, self.timeout)


def download_pdf(http: requests.Session, url: str, max_bytes: int, timeout: float) -> bytes | None:
    try:
        for _ in range(8):  # follow redirects by hand so every hop is checked
            if not is_public_http_url(url):
                return None
            r = http.get(url, timeout=timeout, stream=True, allow_redirects=False,
                         headers={"Accept": "application/pdf,*/*;q=0.8"})
            if r.is_redirect and r.headers.get("Location"):
                url = requests.compat.urljoin(url, r.headers["Location"])
                r.close()
                continue
            break
        else:
            return None
        if r.status_code != 200:
            r.close()
            return None
        buf = bytearray()
        checked = False
        for chunk in r.iter_content(256 * 1024):
            buf += chunk
            if not checked and len(buf) >= 1024:
                if not is_pdf(bytes(buf)):
                    r.close()
                    return None
                checked = True
            if len(buf) > max_bytes:
                r.close()
                log.warning("PDF at %s is larger than %d bytes; skipped", url, max_bytes)
                return None
        data = bytes(buf)
        return data if is_pdf(data) else None
    except requests.RequestException as exc:
        log.info("download failed %s: %s", url, exc)
        return None


def is_pdf(data: bytes) -> bool:
    """PDF header must appear within the first 1024 bytes (PDF spec)."""
    return b"%PDF-" in data[:1024]
