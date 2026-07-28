from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

ffmpeg_required = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe is not on PATH",
)


@pytest.fixture(scope="session")
def sample_wav() -> Path:
    path = FIXTURES / "sample.wav"
    if not path.is_file():  # pragma: no cover - the fixture ships with the repo
        pytest.skip("tests/fixtures/sample.wav is missing")
    return path


@pytest.fixture()
def workdir(tmp_path: Path, sample_wav: Path) -> Path:
    """A temp folder holding one copy of the sample file."""
    target = tmp_path / "input"
    target.mkdir()
    shutil.copy(sample_wav, target / "song.wav")
    return target


def duration_of(path: Path) -> float:
    from audiofx.ffmpeg_runner import probe

    info = probe(path)
    assert info.duration is not None
    return info.duration


def peak_dbfs(path: Path) -> float:
    """Peak level of a file in dBFS, via ffmpeg's volumedetect."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"max_volume: (-?[\d.]+) dB", proc.stderr)
    assert match, f"volumedetect produced no peak for {path}"
    return float(match.group(1))
