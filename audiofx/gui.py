"""Tkinter interface for audiofx.

The song list on the left mirrors the `songs/` folder, the panel on the right
picks the effect, and "Convert" writes into `output/`. Conversions run on a
worker thread and report back through a queue that the UI drains on a timer.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import downloader, preview, theme
from .ffmpeg_runner import (
    AUDIO_EXTENSIONS,
    LOSSY_EXTENSIONS,
    MAX_STEREO_WIDTH,
    MAX_TONE_DB,
    REVERB_ROOMS,
    AudioInfo,
    FFmpegError,
    FxSpec,
    ReverbSettings,
    convert_file,
    ensure_tools,
    probe,
)
from .metadata import copy_metadata
from .presets import (
    Preset,
    PresetError,
    combined_presets,
    delete_user_preset,
    load_presets,
    save_user_preset,
    valid_preset_name,
)

SETTINGS_FILE = Path.home() / ".audiofx-gui.json"

MODES = [
    ("slow", "Slowed (slower)"),
    ("speed", "Sped up (faster)"),
    ("reverb", "Reverb only"),
    ("slowed_reverb", "Slowed + reverb"),
]

# label -> (output extension, fixed bitrate)
QUALITY_CHOICES: dict[str, tuple[str | None, str | None]] = {
    "Same as source (high bitrate)": (None, None),
    "FLAC (lossless)": ("flac", None),
    "WAV (lossless)": ("wav", None),
    "MP3 320 kbps": ("mp3", "320k"),
}

# label -> room key in REVERB_ROOMS; each one is really a decay time
ROOM_CHOICES: dict[str, str] = {
    "Small room": "small",
    "Medium room": "medium",
    "Large room": "large",
    "Concert hall": "hall",
    "Cathedral": "cathedral",
}

CUSTOM_PRESET = "Custom"
CUSTOM_ROOM = "Custom"

# label -> preview length in seconds; None renders the whole track
PREVIEW_LENGTHS: dict[str, float | None] = {
    "10 seconds": 10.0,
    "20 seconds": 20.0,
    "30 seconds": 30.0,
    "Whole song": None,
}

# mode -> (min, max) speed range
SPEED_RANGE = {
    "slow": (0.40, 0.99),
    "slowed_reverb": (0.40, 0.99),
    "speed": (1.01, 2.50),
    "reverb": (1.0, 1.0),
}


def workspace_root() -> Path:
    """Locate the project root (falls back to the working directory)."""
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").is_file() or (root / "songs").is_dir():
        return root
    return Path.cwd()


def unique_path(path: Path) -> Path:
    """Append _2, _3 ... instead of overwriting an existing file."""
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def open_file(path: Path) -> None:
    """Open a file or folder with the operating system's default handler."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":  # pragma: no cover - platform specific
        subprocess.Popen(["open", str(path)])
    else:  # pragma: no cover - platform specific
        subprocess.Popen(["xdg-open", str(path)])


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    open_file(path)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover


def human_duration(seconds: float | None) -> str:
    if not seconds:
        return "-"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


@dataclass
class Song:
    path: Path
    info: AudioInfo | None = None


@dataclass
class JobOptions:
    mode: str
    factor: float
    preserve_pitch: bool
    pitch_shift: float
    reverb_decay: float
    reverb_mix: float
    reverb_bass_cut: float
    quality: str
    preset_name: str | None
    copy_tags: bool
    output_dir: Path
    reverb_predelay: float = 0.0
    reverb_damping: float = 7000.0
    bass: float = 0.0
    treble: float = 0.0
    stereo_width: float = 1.0
    normalize: bool = False

    def build_spec(self) -> FxSpec:
        reverb = None
        if self.mode in ("reverb", "slowed_reverb"):
            reverb = ReverbSettings(
                decay=self.reverb_decay,
                predelay=self.reverb_predelay,
                mix=self.reverb_mix,
                highpass=self.reverb_bass_cut,
                lowpass=self.reverb_damping,
            )
        return FxSpec(
            tempo=1.0 if self.mode == "reverb" else self.factor,
            pitch_semitones=self.pitch_shift or None,
            preserve_pitch=self.preserve_pitch,
            reverb=reverb,
            engine="auto",
            bass_gain=self.bass,
            treble_gain=self.treble,
            stereo_width=self.stereo_width,
            normalize=self.normalize,
        )

    def to_preset(self, name: str, description: str = "") -> Preset:
        """The same settings as a saveable preset."""
        return Preset(
            name=name,
            description=description,
            mode=self.mode,
            factor=self.factor,
            pitch_shift=self.pitch_shift or None,
            preserve_pitch=self.preserve_pitch,
            engine="auto",
            bass=self.bass,
            treble=self.treble,
            stereo_width=self.stereo_width,
            normalize=self.normalize,
            reverb=self.build_spec().reverb,
        )


