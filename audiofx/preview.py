"""Listen to the effect before converting anything.

A preview is an ordinary conversion with two differences: it renders only an
excerpt (`start` + `length`), and it writes an uncompressed wav into the
system temp folder instead of the output folder. Playback goes through
`ffplay`, which ships with ffmpeg - so if audiofx runs at all, the player is
already there, and unlike handing the file to the desktop's default player we
can actually stop it again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from tempfile import gettempdir

from .ffmpeg_runner import NO_WINDOW, FxSpec, convert_file

PREVIEW_DIR_NAME = "audiofx-preview"
PREVIEW_PREFIX = "preview_"

# Long enough to hear the tail of the reverb, short enough to render instantly.
DEFAULT_LENGTH = 20.0
# Songs rarely show their character in the first seconds; start after the intro.
DEFAULT_START = 30.0


class PlayerNotFoundError(RuntimeError):
    """ffplay is not on PATH, so playback cannot be controlled from here."""


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def preview_dir() -> Path:
    path = Path(gettempdir()) / PREVIEW_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_previews(keep: Path | None = None) -> int:
    """Delete rendered previews; returns how many went away.

    A file that a player still holds open cannot be unlinked on Windows. That
    is not an error worth reporting - the next call picks it up.
    """
    removed = 0
    for path in preview_dir().glob(f"{PREVIEW_PREFIX}*.wav"):
        if keep is not None and path == keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def clamp_start(start: float, total: float | None, length: float | None) -> float:
    """Keep the excerpt inside the track.

    Asking for a preview 30 seconds into a 20 second file would render silence,
    so the start slides back far enough for the excerpt to fit.
    """
    start = max(0.0, float(start))
    if not total or total <= 0:
        return start
    window = length if length else 0.0
    latest = max(0.0, total - max(window, 1.0))
    return min(start, latest)


def render(
    source: Path | str,
    spec: FxSpec,
    *,
    start: float = 0.0,
    length: float | None = DEFAULT_LENGTH,
    resampler: str = "auto",
) -> Path:
    """Render an excerpt with `spec` applied and return the temp wav path."""
    source = Path(source)
    stamp = f"{os.getpid()}_{int(time.time() * 1000)}"
    target = preview_dir() / f"{PREVIEW_PREFIX}{stamp}.wav"
    convert_file(
        source,
        target,
        spec,
        resampler=resampler,
        start=start or None,
        duration=length,
    )
    return target


# --------------------------------------------------------------------------
# playback
# --------------------------------------------------------------------------


def find_player() -> list[str] | None:
    """The ffplay command line, or None when ffplay is missing."""
    exe = shutil.which("ffplay")
    if exe is None:
        return None
    # -nodisp: no video window; -autoexit: quit at the end of the file
    return [exe, "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "error"]


class Player:
    """One preview at a time: starting a new one stops the previous."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def play(self, path: Path | str) -> None:
        self.stop()
        command = find_player()
        if command is None:
            raise PlayerNotFoundError(
                "'ffplay' was not found on PATH. It normally comes with ffmpeg; "
                "install a full build to preview inside audiofx."
            )
        process = subprocess.Popen(
            command + [str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **NO_WINDOW,
        )
        with self._lock:
            self._process = process

    def is_playing(self) -> bool:
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:  # pragma: no cover - already gone
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            process.kill()
