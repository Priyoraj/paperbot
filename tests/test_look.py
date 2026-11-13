"""How the app looks: themes and text size (gui/look.py, Settings > Appearance)."""
import threading
from tkinter import ttk

from paperbot import config as config_mod
from paperbot.gui import look
from paperbot.gui.base import page_height

PAGES = {"home", "needs", "people", "requests", "try", "settings", "share"}


def body_px(app) -> int:
    """The size, in pixels, of the font nearly all of the app's text is in."""
    return -int(app.root.tk.call("font", "configure", "SunValleyBodyFont", "-size"))


# ---- sizes and names ---------------------------------------------------------------------------
def test_text_follows_the_displays_scaling_and_the_owners_choice():
    # What was wrong: on a display that Windows scales to 150% the text stayed 14 pixels,
    # two thirds the size of everything else on screen.
    assert look.font_px(14, 1.0, 100) == 14          # an ordinary display: as it always was
    assert look.font_px(14, 1.5, 100) == 21          # scaled to 150%: as large as it should be
    assert look.font_px(14, 1.5, 125) == 26 and look.font_px(14, 1.5, 80) == 17
    assert look.font_px(12, 1.0, 50) == 8            # never too small to read


def test_any_size_asked_for_becomes_one_that_is_on_offer():
    assert look.clean_text_size(100) == 100 and look.clean_text_size("125") == 125
    assert look.clean_text_size(83) == 85 and look.clean_text_size(122.4) == 120
    assert look.clean_text_size(10) == 80 and look.clean_text_size(1e9) == 150
    assert look.clean_text_size("large") == look.clean_text_size(None) == 100
    assert look.clean_text_size(float("nan")) == 100


def test_an_unknown_theme_is_the_one_windows_has(monkeypatch):
    monkeypatch.setattr(look, "windows_prefers_dark", lambda: True)
    assert look.resolve("system") == look.resolve("nonsense") == look.resolve(None) == "dark"
    monkeypatch.setattr(look, "windows_prefers_dark", lambda: False)
    assert look.resolve("system") == "light" and look.resolve(" Midnight") == "midnight"
    assert set(look.THEMES) - {"system"} == set(look.COLORS)       # each has its colours...
    assert len({frozenset(colors) for colors in look.COLORS.values()}) == 1    # ...all of them


def test_the_look_is_kept_in_the_settings_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[owner]\nname = "Priya"\n', encoding="utf-8")
    cfg = config_mod.load(path)
    assert (cfg.look_theme, cfg.look_text_size) == ("system", 100)
    config_mod.set_value("look", "theme", "paper", path)
    config_mod.set_value("look", "text_size", 125, path)
    cfg = config_mod.load(path)
    assert (cfg.look_theme, cfg.look_text_size) == ("paper", 125)
    assert 'name = "Priya"' in path.read_text(encoding="utf-8")    # the rest is untouched


def test_a_page_fills_its_room_and_scrolls_only_when_it_must():
    assert page_height(400, 700, fill=True) == (700, False)   # room to spare: a list uses it
    assert page_height(900, 700, fill=True) == (900, True)    # large text: the page scrolls
    assert page_height(400, 700, fill=False) == (400, False)


# ---- in the open app ---------------------------------------------------------------------------
def test_every_theme_can_be_switched_to_while_the_app_is_open(window):
    app = window
    app.show("people")
    for theme in look.THEMES:
        app.set_theme(theme)
        assert app.cfg.look_theme == theme == config_mod.load(app.cfg.path).look_theme
        assert app.pal["name"] == look.resolve(theme)
        assert app.page() == "people" and set(app.tabs) == PAGES   # the same page, made anew
        app.root.update()
    for key in app.tabs:                                 # and every page still shows
        app.show(key)
        app.refresh_all()
        app.root.update()


def test_the_themes_made_here_have_ticks_and_switches_of_their_own(window):
    app = window
    app.set_theme("contrast")
    style, colors = ttk.Style(app.root), look.COLORS["contrast"]
    assert style.theme_use() == "paperbot-contrast"
    assert "PaperBot.switch" in str(style.layout("Switch.TCheckbutton"))
    assert "PaperBot.check" in str(style.layout("TCheckbutton"))
    assert "PaperBot.radio" in str(style.layout("TRadiobutton"))
    assert style.lookup("TButton", "bordercolor") == colors["border"]    # buttons are outlined
    assert style.lookup("Accent.TButton", "background") == colors["accent"]
    assert app.pal["mode"] == "dark" and app.pal["text_bg"] == colors["bg"]
    app.set_theme("contrast")                            # again: nothing is made twice
    app.set_theme("dark")
    assert style.theme_use() in ("sun-valley-dark", "paperbot-dark")


