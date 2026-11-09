import subprocess
from datetime import time

import pytest

from paperbot.config import hours_contain
from paperbot.mail import pack_attachments, reply_subject, sender_auth_failed
from paperbot.netsafe import is_public_http_url
from paperbot.publisher import find_embedded_pdf, guess_pdf_urls, html_is_challenge
from paperbot.resolve import parse_meta, title_similarity
from paperbot.vpn import Vpn, VpnError, vpn_session


# ---- link safety -----------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://localhost/x", "http://127.0.0.1/", "http://10.0.0.5/admin", "http://192.168.1.1/",
    "http://169.254.169.254/latest", "http://[::1]/", "file:///C:/Windows/win.ini",
    "ftp://example.org/x", "http://intranet.local/", "https://user:pw@example.org/"])
def test_private_or_odd_urls_rejected(url):
    assert not is_public_http_url(url, resolver=lambda h: ["93.184.216.34"])


def test_public_url_allowed_but_not_if_dns_points_inside():
    assert is_public_http_url("https://doi.org/10.1/x", resolver=lambda h: ["93.184.216.34"])
    assert not is_public_http_url("https://evil.example/", resolver=lambda h: ["10.1.2.3"])


# ---- small helpers ------------------------------------------------------------
def test_vpn_hours_window():
    assert hours_contain("00:00-24:00", time(13, 0))
    assert hours_contain("22:00-07:00", time(23, 30)) and hours_contain("22:00-07:00", time(6, 0))
    assert not hours_contain("22:00-07:00", time(12, 0))
    assert hours_contain("09:00-17:00", time(9, 0)) and not hours_contain("09:00-17:00", time(17, 0))


def test_pack_attachments_respects_size():
    files = [("a", b"x" * 6), ("b", b"x" * 6), ("c", b"x" * 3), ("d", b"x" * 20)]
    groups = pack_attachments(files, 10)
    assert [[n for n, _ in g] for g in groups] == [["a"], ["b", "c"], ["d"]]


def test_reply_subject():
    assert reply_subject("Paper request") == "Re: Paper request"
    assert reply_subject("RE: Paper request") == "RE: Paper request"


def test_dmarc_fail_detected():
    from email.message import EmailMessage
    m = EmailMessage()
    m["Authentication-Results"] = "mx.google.com; dkim=fail; spf=softfail; dmarc=fail (p=NONE)"
    assert sender_auth_failed(m)
    m2 = EmailMessage()
    m2["Authentication-Results"] = "mx.google.com; dkim=pass; spf=pass; dmarc=pass"
    assert not sender_auth_failed(m2)


def test_title_similarity_and_meta():
    assert title_similarity("Attention is all you need", "Attention Is All You Need") == 1.0
    assert title_similarity("Deep learning", "Deep learning: a critical review") < 0.92
    assert title_similarity("Attention is all you need", "Is Attention All You Need?") < 0.92
    assert title_similarity("Deep residual learning for image recogniton",  # typo
                            "Deep Residual Learning for Image Recognition") >= 0.92
    meta = parse_meta('<meta name="citation_doi" content="doi:10.1000/ABC.1">'
                      '<meta name="citation_title" content="A Title">')
    assert meta == {"doi": "10.1000/abc.1", "title": "A Title"}


def test_publisher_helpers():
    assert guess_pdf_urls("https://www.sciencedirect.com/science/article/pii/S0893608014002135",
                          None)[0].endswith("/pii/S0893608014002135/pdfft?isDTMRedir=true"
                                            "&download=true")
    assert "arnumber=7780459" in guess_pdf_urls("https://ieeexplore.ieee.org/document/7780459",
                                                None)[0]
    assert guess_pdf_urls("https://link.springer.com/article/10.1007/x", "10.1007/x") == [
        "https://link.springer.com/content/pdf/10.1007/x.pdf"]
    html = ('<html><head><meta http-equiv="refresh" content="0;url=/pdf/abc.pdf"></head>'
            '<iframe src="https://cdn.pub.org/view?file=1"></iframe></html>')
    assert find_embedded_pdf(html, "https://pub.org/a/b")[:2] == [
        "https://pub.org/pdf/abc.pdf", "https://cdn.pub.org/view?file=1"]
    assert html_is_challenge("<title>Just a moment...</title>")
    assert html_is_challenge('<iframe src="https://www.google.com/recaptcha/api2/anchor">')
    assert not html_is_challenge("<title>Deep learning | Nature</title><p>Abstract</p>")


