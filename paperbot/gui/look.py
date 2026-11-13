"""How the app looks: its themes and the size of its text (Settings > Appearance).

Themes. "Light" and "Dark" are the Sun Valley theme, which looks like Windows 11. The others
are made here from a handful of colours on top of Tk's plain "clam" theme, and so are Light and
Dark themselves if Sun Valley can't be loaded. "Same as Windows" follows the light or dark
choice in Windows.

Text size. Sun Valley fixes its fonts in pixels. On a display that Windows scales to 150%, a
window that knows its real size (as this one does, to stay sharp) then showed text a third
smaller than everything else on screen. So the fonts now follow the display's scaling, and the
owner's own choice goes on top of that: 100 is normal, 80 to 150 can be chosen.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

THEMES = {                 # [look] theme in config.toml -> what Settings calls it
    "system": "Same as Windows",
    "light": "Light",
    "dark": "Dark",
    "paper": "Paper",
    "midnight": "Midnight",
    "contrast": "High contrast",
}
TEXT_MIN, TEXT_MAX, TEXT_STEP, TEXT_NORMAL = 80, 150, 5, 100

# Named font -> its size in pixels at normal text size on a display that isn't scaled.
FONT_PX = {
    "SunValleyCaptionFont": 12, "SunValleyBodyFont": 14, "SunValleyBodyStrongFont": 14,
    "SunValleyBodyLargeFont": 18, "SunValleySubtitleFont": 20, "SunValleyTitleFont": 28,
    "SunValleyTitleLargeFont": 40, "SunValleyDisplayFont": 68,
    "TkDefaultFont": 14, "TkTextFont": 14, "TkMenuFont": 14, "TkHeadingFont": 12,
    "TkCaptionFont": 14, "TkSmallCaptionFont": 12, "TkIconFont": 14, "TkTooltipFont": 12,
    "TkFixedFont": 13,
}
# The Sun Valley fonts, for when that theme isn't there to make them: (family, weight).
SPARE_FONTS = {
    "SunValleyCaptionFont": ("Segoe UI", "normal"), "SunValleyBodyFont": ("Segoe UI", "normal"),
    "SunValleyBodyStrongFont": ("Segoe UI", "bold"),
    "SunValleyBodyLargeFont": ("Segoe UI", "normal"),
    "SunValleySubtitleFont": ("Segoe UI", "bold"), "SunValleyTitleFont": ("Segoe UI", "bold"),
    "SunValleyTitleLargeFont": ("Segoe UI", "bold"),
    "SunValleyDisplayFont": ("Segoe UI", "bold"),
}

# Per theme. First line: what the app itself draws with (status colours, text areas). The rest:
# the colours its widgets are made from - used for the themes made here; for Light and Dark
# only when Sun Valley is missing.
COLORS = {
    "light": dict(
        mode="light", ok="#0f7b0f", bad="#c42b1c", warn="#9d5d00", muted="#5f5f5f",
        accent="#005fb8", text_bg="#ffffff", text_fg="#1a1a1a",
        bg="#fafafa", fg="#1c1c1c", field="#ffffff", border="#cfcfcf", button="#ffffff",
        hover="#f0f0f0", press="#e6e6e6", accent_hover="#1a70c4", accent_fg="#ffffff",
        sel_bg="#cce4f7", sel_fg="#1c1c1c", disabled="#a0a0a0", heading="#f1f1f1",
        trough="#ececec", thumb="#bdbdbd"),
    "dark": dict(
        mode="dark", ok="#6ccb5f", bad="#ff99a4", warn="#fce100", muted="#a0a0a0",
        accent="#60cdff", text_bg="#2b2b2b", text_fg="#e6e6e6",
        bg="#1c1c1c", fg="#fafafa", field="#2b2b2b", border="#444444", button="#2d2d2d",
        hover="#383838", press="#262626", accent_hover="#86d9ff", accent_fg="#00131f",
        sel_bg="#264f66", sel_fg="#ffffff", disabled="#6e6e6e", heading="#262626",
        trough="#242424", thumb="#5c5c5c"),
    "paper": dict(         # warm and low-glare, like a printed page
        mode="light", ok="#2f6b2a", bad="#a8321f", warn="#875500", muted="#6f604b",
        accent="#8a4b12", text_bg="#fbf6ea", text_fg="#33291d",
        bg="#f3ead8", fg="#33291d", field="#fbf6ea", border="#c9ba9b", button="#eadfc8",
        hover="#e1d4b8", press="#d6c7a6", accent_hover="#a35c1d", accent_fg="#fffaf0",
        sel_bg="#e2d0a8", sel_fg="#33291d", disabled="#a4977f", heading="#eadfc8",
        trough="#e6dac2", thumb="#c2b28e"),
    "midnight": dict(      # dark blue, easier on the eyes at night than plain grey
        mode="dark", ok="#7ee2a0", bad="#ff9d9d", warn="#ffd866", muted="#93a3bd",
        accent="#7cc4ff", text_bg="#0b1220", text_fg="#e6ebf5",
        bg="#101827", fg="#e6ebf5", field="#0b1220", border="#2f3e5c", button="#1c2740",
        hover="#253352", press="#2d3e61", accent_hover="#a3d6ff", accent_fg="#06203a",
        sel_bg="#27467a", sel_fg="#ffffff", disabled="#5d6c88", heading="#18233a",
        trough="#0d1524", thumb="#3a4f78"),
    "contrast": dict(      # black, white and yellow, for those who need strong contrast
        mode="dark", ok="#3ff23f", bad="#ff8080", warn="#ffff00", muted="#dcdcdc",
        accent="#ffff00", text_bg="#000000", text_fg="#ffffff",
        bg="#000000", fg="#ffffff", field="#000000", border="#ffffff", button="#000000",
        hover="#2b2b2b", press="#454545", accent_hover="#ffff8a", accent_fg="#000000",
        sel_bg="#1aebff", sel_fg="#000000", disabled="#9a9a9a", heading="#000000",
        trough="#000000", thumb="#ffffff"),
}


def windows_prefers_dark() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def clean_theme(value) -> str:
    """Whatever config.toml says -> a key of THEMES."""
    value = str(value or "").strip().lower()
    return value if value in THEMES else "system"


def resolve(theme) -> str:
    """The theme that is shown for this choice: 'system' is light or dark, as in Windows."""
    theme = clean_theme(theme)
    if theme == "system":
        return "dark" if windows_prefers_dark() else "light"
    return theme


def clean_text_size(value) -> int:
    """Whatever config.toml (or a slider) says -> one of the sizes on offer, in percent."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return TEXT_NORMAL
    if size != size:                       # not a number
        return TEXT_NORMAL
    size = round(size / TEXT_STEP) * TEXT_STEP
    return int(min(TEXT_MAX, max(TEXT_MIN, size)))