def test_pictures_are_drawn_with_smooth_edges(window):
    def disc(x, y):
        return (x - 4.5) ** 2 + (y - 4.5) ** 2 <= 16

    picture = look.paint(window.root, 9, 9, [(disc, "#ff0000")], "#000000", margin=3)
    assert (picture.width(), picture.height()) == (12, 9)
    assert picture.get(4, 4) == (255, 0, 0) and picture.get(0, 0) == (0, 0, 0)
    assert picture.get(11, 4) == (0, 0, 0)               # the margin stays empty
    red = picture.get(1, 2)[0]                           # the disc's edge runs through this one
    assert 0 < red < 255


def test_the_text_size_changes_all_text_and_is_remembered(window):
    app = window
    normal = body_px(app)
    assert normal == look.font_px(14, app.scale, 100)
    app.set_text_size(127)                               # becomes a size that is on offer
    assert app.cfg.look_text_size == 125 == config_mod.load(app.cfg.path).look_text_size
    assert body_px(app) == look.font_px(14, app.scale, 125) > normal
    title = -int(app.root.tk.call("font", "configure", "SunValleyTitleFont", "-size"))
    assert title == look.font_px(28, app.scale, 125)     # headings grow with it
    assert set(app.tabs) == PAGES
    app.set_text_size(100)
    assert body_px(app) == normal


def test_the_slider_shows_a_sample_and_applies_when_let_go(window):
    app = window
    app.show("settings")
    tab = app.tabs["settings"]
    assert tab.size_label.cget("text") == "100%" and tab.size_reset.instate(["disabled"])
    tab.size_scale.set(131.7)                            # dragged along...
    assert tab.size_label.cget("text") == "130%" and tab.sample.winfo_manager() == "grid"
    assert app.cfg.look_text_size == 100 and body_px(app) == look.font_px(14, app.scale, 100)
    tab._text_size_chosen()                              # ...and let go of
    app.pump(lambda: app.cfg.look_text_size == 130)
    assert body_px(app) == look.font_px(14, app.scale, 130)
    tab = app.tabs["settings"]                           # the pages were made anew
    assert app.page() == "settings" and tab.size_label.cget("text") == "130%"
    assert not tab.size_reset.instate(["disabled"])
    tab._text_size_reset()                               # "Normal size"
    app.pump(lambda: app.cfg.look_text_size == 100)
    assert body_px(app) == look.font_px(14, app.scale, 100)


def test_choosing_a_theme_in_settings_keeps_what_was_typed(window):
    app = window
    app.show("settings")
    tab = app.tabs["settings"]
    tab.vars[("owner", "name")][1].set("Priya R")        # typed, not saved yet
    tab.theme_var.set("Midnight")
    tab._theme_chosen()
    app.pump(lambda: app.cfg.look_theme == "midnight")
    new = app.tabs["settings"]
    assert new is not tab and app.page() == "settings"
    assert new.vars[("owner", "name")][1].get() == "Priya R" and app.cfg.owner_name == "Priya"
    assert new.theme_var.get() == "Midnight" and app.pal["name"] == "midnight"


def test_a_change_of_look_waits_until_nothing_is_running(window):
    app, gate = window, threading.Event()
    old = app.tabs["home"]
    app.run_task("Fetching…", gate.wait)                 # something that reports to the pages
    app.set_theme("paper")
    assert app.tabs["home"] is old                       # so they are left alone for now
    gate.set()
    app.pump(lambda: app.tabs["home"] is not old)
    assert app.tabs["home"] is not old and app.pal["name"] == "paper"


def test_same_as_windows_follows_windows(window, monkeypatch):
    app = window
    monkeypatch.setattr(look, "windows_prefers_dark", lambda: True)
    app.set_theme("system")
    assert app.pal["name"] == "dark"
    monkeypatch.setattr(look, "windows_prefers_dark", lambda: False)   # switched in Windows
    app._slow_tick()
    assert app.pal["name"] == "light" and app.cfg.look_theme == "system"
