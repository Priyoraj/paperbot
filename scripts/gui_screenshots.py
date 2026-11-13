"""Developer tool: open the desktop app on DEMO data (a throwaway folder, never the real
mailbox, VPN or settings) and save a screenshot of every screen.

    .venv\\Scripts\\python scripts\\gui_screenshots.py <output-folder> [--theme dark]
        [--text-size 125] [--only home,settings]

--theme: system, light, dark, paper, midnight or contrast (--dark and --light still work).
--only: just these screens (names as in the file names it writes).
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
OUT.mkdir(parents=True, exist_ok=True)
DEMO = Path(tempfile.mkdtemp(prefix="paperbot-demo-"))
os.environ["PAPERBOT_HOME"] = str(DEMO)
os.environ["PAPERBOT_MAIL_PASSWORD"] = "demo"  # so the demo looks set up; nothing is sent

ROOT = Path(__file__).resolve().parent.parent
(DEMO / "config.toml").write_text((ROOT / "config.example.toml").read_text(encoding="utf-8"),
                                  encoding="utf-8")

from paperbot import config as config_mod  # noqa: E402
from paperbot.db import DB, now_iso  # noqa: E402

def option(name: str, default: str) -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


THEME = option("--theme", "dark" if "--dark" in sys.argv else
               "light" if "--light" in sys.argv else "system")
TEXT_SIZE = int(option("--text-size", "100"))
ONLY = [s for s in option("--only", "").split(",") if s]

for section, key, value in [("owner", "name", "Priya"), ("owner", "email", "priya@example.com"),
                            ("mail", "username", "priya.papers@example.com"),
                            ("access", "auto_approve_domains", ["college.edu"]),
                            ("look", "theme", THEME), ("look", "text_size", TEXT_SIZE)]:
    config_mod.set_value(section, key, value, DEMO / "config.toml")

db = DB(DEMO / "data" / "paperbot.db")
pdf = DEMO / "data" / "pdfs" / "big.pdf"
pdf.parent.mkdir(parents=True, exist_ok=True)
pdf.write_bytes(b"%PDF-1.7 demo")
db.add_person("asha.rao@uni.edu", "Asha Rao", "approved")
db.add_person("ravi@college.edu", "Ravi Kumar", "approved")
db.add_person("new.person@college.edu", "Meera N", "pending")
db.add_person("offers@spam.biz", "", "denied")
day = datetime.now().replace(microsecond=0)


def req(mid, who, name, status, items, hours_ago):
    rid = db.add_request(mid, who, name, "Paper request", "", status)
    db.conn.execute("UPDATE requests SET created_at=? WHERE id=?",
                    ((day - timedelta(hours=hours_ago)).isoformat(), rid))
    for raw, kind, st, extra in items:
        iid = db.add_item(rid, raw, kind, raw)
        db.update_item(iid, status=st, **extra)
    db.conn.commit()


ok = {"fetched_at": now_iso(), "sent_at": now_iso(), "reported": 1}
req("<1@demo>", "asha.rao@uni.edu", "Asha Rao", "done", [
    ("10.1038/nature14539", "doi", "sent", dict(ok, title="Deep learning", authors="LeCun et al.",
                                               year=2015, source="publisher")),
    ("arXiv:1706.03762", "arxiv", "sent", dict(ok, title="Attention Is All You Need",
                                              authors="Vaswani et al.", year=2017,
                                              source="arxiv"))], 5)
req("<2@demo>", "ravi@college.edu", "Ravi Kumar", "queued", [
    ("10.1007/978-3-319-10590-1_53", "doi", "sent", dict(
        ok, title="Visualizing and Understanding Convolutional Networks", authors="Zeiler et al.",
        year=2014, source="publisher")),
    ("10.1016/j.neunet.2014.09.003", "doi", "manual", dict(
        title="Deep learning in neural networks: An overview", authors="Schmidhuber", year=2015,
        reason="the publisher's site needs a person to download it", reported=1))], 3)
req("<3@demo>", "new.person@college.edu", "Meera N", "held", [
    ("10.1109/cvpr.2016.90", "doi", "new", {})], 1)
req("<4@demo>", "asha.rao@uni.edu", "Asha Rao", "queued", [
    ("10.1109/5.726791", "doi", "manual", dict(
        title="Gradient-based learning applied to document recognition", authors="Lecun et al.",
        year=1998, reason="the publisher's site needs a person to download it", reported=1)),
    ("10.1007/978-0-387-84858-7", "doi", "failed", dict(
        title="The Elements of Statistical Learning", authors="Hastie et al.", year=2009,
        pdf_path=str(pdf), owner_notified=1, reported=1,
        reason="too large to email (31 MB); Priya will share it another way"))], 0.5)
req("<5@demo>", "offers@spam.biz", "", "denied", [("10.9999/x", "doi", "new", {})], 26)
for _ in range(22):  # past the colleagues' share (20 of 30): shows the warning state
    db.log_download("publisher", "asha.rao@uni.edu")

logs = DEMO / "data" / "logs"
logs.mkdir(parents=True, exist_ok=True)
stamp = day.strftime("%Y-%m-%d")
(logs / "paperbot.log").write_text("\n".join(f"{stamp} {t},000 {lvl} paperbot.pipeline: {m}" for t, lvl, m in [
    ("09:10:02", "INFO", "request 1 from asha.rao@uni.edu: 2 item(s), queued"),
    ("09:10:40", "INFO", "connecting VPN Institute VPN"),
    ("09:10:46", "INFO", "internet traffic now leaves through the VPN (local address 10.8.0.6)"),
    ("09:11:15", "INFO", "item 1 fetched from publisher (publishedVersion, 2895 KB)"),
    ("09:11:16", "INFO", "sent 'Re: Paper request' to asha.rao@uni.edu"),
    ("11:02:09", "INFO", "item 4 publisher blocked (the publisher's site blocks automated downloads)"),
    ("13:40:31", "WARNING", "approval prompt sent for new.person@college.edu"),
]) + "\n", encoding="utf-8")


def grab_png(x: int, y: int, w: int, h: int) -> bytes:
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hdc = user32.GetDC(0)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020)
    header = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.c_char_p(header + b"\0" * 1024), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(0, hdc)
    raw = buf.raw
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
    rows = b"".join(b"\x00" + bytes(rgb[i * w * 3:(i + 1) * w * 3]) for i in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


from paperbot.gui import base  # noqa: E402

base.make_dpi_aware()
import tkinter as tk  # noqa: E402

from paperbot import notify  # noqa: E402
from paperbot.gui import window as gui_window  # noqa: E402
from paperbot.gui.window import App  # noqa: E402
from paperbot.sync import NoSync  # noqa: E402

gui_window.make_sync = lambda cfg, db: NoSync()   # the demo never looks at a real mailbox
notify.show_toast = lambda *a, **kw: None         # ...and its demo papers pop up no notification
notify.windows_setting = lambda: "Enabled"        # ...whatever Windows on this computer says
notify.register = lambda: None

root = tk.Tk()
app = App(root)
root.attributes("-topmost", True)
screens = ["home", "needs", "needs-waiting", "people", "requests", "try", "settings",
           "settings-custom", "share", "home-together", "settings-together"]
if ONLY:                # also "settings@midnight" / "home@125": switch theme or text size in
    screens = ONLY      # the running window first (as Settings > Appearance does), then show
suffix = "" if THEME == "system" and TEXT_SIZE == 100 else f"-{THEME}" + (
    "" if TEXT_SIZE == 100 else f"-{TEXT_SIZE}")


def show(i: int) -> None:
    if i >= len(screens):
        root.destroy()
        return
    if "@" in screens[i]:              # a change made while the app is open
        page, change = screens[i].split("@")
        if change.isdigit():
            app.set_text_size(int(change))
        else:
            app.set_theme(change)
        app.show(page)
        app.refresh_all()
        root.after(1800, lambda: shoot(i))
        return
    if screens[i] == "needs-waiting":  # what the tab looks like while a browser window is open
        tab = app.tabs["needs"]
        app.show("needs")
        tab.guide.configure(text="[1/2] Lecun et al. (1998) \"Gradient-based learning applied to "
                                 "document recognition\" (for asha.rao@uni.edu): a browser window "
                                 "is open. Solve the check there if one appears, then download "
                                 "the PDF - PaperBot picks it up.")
        tab.waiting.pack(fill="x", pady=(app.px(8), 0))
        root.after(1800, lambda: shoot(i))
        return
    if screens[i] == "settings-custom":  # switch to custom limits above the recommendation
        tab = app.tabs["settings"]
        app.show("settings")
        tab.vars[("access", "use_recommended_limits")][1].set(False)
        tab._sync_limits(user_toggled=True)
        tab.vars[("access", "max_papers_per_day")][1].set("60")
        tab.vars[("access", "max_papers_per_hour")][1].set("20")
        root.after(1800, lambda: shoot(i))
        return
    if screens[i] == "home-together":  # PaperBot on a second computer has joined in
        db.set_meta(node="a1b2c3", joined="1", synced_at=now_iso())
        db.touch_computer("OFFICE-PC-a1b2c3", "OFFICE-PC", True)
        db.touch_computer("LAPTOP-d4e5f6", "LAPTOP", True)
        app.started_here = True        # no "this computer has joined" dialog in the demo
        app.show("home")
        app.refresh_all()
        root.after(1800, lambda: shoot(i))
        return
    if screens[i] == "settings-together":  # Settings, scrolled to "More than one computer"
        tab = app.tabs["settings"]
        app.show("settings")
        tab.load_values()
        app.refresh_all()
        root.update()
        top = tab.together_status.winfo_y() - app.px(270)      # with "Notifications" above it
        tab.scroll.canvas.yview_moveto(max(0, top) / max(1, tab.scroll.body.winfo_height()))
        root.after(1800, lambda: shoot(i))
        return
    app.show(screens[i])
    app.refresh_all()
    if screens[i] == "requests":
        tree = app.tabs["requests"].tree
        kids = tree.get_children()
        if len(kids) > 1:
            tree.selection_set(kids[1])
    root.after(1800, lambda: shoot(i))


def shoot(i: int) -> None:
    root.update()
    from ctypes import wintypes
    rect = wintypes.RECT()
    ctypes.windll.user32.GetParent.restype = ctypes.c_void_p
    ctypes.windll.user32.GetWindowRect(
        ctypes.c_void_p(ctypes.windll.user32.GetParent(root.winfo_id())), ctypes.byref(rect))
    if "--frame" in sys.argv:          # the whole window, title bar and all
        png = grab_png(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    else:
        png = grab_png(root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(),
                       root.winfo_height())
    (OUT / f"{i + 1}-{screens[i]}{suffix}.png").write_bytes(png)
    print(f"{screens[i]}: window {root.geometry()}, on screen ({rect.left},{rect.top})-"
          f"({rect.right},{rect.bottom}), text {app.cfg.look_text_size}%")
    show(i + 1)


root.after(2500, lambda: show(0))
root.mainloop()
print("screenshots in", OUT.resolve())
