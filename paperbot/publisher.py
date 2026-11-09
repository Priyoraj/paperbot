"""Fetch PDFs from publisher websites through a real browser (Edge/Chrome via Playwright).

Used while the institute VPN is up, so the publisher sees the institute's IP address.
There are deliberately no bot-detection workarounds here: when a site answers with a
CAPTCHA / "verify you are human" page, the paper is marked "blocked" and handed to the
owner (`paperbot manual`), who solves it in a visible browser window.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .ipv4proxy import IPv4Proxy
from .resolve import Paper
from .sources import is_pdf

log = logging.getLogger(__name__)

CHALLENGE_TITLE_RE = re.compile(
    r"just a moment|attention required|verify you are human|are you a robot|security check|"
    r"captcha|access denied|unusual traffic|please verify|bot (check|detection)|"
    r"unable to load page", re.I)                              # IEEE Xplore's bot block
CHALLENGE_TEXT_RE = re.compile(
    r"verify you are (a )?human|are you a robot|complete the security check|checking your browser|"
    r"press (&|and) hold|unusual traffic from your|confirm you are not a robot|"
    r"enable javascript and cookies to continue|"
    r"problem providing the content you requested|"          # Elsevier's automation block
    r"you don'?t have permission to access|request (has been )?blocked", re.I)
BLOCKED_REASON = ("the publisher's site blocks automated downloads (or asked for a human check)")
CHALLENGE_FRAME_RE = re.compile(
    r"recaptcha|hcaptcha|challenges\.cloudflare\.com|captcha-delivery|px-captcha|arkoselabs|"
    r"funcaptcha", re.I)
NO_ACCESS_RE = re.compile(
    r"purchase (this )?(article|pdf|chapter)|buy (this )?(article|pdf|chapter)|rent this article|"
    r"access through your institution|subscribe to (this )?journal|you do not have access|"
    r"get access to (the )?full|institutional (login|sign in)|check access", re.I)
EMBED_RE = re.compile(r"<(?:iframe|embed|object)[^>]+?(?:src|data)\s*=\s*[\"']([^\"']+)[\"']", re.I)
REFRESH_RE = re.compile(r"http-equiv=[\"']refresh[\"'][^>]*url=([^\"'>]+)", re.I)
PDF_LINK_RE = re.compile(r"https?://[^\"'\s<>]+(?:\.pdf|/pdf/|pdfft|getPDF|pdfdirect|"
                         r"sciencedirectassets\.com)[^\"'\s<>]*", re.I)


@dataclass
class FetchResult:
    status: str            # ok | blocked | no_access | failed
    data: bytes | None = None
    reason: str = ""
    url: str = ""
    retryable: bool = False  # the page itself didn't load (network hiccup)


def guess_pdf_urls(page_url: str, doi: str | None) -> list[str]:
    """Known PDF addresses for big publishers, derived from the landing page / DOI."""
    u = urlparse(page_url)
    host, path = (u.hostname or "").lower(), u.path
    out: list[str] = []
    m = re.search(r"/pii/(S?[0-9X]{15,17})", path, re.I)
    if host.endswith("sciencedirect.com") and m:
        out.append(f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}"
                   "/pdfft?isDTMRedir=true&download=true")
    m = re.search(r"/document/(\d+)", path)
    if host.endswith("ieeexplore.ieee.org") and m:
        out.append(f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={m.group(1)}&ref=")
    m = re.match(r"/articles/([^/?#]+)", path)
    if host.endswith("nature.com") and m:
        out.append(f"https://www.nature.com/articles/{m.group(1)}.pdf")
    if doi:
        table = {
            "link.springer.com": f"https://link.springer.com/content/pdf/{doi}.pdf",
            "onlinelibrary.wiley.com": f"https://{host}/doi/pdfdirect/{doi}?download=true",
            "dl.acm.org": f"https://dl.acm.org/doi/pdf/{doi}",
            "tandfonline.com": f"https://www.tandfonline.com/doi/pdf/{doi}?download=true",
            "journals.sagepub.com": f"https://journals.sagepub.com/doi/pdf/{doi}?download=true",
            "pubs.acs.org": f"https://pubs.acs.org/doi/pdf/{doi}",
            "iopscience.iop.org": f"https://iopscience.iop.org/article/{doi}/pdf",
            "worldscientific.com": f"https://www.worldscientific.com/doi/pdf/{doi}",
            "emerald.com": f"https://www.emerald.com/insight/content/doi/{doi}/full/pdf",
            "pubs.aip.org": f"https://pubs.aip.org/doi/pdf/{doi}",
            "ascelibrary.org": f"https://ascelibrary.org/doi/pdf/{doi}",
        }
        for h, url in table.items():
            if host == h or host.endswith("." + h):
                out.append(url)
    return out


def find_embedded_pdf(html: str, base: str) -> list[str]:
    """PDF addresses hidden in interstitial pages (iframes, meta refresh, script redirects)."""
    found: list[str] = []
    for rx in (REFRESH_RE, EMBED_RE):
        for m in rx.finditer(html):
            found.append(urljoin(base, m.group(1).strip().replace("&amp;", "&")))
    for m in PDF_LINK_RE.finditer(html):
        found.append(m.group(0).replace("&amp;", "&").replace("\\/", "/"))
    out, seen = [], set()
    for u in found:
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:6]


def html_is_challenge(html: str) -> bool:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = m.group(1) if m else ""
    return bool(CHALLENGE_TITLE_RE.search(title) or CHALLENGE_FRAME_RE.search(html[:200_000])
                or CHALLENGE_TEXT_RE.search(html[:200_000]))


class _HostGuard:
    """Blocks browser requests to private / intranet addresses (the VPN puts us inside the
    institute network, and links come from other people)."""

    def __init__(self):
        self.cache: dict[str, bool] = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        if p.scheme in ("data", "blob", "about", "chrome-extension", "edge"):
            return True
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        host = p.hostname
        if host not in self.cache:
            try:
                ip = ipaddress.ip_address(host)
                ok = ip.is_global
            except ValueError:
                try:
                    addrs = {i[4][0] for i in socket.getaddrinfo(host, None)}
                    ok = bool(addrs) and all(
                        ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)
                except OSError:
                    ok = False
            self.cache[host] = ok
        return self.cache[host]

    def route(self, route) -> None:
        if self.allowed(route.request.url):
            route.continue_()
        else:
            log.warning("blocked request to non-public address %s", route.request.url)
            route.abort("blockedbyclient")


class PublisherFetcher:
    """Use as a context manager; one browser for the whole batch."""

    def __init__(self, browser: str = "msedge", headless: bool = True,
                 max_pdf_bytes: int = 60_000_000, delay_s: float = 15, force_ipv4: bool = True):
        self.channel = browser
        self.headless = headless
        self.max_pdf_bytes = max_pdf_bytes
        self.delay_s = delay_s
        self.force_ipv4 = force_ipv4
        self._last = 0.0
        self.guard = _HostGuard()

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        # IPv4-only relay: the VPN carries IPv4 only, so IPv6 would go around it
        self._proxy = IPv4Proxy().start() if self.force_ipv4 else None
        proxy = {"server": self._proxy.url} if self._proxy else None
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.launch(channel=self.channel, headless=self.headless,
                                                    proxy=proxy)
        except Exception:
            self._pw.stop()
            if self._proxy:
                self._proxy.stop()
            raise
        self.context = None  # one per paper, see fetch()
        return self

    def __exit__(self, *exc):
        for closer in (self.browser.close, self._pw.stop,
                       self._proxy.stop if self._proxy else (lambda: None)):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    # ---- one paper ------------------------------------------------------
    def fetch(self, paper: Paper) -> FetchResult:
        start = paper.landing_url or (f"https://doi.org/{paper.doi}" if paper.doi else None)
        if not start:
            return FetchResult("failed", reason="no DOI or link to look up")
        res = FetchResult("failed")
        for attempt in (1, 2):
            self._pace()
            self.context = self._new_context()  # fresh session per paper: no cookie cross-talk
            try:
                res = self._fetch_once(paper, start)
            finally:
                try:
                    self.context.close()
                except Exception:  # noqa: BLE001
                    pass
            if not res.retryable:
                break
            log.info("publisher page didn't load (%s); %s", res.url,
                     "retrying once" if attempt == 1 else "giving up")
        return res

    def _new_context(self):
        proxy = {"server": self._proxy.url} if self._proxy else None
        ctx = self.browser.new_context(accept_downloads=True, locale="en-US", proxy=proxy)
        ctx.route("**/*", self.guard.route)
        return ctx

    def _fetch_once(self, paper: Paper, start: str) -> FetchResult:
        from playwright.sync_api import Error as PwError
        page = self.context.new_page()
        downloads: list = []

        def on_download(d):  # Playwright needs a real function, not list.append
            downloads.append(d)

        page.on("download", on_download)
        try:
            try:
                page.goto(start, wait_until="domcontentloaded", timeout=60_000)
            except PwError as exc:
                if not downloads:
                    log.info("navigation to %s failed: %s", start, exc)
            self._settle(page)
            if downloads:
                data = self._read_download(downloads[0])
                if data:
                    return FetchResult("ok", data, url=start)
            landing_url, landing_text = page.url, self._body_text(page)
            if landing_url.startswith(("chrome-error://", "about:blank")):
                return FetchResult("failed", reason="couldn't load the publisher's page",
                                   url=landing_url, retryable=True)
            if self.page_is_challenge(page):
                return FetchResult("blocked", reason=BLOCKED_REASON, url=landing_url)
            blocked = False
            for url in self._pdf_candidates(page, paper):
                data, was_blocked = self._try_url(page, url)
                if data:
                    return FetchResult("ok", data, url=url)
                blocked = blocked or was_blocked
            if blocked:
                return FetchResult("blocked", reason=BLOCKED_REASON, url=landing_url)
            if NO_ACCESS_RE.search(landing_text):
                return FetchResult("no_access", reason="the institute doesn't seem to subscribe "
                                   "to this journal", url=landing_url)
            return FetchResult("failed", reason="couldn't find the PDF on the publisher's page",
                               url=landing_url)
        except Exception as exc:  # noqa: BLE001 - one bad page must not stop the batch
            log.exception("publisher fetch crashed for %s", start)
            return FetchResult("failed", reason=f"browser error ({exc.__class__.__name__})",
                               url=start)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- helpers --------------------------------------------------------
    def _pace(self) -> None:
        wait = self._last + self.delay_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    @staticmethod
    def _settle(page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:  # noqa: BLE001 - busy pages never go idle; that's fine
            pass

    @staticmethod
    def _body_text(page) -> str:
        try:
            return page.inner_text("body", timeout=3_000)[:20_000]
        except Exception:  # noqa: BLE001
            return ""

    def page_is_challenge(self, page) -> bool:
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = ""
        if CHALLENGE_TITLE_RE.search(title):
            return True
        if any(CHALLENGE_FRAME_RE.search(f.url or "") for f in page.frames):
            return True
        return bool(CHALLENGE_TEXT_RE.search(self._body_text(page)[:5_000]))

    def _pdf_candidates(self, page, paper: Paper) -> list[str]:
        urls: list[str] = []
        try:
            urls += page.eval_on_selector_all(
                "meta[name='citation_pdf_url'], meta[name='wkhealth_pdf_url']",
                "els => els.map(e => e.content)")
            urls += page.eval_on_selector_all(
                "link[type='application/pdf']", "els => els.map(e => e.href)")
        except Exception:  # noqa: BLE001
            pass
        urls += guess_pdf_urls(page.url, paper.doi)
        try:
            links = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.href, (e.textContent||'').trim().slice(0,80)])")
        except Exception:  # noqa: BLE001
            links = []
        for href, text in links:
            low = href.lower()
            if re.search(r"suppl|supporting|citation|\.ris|\.bib|reprints|permissions", low + text,
                         re.I):
                continue
            if "pdf" in low or re.search(r"\b(view|download|get|full[- ]text)\s*pdf\b|^pdf$",
                                         text, re.I):
                urls.append(href)
        out, seen = [], set()
        for u in urls:
            if u and u.startswith("http") and u not in seen:
                seen.add(u)
                out.append(u)
        return out[:8]

    def _api_get(self, url: str):
        """GET with the browser's cookies, following redirects by hand (each hop checked)."""
        for _ in range(8):
            if not self.guard.allowed(url):
                return None
            resp = self.context.request.get(url, timeout=90_000, max_redirects=0,
                                            headers={"Accept": "application/pdf,*/*;q=0.8"})
            if 300 <= resp.status < 400 and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                continue
            return resp
        return None

    def _try_url(self, page, url: str) -> tuple[bytes | None, bool]:
        """Returns (pdf bytes or None, saw a challenge page)."""
        from playwright.sync_api import Error as PwError
        blocked = False
        try:
            resp = self._api_get(url)
            if resp is not None:
                blocked = resp.status == 418  # IEEE answers automated clients with 418
                body = resp.body()
                if is_pdf(body) and len(body) <= self.max_pdf_bytes:
                    return body, False
                if "html" in (resp.headers.get("content-type") or ""):
                    html = body.decode("utf-8", errors="replace")
                    blocked = blocked or html_is_challenge(html)
                    for inner in find_embedded_pdf(html, resp.url):
                        r2 = self._api_get(inner)
                        if r2 is not None:
                            b2 = r2.body()
                            if is_pdf(b2) and len(b2) <= self.max_pdf_bytes:
                                return b2, False
        except PwError as exc:
            log.info("direct request failed for %s: %s", url, exc)
        data = self._open_in_browser(page, url)
        if data:
            return data, False
        return None, blocked or self.page_is_challenge(page)

    def _open_in_browser(self, page, url: str) -> bytes | None:
        """Let the browser navigate; a PDF shows up as a download or as a PDF response."""
        from playwright.sync_api import Error as PwError
        downloads: list = []
        pdf_responses: list = []

        def on_response(resp):
            if "application/pdf" in (resp.headers.get("content-type") or ""):
                pdf_responses.append(resp)

        def on_download(d):
            downloads.append(d)

        page.on("download", on_download)
        page.on("response", on_response)
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except PwError:
                pass  # "Download is starting" aborts the navigation; that's expected
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not downloads and not pdf_responses:
                page.wait_for_timeout(500)
            if downloads:
                return self._read_download(downloads[0])
            for resp in pdf_responses:
                try:
                    body = resp.body()
                except PwError:
                    r = self._api_get(resp.url)  # viewer kept the body; ask again with cookies
                    body = r.body() if r is not None else b""
                if is_pdf(body) and len(body) <= self.max_pdf_bytes:
                    return body
            for frame in page.frames[1:]:  # e.g. IEEE shows the PDF inside an iframe
                if "pdf" in (frame.url or "").lower():
                    r = self._api_get(frame.url)
                    if r is not None and is_pdf(r.body()):
                        return r.body()
        finally:
            page.remove_listener("download", on_download)
            page.remove_listener("response", on_response)
        return None

    def _read_download(self, download) -> bytes | None:
        try:
            path = download.path()
        except Exception:  # noqa: BLE001
            return None
        if not path:
            return None
        data = Path(path).read_bytes()
        return data if is_pdf(data) and len(data) <= self.max_pdf_bytes else None
