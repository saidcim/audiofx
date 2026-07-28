from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from audiofx import cli
from audiofx.ffmpeg_runner import REVERB_SIZES
from audiofx.presets import PresetError, load_presets, parse_preset
from conftest import duration_of, ffmpeg_required


def run(*argv: str) -> int:
    return cli.main(list(argv))


def parse(*argv: str):
    return cli.build_parser().parse_args(list(argv))


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------


def test_shipped_presets_load():
    presets = load_presets()
    assert "default" in presets
    default = presets["default"]
    assert default.mode == "slowed_reverb"
    assert default.factor == pytest.approx(0.85)
    assert default.reverb is not None


def test_every_shipped_preset_builds_a_valid_spec():
    for preset in load_presets().values():
        preset.to_spec().validate()


def test_shipped_reverb_presets_cut_the_bass():
    """The bass cut is what keeps the reverb from sounding bass-boosted."""
    for preset in load_presets().values():
        if preset.reverb is not None:
            assert preset.reverb.highpass >= 150


def test_preset_size_shortcut_picks_the_tap_layout():
    preset = parse_preset("x", {"mode": "reverb", "reverb": {"size": "large"}})
    assert preset.reverb is not None
    assert tuple(preset.reverb.delays) == REVERB_SIZES["large"][0]


def test_preset_rejects_unknown_key():
    with pytest.raises(PresetError):
        parse_preset("bad", {"factor": 0.9, "nonsense": 1})


def test_preset_rejects_bad_mode():
    with pytest.raises(PresetError):
        parse_preset("bad", {"mode": "chopped"})


def test_preset_rejects_bad_reverb():
    with pytest.raises(PresetError):
        parse_preset("bad", {"mode": "reverb", "reverb": {"decays": 5, "delays": 50}})


def test_preset_rejects_unknown_room_size():
    with pytest.raises(PresetError):
        parse_preset("bad", {"mode": "reverb", "reverb": {"size": "cathedral"}})


def test_presets_list_command(capsys):
    assert run("presets", "list") == 0
    out = capsys.readouterr().out
    assert "default" in out and "nightcore" in out


def test_presets_show_command(capsys):
    assert run("presets", "show", "default") == 0
    out = capsys.readouterr().out
    assert "slowed_reverb" in out
    assert "reverb mix" in out


def test_presets_show_unknown_returns_usage_error(capsys):
    assert run("presets", "show", "no_such_preset") == 2
    assert "no preset named" in capsys.readouterr().err


# --------------------------------------------------------------------------
# spec resolution
# --------------------------------------------------------------------------


def test_resolve_spec_from_preset():
    spec, mode, name = cli.resolve_spec(parse("convert", "x.mp3", "--preset", "default"))
    assert mode == "slowed_reverb"
    assert name == "default"
    assert spec.tempo == pytest.approx(0.85)
    assert spec.reverb is not None


def test_cli_flags_override_preset():
    spec, _, _ = cli.resolve_spec(
        parse("convert", "x.mp3", "--preset", "default", "--factor", "0.7", "--preserve-pitch")
    )
    assert spec.tempo == pytest.approx(0.7)
    assert spec.preserve_pitch is True


def test_mode_defaults_apply_without_preset():
    spec, mode, name = cli.resolve_spec(parse("convert", "x.mp3", "--mode", "speed"))
    assert mode == "speed" and name is None
    assert spec.tempo == pytest.approx(1.25)
    assert spec.reverb is None


def test_reverb_mode_keeps_tempo():
    spec, _, _ = cli.resolve_spec(parse("convert", "x.mp3", "--mode", "reverb"))
    assert spec.tempo == 1.0
    assert spec.reverb is not None


def test_mode_or_preset_is_required():
    with pytest.raises(cli.UsageError):
        cli.resolve_spec(parse("convert", "x.mp3"))


@pytest.mark.parametrize(
    "argv",
    [
        ("convert", "x.mp3", "--mode", "slow", "--factor", "1.2"),
        ("convert", "x.mp3", "--mode", "speed", "--factor", "0.8"),
        ("convert", "x.mp3", "--mode", "reverb", "--factor", "0.8"),
        ("convert", "x.mp3", "--mode", "slowed_reverb", "--factor", "1.0"),
    ],
)
def test_factor_direction_is_validated(argv):
    with pytest.raises(cli.UsageError):
        cli.resolve_spec(parse(*argv))


