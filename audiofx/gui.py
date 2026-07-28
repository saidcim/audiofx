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
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import downloader
from .ffmpeg_runner import (
    AUDIO_EXTENSIONS,
    LOSSY_EXTENSIONS,
    REVERB_SIZES,
    AudioInfo,
    FFmpegError,
    FxSpec,
    ReverbSettings,
    convert_file,
    ensure_tools,
    probe,
)
from .metadata import copy_metadata
from .presets import load_presets

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

# label -> reverb size key; "hall" switches to IR convolution
ROOM_CHOICES: dict[str, str] = {
    "Small room": "small",
    "Medium room": "medium",
    "Large room": "large",
    "Concert hall (IR)": "hall",
}

CUSTOM_PRESET = "Custom"

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
    reverb_room: str
    reverb_mix: float
    reverb_bass_cut: float
    quality: str
    preset_name: str | None
    copy_tags: bool
    output_dir: Path
    reverb_damping: float = 7000.0
    ir_file: Path | None = None

    def build_spec(self) -> FxSpec:
        reverb = None
        if self.mode in ("reverb", "slowed_reverb"):
            common = dict(
                mix=self.reverb_mix,
                highpass=self.reverb_bass_cut,
                lowpass=self.reverb_damping,
            )
            if self.reverb_room == "hall" and self.ir_file is not None:
                reverb = ReverbSettings(ir_file=self.ir_file, **common)
            else:
                delays, decays = REVERB_SIZES.get(self.reverb_room, REVERB_SIZES["medium"])
                reverb = ReverbSettings(delays=delays, decays=decays, **common)
        return FxSpec(
            tempo=1.0 if self.mode == "reverb" else self.factor,
            pitch_semitones=self.pitch_shift or None,
            preserve_pitch=self.preserve_pitch,
            reverb=reverb,
            engine="auto",
        )