def font_px(base: int, scale: float, percent: int) -> int:
    """Pixels for a font that is `base` pixels on an unscaled display at normal size."""
    return max(8, round(base * scale * percent / 100))


# ---- fonts ---------------------------------------------------------------------------------
def ensure_fonts(root: tk.Misc) -> None:
    """The named fonts the app draws with: made here if Sun Valley didn't make them."""
    have = set(root.tk.splitlist(root.tk.call("font", "names")))
    for name, (family, weight) in SPARE_FONTS.items():
        if name not in have:
            root.tk.call("font", "create", name, "-family", family, "-weight", weight,
                         "-size", -FONT_PX[name])


def set_text_size(root: tk.Misc, percent, scale: float = 1.0) -> float:
    """Show all text at `percent` of its normal size for this display (`scale`: 1.5 where
    Windows scales to 150%). Every widget follows at once, because they all use named fonts.
    Returns the two together: what text-sized measurements are multiplied by."""
    percent = clean_text_size(percent)
    ensure_fonts(root)
    for name, base in FONT_PX.items():
        root.tk.call("font", "configure", name, "-size", -font_px(base, scale, percent))
    line = int(root.tk.call("font", "metrics", "SunValleyBodyFont", "-linespace"))
    ttk.Style(root).configure("Treeview", rowheight=line + round(12 * scale))
    root.paperbot_text = scale * percent / 100
    return root.paperbot_text


def text_scale(widget: tk.Misc) -> float:
    """Display scaling x chosen text size, for measurements that must grow with the text."""
    return getattr(widget._root(), "paperbot_text", 1.0)