class AudioFxApp(ttk.Frame):
    def __init__(
        self,
        master: tk.Tk,
        songs_dir: Path | str | None = None,
        output_dir: Path | str | None = None,
        settings_file: Path | str | None = None,
    ) -> None:
        super().__init__(master, padding=8)
        self.master: tk.Tk = master
        root = workspace_root()
        # folders passed in explicitly win over whatever was saved last time
        self._explicit_songs = songs_dir is not None
        self._explicit_output = output_dir is not None
        self.songs_dir = Path(songs_dir) if songs_dir else root / "songs"
        self.output_dir = Path(output_dir) if output_dir else root / "output"
        self.settings_file = Path(settings_file) if settings_file else SETTINGS_FILE
        self.songs: list[Song] = []
        self.presets = {}
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.download_worker: threading.Thread | None = None
        self.download_cancel = threading.Event()
        self._loading_preset = False

        self.player = preview.Player()
        self.preview_worker: threading.Thread | None = None
        # bumped on every request; a render whose token is stale is discarded
        # instead of played, which is what makes "Stop" work mid-render
        self._preview_token = 0
        self._preview_playing = False
        self._preview_name = ""
        self._seeking = False
        self._playing_since: float | None = None
        self._preview_pace = 1.0
        self._preview_origin = 0.0
        self._preview_span: float | None = None
        self._builtin_names: set[str] = set()
        self.palette = theme.palette_for(theme.DEFAULT_THEME)
        self._prefs_window: tk.Toplevel | None = None

        try:
            self.presets = load_presets()
        except Exception as exc:  # pragma: no cover - broken yaml
            messagebox.showwarning("Presets", f"Could not read the preset file:\n{exc}")

        ir_dir = Path(__file__).parent / "assets" / "ir"
        self.ir_file = ir_dir / "hall_large.wav"

        self.theme_var = tk.StringVar(value=theme.DEFAULT_THEME)
        self._build_ui()
        self._load_settings()
        self.apply_theme()
        preview.clear_previews()
        self.refresh_songs()
        self._after_id: str | None = self.after(120, self._drain_queue)

    # ------------------------------------------------------------------
    # preferences menu + theme
    # ------------------------------------------------------------------

    def open_preferences(self) -> None:
        """Small settings window, opened from the top left corner.

        A menu bar (or a dropdown menu) is drawn by Windows in the system
        colours and would sit there as a white slab on top of a dark window;
        an ordinary Toplevel full of ttk widgets follows the palette.
        """
        if self._prefs_window is not None and self._prefs_window.winfo_exists():
            self._prefs_window.deiconify()
            self._prefs_window.lift()
            return

        window = tk.Toplevel(self.master)
        self._prefs_window = window
        window.title("Preferences")
        window.transient(self.master)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.close_preferences)

        box = ttk.Frame(window, padding=12)
        box.grid(sticky="nsew")
        ttk.Label(box, text="Theme:").grid(row=0, column=0, sticky="w")
        self.theme_combo = ttk.Combobox(
            box,
            state="readonly",
            width=16,
            values=list(theme.THEME_CHOICES.values()),
        )
        self.theme_combo.set(theme.THEME_CHOICES[theme.normalize(self.theme_var.get())])
        self.theme_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_choice)

        ttk.Label(
            box,
            text="Applied straight away and remembered for next time.",
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(box, text="Close", command=self.close_preferences).grid(
            row=2, column=1, sticky="e", pady=(14, 0)
        )
        self.apply_theme()

    def close_preferences(self) -> None:
        if self._prefs_window is not None:
            if self._prefs_window.winfo_exists():
                self._prefs_window.destroy()
            self._prefs_window = None

    def _on_theme_choice(self, _event=None) -> None:
        self.apply_theme(theme.value_for_label(self.theme_combo.get()))

    def apply_theme(self, name: str | None = None) -> None:
        """Repaint the interface; called from the Preferences window."""
        if name is not None:
            self.theme_var.set(theme.normalize(name))
        self.palette = theme.apply_theme(self, self.theme_var.get())
        self.log.configure(**theme.text_options(self.palette))
        if self._prefs_window is not None and self._prefs_window.winfo_exists():
            self._prefs_window.configure(background=self.palette.window)

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2)
        self.rowconfigure(2, weight=1)

        # --- top bar: songs folder ---
        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        top.columnconfigure(2, weight=1)
        ttk.Button(top, text="Preferences", command=self.open_preferences).grid(
            row=0, column=0, padx=(0, 12)
        )
        ttk.Label(top, text="Songs folder:").grid(row=0, column=1, padx=(0, 6))
        self.songs_var = tk.StringVar(value=str(self.songs_dir))
        ttk.Entry(top, textvariable=self.songs_var, state="readonly").grid(
            row=0, column=2, sticky="ew"
        )
        ttk.Button(top, text="Change", command=self.choose_songs_dir).grid(row=0, column=3, padx=4)
        ttk.Button(top, text="Open folder", command=lambda: open_folder(self.songs_dir)).grid(
            row=0, column=4
        )
        ttk.Button(top, text="Refresh", command=self.refresh_songs).grid(row=0, column=5, padx=4)

        # --- optional spotdl download box ---
        download_box = ttk.LabelFrame(self, text="Download with spotdl (optional)", padding=6)
        download_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        download_box.columnconfigure(0, weight=1)

        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(download_box, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _event: self.download_song())
        ttk.Button(download_box, text="Paste", command=self.paste_url).grid(
            row=0, column=1, padx=(6, 0)
        )
        self.download_button = ttk.Button(
            download_box, text="Download", command=self.download_song, width=10
        )
        self.download_button.grid(row=0, column=2, padx=(6, 0))
        self.download_cancel_button = ttk.Button(
            download_box, text="Stop", command=self.cancel_download, state="disabled", width=8
        )
        self.download_cancel_button.grid(row=0, column=3, padx=(6, 0))

        self.download_hint = ttk.Label(
            download_box,
            text="Runs spotdl and saves into the songs folder. Only use it for material "
            "you have the rights to.",
            style="Hint.TLabel",
            justify="left",
        )
        self.download_hint.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        if not downloader.spotdl_available():
            self.download_button.configure(state="disabled")
            self.url_entry.configure(state="disabled")
            self.download_hint.configure(
                text="spotdl is not installed. Install it with: pip install spotdl",
                style="Warn.TLabel",
            )

        # --- left: song list ---
        left = ttk.LabelFrame(self, text="Songs", padding=6)
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        columns = ("length", "format", "size")
        self.tree = ttk.Treeview(left, columns=columns, selectmode="extended")
        self.tree.heading("#0", text="File")
        self.tree.heading("length", text="Length")
        self.tree.heading("format", text="Format")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=280, stretch=True)
        self.tree.column("length", width=60, anchor="e", stretch=False)
        self.tree.column("format", width=70, anchor="center", stretch=False)
        self.tree.column("size", width=80, anchor="e", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self._play_selected)
        self.tree.bind("<<TreeviewSelect>>", self._update_preview_range)

        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        list_bar = ttk.Frame(left)
        list_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            list_bar,
            text="Include subfolders",
            variable=self.recursive_var,
            command=self.refresh_songs,
        ).pack(side="left")
        ttk.Button(list_bar, text="Select all", command=self.select_all).pack(side="right")

        # --- preview: under the list, because it works on the selected song ---
        preview_box = ttk.LabelFrame(left, text="Preview", padding=6)
        preview_box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        preview_box.columnconfigure(6, weight=1)

        self.preview_button = ttk.Button(
            preview_box, text="Play preview", command=self.preview_selected, width=13
        )
        self.preview_button.grid(row=0, column=0, rowspan=2, sticky="w")
        self.preview_stop_button = ttk.Button(
            preview_box, text="Stop", command=self.stop_preview, state="disabled", width=7
        )
        self.preview_stop_button.grid(row=0, column=1, rowspan=2, padx=(6, 10))

        # the seek bar: drag it to choose where the excerpt starts, and watch it
        # travel while the excerpt plays
        self.preview_position_var = tk.DoubleVar(value=preview.DEFAULT_START)
        self.preview_scale = ttk.Scale(
            preview_box,
            from_=0,
            to=preview.DEFAULT_START * 2,
            orient="horizontal",
            variable=self.preview_position_var,
            command=lambda _=None: self._update_preview_time(),
        )
        self.preview_scale.grid(row=0, column=2, columnspan=3, sticky="ew", padx=(0, 8))
        self.preview_scale.bind("<ButtonPress-1>", self._seek_press)
        self.preview_scale.bind("<ButtonRelease-1>", self._seek_release)

        self.preview_time_label = ttk.Label(preview_box, text="0:00 / 0:00", width=13)
        self.preview_time_label.grid(row=0, column=5, sticky="e")

        ttk.Label(preview_box, text="Length:").grid(row=1, column=2, sticky="w")
        self.preview_length_var = tk.StringVar(value="20 seconds")
        ttk.Combobox(
            preview_box,
            textvariable=self.preview_length_var,
            state="readonly",
            values=list(PREVIEW_LENGTHS),
            width=12,
        ).grid(row=1, column=3, sticky="w", padx=(6, 0))
        ttk.Label(
            preview_box,
            text="from wherever the bar sits",
            style="Hint.TLabel",
        ).grid(row=1, column=4, columnspan=2, sticky="w", padx=(8, 0))

        # --- right: settings, on tabs so there is room to add controls ---
        right = ttk.Frame(self)
        right.grid(row=2, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        preset_box = ttk.LabelFrame(right, text="Preset", padding=6)
        preset_box.grid(row=0, column=0, sticky="ew")
        preset_box.columnconfigure(0, weight=1)
        self.preset_var = tk.StringVar(value=CUSTOM_PRESET)
        self.preset_combo = ttk.Combobox(
            preset_box, textvariable=self.preset_var, state="readonly"
        )
        self.preset_combo.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.preset_combo.bind("<<ComboboxSelected>>", self._apply_preset)
        self.preset_hint = ttk.Label(preset_box, text="", style="Hint.TLabel", wraplength=300)
        self.preset_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 4))
        ttk.Button(preset_box, text="Save as...", command=self.save_preset).grid(
            row=2, column=0, sticky="w"
        )
        self.delete_preset_button = ttk.Button(
            preset_box, text="Delete", command=self.delete_preset, state="disabled", width=9
        )
        self.delete_preset_button.grid(row=2, column=1, sticky="e")
        self._refresh_preset_list()

        notebook = ttk.Notebook(right)
        notebook.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

        # --- Effect tab ---
        effect = ttk.Frame(notebook, padding=8)
        effect.columnconfigure(1, weight=1)
        notebook.add(effect, text="Effect")

        self.mode_var = tk.StringVar(value="slowed_reverb")
        for index, (value, label) in enumerate(MODES):
            ttk.Radiobutton(
                effect,
                text=label,
                value=value,
                variable=self.mode_var,
                command=self._on_mode_change,
            ).grid(row=index, column=0, columnspan=3, sticky="w")

        speed_row = len(MODES)
        ttk.Separator(effect, orient="horizontal").grid(
            row=speed_row, column=0, columnspan=3, sticky="ew", pady=6
        )
        self.factor_var = tk.DoubleVar(value=0.85)
        self.speed_scale, self.speed_label = self._slider_row(
            effect, speed_row + 1, "Speed:", self.factor_var, 0.40, 0.99,
            self._on_speed_change,
        )
        nudge = ttk.Frame(effect)
        nudge.grid(row=speed_row + 2, column=1, sticky="w", padx=(6, 0))
        ttk.Button(nudge, text="-", width=3, command=lambda: self._nudge(-0.01)).pack(side="left")
        ttk.Button(nudge, text="+", width=3, command=lambda: self._nudge(0.01)).pack(
            side="left", padx=(4, 0)
        )

        self.preserve_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            effect,
            text="Keep the original pitch",
            variable=self.preserve_var,
            command=self._mark_custom,
        ).grid(row=speed_row + 3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Label(effect, text="Pitch shift:").grid(row=speed_row + 4, column=0, sticky="w")
        self.pitch_var = tk.DoubleVar(value=0.0)
        ttk.Spinbox(
            effect,
            from_=-12,
            to=12,
            increment=1,
            width=6,
            textvariable=self.pitch_var,
            command=self._mark_custom,
        ).grid(row=speed_row + 4, column=1, sticky="w", padx=(6, 0))
        ttk.Label(effect, text="semitones", style="Hint.TLabel").grid(
            row=speed_row + 5, column=1, sticky="w", padx=(6, 0)
        )

        # --- Reverb tab ---
        self.reverb_box = ttk.Frame(notebook, padding=8)
        self.reverb_box.columnconfigure(1, weight=1)
        notebook.add(self.reverb_box, text="Reverb")

        ttk.Label(self.reverb_box, text="Room:").grid(row=0, column=0, sticky="w")
        self.room_var = tk.StringVar(value="Medium room")
        self.room_combo = ttk.Combobox(
            self.reverb_box,
            textvariable=self.room_var,
            state="readonly",
            values=[*ROOM_CHOICES, CUSTOM_ROOM],
            width=16,
        )
        self.room_combo.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.room_combo.bind("<<ComboboxSelected>>", self._on_room_change)

        self.decay_var = tk.DoubleVar(value=REVERB_ROOMS["medium"])
        self.decay_scale, self.decay_label = self._slider_row(
            self.reverb_box, 1, "Decay:", self.decay_var, 0.2, 8.0, self._on_decay_change
        )
        self.mix_var = tk.DoubleVar(value=0.35)
        self.mix_scale, self.mix_label = self._slider_row(
            self.reverb_box, 2, "Amount:", self.mix_var, 0.05, 1.0, self._on_mix_change
        )
        self.predelay_var = tk.DoubleVar(value=0.0)
        self.predelay_scale, self.predelay_label = self._slider_row(
            self.reverb_box, 3, "Pre-delay:", self.predelay_var, 0, 200, self._on_slider_change
        )

        ttk.Label(self.reverb_box, text="Bass cut:").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.bass_cut_var = tk.DoubleVar(value=200.0)
        self.bass_cut_spin = ttk.Spinbox(
            self.reverb_box,
            from_=0,
            to=600,
            increment=20,
            width=7,
            textvariable=self.bass_cut_var,
            command=self._mark_custom,
        )
        self.bass_cut_spin.grid(row=4, column=1, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self.reverb_box, text="Damping:").grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.damping_var = tk.DoubleVar(value=7000.0)
        self.damping_spin = ttk.Spinbox(
            self.reverb_box,
            from_=1000,
            to=16000,
            increment=500,
            width=7,
            textvariable=self.damping_var,
            command=self._mark_custom,
        )
        self.damping_spin.grid(row=5, column=1, sticky="w", padx=(6, 0), pady=(4, 0))

        ttk.Label(
            self.reverb_box,
            text="Decay is how long the tail takes to fade out - it is what makes a\n"
            "small room and a cathedral sound different. Bass cut keeps the low\n"
            "end out of the tail (raise it if it sounds boomy); damping rolls the\n"
            "highs off, the way a real room absorbs them.",
            style="Hint.TLabel",
            justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self._reverb_widgets = [
            self.room_combo,
            self.decay_scale,
            self.mix_scale,
            self.predelay_scale,
            self.bass_cut_spin,
            self.damping_spin,
        ]

        # --- Tone tab ---
        tone = ttk.Frame(notebook, padding=8)
        tone.columnconfigure(1, weight=1)
        notebook.add(tone, text="Tone")

        self.bass_var = tk.DoubleVar(value=0.0)
        self.bass_scale, self.bass_label = self._slider_row(
            tone, 0, "Bass:", self.bass_var, -MAX_TONE_DB, MAX_TONE_DB, self._on_slider_change
        )
        self.treble_var = tk.DoubleVar(value=0.0)
        self.treble_scale, self.treble_label = self._slider_row(
            tone, 1, "Treble:", self.treble_var, -MAX_TONE_DB, MAX_TONE_DB,
            self._on_slider_change,
        )
        self.width_var = tk.DoubleVar(value=1.0)
        self.width_scale, self.width_label = self._slider_row(
            tone, 2, "Stereo width:", self.width_var, 0.0, MAX_STEREO_WIDTH,
            self._on_slider_change,
        )
        self.normalize_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            tone,
            text="Even out the loudness (about -14 LUFS)",
            variable=self.normalize_var,
            command=self._mark_custom,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        ttk.Button(tone, text="Reset tone", command=self.reset_tone).grid(
            row=4, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Label(
            tone,
            text="Bass and treble are shelves applied before the reverb, so the tail\n"
            "reacts to them. Stereo width of 1.0 leaves the image alone, 0 folds it\n"
            "down to mono, and anything above 1 pushes the sides out.",
            style="Hint.TLabel",
            justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # --- Output tab ---
        out_box = ttk.Frame(notebook, padding=8)
        out_box.columnconfigure(0, weight=1)
        notebook.add(out_box, text="Output")

        self.quality_var = tk.StringVar(value=next(iter(QUALITY_CHOICES)))
        ttk.Combobox(
            out_box,
            textvariable=self.quality_var,
            state="readonly",
            values=list(QUALITY_CHOICES),
        ).grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(
            out_box,
            text="MP3 -> MP3 loses a little quality; pick FLAC to keep all of it.",
            style="Hint.TLabel",
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 4))
        self.tags_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            out_box, text="Copy tags and cover art", variable=self.tags_var
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        self.output_var = tk.StringVar(value=str(self.output_dir))
        ttk.Entry(out_box, textvariable=self.output_var, state="readonly").grid(
            row=3, column=0, sticky="ew", pady=(8, 0)
        )
        out_buttons = ttk.Frame(out_box)
        out_buttons.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(out_buttons, text="Change", command=self.choose_output_dir).pack(side="left")
        ttk.Button(
            out_buttons, text="Open folder", command=lambda: open_folder(self.output_dir)
        ).pack(side="left", padx=6)

        action = ttk.Frame(right)
        action.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        action.columnconfigure(0, weight=1)
        self.convert_button = ttk.Button(
            action, text="Convert selected", command=self.convert_selected
        )
        self.convert_button.grid(row=0, column=0, sticky="ew")
        self.all_button = ttk.Button(action, text="All", command=self.convert_all, width=8)
        self.all_button.grid(row=0, column=1, padx=(6, 0))
        self.cancel_button = ttk.Button(
            action, text="Cancel", command=self.cancel, state="disabled", width=8
        )
        self.cancel_button.grid(row=0, column=2, padx=(6, 0))

        # --- bottom: progress + log ---
        bottom = ttk.Frame(self)
        bottom.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(2, weight=1)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bottom, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=2)

        log_frame = ttk.Frame(bottom)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        # deliberately short: row 2 carries the grid weight, so every pixel the
        # log does not claim goes to the song list instead
        self.log = tk.Text(log_frame, height=3, state="disabled", wrap="none")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

        self._on_mode_change()
        self._update_labels()
        self._update_preview_time()

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        songs = data.get("songs_dir")
        output = data.get("output_dir")
        if songs and not self._explicit_songs and Path(songs).is_dir():
            self.songs_dir = Path(songs)
            self.songs_var.set(songs)
        if output and not self._explicit_output:
            self.output_dir = Path(output)
            self.output_var.set(output)
        if data.get("quality") in QUALITY_CHOICES:
            self.quality_var.set(data["quality"])
        if data.get("mode") in dict(MODES):
            self.mode_var.set(data["mode"])
        if isinstance(data.get("factor"), (int, float)):
            self.factor_var.set(float(data["factor"]))
        # the room name is derived from the decay time, not stored
        if isinstance(data.get("reverb_mix"), (int, float)):
            self.mix_var.set(float(data["reverb_mix"]))
        for key, variable in (
            ("reverb_bass_cut", self.bass_cut_var),
            ("reverb_damping", self.damping_var),
            ("reverb_decay", self.decay_var),
            ("reverb_predelay", self.predelay_var),
            ("bass", self.bass_var),
            ("treble", self.treble_var),
            ("stereo_width", self.width_var),
            ("preview_start", self.preview_position_var),
        ):
            if isinstance(data.get(key), (int, float)):
                variable.set(float(data[key]))
        if data.get("theme") in theme.THEME_CHOICES:
            self.theme_var.set(data["theme"])
        if data.get("preview_length") in PREVIEW_LENGTHS:
            self.preview_length_var.set(data["preview_length"])
        self.normalize_var.set(bool(data.get("normalize", False)))
        self.preserve_var.set(bool(data.get("preserve_pitch", False)))
        self.recursive_var.set(bool(data.get("recursive", False)))
        self.room_var.set(self._room_label_for_decay(self.decay_var.get()))
        self._on_mode_change()
        self._update_labels()

    def save_settings(self) -> None:
        payload = {
            "songs_dir": str(self.songs_dir),
            "output_dir": str(self.output_dir),
            "quality": self.quality_var.get(),
            "mode": self.mode_var.get(),
            "factor": round(float(self.factor_var.get()), 3),
            "reverb_mix": round(float(self.mix_var.get()), 3),
            "reverb_decay": round(float(self.decay_var.get()), 2),
            "reverb_predelay": round(float(self.predelay_var.get()), 1),
            "reverb_bass_cut": round(float(self.bass_cut_var.get()), 1),
            "reverb_damping": round(float(self.damping_var.get()), 1),
            "bass": round(float(self.bass_var.get()), 2),
            "treble": round(float(self.treble_var.get()), 2),
            "stereo_width": round(float(self.width_var.get()), 3),
            "normalize": bool(self.normalize_var.get()),
            "preserve_pitch": bool(self.preserve_var.get()),
            "recursive": bool(self.recursive_var.get()),
            "theme": theme.normalize(self.theme_var.get()),
            "preview_length": self.preview_length_var.get(),
            "preview_start": round(float(self.preview_position_var.get()), 1),
        }
        try:
            self.settings_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:  # pragma: no cover - disk error
            pass

    # ------------------------------------------------------------------
    # song list
    # ------------------------------------------------------------------

    def refresh_songs(self) -> None:
        self.songs_dir.mkdir(parents=True, exist_ok=True)
        pattern = "**/*" if self.recursive_var.get() else "*"
        paths = sorted(
            path
            for path in self.songs_dir.glob(pattern)
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
        )
        self.songs = [Song(path) for path in paths]

        self.tree.delete(*self.tree.get_children())
        for index, song in enumerate(self.songs):
            try:
                relative = song.path.relative_to(self.songs_dir)
            except ValueError:  # pragma: no cover
                relative = song.path
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                text=str(relative),
                values=(
                    "...",
                    song.path.suffix.lstrip(".").upper(),
                    human_size(song.path.stat().st_size),
                ),
            )

        if not self.songs:
            self.status_var.set(f"No audio files in {self.songs_dir} - copy some in.")
        else:
            self.status_var.set(f"{len(self.songs)} files listed.")
            threading.Thread(target=self._probe_all, daemon=True).start()

    def _probe_all(self) -> None:
        for index, song in enumerate(list(self.songs)):
            try:
                info = probe(song.path)
            except Exception:
                continue
            self.queue.put(("info", index, info))

    def select_all(self) -> None:
        self.tree.selection_set(self.tree.get_children())

    def _selected_songs(self) -> list[Song]:
        return [self.songs[int(iid)] for iid in self.tree.selection() if iid.isdigit()]

    def _play_selected(self, _event=None) -> None:
        """Double click: open the file in the default player."""
        songs = self._selected_songs()
        if not songs:
            return
        try:
            open_file(songs[0].path)
        except Exception as exc:  # pragma: no cover - platform specific
            messagebox.showwarning("Could not play", str(exc))

    # ------------------------------------------------------------------
    # control callbacks
    # ------------------------------------------------------------------

    def _slider_row(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
        variable: tk.DoubleVar,
        low: float,
        high: float,
        command,
    ) -> tuple[ttk.Scale, ttk.Label]:
        """One "label / slider / value" line, the shape every setting uses."""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(4, 0))
        scale = ttk.Scale(
            parent,
            from_=low,
            to=high,
            orient="horizontal",
            variable=variable,
            command=lambda _=None: command(),
        )
        scale.grid(row=row, column=1, sticky="ew", padx=(6, 6), pady=(4, 0))
        value = ttk.Label(parent, text="", width=11, anchor="e")
        value.grid(row=row, column=2, sticky="e", pady=(4, 0))
        return scale, value

    def _mark_custom(self) -> None:
        if not self._loading_preset:
            self.preset_var.set(CUSTOM_PRESET)
            self._update_preset_hint()

    def _on_slider_change(self) -> None:
        self._update_labels()
        self._mark_custom()

    def _on_speed_change(self) -> None:
        self._update_labels()
        self._mark_custom()

    def _on_mix_change(self) -> None:
        self._update_labels()
        self._mark_custom()

    def _on_decay_change(self) -> None:
        """Moving the decay slider by hand means the room is no longer a preset."""
        if not self._loading_preset:
            self.room_var.set(self._room_label_for_decay(self.decay_var.get()))
        self._update_labels()
        self._mark_custom()

    def _on_room_change(self, _event=None) -> None:
        key = ROOM_CHOICES.get(self.room_var.get())
        if key is not None:
            self.decay_var.set(REVERB_ROOMS[key])
        self._update_labels()
        self._mark_custom()

    @staticmethod
    def _room_label_for_decay(decay: float) -> str:
        for label, key in ROOM_CHOICES.items():
            if abs(REVERB_ROOMS[key] - decay) < 0.05:
                return label
        return CUSTOM_ROOM

    def reset_tone(self) -> None:
        self.bass_var.set(0.0)
        self.treble_var.set(0.0)
        self.width_var.set(1.0)
        self.normalize_var.set(False)
        self._on_slider_change()

    def _nudge(self, delta: float) -> None:
        low, high = SPEED_RANGE[self.mode_var.get()]
        value = min(high, max(low, round(self.factor_var.get() + delta, 3)))
        self.factor_var.set(value)
        self._on_speed_change()

    def _update_labels(self) -> None:
        """Refresh every read-out next to a slider."""
        factor = float(self.factor_var.get())
        if self.mode_var.get() == "reverb":
            self.speed_label.configure(text="1.00x")
        else:
            percent = abs(1 - factor) * 100
            direction = "slower" if factor < 1 else "faster"
            self.speed_label.configure(text=f"{factor:.2f}x {percent:.0f}% {direction}")

        self.decay_label.configure(text=f"{float(self.decay_var.get()):.1f} s")
        self.mix_label.configure(text=f"{float(self.mix_var.get()) * 100:.0f}% wet")
        self.predelay_label.configure(text=f"{float(self.predelay_var.get()):.0f} ms")
        self.bass_label.configure(text=f"{float(self.bass_var.get()):+.1f} dB")
        self.treble_label.configure(text=f"{float(self.treble_var.get()):+.1f} dB")
        width = float(self.width_var.get())
        self.width_label.configure(
            text="mono" if width < 0.05 else f"{width:.2f}x"
        )

    def _on_mode_change(self) -> None:
        mode = self.mode_var.get()
        low, high = SPEED_RANGE[mode]
        reverb_on = mode in ("reverb", "slowed_reverb")
        speed_on = mode != "reverb"

        self.speed_scale.configure(from_=low, to=high, state="normal" if speed_on else "disabled")
        if speed_on:
            value = float(self.factor_var.get())
            if not low <= value <= high:
                self.factor_var.set(0.85 if mode != "speed" else 1.25)
        else:
            self.factor_var.set(1.0)

        for widget in self._reverb_widgets:
            widget.configure(state="normal" if reverb_on else "disabled")
        if reverb_on:
            # comboboxes must stay read-only, never fully editable
            self.room_combo.configure(state="readonly")

        self._update_labels()
        self._mark_custom()

    def _apply_preset(self, _event=None) -> None:
        name = self.preset_var.get()
        preset = self.presets.get(name)
        if preset is None:
            self._update_preset_hint()
            return
        self._loading_preset = True
        try:
            self.mode_var.set(preset.mode)
            self._on_mode_change()
            self.factor_var.set(preset.factor if preset.mode != "reverb" else 1.0)
            self.preserve_var.set(preset.preserve_pitch)
            self.pitch_var.set(preset.pitch_shift or 0.0)
            self.bass_var.set(preset.bass)
            self.treble_var.set(preset.treble)
            self.width_var.set(preset.stereo_width)
            self.normalize_var.set(preset.normalize)
            if preset.reverb is not None:
                self.mix_var.set(preset.reverb.mix)
                self.bass_cut_var.set(preset.reverb.highpass)
                self.damping_var.set(preset.reverb.lowpass)
                self.predelay_var.set(preset.reverb.predelay)
                self.decay_var.set(preset.reverb.decay)
                self.room_var.set(self._room_label_for_decay(preset.reverb.decay))
            self._update_labels()
        finally:
            self._loading_preset = False
        self.preset_var.set(name)
        self._update_preset_hint()

    # ------------------------------------------------------------------
    # saving presets
    # ------------------------------------------------------------------

    def _refresh_preset_list(self) -> None:
        """Reload the built-in and user presets into the combobox."""
        try:
            self.presets = combined_presets()
        except PresetError as exc:  # pragma: no cover - broken yaml
            messagebox.showwarning("Presets", str(exc))
            return
        try:
            self._builtin_names = set(load_presets())
        except PresetError:  # pragma: no cover - broken package file
            self._builtin_names = set()
        self.preset_combo.configure(values=[CUSTOM_PRESET] + sorted(self.presets))
        self._update_preset_hint()

    def _update_preset_hint(self) -> None:
        name = self.preset_var.get()
        preset = self.presets.get(name)
        own = preset is not None and name not in self._builtin_names
        self.delete_preset_button.configure(state="normal" if own else "disabled")
        if preset is None:
            self.preset_hint.configure(text="Your own settings. Save them to reuse later.")
        else:
            suffix = "  (yours)" if own else ""
            self.preset_hint.configure(text=f"{preset.description}{suffix}")

    def save_preset(self) -> None:
        name = self._ask_text("Save preset", "Name for these settings:")
        if name is None:
            return
        try:
            name = valid_preset_name(name)
        except PresetError as exc:
            messagebox.showwarning("Preset name", str(exc))
            return
        if name in self.presets and not messagebox.askyesno(
            "Replace preset", f"'{name}' already exists. Replace it?"
        ):
            return
        try:
            save_user_preset(self._current_options().to_preset(name, "saved from the interface"))
        except (PresetError, OSError) as exc:
            messagebox.showerror("Could not save", str(exc))
            return
        self._refresh_preset_list()
        self._loading_preset = True
        try:
            self.preset_var.set(name)
        finally:
            self._loading_preset = False
        self._update_preset_hint()
        self._log(f"Saved preset '{name}'.")

    def delete_preset(self) -> None:
        name = self.preset_var.get()
        if name in self._builtin_names or name not in self.presets:
            return
        if not messagebox.askyesno("Delete preset", f"Delete the preset '{name}'?"):
            return
        try:
            delete_user_preset(name)
        except (PresetError, OSError) as exc:  # pragma: no cover - disk error
            messagebox.showerror("Could not delete", str(exc))
            return
        self._refresh_preset_list()
        self.preset_var.set(CUSTOM_PRESET)
        self._update_preset_hint()
        self._log(f"Deleted preset '{name}'.")

    def _ask_text(self, title: str, prompt: str) -> str | None:
        """A themed stand-in for simpledialog, which is plain tk and stays light."""
        window = tk.Toplevel(self.master)
        window.title(title)
        window.transient(self.master)
        window.resizable(False, False)
        window.configure(background=self.palette.window)

        answer: dict[str, str | None] = {"value": None}
        box = ttk.Frame(window, padding=12)
        box.grid(sticky="nsew")
        ttk.Label(box, text=prompt).grid(row=0, column=0, columnspan=2, sticky="w")
        entry = ttk.Entry(box, width=32)
        entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        entry.focus_set()

        def accept(_event=None) -> None:
            answer["value"] = entry.get()
            window.destroy()

        entry.bind("<Return>", accept)
        entry.bind("<Escape>", lambda _event: window.destroy())
        ttk.Button(box, text="Save", command=accept).grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Button(box, text="Cancel", command=window.destroy).grid(
            row=2, column=1, sticky="e", pady=(12, 0)
        )
        window.grab_set()
        self.master.wait_window(window)
        return answer["value"]

    def choose_songs_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(self.songs_dir), title="Songs folder")
        if chosen:
            self.songs_dir = Path(chosen)
            self.songs_var.set(chosen)
            self.refresh_songs()

    def choose_output_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(self.output_dir), title="Output folder")
        if chosen:
            self.output_dir = Path(chosen)
            self.output_var.set(chosen)

    # ------------------------------------------------------------------
    # optional spotdl download
    # ------------------------------------------------------------------

    def paste_url(self) -> None:
        """Copy whatever is on the clipboard into the entry."""
        try:
            text = self.master.clipboard_get()
        except tk.TclError:
            return
        self.url_var.set(text.strip())

    def _download_running(self) -> bool:
        return bool(self.download_worker and self.download_worker.is_alive())

    def download_song(self) -> None:
        if self._download_running():
            return
        try:
            query = downloader.normalize_query(self.url_var.get())
        except ValueError:
            messagebox.showinfo("Nothing to download", "Paste a link (or type a song name) first.")
            return
        try:
            downloader.find_spotdl()
        except downloader.SpotdlNotFoundError as exc:
            messagebox.showerror("spotdl not found", str(exc))
            return

        self.download_cancel.clear()
        self.download_button.configure(state="disabled")
        self.download_cancel_button.configure(state="normal")
        self.status_var.set(f"Downloading: {query}")
        self._log(f"--- spotdl: {query} ---")

        self.download_worker = threading.Thread(
            target=self._run_download, args=(query,), daemon=True
        )
        self.download_worker.start()

    def cancel_download(self) -> None:
        self.download_cancel.set()
        self.status_var.set("Stopping the download...")

    def _run_download(self, query: str) -> None:
        try:
            new_files = downloader.download(
                query,
                self.songs_dir,
                on_line=lambda line: self.queue.put(("log", f"    {line}")),
                cancel_event=self.download_cancel,
            )
        except Exception as exc:
            self.queue.put(("download_finished", None, str(exc)))
            return
        for path in new_files:
            self.queue.put(("log", f"[OK] {path.name}"))
        self.queue.put(("download_finished", downloader.summarize(new_files), None))

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------

    def _preview_song(self) -> Song | None:
        """What to preview: the first selected file, else the first listed."""
        selected = self._selected_songs()
        if selected:
            return selected[0]
        return self.songs[0] if self.songs else None

    def _preview_start(self) -> float:
        try:
            return float(self.preview_position_var.get())
        except (tk.TclError, ValueError):
            return preview.DEFAULT_START

    def _preview_duration(self) -> float:
        song = self._preview_song()
        if song is not None and song.info and song.info.duration:
            return float(song.info.duration)
        return 0.0

    def _update_preview_range(self, _event=None) -> None:
        """Fit the seek bar to whichever song is selected."""
        duration = self._preview_duration()
        self.preview_scale.configure(to=max(duration, 1.0))
        if duration and self._preview_start() > duration:
            self.preview_position_var.set(0.0)
        self._update_preview_time()

    def _update_preview_time(self) -> None:
        position = self._preview_start()
        duration = self._preview_duration()
        self.preview_time_label.configure(
            text=f"{human_duration(position) if position else '0:00'}"
            f" / {human_duration(duration)}"
        )

    def _seek_press(self, _event=None) -> None:
        self._seeking = True

    def _seek_release(self, _event=None) -> None:
        self._seeking = False
        self._update_preview_time()
        self._mark_custom_position()

    def _mark_custom_position(self) -> None:
        """Dragging the bar while a preview plays restarts it from there."""
        if self.player.is_playing() or (
            self.preview_worker and self.preview_worker.is_alive()
        ):
            self.preview_selected()

    def preview_selected(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(
                "Conversion in progress", "Wait for the conversion to finish first."
            )
            return
        if self.preview_worker and self.preview_worker.is_alive():
            return
        song = self._preview_song()
        if song is None:
            messagebox.showinfo(
                "Nothing to preview", "Add some files to the songs folder first."
            )
            return

        options = self._current_options()
        length = PREVIEW_LENGTHS.get(self.preview_length_var.get(), preview.DEFAULT_LENGTH)
        total = song.info.duration if song.info else None
        start = preview.clamp_start(self._preview_start(), total, length)

        self.player.stop()
        self._preview_token += 1
        self._preview_name = song.path.name
        # the bar walks the source timeline, so it advances at the playback
        # speed the effect produces, not at wall-clock speed
        self._preview_pace = options.build_spec().tempo
        self._preview_origin = start
        self._preview_span = length
        self.preview_position_var.set(start)
        self.preview_button.configure(state="disabled")
        self.preview_stop_button.configure(state="normal")
        self.status_var.set(f"Rendering preview: {song.path.name}")

        self.preview_worker = threading.Thread(
            target=self._run_preview,
            args=(song, options, start, length, self._preview_token),
            daemon=True,
        )
        self.preview_worker.start()

    def stop_preview(self) -> None:
        # bumping the token discards a render that is still running, so
        # pressing Stop during the render does not end in sudden playback
        self._preview_token += 1
        self.player.stop()
        self._preview_playing = False
        self.preview_button.configure(state="normal")
        self.preview_stop_button.configure(state="disabled")
        if self.status_var.get().startswith(("Playing preview", "Rendering preview")):
            self.status_var.set("Preview stopped.")

    def _run_preview(
        self,
        song: Song,
        options: JobOptions,
        start: float,
        length: float | None,
        token: int,
    ) -> None:
        try:
            path = preview.render(
                song.path, options.build_spec(), start=start, length=length
            )
        except Exception as exc:
            self.queue.put(("preview_finished", token, None, str(exc)))
            return
        self.queue.put(("preview_finished", token, path, None))

    def _preview_ready(self, token: int, path: Path | None, error: str | None) -> None:
        self.preview_button.configure(state="normal")
        if token != self._preview_token:  # stopped while it was rendering
            preview.clear_previews()
            return
        if error is not None:
            self.preview_stop_button.configure(state="disabled")
            self.status_var.set("Preview failed.")
            self._log(f"[ERROR] preview: {error}")
            messagebox.showerror("Preview failed", error)
            return

        assert path is not None
        preview.clear_previews(keep=path)
        try:
            self.player.play(path)
        except preview.PlayerNotFoundError as exc:
            self.preview_stop_button.configure(state="disabled")
            self._log(f"    {exc}")
            try:
                open_file(path)
            except Exception as failure:  # pragma: no cover - platform specific
                messagebox.showwarning("Could not play", str(failure))
                return
            self.status_var.set("Preview opened in the default player.")
            return

        self._preview_playing = True
        self._playing_since = time.monotonic()
        self.preview_stop_button.configure(state="normal")
        self.status_var.set(f"Playing preview: {self._preview_name}")

    def _poll_player(self) -> None:
        """Advance the seek bar, and notice when playback ended on its own."""
        playing = self.player.is_playing()
        if playing and not self._seeking and self._playing_since is not None:
            elapsed = time.monotonic() - self._playing_since
            position = self._preview_origin + elapsed * self._preview_pace
            if self._preview_span:
                position = min(position, self._preview_origin + self._preview_span)
            duration = self._preview_duration()
            self.preview_position_var.set(min(position, duration) if duration else position)
            self._update_preview_time()

        if playing == self._preview_playing:
            return
        self._preview_playing = playing
        self.preview_stop_button.configure(state="normal" if playing else "disabled")
        if not playing:
            self._playing_since = None
            if self.status_var.get().startswith("Playing preview"):
                self.status_var.set("Preview finished.")

    # ------------------------------------------------------------------
    # conversion
    # ------------------------------------------------------------------

    def _current_options(self) -> JobOptions:
        preset = self.preset_var.get()
        return JobOptions(
            mode=self.mode_var.get(),
            factor=round(float(self.factor_var.get()), 3),
            preserve_pitch=bool(self.preserve_var.get()),
            pitch_shift=float(self.pitch_var.get() or 0),
            reverb_decay=round(float(self.decay_var.get()), 2),
            reverb_predelay=round(float(self.predelay_var.get()), 1),
            reverb_mix=round(float(self.mix_var.get()), 3),
            reverb_bass_cut=float(self.bass_cut_var.get()),
            reverb_damping=float(self.damping_var.get()),
            bass=round(float(self.bass_var.get()), 2),
            treble=round(float(self.treble_var.get()), 2),
            stereo_width=round(float(self.width_var.get()), 3),
            normalize=bool(self.normalize_var.get()),
            quality=self.quality_var.get(),
            preset_name=None if preset == CUSTOM_PRESET else preset,
            copy_tags=bool(self.tags_var.get()),
            output_dir=self.output_dir,
        )

    def convert_selected(self) -> None:
        songs = self._selected_songs()
        if not songs:
            messagebox.showinfo("Nothing selected", "Select at least one song from the list.")
            return
        self._start(songs)

    def convert_all(self) -> None:
        if not self.songs:
            messagebox.showinfo("Empty list", "Add some files to the songs folder first.")
            return
        self._start(list(self.songs))

    def _start(self, songs: list[Song]) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self._download_running():
            messagebox.showinfo("Download in progress", "Wait for the download to finish first.")
            return
        # a preview playing over the conversion only confuses the status line
        self.stop_preview()
        options = self._current_options()

        self.cancel_event.clear()
        self.convert_button.configure(state="disabled")
        self.all_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.configure(maximum=len(songs), value=0)
        self._log(
            f"--- {len(songs)} files, effect: {options.mode}, speed: {options.factor:g}x ---"
        )

        self.worker = threading.Thread(target=self._run_jobs, args=(songs, options), daemon=True)
        self.worker.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set("Cancelling...")

    def _run_jobs(self, songs: list[Song], options: JobOptions) -> None:
        from .cli import output_name

        spec = options.build_spec()
        extension, fixed_bitrate = QUALITY_CHOICES[options.quality]
        done = failed = 0

        for index, song in enumerate(songs, start=1):
            if self.cancel_event.is_set():
                self.queue.put(("log", "Cancelled."))
                break

            target_dir = options.output_dir
            try:
                relative = song.path.parent.relative_to(self.songs_dir)
                target_dir = options.output_dir / relative
            except ValueError:
                pass

            name = output_name(song.path, options.mode, options.preset_name, extension)
            target = unique_path(target_dir / name)
            self.queue.put(("status", f"[{index}/{len(songs)}] {song.path.name}"))

            bitrate = fixed_bitrate
            if bitrate is None and target.suffix.lower() in LOSSY_EXTENSIONS:
                source_rate = (song.info.bit_rate if song.info else None) or 0
                bitrate = f"{max(source_rate, 320_000) // 1000}k"

            try:
                convert_file(song.path, target, spec, bitrate=bitrate)
                if options.copy_tags:
                    for warning in copy_metadata(song.path, target):
                        self.queue.put(("log", f"    warning: {warning}"))
                done += 1
                self.queue.put(("log", f"[OK] {song.path.name} -> {target.name}"))
            except FFmpegError as exc:
                failed += 1
                self.queue.put(("log", f"[ERROR] {song.path.name}: {exc}"))
            except Exception as exc:  # pragma: no cover - unexpected
                failed += 1
                self.queue.put(("log", f"[ERROR] {song.path.name}: {exc}"))
            finally:
                self.queue.put(("progress", index))

        self.queue.put(("finished", done, failed))

    # ------------------------------------------------------------------
    # queue pump
    # ------------------------------------------------------------------

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_queue(self) -> None:
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "log":
                    self._log(message[1])
                elif kind == "status":
                    self.status_var.set(message[1])
                elif kind == "progress":
                    self.progress.configure(value=message[1])
                elif kind == "info":
                    index, info = message[1], message[2]
                    if 0 <= index < len(self.songs):
                        self.songs[index].info = info
                        if self.tree.exists(str(index)):
                            values = list(self.tree.item(str(index), "values"))
                            values[0] = human_duration(info.duration)
                            self.tree.item(str(index), values=values)
                elif kind == "download_finished":
                    summary, error = message[1], message[2]
                    self.download_button.configure(state="normal")
                    self.download_cancel_button.configure(state="disabled")
                    if error:
                        self.status_var.set("Download failed.")
                        self._log(f"[ERROR] {error}")
                        messagebox.showerror("Download failed", error)
                    else:
                        self.url_var.set("")
                        self.refresh_songs()  # writes its own status text
                        self.status_var.set(summary)
                elif kind == "preview_finished":
                    self._preview_ready(message[1], message[2], message[3])
                elif kind == "finished":
                    done, failed = message[1], message[2]
                    self.status_var.set(
                        f"Finished: {done} succeeded, {failed} failed -> {self.output_dir}"
                    )
                    self.convert_button.configure(state="normal")
                    self.all_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
        except queue.Empty:
            pass
        self._poll_player()
        self._after_id = self.after(120, self._drain_queue)

    def stop(self) -> None:
        """Stop running work and cancel the pending timer."""
        self.cancel_event.set()
        self.download_cancel.set()
        self._preview_token += 1
        self.player.stop()
        self.close_preferences()
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:  # pragma: no cover - window already gone
                pass
            self._after_id = None

    def on_close(self) -> None:
        self.stop()
        preview.clear_previews()
        self.save_settings()
        self.master.destroy()


def launch(songs_dir: str | Path | None = None, output_dir: str | Path | None = None) -> int:
    """Open the interface. The return value is the process exit code."""
    window = tk.Tk()
    window.title("audiofx - slowed / sped up / reverb")
    # the full layout wants ~960 px; on a shorter screen give it what there is
    # rather than letting Windows push the bottom of the window off the desktop
    height = min(980, window.winfo_screenheight() - 110)
    width = min(1060, window.winfo_screenwidth() - 80)
    window.geometry(f"{width}x{height}+40+15")
    window.minsize(880, 620)

    try:
        ensure_tools()
    except FFmpegError as exc:
        window.withdraw()
        messagebox.showerror("ffmpeg not found", str(exc))
        window.destroy()
        return 1

    app = AudioFxApp(window, songs_dir, output_dir)
    app.songs_dir.mkdir(parents=True, exist_ok=True)
    app.output_dir.mkdir(parents=True, exist_ok=True)
    window.protocol("WM_DELETE_WINDOW", app.on_close)
    window.mainloop()
    return 0
