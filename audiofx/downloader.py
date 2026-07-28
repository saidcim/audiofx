"""Optional spotdl wrapper used by the download box in the GUI.

spotdl is not a dependency: when it is missing the feature is simply disabled
and the rest of the program keeps working. You are responsible for having the
rights to whatever you download with it.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .ffmpeg_runner import AUDIO_EXTENSIONS

# spotdl's filename template; the folder is pinned by passing a full path.
OUTPUT_TEMPLATE = "{artists} - {title}.{output-ext}"

# open.spotify.com/track/... , /intl-tr/album/... , spotify:track:... etc.
SPOTIFY_URL = re.compile(
    r"^(https?://)?(open|play)\.spotify\.com/(intl-[a-z]{2}/)?"
    r"(track|album|playlist|artist)/[A-Za-z0-9]+",
    re.IGNORECASE,
)
SPOTIFY_URI = re.compile(r"^spotify:(track|album|playlist|artist):[A-Za-z0-9]+$", re.IGNORECASE)

INSTALL_HINT = (
    "'spotdl' was not found. Install it with:\n"
    "  pip install spotdl\n"
    "If it is installed, add your Python Scripts folder to PATH."
)


class SpotdlError(RuntimeError):
    """spotdl ran but reported an error."""


class SpotdlNotFoundError(SpotdlError):
    """spotdl is neither on PATH nor importable as a module."""


def _python_executable() -> str:
    """When launched through pythonw, fall back to python for the pipes."""
    exe = Path(sys.executable)
    if exe.stem.lower() == "pythonw":
        candidate = exe.with_name(exe.name.replace("pythonw", "python"))
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def find_spotdl() -> list[str]:
    """Return the command that runs spotdl (PATH first, then `-m spotdl`)."""
    exe = shutil.which("spotdl")
    if exe:
        return [exe]
    if importlib.util.find_spec("spotdl") is not None:
        return [_python_executable(), "-m", "spotdl"]
    raise SpotdlNotFoundError(INSTALL_HINT)


def spotdl_available() -> bool:
    try:
        find_spotdl()
    except SpotdlNotFoundError:
        return False
    return True


def normalize_query(text: str) -> str:
    """Trim the link; a plain search string is passed through unchanged."""
    query = text.strip().strip('"').strip("'")
    if not query:
        raise ValueError("Empty link.")
    return query


def is_spotify_link(text: str) -> bool:
    query = text.strip()
    return bool(SPOTIFY_URL.match(query) or SPOTIFY_URI.match(query))


def build_command(
    query: str, dest_dir: Path, *, audio_format: str | None = None, bitrate: str | None = None
) -> list[str]:
    """Assemble the spotdl command line (kept separate so it can be tested)."""
    command = find_spotdl() + [
        "download",
        query,
        "--output",
        str(Path(dest_dir) / OUTPUT_TEMPLATE),
        "--overwrite",
        "skip",
        "--print-errors",
        "--simple-tui",
    ]
    if audio_format:
        command += ["--format", audio_format]
    if bitrate:
        command += ["--bitrate", bitrate]
    return command


def _audio_files(directory: Path) -> set[Path]:
    return {
        path
        for path in directory.glob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    }


def _popen(command: Sequence[str]) -> subprocess.Popen:
    kwargs: dict = {}
    if sys.platform == "win32":  # pragma: no cover - platform specific
        # keep a console window from popping up when launched via pythonw
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **kwargs,
    )


def _kill_on_cancel(process: subprocess.Popen, cancel_event: threading.Event) -> None:
    while process.poll() is None:
        if cancel_event.wait(0.4):
            try:
                process.terminate()
            except OSError:  # pragma: no cover - process already gone
                pass
            return


def download(
    query: str,
    dest_dir: Path,
    *,
    on_line: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    audio_format: str | None = None,
    bitrate: str | None = None,
) -> list[Path]:
    """Download `query` into `dest_dir`; return the files that appeared.

    `on_line` receives spotdl's output line by line. Set `cancel_event` to
    terminate the download early.
    """
    query = normalize_query(query)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    command = build_command(query, dest_dir, audio_format=audio_format, bitrate=bitrate)
    before = _audio_files(dest_dir)

    process = _popen(command)
    watcher: threading.Thread | None = None
    if cancel_event is not None:
        watcher = threading.Thread(
            target=_kill_on_cancel, args=(process, cancel_event), daemon=True
        )
        watcher.start()

    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip()
        if line and on_line is not None:
            on_line(line)
    code = process.wait()
    if watcher is not None:
        # the watcher exits on its own once the process is gone
        watcher.join(timeout=1)

    new_files = sorted(_audio_files(dest_dir) - before)
    cancelled = cancel_event is not None and cancel_event.is_set() and not new_files
    if code != 0 and not new_files and not cancelled:
        raise SpotdlError(f"spotdl exited with code {code}; see the log for details.")
    return new_files


def summarize(new_files: Iterable[Path]) -> str:
    files = list(new_files)
    if not files:
        return "No new files (they may already exist)."
    if len(files) == 1:
        return f"Downloaded: {files[0].name}"
    return f"Downloaded {len(files)} files."