# ---- themes --------------------------------------------------------------------------------
def _load_sun_valley(root: tk.Misc) -> bool:
    try:
        import sv_ttk
        sv_ttk.get_theme(root)             # loads it (and its fonts) without switching to it
        return True
    except Exception:  # noqa: BLE001 - not installed, or it wouldn't load: the app still looks fine
        return False


def apply_theme(root: tk.Misc, theme, scale: float = 1.0) -> dict:
    """Switch the app to `theme` (a key of THEMES). Returns the colours the app's own drawing
    uses, plus 'name': the theme now shown ('system' has become 'light' or 'dark').
    Follow it with set_text_size(): row heights belong to the theme."""
    name = resolve(theme)
    colors = dict(COLORS[name], name=name)
    style = ttk.Style(root)
    sun_valley = _load_sun_valley(root)
    ensure_fonts(root)
    if sun_valley and name in ("light", "dark"):
        style.theme_use(f"sun-valley-{name}")
        try:                               # its own colours, now rather than a moment later
            root.tk.call("configure_colors")
        except tk.TclError:
            pass
    else:
        made = f"paperbot-{name}"
        new = made not in style.theme_names()
        if new:
            style.theme_create(made, parent="clam", settings=made_theme(colors, scale))
        style.theme_use(made)
        if new:
            draw_indicators(root, style, colors, scale)
        root.tk_setPalette(
            background=colors["bg"], foreground=colors["fg"], activeBackground=colors["hover"],
            activeForeground=colors["fg"], highlightColor=colors["accent"],
            highlightBackground=colors["bg"], selectBackground=colors["accent"],
            selectForeground=colors["accent_fg"], insertBackground=colors["fg"],
            troughColor=colors["trough"], disabledForeground=colors["disabled"])
    # The list a drop-down opens is an old-style widget: it takes its looks from here.
    lists = {"font": "SunValleyBodyFont"}
    if not (sun_valley and name in ("light", "dark")):
        lists.update(background=colors["field"], foreground=colors["fg"],
                     selectBackground=colors["accent"], selectForeground=colors["accent_fg"])
    for option, value in lists.items():
        root.option_add(f"*TCombobox*Listbox.{option}", value)
    return colors


