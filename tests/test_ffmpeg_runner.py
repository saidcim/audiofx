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


def test_tap_gains_put_the_tail_on_an_exponential_slope():
    """A tap `d` ms in must sit at 10**(-3*d/decay), so that chained stages -
    where delays add and gains multiply - land every combination on the same
    curve. That identity is what makes the decay time mean anything."""
    decay = 1.5
    for delays, gains in fr.reverb_taps(decay):
        for delay, gain in zip(delays, gains):
            assert gain == pytest.approx(10 ** (-3 * delay / (decay * 1000)), rel=1e-3)


def test_the_tail_spans_exactly_one_decay_time():
    for decay in (0.5, 1.2, 3.0):
        spread = sum(max(delays) for delays, _ in fr.reverb_taps(decay))
        assert spread == pytest.approx(decay * 1000, rel=0.01)


def test_a_longer_decay_moves_every_tap_further_out():
    short = fr.reverb_taps(0.5)
    long = fr.reverb_taps(3.0)
    for (short_delays, _), (long_delays, _) in zip(short, long):
        assert max(long_delays) > max(short_delays)


def test_every_room_has_its_own_decay():
    decays = list(fr.REVERB_ROOMS.values())
    assert decays == sorted(decays), "rooms should grow in order"
    assert len(set(decays)) == len(decays), "two rooms sounding alike is the bug"


def test_reverb_send_highpasses_before_the_tail():
    before, after = fr.build_reverb_send_filters(fr.ReverbSettings())
    assert after == []
    assert before[0] == "highpass=f=200:poles=2"
    assert sum(1 for part in before if part.startswith("aecho=")) == fr.REVERB_STAGES
    assert before[-2] == "lowpass=f=7000"
    assert before[-1].startswith("volume=")


def test_wet_level_is_normalised_by_the_echo_gain():
    reverb = fr.ReverbSettings(mix=0.35)
    before, _ = fr.build_reverb_send_filters(reverb)
    level = float(before[-1].split("=")[1])
    expected = 0.35 / fr.echo_power_gain(reverb.stages)
    assert level == pytest.approx(expected, rel=1e-4)
    # more taps must not mean a louder reverb
    assert level < 0.35


def test_loudness_does_not_change_with_the_room():
    """Picking a bigger room has to change the character, not the volume."""
    gains = [fr.echo_power_gain(fr.reverb_taps(d)) for d in fr.REVERB_ROOMS.values()]
    assert max(gains) - min(gains) < 0.01


def test_the_send_drops_the_direct_signal():
    """in_gain=0 on the first stage. With 1 the send would carry a copy of the
    dry signal and `mix` would mostly just turn the track up."""
    before, _ = fr.build_reverb_send_filters(fr.ReverbSettings())
    echoes = [part for part in before if part.startswith("aecho=")]
    assert echoes[0].startswith("aecho=0:1:")
    for later in echoes[1:]:
        assert later.startswith("aecho=1:1:")


def test_predelay_comes_before_everything_else():
    before, _ = fr.build_reverb_send_filters(fr.ReverbSettings(predelay=40))
    assert before[0] == "adelay=40:all=1"


def test_no_predelay_means_no_delay_filter():
    before, _ = fr.build_reverb_send_filters(fr.ReverbSettings(predelay=0))
    assert not any(part.startswith("adelay") for part in before)


def test_ir_wet_level_is_not_rescaled(tmp_path: Path):
    ir = tmp_path / "ir.wav"
    ir.write_bytes(b"RIFF")
    _, after = fr.build_reverb_send_filters(fr.ReverbSettings(ir_file=ir, mix=0.4))
    assert after[-1] == "volume=0.4"


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
    assert "[ir_in][1:a]afir=wet=1[conv]" in plan.description


