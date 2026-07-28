from __future__ import annotations

import math
from pathlib import Path

import pytest

from audiofx import ffmpeg_runner as fr
from conftest import duration_of, ffmpeg_required, peak_dbfs

SR = 44100


# --------------------------------------------------------------------------
# atempo chaining
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ratio", [0.4, 0.25, 0.1, 0.5, 0.85, 1.25, 2.0, 3.0, 8.0])
def test_atempo_chain_multiplies_back_to_ratio(ratio):
    factors = fr.atempo_chain(ratio)
    assert factors, "every ratio other than 1.0 needs at least one atempo"
    assert math.prod(factors) == pytest.approx(ratio, rel=1e-9)
    for factor in factors:
        assert fr.ATEMPO_MIN <= factor <= fr.ATEMPO_MAX


def test_atempo_chain_identity_is_empty():
    assert fr.atempo_chain(1.0) == []


def test_atempo_chain_rejects_non_positive():
    with pytest.raises(ValueError):
        fr.atempo_chain(0)


# --------------------------------------------------------------------------
# time / pitch filters
# --------------------------------------------------------------------------


def test_classic_slowed_uses_asetrate_without_tempo_correction():
    spec = fr.FxSpec(tempo=0.85)
    filters = fr.build_time_pitch_filters(spec, SR, "classic")
    assert filters == [f"asetrate={int(SR * 0.85)}", f"aresample={SR}"]


def test_preserve_pitch_uses_atempo_only():
    spec = fr.FxSpec(tempo=0.85, preserve_pitch=True)
    assert fr.build_time_pitch_filters(spec, SR, "classic") == ["atempo=0.85"]


def test_preserve_pitch_below_atempo_limit_is_chained():
    spec = fr.FxSpec(tempo=0.4, preserve_pitch=True)
    filters = fr.build_time_pitch_filters(spec, SR, "classic")
    assert len(filters) == 2
    values = [float(f.split("=")[1]) for f in filters]
    assert math.prod(values) == pytest.approx(0.4)


def test_explicit_pitch_shift_compensates_tempo():
    # -3 semitones at unchanged tempo: asetrate drops the pitch, atempo undoes
    # the speed change that came with it
    spec = fr.FxSpec(tempo=1.0, pitch_semitones=-3)
    filters = fr.build_time_pitch_filters(spec, SR, "classic")
    assert filters[0].startswith("asetrate=")
    assert filters[1] == f"aresample={SR}"
    tempo_correction = math.prod(float(f.split("=")[1]) for f in filters[2:])
    ratio = int(round(SR * 2 ** (-3 / 12))) / SR
    assert tempo_correction == pytest.approx(1 / ratio, rel=1e-6)


def test_identity_spec_produces_no_filters():
    assert fr.build_time_pitch_filters(fr.FxSpec(), SR, "classic") == []


def test_rubberband_engine_emits_single_filter():
    spec = fr.FxSpec(tempo=0.85, preserve_pitch=True)
    assert fr.build_time_pitch_filters(spec, SR, "rubberband") == [
        "rubberband=tempo=0.85:pitch=1"
    ]


def test_soxr_resampler_is_injected_into_aresample():
    spec = fr.FxSpec(tempo=0.85)
    filters = fr.build_time_pitch_filters(spec, SR, "classic", resampler="soxr")
    assert filters[1] == f"aresample={SR}:resampler=soxr:precision={fr.SOXR_PRECISION}"


def test_resolve_resampler_choices():
    assert fr.resolve_resampler("swr") is None
    assert fr.resolve_resampler(None) is None
    assert fr.resolve_resampler("soxr") == "soxr"
    with pytest.raises(ValueError):
        fr.resolve_resampler("hqmax")


# --------------------------------------------------------------------------
# reverb
# --------------------------------------------------------------------------


def test_echo_stages_split_four_taps_into_two_filters():
    delays = (29.0, 41.0, 71.0, 113.0)
    decays = (0.5, 0.42, 0.42, 0.32)
    stages = fr._echo_stages(delays, decays)
    assert len(stages) == 2
    assert stages[0] == (delays[:2], decays[:2])
    assert stages[1] == (delays[2:], decays[2:])


def test_echo_stages_keep_short_tap_lists_in_one_filter():
    assert len(fr._echo_stages((60.0,), (0.4,))) == 1


def test_reverb_send_highpasses_before_the_tail():
    before, after = fr.build_reverb_send_filters(fr.ReverbSettings())
    assert after == []
    assert before[0] == "highpass=f=200:poles=2"
    assert sum(1 for part in before if part.startswith("aecho=")) == 2
    assert before[-2] == "lowpass=f=7000"
    assert before[-1].startswith("volume=")


