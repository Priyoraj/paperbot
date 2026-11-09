"""Picking up the PDF a person downloads in the ordinary browser window (no real browser)."""
import threading
import time

from paperbot.humanbrowser import HumanBrowser, ManualControl
from paperbot.resolve import Paper

PDF = b"%PDF-1.7\n" + b"x" * 3000


def make(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr("paperbot.humanbrowser.find_browser", lambda channel: "browser.exe")
    hb = HumanBrowser("msedge", tmp_path, control=ManualControl(), timeout_s=10, **kw)
    opened = []
    monkeypatch.setattr(hb, "_open", opened.append)
    monkeypatch.setattr(hb, "_our_pids", lambda: [])
    hb.inbox.mkdir(parents=True, exist_ok=True)
    hb._proxy = None
    return hb, opened


def later(seconds, fn):
    threading.Timer(seconds, fn).start()


def test_picks_up_a_finished_pdf_and_ignores_partial_and_non_pdf(tmp_path, monkeypatch):
    hb, opened = make(tmp_path, monkeypatch)
    later(0.3, lambda: (hb.inbox / "page.html").write_text("<html>"))
    later(0.3, lambda: (hb.inbox / "paper.pdf.crdownload").write_bytes(PDF[:100]))
    later(0.6, lambda: (hb.inbox / "notes.pdf").write_bytes(b"not a pdf at all"))
    later(1.0, lambda: (hb.inbox / "paper.pdf").write_bytes(PDF))
    data = hb.capture(Paper(doi="10.1000/x"), None)
    assert data == PDF
    assert opened == ["https://doi.org/10.1000/x"]


def test_skip_and_same_file_twice(tmp_path, monkeypatch):
    hb, _ = make(tmp_path, monkeypatch)
    later(0.5, hb.control.skip.set)
    t = time.monotonic()
    assert hb.capture(Paper(doi="10.1000/a"), None) is None and time.monotonic() - t < 5
    later(0.3, lambda: (hb.inbox / "a.pdf").write_bytes(PDF))
    assert hb.capture(Paper(doi="10.1000/b"), None) == PDF
    # the same PDF downloaded again (e.g. a double click) must not count for the next paper
    later(0.3, lambda: (hb.inbox / "a (1).pdf").write_bytes(PDF))
    later(1.5, hb.control.stop.set)
    assert hb.capture(Paper(doi="10.1000/c"), None) is None


def test_prefs_send_downloads_to_the_watched_folder(tmp_path, monkeypatch):
    import json
    hb, _ = make(tmp_path, monkeypatch)
    hb._write_prefs()
    prefs = json.loads((hb.profile / "Default" / "Preferences").read_text())
    assert prefs["download"]["default_directory"] == str(hb.inbox)
    assert prefs["download"]["prompt_for_download"] is False
    assert prefs["plugins"]["always_open_pdf_externally"] is True
