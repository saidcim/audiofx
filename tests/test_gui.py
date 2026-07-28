from __future__ import annotations

import queue
import shutil
import threading
from pathlib import Path

import pytest

from audiofx import gui, theme
from audiofx.ffmpeg_runner import REVERB_SIZES
from conftest import duration_of, ffmpeg_required


def make_options(tmp_path: Path, **overrides) -> gui.JobOptions:
    defaults = dict(
        mode="slowed_reverb",
        factor=0.85,
        preserve_pitch=False,
        pitch_shift=0.0,
        reverb_room="medium",
        reverb_mix=0.35,
        reverb_bass_cut=200.0,
        quality="Same as source (high bitrate)",
        preset_name=None,
        copy_tags=False,
        output_dir=tmp_path / "out",
    )
    defaults.update(overrides)
    return gui.JobOptions(**defaults)


# --------------------------------------------------------------------------
# pure logic
# --------------------------------------------------------------------------


def test_build_spec_slowed_reverb(tmp_path: Path):
    spec = make_options(tmp_path).build_spec()
    assert spec.tempo == pytest.approx(0.85)
    assert spec.reverb is not None
    assert tuple(spec.reverb.delays) == REVERB_SIZES["medium"][0]
    assert spec.reverb.highpass == pytest.approx(200)
    spec.validate()


def test_build_spec_room_size_changes_the_taps(tmp_path: Path):
    spec = make_options(tmp_path, reverb_room="small").build_spec()
    assert spec.reverb is not None
    assert tuple(spec.reverb.delays) == REVERB_SIZES["small"][0]


def test_build_spec_reverb_keeps_tempo(tmp_path: Path):
    spec = make_options(tmp_path, mode="reverb", factor=0.5).build_spec()
    assert spec.tempo == 1.0
    assert spec.reverb is not None


def test_build_spec_speed_has_no_reverb(tmp_path: Path):
    spec = make_options(tmp_path, mode="speed", factor=1.25).build_spec()
    assert spec.tempo == pytest.approx(1.25)
    assert spec.reverb is None


def test_build_spec_uses_ir_for_the_hall(tmp_path: Path):
    ir = tmp_path / "ir.wav"
    ir.write_bytes(b"RIFF")
    spec = make_options(tmp_path, mode="reverb", reverb_room="hall", ir_file=ir).build_spec()
    assert spec.reverb is not None and spec.reverb.ir_file == ir


def test_unique_path_avoids_overwrite(tmp_path: Path):
    target = tmp_path / "song.wav"
    assert gui.unique_path(target) == target
    target.write_bytes(b"x")
    assert gui.unique_path(target).name == "song_2.wav"
    (tmp_path / "song_2.wav").write_bytes(b"x")
    assert gui.unique_path(target).name == "song_3.wav"


def test_speed_ranges_cover_every_mode():
    assert set(gui.SPEED_RANGE) == {mode for mode, _ in gui.MODES}
    for _mode, (low, high) in gui.SPEED_RANGE.items():
        assert low <= high


def test_room_choices_map_to_known_sizes():
    for key in gui.ROOM_CHOICES.values():
        assert key == "hall" or key in REVERB_SIZES


def test_quality_choices_are_known_extensions():
    for extension, bitrate in gui.QUALITY_CHOICES.values():
        assert extension in (None, "flac", "wav", "mp3")
        assert bitrate is None or bitrate.endswith("k")


def test_human_helpers():
    assert gui.human_duration(None) == "-"
    assert gui.human_duration(125) == "2:05"
    assert gui.human_size(2048).startswith("2.0")


def test_preview_lengths_are_positive_or_whole_song():
    assert None in gui.PREVIEW_LENGTHS.values()
    for length in gui.PREVIEW_LENGTHS.values():
        assert length is None or length > 0


# --------------------------------------------------------------------------
# interface (only when a Tk window can be created)
# --------------------------------------------------------------------------