def test_wet_level_is_normalised_by_the_echo_gain():
    reverb = fr.ReverbSettings(mix=0.35)
    before, _ = fr.build_reverb_send_filters(reverb)
    level = float(before[-1].split("=")[1])
    expected = 0.35 / fr.echo_power_gain(reverb.delays, reverb.decays)
    assert level == pytest.approx(expected, rel=1e-4)
    # more taps must not mean a louder reverb
    assert level < 0.35


def test_echo_power_gain_grows_with_the_taps():
    quiet = fr.echo_power_gain((50.0,), (0.2,))
    loud = fr.echo_power_gain((29.0, 41.0, 71.0, 113.0), (0.5, 0.42, 0.42, 0.32))
    assert 1.0 < quiet < loud


def test_ir_wet_level_is_not_rescaled(tmp_path: Path):
    ir = tmp_path / "ir.wav"
    ir.write_bytes(b"RIFF")
    _, after = fr.build_reverb_send_filters(fr.ReverbSettings(ir_file=ir, mix=0.4))
    assert after[-1] == "volume=0.4"


def test_reverb_send_uses_unit_gains_in_the_echoes():
    before, _ = fr.build_reverb_send_filters(fr.ReverbSettings())
    for part in before:
        if part.startswith("aecho="):
            # the dry signal is mixed back separately, so in/out gain stay at 1
            assert part.split(":")[0] == "aecho=1" and part.split(":")[1] == "1"


def test_reverb_send_can_disable_the_filters():
    before, _ = fr.build_reverb_send_filters(fr.ReverbSettings(highpass=0, lowpass=0))
    assert not any(part.startswith(("highpass", "lowpass")) for part in before)


def test_ir_reverb_splits_around_the_afir_stage(tmp_path: Path):
    ir = tmp_path / "ir.wav"
    ir.write_bytes(b"RIFF")
    before, after = fr.build_reverb_send_filters(fr.ReverbSettings(ir_file=ir))
    assert before == ["highpass=f=200:poles=2"]
    assert after == ["lowpass=f=7000", "volume=0.35"]
    assert not any(part.startswith("aecho") for part in before + after)


def test_reverb_graph_ends_with_the_safety_limiter():
    graph = fr.plan_filters(fr.FxSpec(reverb=fr.ReverbSettings()), SR, "classic").description
    assert f"alimiter=limit={fr.SAFETY_LIMIT}:level=0:latency=1[out]" in graph


def test_reverb_graph_keeps_the_dry_signal_untouched():
    spec = fr.FxSpec(tempo=0.85, reverb=fr.ReverbSettings())
    plan = fr.plan_filters(spec, SR, "classic")
    graph = plan.description

    assert "-filter_complex" in plan.args
    assert f"[0:a]asetrate={int(SR * 0.85)},aresample={SR},asplit=2[dry][send]" in graph
    assert "amix=inputs=2:normalize=0" in graph
    # trim = 1 / (1 + mix) hands the wet signal its headroom back
    assert "volume=0.740741" in graph


def test_reverb_graph_trim_matches_the_mix():
    spec = fr.FxSpec(reverb=fr.ReverbSettings(mix=1.0))
    graph = fr.plan_filters(spec, SR, "classic").description
    assert "amix=inputs=2:normalize=0,volume=0.5," in graph


def test_ir_graph_adds_the_second_input(tmp_path: Path):
    ir = tmp_path / "ir.wav"
    ir.write_bytes(b"RIFF")
    spec = fr.FxSpec(tempo=0.9, reverb=fr.ReverbSettings(ir_file=ir))
    plan = fr.plan_filters(spec, SR, "classic")
    assert plan.extra_inputs == [ir]
    assert "[ir_in][1:a]afir=dry=0:wet=1[conv]" in plan.description


def test_plan_without_reverb_uses_a_simple_chain():
    plan = fr.plan_filters(fr.FxSpec(tempo=0.85), SR, "classic")
    assert plan.args[:2] == ["-map", "0:a:0"]
    assert plan.args[2] == "-filter:a"
    assert plan.extra_inputs == []