# ---- VPN control -----------------------------------------------------------------
class FakeRasdial:
    def __init__(self, connect_code=0, connected=False):
        self.connected = connected
        self.connect_code = connect_code
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(args)
        if args == ["rasdial"]:
            out = "Connected to\nInstitute VPN\nCommand completed successfully.\n" if self.connected \
                else "No connections\nCommand completed successfully.\n"
            return subprocess.CompletedProcess(args, 0, out.encode(), b"")
        if args[-1] == "/disconnect":
            self.connected = False
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if self.connect_code:
            msg = f"Remote Access error {self.connect_code} - failed".encode()
            return subprocess.CompletedProcess(args, self.connect_code, msg, b"")
        self.connected = True
        return subprocess.CompletedProcess(args, 0, b"Command completed successfully.", b"")


def test_vpn_connects_then_disconnects_only_if_we_connected(monkeypatch):
    monkeypatch.setattr("paperbot.vpn.time.sleep", lambda s: None)
    fake = FakeRasdial()
    with vpn_session(Vpn("Institute VPN", runner=fake)):
        assert fake.connected
    assert not fake.connected
    assert ["rasdial", "Institute VPN", "/disconnect"] in fake.calls

    already = FakeRasdial(connected=True)
    with vpn_session(Vpn("Institute VPN", runner=already)):
        pass
    assert already.connected  # the owner's own VPN session is left alone
    assert not any(c[-1] == "/disconnect" for c in already.calls)


def test_vpn_error_is_explained(monkeypatch):
    monkeypatch.setattr("paperbot.vpn.time.sleep", lambda s: None)
    with pytest.raises(VpnError, match="691.*username/password"):
        Vpn("Institute VPN", runner=FakeRasdial(connect_code=691)).connect()


def test_vpn_uses_windows_saved_sign_in_first(monkeypatch):
    monkeypatch.setattr("paperbot.vpn.time.sleep", lambda s: None)
    fake = FakeRasdial()

    def dialer(name):  # stands in for ras_dial_saved
        fake.connected = True
        return 0

    Vpn("Institute VPN", runner=fake, dialer=dialer).connect()
    assert fake.connected
    assert not any(c[:2] == ["rasdial", "Institute VPN"] for c in fake.calls)  # no rasdial dialing

    # nothing saved in Windows -> credentials stored with PaperBot are used via rasdial
    fake2 = FakeRasdial()
    Vpn("Institute VPN", runner=fake2, dialer=lambda n: -1,
        credentials=lambda: ("user", "pw")).connect()
    assert ["rasdial", "Institute VPN", "user", "pw"] in fake2.calls and fake2.connected

    with pytest.raises(VpnError, match="no saved sign-in"):
        Vpn("Institute VPN", runner=FakeRasdial(), dialer=lambda n: -1).connect()


def test_vpn_waits_until_windows_routes_through_it(monkeypatch):
    monkeypatch.setattr("paperbot.vpn.time.sleep", lambda s: None)
    fake = FakeRasdial()
    # home address before dialing, still home twice after, then the VPN's address
    seq = iter(["192.168.1.5", "192.168.1.5", "192.168.1.5", "10.8.0.6"])
    Vpn("Institute VPN", runner=fake, source_ip=lambda: next(seq)).connect()
    assert fake.connected and next(seq, None) is None  # it polled until the switch


def test_vpn_gives_up_and_disconnects_if_routing_never_switches(monkeypatch):
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr("paperbot.vpn.time.sleep", lambda s: None)
    monkeypatch.setattr("paperbot.vpn.time.monotonic", lambda: next(clock))
    fake = FakeRasdial()
    with pytest.raises(VpnError, match="home connection"):
        Vpn("Institute VPN", runner=fake, source_ip=lambda: "192.168.1.5").connect()
    assert not fake.connected  # not left half-working
