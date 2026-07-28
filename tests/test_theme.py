from __future__ import annotations

import dataclasses

import pytest

from audiofx import theme


# --------------------------------------------------------------------------
# palettes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT])
def test_every_colour_is_filled_in(palette: theme.Palette):
    for field in dataclasses.fields(palette):
        value = getattr(palette, field.name)
        if field.name == "name":
            continue
        assert value.startswith("#") and len(value) == 7, field.name


def brightness(colour: str) -> int:
    return sum(int(colour[i : i + 2], 16) for i in (1, 3, 5))


def test_dark_and_light_are_actually_opposites():
    assert brightness(theme.DARK.window) < brightness(theme.DARK.text)
    assert brightness(theme.LIGHT.window) > brightness(theme.LIGHT.text)
    assert brightness(theme.DARK.window) < brightness(theme.LIGHT.window)


def test_every_menu_choice_resolves_to_a_palette():
    for name in theme.THEME_CHOICES:
        assert isinstance(theme.palette_for(name), theme.Palette)


def test_unknown_theme_falls_back_to_the_default():
    assert theme.normalize("neon") == theme.DEFAULT_THEME
    assert theme.normalize(None) == theme.DEFAULT_THEME
    assert theme.palette_for("neon") is theme.PALETTES[theme.DEFAULT_THEME]


def test_the_default_is_dark():
    assert theme.DEFAULT_THEME == "dark"


def test_text_options_cover_what_ttk_cannot_reach():
    text = theme.text_options(theme.DARK)
    assert text["background"] == theme.DARK.field
    assert text["insertbackground"] == theme.DARK.text
    assert text["selectbackground"] == theme.DARK.selection


# --------------------------------------------------------------------------
# applying (only when a Tk window can be created)
# --------------------------------------------------------------------------


@pytest.fixture()
def frame(tk_root):
    import tkinter as tk
    from tkinter import ttk

    window = tk.Toplevel(tk_root)
    window.withdraw()
    widget = ttk.Frame(window)
    widget.grid()
    yield widget
    window.destroy()


def test_apply_theme_paints_the_window(frame):
    palette = theme.apply_theme(frame, "dark")
    assert palette is theme.DARK
    assert frame.winfo_toplevel().cget("background") == theme.DARK.window


def test_apply_theme_uses_its_own_theme_for_the_bundled_palettes(frame):
    from tkinter import ttk

    theme.apply_theme(frame, "light")
    assert ttk.Style(frame).theme_use() == theme.THEME_NAME
    assert ttk.Style(frame).lookup("TLabel", "foreground") == theme.LIGHT.text


def test_the_system_theme_is_not_stained_by_the_dark_palette(frame):
    """On Linux the platform theme is clam, the very theme clam-based palettes
    would otherwise overwrite - so "System default" must not come back dark."""
    from tkinter import ttk

    theme.apply_theme(frame, "dark")
    theme.apply_theme(frame, theme.SYSTEM)
    style = ttk.Style(frame)
    assert style.theme_use() != theme.THEME_NAME
    assert style.lookup("TFrame", "background") != theme.DARK.window


def test_switching_back_and_forth_keeps_the_colours_consistent(frame):
    from tkinter import ttk

    theme.apply_theme(frame, "dark")
    theme.apply_theme(frame, "light")
    theme.apply_theme(frame, "dark")
    assert ttk.Style(frame).lookup("TLabel", "background") == theme.DARK.window


def test_system_theme_follows_the_platform(frame):
    from tkinter import ttk

    palette = theme.apply_theme(frame, theme.SYSTEM)
    style = ttk.Style(frame)
    # the plain tk widgets take the platform theme's own frame colour, so the
    # log box does not sit on a colour nothing else uses
    assert palette.window == style.lookup("TFrame", "background")


def test_named_styles_exist_under_every_theme(frame):
    from tkinter import ttk

    for name in theme.THEME_CHOICES:
        palette = theme.apply_theme(frame, name)
        style = ttk.Style(frame)
        assert style.lookup("Hint.TLabel", "foreground") == palette.muted
        assert style.lookup("Warn.TLabel", "foreground") == palette.warning
