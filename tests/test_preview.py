from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from audiofx import preview
from audiofx.ffmpeg_runner import FxSpec
from conftest import duration_of, ffmpeg_required


# --------------------------------------------------------------------------
# excerpt maths
# --------------------------------------------------------------------------


def test_clamp_start_leaves_a_fitting_excerpt_alone():
    assert preview.clamp_start(30, 120, 20) == 30


def test_clamp_start_slides_back_when_the_track_is_short():
    # a 20 s excerpt of a 25 s track can start no later than 5 s in
    assert preview.clamp_start(30, 25, 20) == pytest.approx(5)


def test_clamp_start_keeps_a_second_of_the_whole_song():
    assert preview.clamp_start(200, 10, None) == pytest.approx(9)


def test_clamp_start_trusts_an_unknown_duration():
    assert preview.clamp_start(30, None, 20) == 30


def test_clamp_start_refuses_negative_offsets():
    assert preview.clamp_start(-5, 120, 20) == 0


# --------------------------------------------------------------------------
# temp folder housekeeping
# --------------------------------------------------------------------------


def test_preview_dir_is_created(preview_tmpdir: Path):
    assert preview.preview_dir() == preview_tmpdir
    assert preview_tmpdir.is_dir()


def test_clear_previews_removes_only_previews(preview_tmpdir: Path):
    old = preview_tmpdir / f"{preview.PREVIEW_PREFIX}old.wav"
    other = preview_tmpdir / "something-else.wav"
    for path in (old, other):
        path.write_bytes(b"x")

    assert preview.clear_previews() == 1
    assert not old.exists()
    assert other.exists()


def test_clear_previews_can_keep_the_current_file(preview_tmpdir: Path):
    keep = preview_tmpdir / f"{preview.PREVIEW_PREFIX}keep.wav"
    drop = preview_tmpdir / f"{preview.PREVIEW_PREFIX}drop.wav"
    for path in (keep, drop):
        path.write_bytes(b"x")

    preview.clear_previews(keep=keep)
    assert keep.exists()
    assert not drop.exists()


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


@ffmpeg_required
def test_render_writes_an_excerpt_into_the_temp_folder(workdir: Path, preview_tmpdir: Path):
    path = preview.render(workdir / "song.wav", FxSpec(tempo=0.5), start=0.5, length=1.0)

    assert path.parent == preview_tmpdir
    assert path.suffix == ".wav"
    assert duration_of(path) == pytest.approx(1.0, rel=0.1)


@ffmpeg_required
def test_render_without_a_length_takes_the_whole_track(workdir: Path):
    path = preview.render(workdir / "song.wav", FxSpec(tempo=0.5), length=None)
    # the 2 s fixture at half speed
    assert duration_of(path) == pytest.approx(4.0, rel=0.05)


@ffmpeg_required
def test_two_renders_do_not_share_a_file(workdir: Path):
    first = preview.render(workdir / "song.wav", FxSpec(tempo=0.9), length=1.0)
    time.sleep(0.01)
    second = preview.render(workdir / "song.wav", FxSpec(tempo=0.8), length=1.0)
    assert first != second


# --------------------------------------------------------------------------
# playback
# --------------------------------------------------------------------------


def test_find_player_builds_a_silent_ffplay_command(monkeypatch):
    monkeypatch.setattr(preview.shutil, "which", lambda name: f"/usr/bin/{name}")
    command = preview.find_player()
    assert command is not None
    assert command[0].endswith("ffplay")
    assert "-nodisp" in command and "-autoexit" in command


def test_find_player_returns_none_without_ffplay(monkeypatch):
    monkeypatch.setattr(preview.shutil, "which", lambda name: None)
    assert preview.find_player() is None


def test_play_without_ffplay_says_so(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(preview, "find_player", lambda: None)
    with pytest.raises(preview.PlayerNotFoundError) as excinfo:
        preview.Player().play(tmp_path / "nothing.wav")
    assert "ffplay" in str(excinfo.value)


def test_player_starts_and_stops(monkeypatch, tmp_path: Path):
    # stand in for ffplay: a process that would outlive the test if not stopped
    monkeypatch.setattr(
        preview, "find_player", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    player = preview.Player()
    player.play(tmp_path / "ignored.wav")
    assert player.is_playing()

    player.stop()
    assert not player.is_playing()


def test_stopping_an_idle_player_is_harmless():
    preview.Player().stop()


def test_a_second_play_replaces_the_first(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        preview, "find_player", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    player = preview.Player()
    player.play(tmp_path / "one.wav")
    first = player._process
    player.play(tmp_path / "two.wav")
    try:
        assert first is not player._process
        assert first.poll() is not None, "the first player was left running"
    finally:
        player.stop()