def test_reverb_flags_override_preset_values():
    spec, _, _ = cli.resolve_spec(
        parse(
            "convert",
            "x.mp3",
            "--mode",
            "reverb",
            "--reverb-size",
            "large",
            "--reverb-mix",
            "0.6",
            "--reverb-bass-cut",
            "300",
            "--reverb-damping",
            "5000",
        )
    )
    assert spec.reverb is not None
    assert tuple(spec.reverb.delays) == REVERB_SIZES["large"][0]
    assert spec.reverb.mix == pytest.approx(0.6)
    assert spec.reverb.highpass == pytest.approx(300)
    assert spec.reverb.lowpass == pytest.approx(5000)


def test_reverb_size_flag_clears_a_preset_ir_file():
    spec, _, _ = cli.resolve_spec(
        parse("convert", "x.mp3", "--preset", "reverb_hall", "--reverb-size", "small")
    )
    assert spec.reverb is not None and spec.reverb.ir_file is None


def test_invalid_reverb_mix_is_rejected():
    with pytest.raises(cli.UsageError):
        cli.resolve_spec(parse("convert", "x.mp3", "--mode", "reverb", "--reverb-mix", "0"))


def test_output_name_pattern():
    source = Path("/music/My Song.mp3")
    assert cli.output_name(source, "slowed_reverb", "default") == (
        "My Song_slowed_reverb_default.mp3"
    )
    assert cli.output_name(source, "slow", None) == "My Song_slow.mp3"
    assert cli.output_name(source, "slow", None, "wav") == "My Song_slow.wav"


def test_collect_inputs_filters_and_recurses(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    for name in ("a.mp3", "b.wav", "c.txt", "sub/d.flac"):
        (tmp_path / name).write_bytes(b"x")

    flat = cli.collect_inputs(tmp_path, (".mp3", ".wav", ".flac"), recursive=False)
    assert [p.name for p in flat] == ["a.mp3", "b.wav"]

    deep = cli.collect_inputs(tmp_path, (".mp3", ".wav", ".flac"), recursive=True)
    assert [p.name for p in deep] == ["a.mp3", "b.wav", "d.flac"]


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------


@ffmpeg_required
def test_convert_command_creates_output(workdir: Path, tmp_path: Path, capsys):
    out_dir = tmp_path / "out"
    code = run(
        "convert",
        str(workdir / "song.wav"),
        "-o",
        str(out_dir),
        "--mode",
        "slowed_reverb",
        "--preset",
        "default",
    )
    assert code == 0
    produced = out_dir / "song_slowed_reverb_default.wav"
    assert produced.is_file()
    # 154 ms of reverb tail on top of the slowed length
    assert duration_of(produced) == pytest.approx(2 / 0.85 + 0.154, rel=0.03)
    assert "Done." in capsys.readouterr().out


@ffmpeg_required
def test_convert_dry_run_does_not_write(workdir: Path, tmp_path: Path, capsys):
    out_dir = tmp_path / "out"
    assert (
        run("convert", str(workdir / "song.wav"), "-o", str(out_dir), "--mode", "slow", "--dry-run")
        == 0
    )
    assert not (out_dir / "song_slow.wav").exists()
    assert "command" in capsys.readouterr().out


@ffmpeg_required
def test_convert_missing_input_returns_error(tmp_path: Path, capsys):
    assert run("convert", str(tmp_path / "nope.mp3"), "--mode", "slow") == 2
    assert "not found" in capsys.readouterr().err


@ffmpeg_required
def test_convert_format_switch(workdir: Path, tmp_path: Path):
    out_dir = tmp_path / "out"
    assert (
        run(
            "convert",
            str(workdir / "song.wav"),
            "-o",
            str(out_dir),
            "--mode",
            "speed",
            "--factor",
            "1.5",
            "--format",
            "mp3",
            "-q",
        )
        == 0
    )
    produced = out_dir / "song_speed.mp3"
    assert produced.is_file()
    assert duration_of(produced) == pytest.approx(2 / 1.5, rel=0.05)


@ffmpeg_required
def test_batch_mirrors_directory_structure(workdir: Path, tmp_path: Path):
    (workdir / "album").mkdir()
    shutil.copy(workdir / "song.wav", workdir / "album" / "track2.wav")
    out_dir = tmp_path / "batch_out"

    assert (
        run("batch", str(workdir), "-o", str(out_dir), "--mode", "slow", "--recursive", "-q") == 0
    )

    assert (out_dir / "song_slow.wav").is_file()
    assert (out_dir / "album" / "track2_slow.wav").is_file()


@ffmpeg_required
def test_batch_without_matching_files(tmp_path: Path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run("batch", str(empty), "--mode", "slow") == 2
    assert "No audio files to process" in capsys.readouterr().err


@ffmpeg_required
def test_check_command(capsys):
    assert run("check") == 0
    out = capsys.readouterr().out
    assert "ffmpeg" in out and "atempo" in out