@pytest.fixture()
def app(tk_root, tmp_path: Path, sample_wav: Path):
    import tkinter as tk

    songs = tmp_path / "songs"
    out = tmp_path / "out"
    songs.mkdir()
    shutil.copy(sample_wav, songs / "song.wav")

    window = tk.Toplevel(tk_root)
    window.withdraw()
    # an isolated settings file keeps the tests away from ~/.audiofx-gui.json
    instance = gui.AudioFxApp(window, songs, out, settings_file=tmp_path / "settings.json")
    yield instance
    instance.stop()
    window.destroy()


def test_app_lists_songs(app):
    assert len(app.songs) == 1
    assert app.tree.get_children() == ("0",)


def test_mode_change_adjusts_speed_range(app):
    app.mode_var.set("speed")
    app._on_mode_change()
    assert float(app.speed_scale.cget("from")) == pytest.approx(1.01)
    assert app.factor_var.get() > 1.0

    app.mode_var.set("reverb")
    app._on_mode_change()
    assert app.factor_var.get() == 1.0
    assert str(app.speed_scale.cget("state")) == "disabled"


def test_selecting_preset_fills_controls(app):
    if "nightcore" not in app.presets:  # pragma: no cover
        pytest.skip("preset missing")
    app.preset_var.set("nightcore")
    app._apply_preset()
    assert app.mode_var.get() == "speed"
    assert app.factor_var.get() == pytest.approx(1.25)
    assert app.preset_var.get() == "nightcore"


def test_selecting_reverb_preset_fills_the_reverb_controls(app):
    if "dreamy" not in app.presets:  # pragma: no cover
        pytest.skip("preset missing")
    app.preset_var.set("dreamy")
    app._apply_preset()
    assert app.room_var.get() == "Large room"
    assert app.mix_var.get() == pytest.approx(0.5)
    assert app.bass_cut_var.get() == pytest.approx(240)


def test_hall_preset_selects_the_ir_room(app):
    if "reverb_hall" not in app.presets:  # pragma: no cover
        pytest.skip("preset missing")
    app.preset_var.set("reverb_hall")
    app._apply_preset()
    assert app.room_var.get() == "Concert hall (IR)"
    assert app._current_options().reverb_room == "hall"


def test_manual_change_switches_preset_to_custom(app):
    app.preset_var.set("default")
    app._apply_preset()
    app.factor_var.set(0.7)
    app._on_speed_change()
    assert app.preset_var.get() == gui.CUSTOM_PRESET


def test_download_needs_a_link(app, monkeypatch):
    seen = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *args: seen.append(args))
    app.url_var.set("   ")
    app.download_song()
    assert seen and app.download_worker is None


def test_download_adds_song_to_the_list(app, monkeypatch, sample_wav: Path):
    monkeypatch.setattr(gui.downloader, "find_spotdl", lambda: ["spotdl"])

    def fake_download(query, dest_dir, *, on_line=None, cancel_event=None, **kwargs):
        on_line(f"Downloaded {query}")
        target = Path(dest_dir) / "Artist - New.wav"
        shutil.copy(sample_wav, target)
        return [target]

    monkeypatch.setattr(gui.downloader, "download", fake_download)

    app.url_var.set("https://open.spotify.com/track/x")
    app.download_song()
    assert str(app.download_button.cget("state")) == "disabled"
    app.download_worker.join(timeout=10)
    app._drain_queue()

    assert [song.path.name for song in app.songs] == ["Artist - New.wav", "song.wav"]
    assert app.url_var.get() == ""
    assert str(app.download_button.cget("state")) == "normal"
    assert "Downloaded" in app.status_var.get()