def made_theme(c: dict, scale: float = 1.0) -> dict:
    """The settings of a ttk theme in the colours `c`, on top of 'clam' (which draws
    everything itself, so every colour can be chosen)."""
    def p(n: float) -> int:                # pixels that follow the display's scaling
        return max(1, round(n * scale))

    flat = dict(lightcolor=c["button"], darkcolor=c["button"])
    field = dict(fieldbackground=c["field"], foreground=c["fg"], bordercolor=c["border"],
                 lightcolor=c["field"], darkcolor=c["field"], insertcolor=c["fg"],
                 selectbackground=c["accent"], selectforeground=c["accent_fg"],
                 padding=(p(6), p(4)))
    field_map = dict(bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["accent"])],
                     fieldbackground=[("disabled", c["bg"])],
                     foreground=[("disabled", c["disabled"])])
    tick = dict(background=c["bg"], foreground=c["fg"], indicatorbackground=c["field"],
                indicatorforeground=c["fg"], indicatorsize=p(15),
                indicatormargin=(p(2), p(2), p(8), p(2)), upperbordercolor=c["border"],
                lowerbordercolor=c["border"], padding=p(3), focuscolor=c["accent"])
    tick_map = dict(indicatorbackground=[("pressed", c["hover"]), ("selected", c["accent"])],
                    indicatorforeground=[("selected", c["accent_fg"])],
                    upperbordercolor=[("selected", c["accent"])],
                    lowerbordercolor=[("selected", c["accent"])],
                    background=[("active", c["bg"])],
                    foreground=[("disabled", c["disabled"])])
    arrows = dict(arrowcolor=c["fg"], arrowsize=p(14), background=c["button"])
    return {
        ".": {"configure": dict(
            background=c["bg"], foreground=c["fg"], bordercolor=c["border"],
            lightcolor=c["bg"], darkcolor=c["bg"], troughcolor=c["trough"],
            focuscolor=c["accent"], selectbackground=c["accent"],
            selectforeground=c["accent_fg"], fieldbackground=c["field"],
            insertcolor=c["fg"], insertwidth=p(1), font="SunValleyBodyFont"),
            "map": dict(foreground=[("disabled", c["disabled"])])},
        "TFrame": {"configure": dict(background=c["bg"])},
        "Card.TFrame": {"configure": dict(background=c["bg"], relief="solid", borderwidth=1,
                                          bordercolor=c["border"], lightcolor=c["border"],
                                          darkcolor=c["border"])},
        "TLabel": {"configure": dict(background=c["bg"], foreground=c["fg"])},
        "TButton": {
            "configure": dict(background=c["button"], foreground=c["fg"],
                              bordercolor=c["border"], padding=(p(12), p(5)), anchor="center",
                              focuscolor=c["button"], relief="raised", **flat),
            "map": dict(background=[("disabled", c["bg"]), ("pressed", c["press"]),
                                    ("active", c["hover"])],
                        lightcolor=[("disabled", c["bg"]), ("pressed", c["press"]),
                                    ("active", c["hover"])],
                        darkcolor=[("disabled", c["bg"]), ("pressed", c["press"]),
                                   ("active", c["hover"])],
                        focuscolor=[("disabled", c["bg"]), ("pressed", c["press"]),
                                    ("active", c["hover"])],
                        bordercolor=[("disabled", c["disabled"]), ("focus", c["accent"])],
                        foreground=[("disabled", c["disabled"])])},
        "Accent.TButton": {
            "configure": dict(background=c["accent"], foreground=c["accent_fg"],
                              bordercolor=c["accent"], lightcolor=c["accent"],
                              darkcolor=c["accent"], focuscolor=c["accent"]),
            "map": dict(background=[("disabled", c["button"]), ("pressed", c["accent_hover"]),
                                    ("active", c["accent_hover"])],
                        lightcolor=[("disabled", c["button"]), ("pressed", c["accent_hover"]),
                                    ("active", c["accent_hover"])],
                        darkcolor=[("disabled", c["button"]), ("pressed", c["accent_hover"]),
                                   ("active", c["accent_hover"])],
                        focuscolor=[("disabled", c["button"]), ("pressed", c["accent_hover"]),
                                    ("active", c["accent_hover"])],
                        bordercolor=[("disabled", c["border"]), ("focus", c["fg"])],
                        foreground=[("disabled", c["disabled"])])},
        "TCheckbutton": {"configure": tick, "map": tick_map},
        "TRadiobutton": {"configure": tick, "map": tick_map},
        "TEntry": {"configure": field, "map": field_map},
        "TCombobox": {"configure": dict(field, **arrows),
                      "map": dict(field_map,
                                  fieldbackground=[("disabled", c["bg"]),
                                                   ("readonly", c["field"])],
                                  selectbackground=[("readonly", c["field"])],
                                  selectforeground=[("readonly", c["fg"])],
                                  background=[("active", c["hover"])])},
        "TSpinbox": {"configure": dict(field, **dict(arrows, arrowsize=p(11))),
                     "map": dict(field_map, background=[("active", c["hover"])])},
        "TNotebook": {"configure": dict(background=c["bg"], bordercolor=c["border"],
                                        lightcolor=c["bg"], darkcolor=c["bg"],
                                        tabmargins=(p(2), p(4), p(2), 0))},
        "TNotebook.Tab": {
            "configure": dict(background=c["bg"], foreground=c["muted"],
                              bordercolor=c["border"], lightcolor=c["bg"],
                              padding=(p(14), p(7)), focuscolor=c["bg"]),
            "map": dict(background=[("selected", c["field"]), ("active", c["hover"])],
                        lightcolor=[("selected", c["field"]), ("active", c["hover"])],
                        focuscolor=[("selected", c["field"]), ("active", c["hover"])],
                        foreground=[("selected", c["fg"]), ("active", c["fg"])])},
        "Treeview": {
            "configure": dict(background=c["field"], fieldbackground=c["field"],
                              foreground=c["fg"], bordercolor=c["border"],
                              lightcolor=c["field"], darkcolor=c["field"],
                              font="SunValleyBodyFont"),
            "map": dict(background=[("selected", c["sel_bg"])],
                        foreground=[("selected", c["sel_fg"])])},
        "Treeview.Heading": {
            "configure": dict(background=c["heading"], foreground=c["muted"],
                              font="SunValleyCaptionFont", relief="flat",
                              padding=(p(6), p(5)), bordercolor=c["border"],
                              lightcolor=c["heading"], darkcolor=c["heading"]),
            "map": dict(background=[("active", c["hover"])])},
        "TScrollbar": {
            "configure": dict(background=c["thumb"], troughcolor=c["trough"],
                              bordercolor=c["trough"], lightcolor=c["thumb"],
                              darkcolor=c["thumb"], arrowcolor=c["fg"], arrowsize=p(11),
                              gripcount=0, relief="flat"),
            "map": dict(background=[("disabled", c["trough"]), ("active", c["accent"])],
                        lightcolor=[("disabled", c["trough"]), ("active", c["accent"])],
                        darkcolor=[("disabled", c["trough"]), ("active", c["accent"])],
                        arrowcolor=[("disabled", c["trough"])])},
        "TProgressbar": {"configure": dict(background=c["accent"], troughcolor=c["trough"],
                                           bordercolor=c["border"], lightcolor=c["accent"],
                                           darkcolor=c["accent"])},
        "TScale": {"configure": dict(background=c["accent"], troughcolor=c["trough"],
                                     bordercolor=c["border"], lightcolor=c["accent"],
                                     darkcolor=c["accent"], sliderlength=p(18), gripcount=0)},
        "TSeparator": {"configure": dict(background=c["border"])},
    }


