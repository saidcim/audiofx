from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from audiofx import downloader


# --------------------------------------------------------------------------
# link recognition
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        "http://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX",
        "https://open.spotify.com/intl-tr/track/4cOdK2wGLETKBW3PvgPWqT",
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
    ],
)
def test_is_spotify_link_accepts_known_forms(text: str):
    assert downloader.is_spotify_link(text)


@pytest.mark.parametrize(
    "text",
    ["", "some song name", "https://example.com/track/1", "spotify:podcast:123"],
)
def test_is_spotify_link_rejects_other_text(text: str):
    assert not downloader.is_spotify_link(text)


def test_normalize_query_strips_quotes_and_space():
    assert downloader.normalize_query('  "https://open.spotify.com/track/x" ') == (
        "https://open.spotify.com/track/x"
    )


def test_normalize_query_rejects_empty():
    with pytest.raises(ValueError):
        downloader.normalize_query("   ")


# --------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------


def test_build_command_puts_files_in_dest_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(downloader, "find_spotdl", lambda: ["spotdl"])
    command = downloader.build_command("https://open.spotify.com/track/x", tmp_path)

    assert command[:3] == ["spotdl", "download", "https://open.spotify.com/track/x"]
    output = Path(command[command.index("--output") + 1])
    assert output.parent == tmp_path
    assert output.name == downloader.OUTPUT_TEMPLATE
    assert "--overwrite" in command and "skip" in command


def test_build_command_optional_format_and_bitrate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(downloader, "find_spotdl", lambda: ["spotdl"])
    command = downloader.build_command("x", tmp_path, audio_format="flac", bitrate="320k")
    assert command[command.index("--format") + 1] == "flac"
    assert command[command.index("--bitrate") + 1] == "320k"


def test_find_spotdl_falls_back_to_module(monkeypatch):
    monkeypatch.setattr(downloader.shutil, "which", lambda _name: None)
    monkeypatch.setattr(downloader.importlib.util, "find_spec", lambda _name: object())
    assert downloader.find_spotdl() == [downloader._python_executable(), "-m", "spotdl"]


def test_find_spotdl_raises_when_missing(monkeypatch):
    monkeypatch.setattr(downloader.shutil, "which", lambda _name: None)
    monkeypatch.setattr(downloader.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(downloader.SpotdlNotFoundError):
        downloader.find_spotdl()


# --------------------------------------------------------------------------
# download flow (a fake python script stands in for spotdl)
# --------------------------------------------------------------------------


def fake_spotdl(monkeypatch, tmp_path: Path, script: str) -> None:
    """Run the given python code instead of spotdl."""
    path = tmp_path / "fake_spotdl.py"
    path.write_text(script, encoding="utf-8")
    monkeypatch.setattr(downloader, "find_spotdl", lambda: [sys.executable, str(path)])
    monkeypatch.setattr(
        downloader,
        "build_command",
        lambda query, dest, **kw: [sys.executable, str(path), query, str(dest)],
    )


def test_download_reports_new_files_and_lines(tmp_path: Path, monkeypatch):
    dest = tmp_path / "songs"
    fake_spotdl(
        monkeypatch,
        tmp_path,
        "import sys, pathlib\n"
        "dest = pathlib.Path(sys.argv[2])\n"
        "dest.mkdir(parents=True, exist_ok=True)\n"
        "print('Processing query:', sys.argv[1])\n"
        "(dest / 'Artist - Song.mp3').write_bytes(b'x')\n"
        "print('Downloaded')\n",
    )

    lines: list[str] = []
    new_files = downloader.download("https://open.spotify.com/track/x", dest, on_line=lines.append)

    assert [path.name for path in new_files] == ["Artist - Song.mp3"]
    assert any("Processing query" in line for line in lines)
    assert downloader.summarize(new_files).startswith("Downloaded:")


def test_download_ignores_files_that_were_already_there(tmp_path: Path, monkeypatch):
    dest = tmp_path / "songs"
    dest.mkdir()
    (dest / "old.mp3").write_bytes(b"x")
    fake_spotdl(monkeypatch, tmp_path, "print('Skipping old.mp3')\n")

    assert downloader.download("x", dest) == []
    assert downloader.summarize([]).startswith("No new files")


def test_download_raises_on_failure(tmp_path: Path, monkeypatch):
    fake_spotdl(monkeypatch, tmp_path, "import sys\nprint('LookupError')\nsys.exit(1)\n")
    with pytest.raises(downloader.SpotdlError):
        downloader.download("x", tmp_path / "songs")


def test_download_cancel_stops_the_process(tmp_path: Path, monkeypatch):
    fake_spotdl(monkeypatch, tmp_path, "import time\nprint('start', flush=True)\ntime.sleep(30)\n")

    cancel = threading.Event()
    lines: list[str] = []

    def on_line(line: str) -> None:
        lines.append(line)
        cancel.set()

    assert downloader.download("x", tmp_path / "songs", on_line=on_line, cancel_event=cancel) == []
    assert lines == ["start"]