class AudioFxApp(ttk.Frame):
    def __init__(
        self,
        master: tk.Tk,
        songs_dir: Path | str | None = None,
        output_dir: Path | str | None = None,
        settings_file: Path | str | None = None,
    ) -> None:
        super().__init__(master, padding=10)
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

        try:
            self.presets = load_presets()
        except Exception as exc:  # pragma: no cover - broken yaml
            messagebox.showwarning("Presets", f"Could not read the preset file:\n{exc}")

        ir_dir = Path(__file__).parent / "assets" / "ir"
        self.ir_file = ir_dir / "hall_large.wav"

        self._build_ui()
        self._load_settings()
        self.refresh_songs()
        self._after_id: str | None = self.after(120, self._drain_queue)

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
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Songs folder:").grid(row=0, column=0, padx=(0, 6))
        self.songs_var = tk.StringVar(value=str(self.songs_dir))
        ttk.Entry(top, textvariable=self.songs_var, state="readonly").grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(top, text="Change", command=self.choose_songs_dir).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="Open folder", command=lambda: open_folder(self.songs_dir)).grid(
            row=0, column=3
        )
        ttk.Button(top, text="Refresh", command=self.refresh_songs).grid(row=0, column=4, padx=4)

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
            foreground="#666",
            justify="left",
        )
        self.download_hint.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        if not downloader.spotdl_available():
            self.download_button.configure(state="disabled")
            self.url_entry.configure(state="disabled")
            self.download_hint.configure(
                text="spotdl is not installed. Install it with: pip install spotdl",
                foreground="#a33",
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

        # --- right: settings ---
        right = ttk.Frame(self)
        right.grid(row=2, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(99, weight=1)

        preset_box = ttk.LabelFrame(right, text="Preset", padding=6)
        preset_box.grid(row=0, column=0, sticky="ew")
        preset_box.columnconfigure(0, weight=1)
        self.preset_var = tk.StringVar(value=CUSTOM_PRESET)
        self.preset_combo = ttk.Combobox(
            preset_box,
            textvariable=self.preset_var,
            state="readonly",
            values=[CUSTOM_PRESET] + sorted(self.presets),
        )
        self.preset_combo.grid(row=0, column=0, sticky="ew")
        self.preset_combo.bind("<<ComboboxSelected>>", self._apply_preset)

        effect_box = ttk.LabelFrame(right, text="Effect", padding=6)
        effect_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        effect_box.columnconfigure(0, weight=1)
        self.mode_var = tk.StringVar(value="slowed_reverb")
        for index, (value, label) in enumerate(MODES):
            ttk.Radiobutton(
                effect_box,
                text=label,
                value=value,
                variable=self.mode_var,
                command=self._on_mode_change,
            ).grid(row=index, column=0, sticky="w")

        speed_box = ttk.LabelFrame(right, text="Speed", padding=6)
        speed_box.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        speed_box.columnconfigure(0, weight=1)
        self.factor_var = tk.DoubleVar(value=0.85)
        self.speed_scale = ttk.Scale(
            speed_box,
            from_=0.40,
            to=0.99,
            orient="horizontal",
            variable=self.factor_var,
            command=lambda _=None: self._on_speed_change(),
        )
        self.speed_scale.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.speed_label = ttk.Label(speed_box, text="")
        self.speed_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(speed_box, text="-", width=3, command=lambda: self._nudge(-0.01)).grid(
            row=1, column=1
        )
        ttk.Button(speed_box, text="+", width=3, command=lambda: self._nudge(0.01)).grid(
            row=1, column=2
        )
        self.preserve_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            speed_box,
            text="Keep the original pitch",
            variable=self.preserve_var,
            command=self._mark_custom,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        pitch_row = ttk.Frame(speed_box)
        pitch_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(pitch_row, text="Pitch shift (semitones):").pack(side="left")
        self.pitch_var = tk.DoubleVar(value=0.0)
        ttk.Spinbox(
            pitch_row,
            from_=-12,
            to=12,
            increment=1,
            width=5,
            textvariable=self.pitch_var,
            command=self._mark_custom,
        ).pack(side="left", padx=6)

        self.reverb_box = ttk.LabelFrame(right, text="Reverb", padding=6)
        self.reverb_box.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.reverb_box.columnconfigure(1, weight=1)

        ttk.Label(self.reverb_box, text="Room:").grid(row=0, column=0, sticky="w")
        self.room_var = tk.StringVar(value="Medium room")
        self.room_combo = ttk.Combobox(
            self.reverb_box,
            textvariable=self.room_var,
            state="readonly",
            values=list(ROOM_CHOICES),
            width=18,
        )
        self.room_combo.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.room_combo.bind("<<ComboboxSelected>>", lambda _event: self._mark_custom())

        ttk.Label(self.reverb_box, text="Amount:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.mix_var = tk.DoubleVar(value=0.35)
        self.mix_scale = ttk.Scale(
            self.reverb_box,
            from_=0.05,
            to=0.80,
            orient="horizontal",
            variable=self.mix_var,
            command=lambda _=None: self._on_mix_change(),
        )
        self.mix_scale.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
        self.mix_label = ttk.Label(self.reverb_box, text="")
        self.mix_label.grid(row=2, column=1, sticky="w", padx=(6, 0))

        ttk.Label(self.reverb_box, text="Bass cut (Hz):").grid(row=3, column=0, sticky="w")
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
        self.bass_cut_spin.grid(row=3, column=1, sticky="w", padx=(6, 0))
        ttk.Label(
            self.reverb_box,
            text="Keeps the low end out of the reverb tail. Raise it if the\n"
            "result sounds boomy or bass-boosted; 0 turns it off.",
            foreground="#666",
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        out_box = ttk.LabelFrame(right, text="Output", padding=6)
        out_box.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        out_box.columnconfigure(0, weight=1)
        self.quality_var = tk.StringVar(value=next(iter(QUALITY_CHOICES)))
        ttk.Combobox(
            out_box,
            textvariable=self.quality_var,
            state="readonly",
            values=list(QUALITY_CHOICES),
        ).grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(
            out_box,
            text="MP3 -> MP3 always loses a little quality;\npick FLAC if you want none of that.",
            foreground="#666",
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 4))
        self.tags_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            out_box, text="Copy tags and cover art", variable=self.tags_var
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        self.output_var = tk.StringVar(value=str(self.output_dir))
        ttk.Entry(out_box, textvariable=self.output_var, state="readonly").grid(
            row=3, column=0, sticky="ew", pady=(6, 0)
        )
        out_buttons = ttk.Frame(out_box)
        out_buttons.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(out_buttons, text="Change", command=self.choose_output_dir).pack(side="left")
        ttk.Button(
            out_buttons, text="Open folder", command=lambda: open_folder(self.output_dir)
        ).pack(side="left", padx=6)

        action = ttk.Frame(right)
        action.grid(row=5, column=0, sticky="ew", pady=(10, 0))
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
        self.log = tk.Text(log_frame, height=6, state="disabled", wrap="none")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

        self._on_mode_change()
        self._update_mix_label()

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
        if data.get("room") in ROOM_CHOICES:
            self.room_var.set(data["room"])
        if isinstance(data.get("reverb_mix"), (int, float)):
            self.mix_var.set(float(data["reverb_mix"]))
        if isinstance(data.get("reverb_bass_cut"), (int, float)):
            self.bass_cut_var.set(float(data["reverb_bass_cut"]))
        self.preserve_var.set(bool(data.get("preserve_pitch", False)))
        self.recursive_var.set(bool(data.get("recursive", False)))
        self._on_mode_change()
        self._update_mix_label()

    def save_settings(self) -> None:
        payload = {
            "songs_dir": str(self.songs_dir),
            "output_dir": str(self.output_dir),
            "quality": self.quality_var.get(),
            "mode": self.mode_var.get(),
            "factor": round(float(self.factor_var.get()), 3),
            "room": self.room_var.get(),
            "reverb_mix": round(float(self.mix_var.get()), 3),
            "reverb_bass_cut": round(float(self.bass_cut_var.get()), 1),
            "preserve_pitch": bool(self.preserve_var.get()),
            "recursive": bool(self.recursive_var.get()),
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

    def _mark_custom(self) -> None:
        if not self._loading_preset:
            self.preset_var.set(CUSTOM_PRESET)

    def _on_speed_change(self) -> None:
        self._update_speed_label()
        self._mark_custom()

    def _on_mix_change(self) -> None:
        self._update_mix_label()
        self._mark_custom()

    def _nudge(self, delta: float) -> None:
        low, high = SPEED_RANGE[self.mode_var.get()]
        value = min(high, max(low, round(self.factor_var.get() + delta, 3)))
        self.factor_var.set(value)
        self._on_speed_change()

    def _update_speed_label(self) -> None:
        factor = float(self.factor_var.get())
        if self.mode_var.get() == "reverb":
            self.speed_label.configure(text="Speed unchanged (1.00x)")
            return
        percent = abs(1 - factor) * 100
        direction = "slower" if factor < 1 else "faster"
        self.speed_label.configure(text=f"{factor:.2f}x  ({percent:.0f}% {direction})")

    def _update_mix_label(self) -> None:
        self.mix_label.configure(text=f"{float(self.mix_var.get()) * 100:.0f}% wet")

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

        for child in self.reverb_box.winfo_children():
            try:
                child.configure(state="normal" if reverb_on else "disabled")
            except tk.TclError:  # pragma: no cover - widget without a state
                pass
        if reverb_on:
            # comboboxes must stay read-only, never fully editable
            self.room_combo.configure(state="readonly")

        self._update_speed_label()
        self._mark_custom()

    def _apply_preset(self, _event=None) -> None:
        name = self.preset_var.get()
        preset = self.presets.get(name)
        if preset is None:
            return
        self._loading_preset = True
        try:
            self.mode_var.set(preset.mode)
            self._on_mode_change()
            self.factor_var.set(preset.factor if preset.mode != "reverb" else 1.0)
            self.preserve_var.set(preset.preserve_pitch)
            self.pitch_var.set(preset.pitch_shift or 0.0)
            if preset.reverb is not None:
                self.mix_var.set(preset.reverb.mix)
                self.bass_cut_var.set(preset.reverb.highpass)
                self.room_var.set(self._room_label_for(preset.reverb))
                if preset.reverb.ir_file is not None:
                    self.ir_file = Path(preset.reverb.ir_file)
            self._update_speed_label()
            self._update_mix_label()
        finally:
            self._loading_preset = False
        self.preset_var.set(name)

    @staticmethod
    def _room_label_for(reverb: ReverbSettings) -> str:
        """Map a preset's reverb back onto one of the room choices."""
        if reverb.ir_file is not None:
            return "Concert hall (IR)"
        taps = tuple(float(d) for d in reverb.delays)
        for label, key in ROOM_CHOICES.items():
            known = REVERB_SIZES.get(key)
            if known and tuple(known[0]) == taps:
                return label
        return "Medium room"

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
    # conversion
    # ------------------------------------------------------------------

    def _current_options(self) -> JobOptions:
        preset = self.preset_var.get()
        return JobOptions(
            mode=self.mode_var.get(),
            factor=round(float(self.factor_var.get()), 3),
            preserve_pitch=bool(self.preserve_var.get()),
            pitch_shift=float(self.pitch_var.get() or 0),
            reverb_room=ROOM_CHOICES.get(self.room_var.get(), "medium"),
            reverb_mix=round(float(self.mix_var.get()), 3),
            reverb_bass_cut=float(self.bass_cut_var.get()),
            quality=self.quality_var.get(),
            preset_name=None if preset == CUSTOM_PRESET else preset,
            copy_tags=bool(self.tags_var.get()),
            output_dir=self.output_dir,
            ir_file=self.ir_file if self.ir_file.is_file() else None,
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
        options = self._current_options()
        if options.reverb_room == "hall" and options.ir_file is None:
            messagebox.showwarning(
                "IR missing", "The impulse response file is missing; using the tap reverb instead."
            )
            options.reverb_room = "large"

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
        self._after_id = self.after(120, self._drain_queue)

    def stop(self) -> None:
        """Stop running work and cancel the pending timer."""
        self.cancel_event.set()
        self.download_cancel.set()
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:  # pragma: no cover - window already gone
                pass
            self._after_id = None

    def on_close(self) -> None:
        self.stop()
        self.save_settings()
        self.master.destroy()


def launch(songs_dir: str | Path | None = None, output_dir: str | Path | None = None) -> int:
    """Open the interface. The return value is the process exit code."""
    window = tk.Tk()
    window.title("audiofx - slowed / sped up / reverb")
    window.geometry("1060x980")
    window.minsize(900, 780)

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