# ---- ticks, dots and switches for the themes made here ---------------------------------------
# 'clam' marks a ticked box with a cross, which reads as "no". These are drawn here instead:
# small pictures, at the size the display's scaling asks for, with smoothed edges.
def _rounded(x0: float, y0: float, x1: float, y1: float, r: float):
    """A test for "inside this rectangle with rounded corners" (r = half its height: a pill)."""
    def inside(x: float, y: float) -> bool:
        cx, cy = min(max(x, x0 + r), x1 - r), min(max(y, y0 + r), y1 - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    return inside


def _stroke(points: list[tuple[float, float]], width: float):
    """A test for "on this line through the points, drawn `width` thick"."""
    reach = (width / 2) ** 2

    def inside(x: float, y: float) -> bool:
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            dx, dy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
            if (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2 <= reach:
                return True
        return False
    return inside


def paint(root: tk.Misc, width: int, height: int, layers: list, background: str,
          margin: int = 0, fine: int = 3) -> tk.PhotoImage:
    """A picture of `layers` - [(inside(x, y), '#rrggbb')], later ones on top - on
    `background`, with `margin` empty pixels on its right. A pixel on an edge is looked at
    in fine x fine places, which smooths the edge."""
    def rgb(color: str) -> tuple[int, int, int]:
        return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)

    ground, paints = rgb(background), [(inside, rgb(color)) for inside, color in layers][::-1]

    def at(x: float, y: float) -> tuple[int, int, int]:
        for inside, color in paints:                   # the topmost layer that is there
            if inside(x, y):
                return color
        return ground

    spots = [(i + 0.5) / fine for i in range(fine)]
    blank = " ".join(["#%02x%02x%02x" % ground] * margin)
    rows = []
    for py in range(height):
        row = []
        for px in range(width):
            color = at(px + 0.5, py + 0.5)
            if not (color == at(px, py) == at(px + 1, py) == at(px, py + 1)
                    == at(px + 1, py + 1)):            # an edge runs through this pixel
                found = [at(px + sx, py + sy) for sy in spots for sx in spots]
                color = tuple(sum(c[i] for c in found) // len(found) for i in range(3))
            row.append("#%02x%02x%02x" % color)
        rows.append("{" + " ".join(row) + (" " + blank if margin else "") + "}")
    image = tk.PhotoImage(master=root, width=width + margin, height=height)
    image.put(" ".join(rows))
    return image


def draw_indicators(root: tk.Misc, style: ttk.Style, c: dict, scale: float = 1.0) -> None:
    """Tick boxes, radio buttons and on/off switches for the theme in use (one made here)."""
    n, gap = round(18 * scale), round(8 * scale)          # a box; the space after it
    w, h = round(38 * scale), round(20 * scale)           # a switch
    line = max(1.5, n / 12)                               # how thick an outline is
    half = n / 2

    def box(edge: str, fill: str, mark: str = "") -> tk.PhotoImage:
        layers = [(_rounded(0.5, 0.5, n - 0.5, n - 0.5, n * 0.2), edge),
                  (_rounded(0.5 + line, 0.5 + line, n - 0.5 - line, n - 0.5 - line,
                            max(1.0, n * 0.2 - line)), fill)]
        if mark:
            layers.append((_stroke([(n * 0.24, n * 0.53), (n * 0.43, n * 0.71),
                                    (n * 0.78, n * 0.31)], n * 0.13), mark))
        return paint(root, n, n, layers, c["bg"], margin=gap)

    def dot(edge: str, fill: str, mark: str = "") -> tk.PhotoImage:
        layers = [(_rounded(0.5, 0.5, n - 0.5, n - 0.5, half - 0.5), edge),
                  (_rounded(0.5 + line, 0.5 + line, n - 0.5 - line, n - 0.5 - line,
                            half - 0.5 - line), fill)]
        if mark:
            layers.append((_rounded(half - n * 0.2, half - n * 0.2, half + n * 0.2,
                                    half + n * 0.2, n * 0.2), mark))
        return paint(root, n, n, layers, c["bg"], margin=gap)

    def switch(edge: str, fill: str, knob: str, on: bool) -> tk.PhotoImage:
        r = h * (0.30 if on else 0.26)
        cx = w - h / 2 if on else h / 2
        layers = [(_rounded(0.5, 0.5, w - 0.5, h - 0.5, h / 2 - 0.5), edge),
                  (_rounded(0.5 + line, 0.5 + line, w - 0.5 - line, h - 0.5 - line,
                            h / 2 - 0.5 - line), fill),
                  (_rounded(cx - r, h / 2 - r, cx + r, h / 2 + r, r), knob)]
        return paint(root, w, h, layers, c["bg"], margin=gap)

    off, on = c["muted"], c["accent"]
    pictures = {          # (not chosen, chosen); the app has none of these greyed out
        "check": (box(off, c["field"]), box(on, on, c["accent_fg"])),
        "radio": (dot(off, c["field"]), dot(on, on, c["accent_fg"])),
        "switch": (switch(off, c["field"], off, False), switch(on, on, c["accent_fg"], True)),
    }
    kept = getattr(root, "paperbot_pictures", None)       # Tk forgets pictures nobody holds
    if kept is None:
        kept = root.paperbot_pictures = []
    for kind, widget in (("check", "TCheckbutton"), ("radio", "TRadiobutton"),
                         ("switch", "Switch.TCheckbutton")):
        plain, chosen = pictures[kind]
        kept.extend(pictures[kind])
        element = f"PaperBot.{kind}"
        style.element_create(element, "image", plain, ("selected", chosen), sticky="w")
        part = widget.split(".")[-1][1:]                  # 'Checkbutton' or 'Radiobutton'
        style.layout(widget, [(f"{part}.padding", {"sticky": "nswe", "children": [
            (element, {"side": "left", "sticky": ""}),
            (f"{part}.focus", {"side": "left", "sticky": "w", "children": [
                (f"{part}.label", {"sticky": "nswe"})]})]})])


def title_bar(root: tk.Misc, colors: dict) -> bool:
    """Give the window's title bar the theme's colours (Windows 11; before that it is at
    least dark with a dark theme). Nothing depends on it working. False: the window isn't
    on screen yet - try again in a moment. (It must not be put there from here: a window
    shown before it has been given its place ends up wherever Windows likes.)"""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                              ctypes.c_uint]
        hwnd = user32.GetParent(root.winfo_id())          # the frame Windows draws around it
        if not hwnd:
            return False

        def put(attribute: int, value: int) -> None:
            number = ctypes.c_uint(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(number),
                                      ctypes.sizeof(number))

        def bgr(color: str) -> int:        # '#rrggbb' -> what Windows calls a COLORREF
            return int(color[5:7] + color[3:5] + color[1:3], 16)

        put(20, 1 if colors["mode"] == "dark" else 0)     # light or dark window buttons
        put(35, bgr(colors["bg"]))                        # the bar itself
        put(36, bgr(colors["fg"]))                        # the window's name on it
    except Exception:  # noqa: BLE001 - an older Windows
        pass
    return True
