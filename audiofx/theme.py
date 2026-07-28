"""Colour themes for the interface.

ttk widgets take their colours from a Style rather than from widget options,
and of the built-in themes only 'clam' lets every part of every widget be
recoloured - the native Windows and macOS themes draw with the platform's own
colours and ignore most of what you configure. So both palettes here are drawn
on top of clam, and the 'system' choice hands the look back to the platform.

Plain tk widgets (the log box, the menus) are not styled by ttk at all; they
get their colours from `text_options()` / `menu_options()`.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import ttk


@dataclass(frozen=True)
class Palette:
    name: str
    window: str  # panels and the window itself
    surface: str  # buttons, headings - one step away from the window
    field: str  # entries, the song list, the log
    text: str
    muted: str  # hints and disabled text
    warning: str
    accent: str  # group titles, progress bar, focus
    accent_text: str
    border: str
    selection: str
    selection_text: str
    trough: str  # slider grooves, scrollbar backgrounds


DARK = Palette(
    name="dark",
    window="#1e2128",
    surface="#2a2f3a",
    field="#161920",
    text="#e6e8ee",
    muted="#98a1b0",
    warning="#ff8a80",
    accent="#5b9dff",
    accent_text="#0d1017",
    border="#3a4150",
    selection="#2f5ca8",
    selection_text="#ffffff",
    trough="#12151b",
)

LIGHT = Palette(
    name="light",
    window="#f4f5f7",
    surface="#e7e9ee",
    field="#ffffff",
    text="#1c1f26",
    muted="#5f6773",
    warning="#a33030",
    accent="#2f6fdd",
    accent_text="#ffffff",
    border="#c3c9d4",
    selection="#cfe0ff",
    selection_text="#10131a",
    trough="#dfe2e8",
)

SYSTEM = "system"
DEFAULT_THEME = "dark"

# The palettes are painted onto a private clone of clam rather than onto clam
# itself: on Linux clam *is* the platform theme, so configuring it would leave
# the dark colours behind for anyone who then picks "System default".
THEME_NAME = "audiofx"

PALETTES: dict[str, Palette] = {DARK.name: DARK, LIGHT.name: LIGHT}

# value -> menu label
THEME_CHOICES: dict[str, str] = {
    "dark": "Dark",
    "light": "Light",
    SYSTEM: "System default",
}


def value_for_label(label: str) -> str:
    """Turn a menu label back into the theme name behind it."""
    for value, text in THEME_CHOICES.items():
        if text == label:
            return value
    return DEFAULT_THEME


def palette_for(name: str) -> Palette:
    """The palette a theme name maps to (system follows the light one)."""
    if name == SYSTEM:
        return LIGHT
    return PALETTES.get(name, PALETTES[DEFAULT_THEME])


def normalize(name: str | None) -> str:
    return name if name in THEME_CHOICES else DEFAULT_THEME


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------


def _native_theme(style: ttk.Style) -> str:
    names = style.theme_names()
    for candidate in ("vista", "aqua", "winnative", "clam", "default"):
        if candidate in names:
            return candidate
    return style.theme_use()  # pragma: no cover - exotic Tk build


def _use_own_theme(style: ttk.Style) -> None:
    """Switch to our clone of clam, creating it the first time."""
    try:
        if THEME_NAME not in style.theme_names():
            style.theme_create(THEME_NAME, parent="clam", settings={})
        style.theme_use(THEME_NAME)
    except tk.TclError:  # pragma: no cover - clam always ships with Tk
        style.theme_use("clam")


def _configure(style: ttk.Style, p: Palette) -> None:
    style.configure(
        ".",
        background=p.window,
        foreground=p.text,
        fieldbackground=p.field,
        troughcolor=p.trough,
        bordercolor=p.border,
        lightcolor=p.window,
        darkcolor=p.window,
        focuscolor=p.accent,
        insertcolor=p.text,
        selectbackground=p.selection,
        selectforeground=p.selection_text,
    )

    style.configure("TFrame", background=p.window)
    style.configure("TLabel", background=p.window, foreground=p.text)
    # clam greys the background of a disabled label; keep it on the panel colour
    style.map(
        "TLabel",
        background=[("disabled", p.window)],
        foreground=[("disabled", p.muted)],
    )
    style.configure("TLabelframe", background=p.window, bordercolor=p.border)
    style.configure("TLabelframe.Label", background=p.window, foreground=p.accent)

    style.configure("TButton", background=p.surface, foreground=p.text, padding=(8, 4))
    style.map(
        "TButton",
        background=[("disabled", p.window), ("pressed", p.accent), ("active", p.border)],
        foreground=[("disabled", p.muted), ("pressed", p.accent_text)],
    )
    style.configure("TMenubutton", background=p.surface, foreground=p.text, arrowcolor=p.text)
    style.map("TMenubutton", background=[("active", p.border)])

    for kind in ("TCheckbutton", "TRadiobutton"):
        # in clam the indicator is a box filled with -indicatorbackground and a
        # tick/dot drawn in -indicatorforeground
        style.configure(
            kind,
            background=p.window,
            foreground=p.text,
            indicatorbackground=p.field,
            indicatorforeground=p.accent,
        )
        style.map(
            kind,
            background=[("active", p.window)],
            foreground=[("disabled", p.muted)],
            indicatorbackground=[("disabled", p.window), ("pressed", p.border)],
            indicatorforeground=[("disabled", p.muted)],
        )

    style.configure(
        "TEntry", fieldbackground=p.field, foreground=p.text, insertcolor=p.text, padding=3
    )
    style.map(
        "TEntry",
        fieldbackground=[("readonly", p.surface), ("disabled", p.window)],
        foreground=[("disabled", p.muted)],
    )

    style.configure(
        "TCombobox",
        fieldbackground=p.field,
        background=p.surface,
        foreground=p.text,
        arrowcolor=p.text,
        padding=3,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", p.field), ("disabled", p.window)],
        foreground=[("disabled", p.muted)],
        arrowcolor=[("disabled", p.muted)],
    )

    style.configure(
        "TSpinbox",
        fieldbackground=p.field,
        background=p.surface,
        foreground=p.text,
        arrowcolor=p.text,
        padding=3,
    )
    style.map(
        "TSpinbox",
        fieldbackground=[("disabled", p.window)],
        foreground=[("disabled", p.muted)],
        arrowcolor=[("disabled", p.muted)],
    )

    style.configure("TScale", background=p.window, troughcolor=p.trough)
    style.map("TScale", background=[("active", p.window)])
    style.configure(
        "Horizontal.TProgressbar",
        background=p.accent,
        troughcolor=p.trough,
        bordercolor=p.border,
        lightcolor=p.accent,
        darkcolor=p.accent,
    )
    style.configure(
        "TScrollbar", background=p.surface, troughcolor=p.trough, arrowcolor=p.text
    )
    style.map("TScrollbar", background=[("active", p.border)])

    style.configure(
        "Treeview",
        background=p.field,
        fieldbackground=p.field,
        foreground=p.text,
        bordercolor=p.border,
        rowheight=22,
    )
    style.map(
        "Treeview",
        background=[("selected", p.selection)],
        foreground=[("selected", p.selection_text)],
    )
    style.configure(
        "Treeview.Heading", background=p.surface, foreground=p.text, relief="flat"
    )
    style.map("Treeview.Heading", background=[("active", p.border)])


def _configure_named(style: ttk.Style, p: Palette) -> None:
    """Styles the interface asks for by name; they exist under every theme."""
    style.configure("Hint.TLabel", foreground=p.muted)
    style.configure("Warn.TLabel", foreground=p.warning)


def _restyle_popdowns(widget: tk.Misc, p: Palette) -> None:
    """Recolour the listbox a combobox drops down.

    That listbox is a plain tk widget created on demand, so an option database
    entry only reaches the ones opened later. Asking Tk for the popdown builds
    it now and lets us paint it directly.
    """
    for child in widget.winfo_children():
        if isinstance(child, ttk.Combobox):
            try:
                popdown = child.tk.call("ttk::combobox::PopdownWindow", child)
                child.tk.call(
                    f"{popdown}.f.l",
                    "configure",
                    "-background",
                    p.field,
                    "-foreground",
                    p.text,
                    "-selectbackground",
                    p.selection,
                    "-selectforeground",
                    p.selection_text,
                )
            except tk.TclError:  # pragma: no cover - Tk internals moved
                pass
        _restyle_popdowns(child, p)


def apply_theme(widget: tk.Misc, name: str) -> Palette:
    """Switch the whole interface to `name` and return the palette in use."""
    name = normalize(name)
    style = ttk.Style(widget)
    palette = palette_for(name)

    if name == SYSTEM:
        style.theme_use(_native_theme(style))
        # follow whatever background the platform theme draws frames with, so
        # the plain tk widgets do not stand out against the native chrome
        native_bg = style.lookup("TFrame", "background")
        if native_bg:
            palette = replace(palette, window=native_bg, surface=native_bg)
    else:
        _use_own_theme(style)
        _configure(style, palette)

    _configure_named(style, palette)

    # every window of the program hangs off the Tk root, so painting from
    # there reaches secondary windows (the Preferences dialog) as well
    root = widget.nametowidget(".")
    root.option_add("*TCombobox*Listbox.background", palette.field)
    root.option_add("*TCombobox*Listbox.foreground", palette.text)
    root.option_add("*TCombobox*Listbox.selectBackground", palette.selection)
    root.option_add("*TCombobox*Listbox.selectForeground", palette.selection_text)

    for window in [root, *_toplevels(root)]:
        try:
            window.configure(background=palette.window)
        except tk.TclError:  # pragma: no cover - window closing
            pass
    _restyle_popdowns(root, palette)
    return palette


def _toplevels(root: tk.Misc) -> list[tk.Misc]:
    return [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]


def text_options(p: Palette) -> dict:
    """Colours for a tk.Text / tk.Listbox, which ttk never touches."""
    return dict(
        background=p.field,
        foreground=p.text,
        insertbackground=p.text,
        selectbackground=p.selection,
        selectforeground=p.selection_text,
        highlightthickness=0,
        borderwidth=0,
    )
