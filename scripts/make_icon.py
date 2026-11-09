"""Draws the PaperBot icon (a page on a blue tile) without any image library and writes
paperbot/assets/paperbot.png (window icon) and paperbot.ico (shortcut icon)."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "paperbot" / "assets"
TOP, BOTTOM = (43, 111, 224), (26, 79, 184)          # tile gradient
PAGE, FOLD, INK = (255, 255, 255), (201, 216, 245), (31, 95, 209)


def inside_round_rect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def color_at(x, y):
    """Colour of the icon at (x, y) in 0..1 coordinates, or None for transparent."""
    if not inside_round_rect(x, y, 0.04, 0.04, 0.96, 0.96, 0.2):
        return None
    if 0.36 <= x <= 0.64 and any(abs(y - ly) <= 0.022 for ly in (0.47, 0.56)):
        return INK
    if 0.36 <= x <= 0.55 and abs(y - 0.65) <= 0.022:
        return INK
    in_rect = 0.29 <= x <= 0.71 and 0.19 <= y <= 0.81
    cut = x - 0.58 > y - 0.19                 # the top-right corner is folded away
    if in_rect and not cut:
        return FOLD if (x >= 0.58 and y <= 0.32) else PAGE
    t = (y - 0.04) / 0.92
    return tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))


def render(size: int, ss: int = 4) -> bytes:
    rows = bytearray()
    for py in range(size):
        rows.append(0)
        for px in range(size):
            acc = [0, 0, 0]
            hits = 0
            for sy in range(ss):
                for sx in range(ss):
                    c = color_at((px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size)
                    if c:
                        hits += 1
                        acc[0] += c[0]
                        acc[1] += c[1]
                        acc[2] += c[2]
            if hits:
                rows += bytes([acc[0] // hits, acc[1] // hits, acc[2] // hits,
                               255 * hits // (ss * ss)])
            else:
                rows += b"\0\0\0\0"
    return png(size, size, bytes(rows))


def png(w: int, h: int, rgba_rows: bytes) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rgba_rows, 9)) + chunk(b"IEND", b""))


def ico(pngs: dict[int, bytes]) -> bytes:
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, data in sorted(pngs.items(), reverse=True):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data),
                               offset + len(blobs))
        blobs += data
    return head + entries + blobs


if __name__ == "__main__":
    ASSETS.mkdir(parents=True, exist_ok=True)
    images = {s: render(s, 4 if s <= 64 else 3) for s in (256, 64, 48, 32, 16)}
    (ASSETS / "paperbot.png").write_bytes(images[64])
    (ASSETS / "paperbot.ico").write_bytes(ico(images))
    (ASSETS / "paperbot-256.png").write_bytes(images[256])
    print("icons written to", ASSETS)