def test_plan_for_identity_spec_has_no_filter_argument():
    plan = fr.plan_filters(fr.FxSpec(), SR, "classic")
    assert plan.args == ["-map", "0:a:0"]


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        fr.FxSpec(tempo=0),
        fr.FxSpec(tempo=-1),
        fr.FxSpec(pitch_semitones=100),
        fr.FxSpec(engine="magic"),
        fr.FxSpec(reverb=fr.ReverbSettings(mix=0)),
        fr.FxSpec(reverb=fr.ReverbSettings(mix=3)),
        fr.FxSpec(reverb=fr.ReverbSettings(highpass=8000, lowpass=4000)),
        fr.FxSpec(reverb=fr.ReverbSettings(highpass=-1)),
        fr.FxSpec(reverb=fr.ReverbSettings(decays=(2.0,), delays=(50.0,))),
        fr.FxSpec(reverb=fr.ReverbSettings(delays=(10, 20), decays=(0.3,))),
    ],
)
def test_invalid_specs_are_rejected(spec):
    with pytest.raises(ValueError):
        spec.validate()


def test_pitch_ratio_follows_tempo_by_default():
    assert fr.FxSpec(tempo=0.8).pitch_ratio == pytest.approx(0.8)
    assert fr.FxSpec(tempo=0.8, preserve_pitch=True).pitch_ratio == 1.0
    assert fr.FxSpec(pitch_semitones=12).pitch_ratio == pytest.approx(2.0)


# --------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------


def test_build_command_simple_path(tmp_path: Path):
    cmd = fr.build_command(
        tmp_path / "in.mp3",
        tmp_path / "out.mp3",
        fr.FxSpec(tempo=0.85),
        info=fr.AudioInfo(sample_rate=SR, bit_rate=320000),
    )
    assert "-filter:a" in cmd
    assert cmd[cmd.index("-filter:a") + 1] == f"asetrate={int(SR * 0.85)},aresample={SR}"
    assert cmd[cmd.index("-b:a") + 1] == "320k"
    assert cmd[-1] == str(tmp_path / "out.mp3")


def test_build_command_ir_uses_two_inputs(tmp_path: Path):
    ir = tmp_path / "ir.wav"
    ir.write_bytes(b"RIFF")
    spec = fr.FxSpec(tempo=0.9, reverb=fr.ReverbSettings(ir_file=ir))
    cmd = fr.build_command(tmp_path / "in.wav", tmp_path / "out.wav", spec)
    assert cmd.count("-i") == 2
    assert "afir=dry=0:wet=1" in cmd[cmd.index("-filter_complex") + 1]
    assert cmd[cmd.index("-map") + 1] == "[out]"


def test_build_command_no_bitrate_for_lossless(tmp_path: Path):
    cmd = fr.build_command(tmp_path / "in.wav", tmp_path / "out.wav", fr.FxSpec())
    assert "-b:a" not in cmd


def test_build_command_has_no_excerpt_options_by_default(tmp_path: Path):
    cmd = fr.build_command(tmp_path / "in.wav", tmp_path / "out.wav", fr.FxSpec())
    assert "-ss" not in cmd and "-t" not in cmd


def test_build_command_seeks_before_the_input(tmp_path: Path):
    cmd = fr.build_command(
        tmp_path / "in.mp3",
        tmp_path / "out.wav",
        fr.FxSpec(tempo=0.85),
        start=12.5,
        duration=20,
    )
    # -ss must come first: after -i it decodes and throws away everything
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "12.5"
    assert cmd[cmd.index("-t") + 1] == "20"


def test_excerpt_never_seeks_the_impulse_response(tmp_path: Path):
    ir = tmp_path / "ir.wav"
    ir.write_bytes(b"RIFF")
    spec = fr.FxSpec(tempo=0.9, reverb=fr.ReverbSettings(ir_file=ir))
    cmd = fr.build_command(tmp_path / "in.wav", tmp_path / "out.wav", spec, start=5)
    assert cmd.count("-ss") == 1
    assert cmd[cmd.index("-ss") + 1 : cmd.index("-ss") + 4] == [
        "5",
        "-i",
        str(tmp_path / "in.wav"),
    ]


@ffmpeg_required
def test_excerpt_length_follows_the_requested_duration(workdir: Path, tmp_path: Path):
    target = tmp_path / "excerpt.wav"
    fr.convert_file(
        workdir / "song.wav", target, fr.FxSpec(tempo=0.5), start=0.5, duration=1.0
    )
    assert duration_of(target) == pytest.approx(1.0, rel=0.1)


def test_missing_binary_message_is_actionable(monkeypatch):
    monkeypatch.setattr(fr.shutil, "which", lambda name: None)
    with pytest.raises(fr.FFmpegNotFoundError) as excinfo:
        fr.ensure_tools()
    assert "PATH" in str(excinfo.value)


def test_summarize_stderr_keeps_tail():
    text = "\n".join(f"line {i}" for i in range(20))
    summary = fr.summarize_stderr(text, max_lines=3)
    assert summary.splitlines() == ["line 17", "line 18", "line 19"]