def test_download_failure_is_reported(app, monkeypatch):
    monkeypatch.setattr(gui.downloader, "find_spotdl", lambda: ["spotdl"])
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args: errors.append(args))

    def boom(*args, **kwargs):
        raise gui.downloader.SpotdlError("spotdl exited with code 1")

    monkeypatch.setattr(gui.downloader, "download", boom)
    app.url_var.set("https://open.spotify.com/track/x")
    app.download_song()
    app.download_worker.join(timeout=10)
    app._drain_queue()

    assert errors and "spotdl" in errors[0][1]
    assert str(app.download_button.cget("state")) == "normal"


def test_conversion_waits_for_download(app, monkeypatch):
    monkeypatch.setattr(gui.downloader, "find_spotdl", lambda: ["spotdl"])
    release = threading.Event()

    def slow_download(*args, **kwargs):
        release.wait(5)
        return []

    monkeypatch.setattr(gui.downloader, "download", slow_download)
    infos = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *args: infos.append(args))

    app.url_var.set("https://open.spotify.com/track/x")
    app.download_song()
    try:
        app.select_all()
        app.convert_selected()
        assert infos and "Download" in infos[0][0]
        assert app.worker is None
    finally:
        release.set()
        app.download_worker.join(timeout=10)


# --------------------------------------------------------------------------
# theme + preferences
# --------------------------------------------------------------------------


def test_the_interface_starts_dark(app):
    assert app.theme_var.get() == theme.DEFAULT_THEME
    assert app.palette is theme.DARK
    assert app.log.cget("background") == theme.DARK.field


def test_preferences_window_offers_every_theme(app):
    app.open_preferences()
    try:
        assert app._prefs_window is not None
        assert list(app.theme_combo.cget("values")) == list(theme.THEME_CHOICES.values())
        assert app.theme_combo.get() == theme.THEME_CHOICES[theme.DEFAULT_THEME]
    finally:
        app.close_preferences()


def test_preferences_window_is_reused(app):
    app.open_preferences()
    first = app._prefs_window
    app.open_preferences()
    try:
        assert app._prefs_window is first
    finally:
        app.close_preferences()
    assert app._prefs_window is None


def test_picking_a_theme_repaints_immediately(app):
    app.open_preferences()
    try:
        app.theme_combo.set(theme.THEME_CHOICES["light"])
        app._on_theme_choice()
        assert app.theme_var.get() == "light"
        assert app.palette is theme.LIGHT
        assert app.log.cget("background") == theme.LIGHT.field
    finally:
        app.close_preferences()
        app.apply_theme("dark")


def test_switching_theme_repaints_the_log(app):
    app.apply_theme("light")
    assert app.palette is theme.LIGHT
    assert app.log.cget("background") == theme.LIGHT.field
    app.apply_theme("dark")
    assert app.log.cget("background") == theme.DARK.field


def test_unknown_theme_falls_back_instead_of_failing(app):
    app.apply_theme("neon")
    assert app.theme_var.get() == theme.DEFAULT_THEME


def test_theme_survives_a_restart(app, tmp_path: Path):
    import tkinter as tk

    app.apply_theme("light")
    app.save_settings()

    window = tk.Toplevel(app.master)
    window.withdraw()
    reopened = gui.AudioFxApp(
        window, app.songs_dir, app.output_dir, settings_file=app.settings_file
    )
    try:
        assert reopened.theme_var.get() == "light"
    finally:
        reopened.stop()
        window.destroy()
        app.apply_theme("dark")


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------


class FakePlayer:
    def __init__(self) -> None:
        self.played: list[Path] = []
        self.stops = 0
        self.playing = False

    def play(self, path) -> None:
        self.played.append(Path(path))
        self.playing = True

    def stop(self) -> None:
        self.stops += 1
        self.playing = False

    def is_playing(self) -> bool:
        return self.playing


def test_preview_needs_a_song(app, monkeypatch):
    infos = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *args: infos.append(args))
    app.songs = []
    app.tree.delete(*app.tree.get_children())
    app.preview_selected()
    assert infos and app.preview_worker is None