def test_afir_is_never_given_a_zero_input_gain():
    """afir's `dry` scales its *input*, it is not a dry/wet balance. dry=0
    feeds it silence and the whole reverb disappears - the bug that made the
    concert hall setting produce nothing at all."""
    ir = Path(__file__).parent.parent / "audiofx" / "assets" / "ir" / "hall_large.wav"
    if not ir.is_file():  # pragma: no cover - assets always ship
        pytest.skip("IR asset missing")
    graph = fr.plan_filters(fr.FxSpec(reverb=fr.ReverbSettings(ir_file=ir)), SR).description
    assert "dry=0" not in graph


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
        fr.FxSpec(reverb=fr.ReverbSettings(decay=0)),
        fr.FxSpec(reverb=fr.ReverbSettings(decay=100)),
        fr.FxSpec(reverb=fr.ReverbSettings(predelay=-5)),
        fr.FxSpec(reverb=fr.ReverbSettings(predelay=5000)),
        fr.FxSpec(bass_gain=50),
        fr.FxSpec(treble_gain=-50),
        fr.FxSpec(stereo_width=-1),
        fr.FxSpec(stereo_width=99),
    ],
)
def test_invalid_specs_are_rejected(spec):
    with pytest.raises(ValueError):
        spec.validate()


# --------------------------------------------------------------------------
# tone, width and loudness
# --------------------------------------------------------------------------


def test_tone_filters_are_shelves_around_the_defaults():
    assert fr.build_tone_filters(fr.FxSpec()) == []
    filters = fr.build_tone_filters(fr.FxSpec(bass_gain=6, treble_gain=-3))
    assert filters == [f"bass=g=6:f={fr._fmt(fr.BASS_SHELF_HZ)}",
                       f"treble=g=-3:f={fr._fmt(fr.TREBLE_SHELF_HZ)}"]


def test_tone_runs_before_the_split_so_the_tail_hears_it():
    spec = fr.FxSpec(bass_gain=6, reverb=fr.ReverbSettings())
    graph = fr.plan_filters(spec, SR, "classic").description
    assert graph.index("bass=g=6") < graph.index("asplit=2")


def test_master_chain_is_empty_when_nothing_is_asked_for():
    assert fr.build_master_filters(fr.FxSpec(), SR) == []


def test_stereo_width_and_loudness_land_after_the_mix():
    spec = fr.FxSpec(stereo_width=1.5, normalize=True, reverb=fr.ReverbSettings())
    graph = fr.plan_filters(spec, SR, "classic").description
    assert graph.index("amix=") < graph.index("extrastereo=")
    assert graph.index("extrastereo=") < graph.index("loudnorm=")


def test_loudnorm_is_followed_by_a_resample():
    """loudnorm runs its internals at 192 kHz and hands that rate on; without
    the resample the output file would silently change sample rate."""
    filters = fr.build_master_filters(fr.FxSpec(normalize=True), SR)
    assert filters[filters.index(f"loudnorm=I=-14:TP=-1.5:LRA=11") + 1] == f"aresample={SR}"


def test_anything_that_can_add_level_gets_the_limiter():
    for spec in (
        fr.FxSpec(bass_gain=6),
        fr.FxSpec(normalize=True),
        fr.FxSpec(stereo_width=1.5),
        fr.FxSpec(reverb=fr.ReverbSettings()),
    ):
        assert any(f.startswith("alimiter") for f in fr.build_master_filters(spec, SR))


def test_a_plain_slowdown_needs_no_limiter():
    assert fr.build_master_filters(fr.FxSpec(tempo=0.85), SR) == []


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
    assert "afir=wet=1" in cmd[cmd.index("-filter_complex") + 1]
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
        # the tail adds one decay time to the end of the file
        (fr.FxSpec(tempo=0.85, reverb=fr.ReverbSettings()),
         2 / 0.85 + fr.ReverbSettings().tail_ms / 1000),
        (fr.FxSpec(reverb=fr.ReverbSettings()), 2 + fr.ReverbSettings().tail_ms / 1000),
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
    expected = 2 / 0.85 + fr.ReverbSettings().tail_ms / 1000
    assert info.duration == pytest.approx(expected, rel=0.05)


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