# --------------------------------------------------------------------------
# integration (real ffmpeg)
# --------------------------------------------------------------------------


@ffmpeg_required
def test_probe_reads_sample(sample_wav: Path):
    info = fr.probe(sample_wav)
    assert info.sample_rate == SR
    assert info.duration == pytest.approx(2.0, abs=0.05)


@ffmpeg_required
@pytest.mark.parametrize(
    "spec,expected",
    [
        (fr.FxSpec(tempo=0.5), 4.0),
        (fr.FxSpec(tempo=2.0), 1.0),
        (fr.FxSpec(tempo=0.4, preserve_pitch=True), 5.0),
        # the reverb tail adds roughly the sum of the longest tap per stage
        (fr.FxSpec(tempo=0.85, reverb=fr.ReverbSettings()), 2 / 0.85 + 0.154),
        (fr.FxSpec(reverb=fr.ReverbSettings()), 2.154),
    ],
)
def test_conversion_duration_matches_factor(sample_wav, tmp_path, spec, expected):
    out = tmp_path / "out.wav"
    fr.convert_file(sample_wav, out, spec)
    assert out.is_file() and out.stat().st_size > 0
    # rubberband can shift the length by a few ms, hence the 5% tolerance
    assert duration_of(out) == pytest.approx(expected, rel=0.05)


@ffmpeg_required
def test_reverb_never_clips_even_at_full_scale(tmp_path):
    """Regression test: the old aecho mix summed above 0 dBFS and clipped.

    A sustained full-scale tone is the worst case - every echo tap lines up
    with the dry signal - so if this stays under 0 dBFS, music will too.
    """
    loud = tmp_path / "loud.wav"
    fr.run_ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
            # ffmpeg's sine source sits at -18 dBFS; lift it to full scale
            "-af", "volume=8", "-c:a", "pcm_s16le", str(loud),
        ]
    )
    assert peak_dbfs(loud) == pytest.approx(0.0, abs=0.3)

    out = tmp_path / "reverb.wav"
    fr.convert_file(loud, out, fr.FxSpec(tempo=0.85, reverb=fr.ReverbSettings(mix=0.8)))
    assert peak_dbfs(out) <= -0.1


@ffmpeg_required
def test_conversion_to_mp3_produces_valid_file(sample_wav, tmp_path):
    out = tmp_path / "out.mp3"
    result = fr.convert_file(sample_wav, out, fr.FxSpec(tempo=0.85, reverb=fr.ReverbSettings()))
    assert result.filter_chain
    info = fr.probe(out)
    assert info.codec == "mp3"
    assert info.duration == pytest.approx(2 / 0.85 + 0.154, rel=0.05)


@ffmpeg_required
def test_ir_convolution_runs(sample_wav, tmp_path):
    ir = Path(fr.__file__).parent / "assets" / "ir" / "room_small.wav"
    if not ir.is_file():  # pragma: no cover
        pytest.skip("IR file is missing")
    out = tmp_path / "ir.wav"
    fr.convert_file(sample_wav, out, fr.FxSpec(reverb=fr.ReverbSettings(ir_file=ir)))
    assert out.stat().st_size > 0


@ffmpeg_required
def test_soxr_chain_is_accepted_by_ffmpeg(sample_wav, tmp_path):
    if not fr.has_soxr():
        pytest.skip("this ffmpeg was built without libsoxr")
    out = tmp_path / "soxr.wav"
    result = fr.convert_file(sample_wav, out, fr.FxSpec(tempo=0.8), resampler="soxr")
    assert "resampler=soxr" in result.filter_chain
    assert duration_of(out) == pytest.approx(2 / 0.8, rel=0.02)


@ffmpeg_required
def test_no_overwrite_raises(sample_wav, tmp_path):
    out = tmp_path / "out.wav"
    out.write_bytes(b"x")
    with pytest.raises(fr.FFmpegError):
        fr.convert_file(sample_wav, out, fr.FxSpec(tempo=0.9), overwrite=False)


@ffmpeg_required
def test_missing_input_raises(tmp_path):
    with pytest.raises(fr.FFmpegError):
        fr.convert_file(tmp_path / "nope.wav", tmp_path / "out.wav", fr.FxSpec(tempo=0.9))


@ffmpeg_required
def test_dry_run_writes_nothing(sample_wav, tmp_path):
    out = tmp_path / "out.wav"
    result = fr.convert_file(sample_wav, out, fr.FxSpec(tempo=0.9), dry_run=True)
    assert not out.exists()
    assert result.command[0].lower().endswith(("ffmpeg", "ffmpeg.exe"))