def test_preview_ready_plays_the_render(app, tmp_path: Path):
    app.player = FakePlayer()
    app._preview_name = "song.wav"
    rendered = gui.preview.preview_dir() / "preview_x.wav"
    rendered.write_bytes(b"x")

    app._preview_ready(app._preview_token, rendered, None)

    assert app.player.played == [rendered]
    assert "Playing preview" in app.status_var.get()
    assert str(app.preview_stop_button.cget("state")) == "normal"


def test_stopped_preview_is_not_played_when_it_arrives(app):
    app.player = FakePlayer()
    stale = app._preview_token
    app.stop_preview()  # the user pressed Stop while it was still rendering

    rendered = gui.preview.preview_dir() / "preview_x.wav"
    rendered.write_bytes(b"x")
    app._preview_ready(stale, rendered, None)

    assert app.player.played == []
    assert not rendered.exists(), "a discarded render should not be left behind"


def test_preview_failure_is_reported(app, monkeypatch):
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args: errors.append(args))
    app.player = FakePlayer()

    app._preview_ready(app._preview_token, None, "ffmpeg exited with code 1")

    assert errors and "ffmpeg" in errors[0][1]
    assert str(app.preview_button.cget("state")) == "normal"


def test_missing_ffplay_falls_back_to_the_default_player(app, monkeypatch, tmp_path: Path):
    opened = []
    monkeypatch.setattr(gui, "open_file", lambda path: opened.append(path))

    class NoPlayer(FakePlayer):
        def play(self, path):
            raise gui.preview.PlayerNotFoundError("ffplay was not found")

    app.player = NoPlayer()
    rendered = tmp_path / "preview.wav"
    rendered.write_bytes(b"x")
    app._preview_ready(app._preview_token, rendered, None)

    assert opened == [rendered]
    assert "default player" in app.status_var.get()


def test_starting_a_conversion_stops_the_preview(app, monkeypatch):
    app.player = FakePlayer()
    app.player.playing = True
    monkeypatch.setattr(gui.AudioFxApp, "_run_jobs", lambda *args: None)

    app.select_all()
    app.convert_selected()
    app.worker.join(timeout=5)

    assert app.player.stops >= 1


@ffmpeg_required
def test_run_preview_renders_an_excerpt(app, tmp_path: Path):
    options = make_options(tmp_path, mode="slow", factor=0.5)
    app._run_preview(app.songs[0], options, 0.0, 1.0, app._preview_token)

    # the song list probes in the background, so the queue holds other messages
    messages = [m for m in list(app.queue.queue) if m[0] == "preview_finished"]
    assert len(messages) == 1
    _kind, token, path, error = messages[0]
    assert error is None
    assert token == app._preview_token
    assert duration_of(path) == pytest.approx(1.0, rel=0.1)


@ffmpeg_required
def test_run_jobs_writes_output(app, tmp_path: Path):
    options = make_options(tmp_path, mode="slow", factor=0.5, output_dir=app.output_dir)
    app._run_jobs(list(app.songs), options)

    produced = app.output_dir / "song_slow.wav"
    assert produced.is_file()
    assert duration_of(produced) == pytest.approx(4.0, rel=0.05)

    messages = []
    while True:
        try:
            messages.append(app.queue.get_nowait())
        except queue.Empty:
            break
    assert ("finished", 1, 0) in messages


@ffmpeg_required
def test_run_jobs_reports_failure_without_stopping(app, tmp_path: Path):
    broken = app.songs_dir / "broken.mp3"
    broken.write_bytes(b"not audio at all")
    app.refresh_songs()

    options = make_options(tmp_path, mode="slow", factor=0.8, output_dir=app.output_dir)
    app._run_jobs(list(app.songs), options)

    finished = [m for m in list(app.queue.queue) if m[0] == "finished"]
    assert finished and finished[-1][1] == 1 and finished[-1][2] == 1
