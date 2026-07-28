"""ffmpeg wrapper: filter graph construction + subprocess execution.

The module deliberately builds raw ffmpeg command lines instead of using a
binding library, so every filter that ends up in the graph is visible and
testable without ffmpeg being installed.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Sequence

# atempo only accepts factors inside this range; anything else must be chained.
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0

DEFAULT_SAMPLE_RATE = 44100
DEFAULT_BITRATE = "192k"

# Output formats that need an explicit bitrate to avoid ffmpeg's low defaults.
LOSSY_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"}

AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma")

ENGINES = ("auto", "classic", "rubberband")

# soxr is a noticeably cleaner resampler than ffmpeg's built-in swr; it only
# matters for the asetrate path, which resamples every sample of the file.
RESAMPLERS = ("auto", "soxr", "swr")
SOXR_PRECISION = 28

# Final safety net after the wet/dry mix. It is set just below full scale and
# never engages on normal material, but it makes clipping impossible even for
# pathological input (a sustained full-scale tone lines every echo tap up).
SAFETY_LIMIT = 0.98

# Tap layouts for the reverb tail, shared by the CLI and the GUI. Delays are in
# milliseconds; the values are mutually prime-ish so the taps never line up and
# smear into a single flutter echo.
REVERB_SIZES: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {
    "small": ((17.0, 29.0, 43.0, 61.0), (0.45, 0.36, 0.3, 0.22)),
    "medium": ((29.0, 41.0, 71.0, 113.0), (0.5, 0.42, 0.42, 0.32)),
    "large": ((53.0, 79.0, 131.0, 191.0), (0.55, 0.45, 0.4, 0.3)),
}


class FFmpegError(RuntimeError):
    """ffmpeg ran but reported an error, or produced no output."""


class FFmpegNotFoundError(FFmpegError):
    """ffmpeg/ffprobe is not on PATH."""


# --------------------------------------------------------------------------
# binary discovery
# --------------------------------------------------------------------------


def find_binary(name: str) -> str:
    """Return the full path of `name`, or raise a helpful error."""
    path = shutil.which(name)
    if path is None:
        raise FFmpegNotFoundError(
            f"'{name}' was not found on PATH. Install it with:\n"
            "  Windows : winget install Gyan.FFmpeg\n"
            "  macOS   : brew install ffmpeg\n"
            "  Debian  : sudo apt install ffmpeg\n"
            "If it is installed, add the ffmpeg bin folder to your PATH."
        )
    return path


def ensure_tools() -> tuple[str, str]:
    """Verify both ffmpeg and ffprobe exist. Returns (ffmpeg, ffprobe)."""
    return find_binary("ffmpeg"), find_binary("ffprobe")


@lru_cache(maxsize=8)
def available_filters(ffmpeg: str | None = None) -> frozenset[str]:
    """Names of the filters compiled into this ffmpeg build."""
    ffmpeg = ffmpeg or find_binary("ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        # rows look like: " .. atempo  A->A  Adjust audio tempo."
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return frozenset(names)


def has_filter(name: str, ffmpeg: str | None = None) -> bool:
    return name in available_filters(ffmpeg)


@lru_cache(maxsize=8)
def has_soxr(ffmpeg: str | None = None) -> bool:
    """True when this ffmpeg was built with libsoxr."""
    ffmpeg = ffmpeg or find_binary("ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-buildconf"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return "--enable-libsoxr" in f"{proc.stdout}{proc.stderr}"


def resolve_resampler(value: str | None, ffmpeg: str | None = None) -> str | None:
    """Turn 'auto'/'soxr'/'swr' into the resampler name to put in the chain."""
    if value in (None, "swr"):
        return None
    if value == "soxr":
        return "soxr"
    if value != "auto":
        raise ValueError(f"resampler must be one of {RESAMPLERS}")
    return "soxr" if has_soxr(ffmpeg) else None


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioInfo:
    sample_rate: int = DEFAULT_SAMPLE_RATE
    channels: int = 2
    duration: float | None = None
    bit_rate: int | None = None
    codec: str | None = None


def probe(path: Path | str, ffprobe: str | None = None) -> AudioInfo:
    """Read stream properties of the first audio stream via ffprobe."""
    ffprobe = ffprobe or find_binary("ffprobe")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels,duration,bit_rate,codec_name:format=duration,bit_rate",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise FFmpegError(f"Could not read file: {path}\n{summarize_stderr(proc.stderr)}")

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - malformed ffprobe output
        raise FFmpegError(f"Could not parse ffprobe output: {exc}") from exc

    streams = data.get("streams") or []
    if not streams:
        raise FFmpegError(f"No audio stream found in: {path}")

    stream = streams[0]
    fmt = data.get("format") or {}

    def _num(*values):
        for value in values:
            if value in (None, "", "N/A"):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    duration = _num(stream.get("duration"), fmt.get("duration"))
    bit_rate = _num(stream.get("bit_rate"), fmt.get("bit_rate"))
    sample_rate = _num(stream.get("sample_rate")) or DEFAULT_SAMPLE_RATE
    channels = _num(stream.get("channels")) or 2

    return AudioInfo(
        sample_rate=int(sample_rate),
        channels=int(channels),
        duration=duration,
        bit_rate=int(bit_rate) if bit_rate else None,
        codec=stream.get("codec_name"),
    )


# --------------------------------------------------------------------------
# effect description
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReverbSettings:
    """Reverb as a send/return effect.

    The reverb is never mixed by summing the dry signal with delayed copies of
    itself at full level - that is what makes naive `aecho` chains sound boomy
    and clipped ("bass boosted"). Instead the signal is split, the send is
    high-passed so low frequencies never enter the tail, the tail is built
    there, and only `mix` of it is added back to the untouched dry signal.
    """

    delays: Sequence[float] = REVERB_SIZES["medium"][0]
    decays: Sequence[float] = REVERB_SIZES["medium"][1]
    mix: float = 0.35
    highpass: float = 200.0  # Hz; 0 disables. Keeps bass out of the tail.
    lowpass: float = 7000.0  # Hz; 0 disables. Damps the tail like a real room.
    ir_file: Path | None = None

    def validate(self) -> None:
        if not 0 < self.mix <= 2:
            raise ValueError("reverb mix must be between 0 and 2")
        if self.highpass < 0 or self.lowpass < 0:
            raise ValueError("reverb highpass/lowpass must not be negative")
        if self.highpass and self.lowpass and self.highpass >= self.lowpass:
            raise ValueError("reverb highpass must be below lowpass")
        if self.ir_file is not None:
            if not Path(self.ir_file).is_file():
                raise ValueError(f"IR file not found: {self.ir_file}")
            return
        if not self.delays or not self.decays:
            raise ValueError("reverb delays/decays must not be empty")
        if len(self.delays) != len(self.decays):
            raise ValueError("reverb delays and decays must have the same length")
        if any(d <= 0 for d in self.delays):
            raise ValueError("reverb delay values must be positive")
        if any(not 0 < d <= 1 for d in self.decays):
            raise ValueError("reverb decay values must be between 0 and 1")

    @property
    def tail_ms(self) -> float:
        """Roughly how much longer the output gets."""
        if self.ir_file is not None:
            return 0.0
        stages = _echo_stages(self.delays, self.decays)
        return sum(max(delays) for delays, _ in stages)


@dataclass(frozen=True)
class FxSpec:
    """What to do to the audio: tempo, pitch and reverb."""

    tempo: float = 1.0
    pitch_semitones: float | None = None
    preserve_pitch: bool = False
    reverb: ReverbSettings | None = None
    engine: str = "auto"

    def validate(self) -> None:
        if not 0 < self.tempo <= 100:
            raise ValueError("factor must be between 0 and 100")
        if self.pitch_semitones is not None and abs(self.pitch_semitones) > 48:
            raise ValueError("pitch-shift must be between -48 and +48 semitones")
        if self.engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}")
        if self.reverb is not None:
            self.reverb.validate()

    @property
    def pitch_ratio(self) -> float:
        """Frequency multiplier applied to the source material."""
        if self.pitch_semitones is not None:
            return 2 ** (self.pitch_semitones / 12)
        if self.preserve_pitch:
            return 1.0
        # classic slowed/nightcore behaviour: pitch follows tempo
        return self.tempo

    @property
    def is_identity(self) -> bool:
        return (
            _close(self.tempo, 1.0)
            and _close(self.pitch_ratio, 1.0)
            and self.reverb is None
        )


# --------------------------------------------------------------------------
# filter chain
# --------------------------------------------------------------------------


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def _fmt(value: float) -> str:
    """Compact fixed-point formatting; ffmpeg dislikes scientific notation."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def atempo_chain(ratio: float) -> list[float]:
    """Split `ratio` into atempo factors that each stay within 0.5-2.0."""
    if ratio <= 0:
        raise ValueError("tempo ratio must be positive")
    if _close(ratio, 1.0, 1e-9):
        return []

    factors: list[float] = []
    remaining = ratio
    while remaining > ATEMPO_MAX:
        factors.append(ATEMPO_MAX)
        remaining /= ATEMPO_MAX
    while remaining < ATEMPO_MIN:
        factors.append(ATEMPO_MIN)
        remaining /= ATEMPO_MIN
    if not _close(remaining, 1.0, 1e-9):
        factors.append(remaining)
    return factors


def resolve_engine(spec: FxSpec, ffmpeg: str | None = None) -> str:
    """Turn engine='auto' into a concrete engine name."""
    if spec.engine == "classic":
        return "classic"

    rubberband = has_filter("rubberband", ffmpeg)
    if spec.engine == "rubberband":
        if not rubberband:
            raise FFmpegError(
                "This ffmpeg build has no 'rubberband' filter. Use --engine classic, "
                "or install a build configured with --enable-librubberband."
            )
        return "rubberband"

    # auto: rubberband only pays off when tempo and pitch move independently
    if not _close(spec.tempo, spec.pitch_ratio) and rubberband:
        return "rubberband"
    return "classic"


def build_time_pitch_filters(
    spec: FxSpec,
    sample_rate: int,
    engine: str = "classic",
    resampler: str | None = None,
) -> list[str]:
    """Filters that realise the requested tempo/pitch pair."""
    tempo = spec.tempo
    pitch = spec.pitch_ratio

    if _close(tempo, 1.0) and _close(pitch, 1.0):
        return []

    if engine == "rubberband":
        return [f"rubberband=tempo={_fmt(tempo)}:pitch={_fmt(pitch)}"]

    filters: list[str] = []
    effective_pitch = 1.0
    if not _close(pitch, 1.0):
        # asetrate replays the samples at a different rate: pitch and tempo
        # both scale by `pitch`; aresample brings the rate back to normal.
        new_rate = int(round(sample_rate * pitch))
        if new_rate < 1:
            raise ValueError("pitch ratio is too low to produce a valid sample rate")
        effective_pitch = new_rate / sample_rate
        filters.append(f"asetrate={new_rate}")
        if resampler == "soxr":
            filters.append(
                f"aresample={sample_rate}:resampler=soxr:precision={SOXR_PRECISION}"
            )
        else:
            filters.append(f"aresample={sample_rate}")

    # compensate whatever tempo change asetrate already introduced
    filters.extend(f"atempo={_fmt(f)}" for f in atempo_chain(tempo / effective_pitch))
    return filters


def _echo_stages(
    delays: Sequence[float], decays: Sequence[float]
) -> list[tuple[Sequence[float], Sequence[float]]]:
    """Split the taps into chained aecho stages.

    Two chained `aecho` filters convolve with each other, so N+M taps become
    N*M echoes. That density is what makes the tail sound like a room instead
    of a slap-back echo, and it costs nothing extra.
    """
    if len(delays) >= 4:
        middle = len(delays) // 2
        return [
            (delays[:middle], decays[:middle]),
            (delays[middle:], decays[middle:]),
        ]
    return [(delays, decays)]


def echo_power_gain(delays: Sequence[float], decays: Sequence[float]) -> float:
    """How much louder the echo network makes the signal, in RMS terms.

    Each `aecho` passes the input through plus one delayed copy per tap, so its
    output power is 1 + sum(decay^2) and chained stages multiply. Dividing the
    wet path by this keeps "mix" meaning the same amount of reverb no matter
    how many taps the room uses.
    """
    gain = 1.0
    for _stage_delays, stage_decays in _echo_stages(delays, decays):
        gain *= math.sqrt(1 + sum(d * d for d in stage_decays))
    return gain


def build_reverb_send_filters(reverb: ReverbSettings) -> tuple[list[str], list[str]]:
    """Filters for the reverb send, split around the (optional) afir stage.

    Returns (before_ir, after_ir). When no IR file is used the whole chain is
    in the first list.
    """
    before: list[str] = []
    after: list[str] = []

    if reverb.highpass:
        # Two poles: gentle enough to stay natural, steep enough to stop the
        # low end from piling up in the tail.
        before.append(f"highpass=f={_fmt(reverb.highpass)}:poles=2")

    target = before if reverb.ir_file is None else after
    level = reverb.mix
    if reverb.ir_file is None:
        for delays, decays in _echo_stages(reverb.delays, reverb.decays):
            taps = "|".join(_fmt(d) for d in delays)
            gains = "|".join(_fmt(d) for d in decays)
            # in_gain/out_gain are 1 here: this path carries only the wet
            # signal, the dry one is mixed back in separately.
            target.append(f"aecho=1:1:{taps}:{gains}")
        level /= echo_power_gain(reverb.delays, reverb.decays)
    # afir normalises the impulse response itself, so the IR path needs no
    # correction of its own.

    if reverb.lowpass:
        target.append(f"lowpass=f={_fmt(reverb.lowpass)}")

    target.append(f"volume={_fmt(level)}")
    return before, after


def build_filter_chain(
    spec: FxSpec,
    sample_rate: int,
    engine: str = "classic",
    resampler: str | None = None,
) -> str:
    """Linear (single input, single output) part of the processing."""
    return ",".join(build_time_pitch_filters(spec, sample_rate, engine, resampler))


@dataclass
class FilterPlan:
    """How the filters are handed to ffmpeg for one conversion."""

    args: list[str] = field(default_factory=list)
    description: str = ""
    extra_inputs: list[Path] = field(default_factory=list)


def plan_filters(
    spec: FxSpec,
    sample_rate: int,
    engine: str = "classic",
    resampler: str | None = None,
) -> FilterPlan:
    """Build either a simple -filter:a chain or a full -filter_complex graph."""
    time_pitch = build_time_pitch_filters(spec, sample_rate, engine, resampler)
    reverb = spec.reverb

    if reverb is None:
        chain = ",".join(time_pitch)
        args = ["-map", "0:a:0"]
        if chain:
            args += ["-filter:a", chain]
        return FilterPlan(args=args, description=chain or "(no filters)")

    before_ir, after_ir = build_reverb_send_filters(reverb)
    head = ",".join(time_pitch)
    split = f"[0:a]{head + ',' if head else ''}asplit=2[dry][send]"

    parts = [split]
    if reverb.ir_file is None:
        parts.append(f"[send]{','.join(before_ir)}[wet]")
    else:
        pre = ",".join(before_ir)
        parts.append(f"[send]{pre}[ir_in]" if pre else "[send]anull[ir_in]")
        # dry=0 keeps afir's own dry path silent - we do the mixing ourselves.
        parts.append("[ir_in][1:a]afir=dry=0:wet=1[conv]")
        parts.append(f"[conv]{','.join(after_ir)}[wet]")

    # normalize=0 keeps amix from rescaling. The trim gives the wet signal its
    # headroom back, and the limiter catches the rare peak that survives it -
    # together they are what stops the "boomy, clipped" reverb sound.
    trim = 1 / (1 + reverb.mix)
    parts.append(
        f"[dry][wet]amix=inputs=2:normalize=0,volume={_fmt(trim)},"
        f"alimiter=limit={_fmt(SAFETY_LIMIT)}:level=0:latency=1[out]"
    )

    graph = ";".join(parts)
    args = ["-filter_complex", graph, "-map", "[out]"]
    extra = [Path(reverb.ir_file)] if reverb.ir_file is not None else []
    return FilterPlan(args=args, description=graph, extra_inputs=extra)


# --------------------------------------------------------------------------
# command construction & execution
# --------------------------------------------------------------------------


def build_command(
    input_path: Path | str,
    output_path: Path | str,
    spec: FxSpec,
    *,
    info: AudioInfo | None = None,
    engine: str = "classic",
    ffmpeg: str = "ffmpeg",
    overwrite: bool = True,
    bitrate: str | None = None,
    resampler: str | None = None,
) -> list[str]:
    """Assemble the ffmpeg argv for one conversion."""
    info = info or AudioInfo()
    input_path = Path(input_path)
    output_path = Path(output_path)

    plan = plan_filters(spec, info.sample_rate, engine, resampler)

    cmd: list[str] = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    cmd.append("-y" if overwrite else "-n")
    cmd += ["-i", str(input_path)]
    for extra in plan.extra_inputs:
        cmd += ["-i", str(extra)]

    cmd += plan.args
    cmd += ["-map_metadata", "0"]

    target_bitrate = bitrate
    if target_bitrate is None and output_path.suffix.lower() in LOSSY_EXTENSIONS:
        target_bitrate = f"{info.bit_rate // 1000}k" if info.bit_rate else DEFAULT_BITRATE
    if target_bitrate:
        cmd += ["-b:a", target_bitrate]

    cmd.append(str(output_path))
    return cmd


def summarize_stderr(stderr: str, max_lines: int = 6) -> str:
    """Keep the tail of ffmpeg's stderr - that is where the real error lives."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return "ffmpeg returned no error message."
    return "\n".join(lines[-max_lines:])


def run_ffmpeg(cmd: Sequence[str]) -> str:
    """Run an ffmpeg command; raise FFmpegError with a readable message."""
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:  # ffmpeg vanished between check and run
        raise FFmpegNotFoundError(str(exc)) from exc

    if proc.returncode != 0:
        raise FFmpegError(
            f"ffmpeg exited with code {proc.returncode}:\n{summarize_stderr(proc.stderr)}"
        )
    return proc.stderr or ""


@dataclass
class ConversionResult:
    input_path: Path
    output_path: Path
    command: list[str] = field(default_factory=list)
    engine: str = "classic"
    filter_chain: str = ""
    source: AudioInfo | None = None
    resampler: str | None = None


def convert_file(
    input_path: Path | str,
    output_path: Path | str,
    spec: FxSpec,
    *,
    overwrite: bool = True,
    bitrate: str | None = None,
    resampler: str = "auto",
    dry_run: bool = False,
) -> ConversionResult:
    """Probe, build and run a single conversion."""
    spec.validate()
    ffmpeg, ffprobe = ensure_tools()

    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.is_file():
        raise FFmpegError(f"Input file not found: {input_path}")
    if input_path.resolve() == output_path.resolve():
        raise FFmpegError("Input and output must not be the same file.")
    if output_path.exists() and not overwrite:
        raise FFmpegError(f"Output already exists: {output_path} (use overwrite to replace)")

    info = probe(input_path, ffprobe)
    engine = resolve_engine(spec, ffmpeg)
    resolved_resampler = resolve_resampler(resampler, ffmpeg)
    plan = plan_filters(spec, info.sample_rate, engine, resolved_resampler)
    cmd = build_command(
        input_path,
        output_path,
        spec,
        info=info,
        engine=engine,
        ffmpeg=ffmpeg,
        overwrite=overwrite,
        bitrate=bitrate,
        resampler=resolved_resampler,
    )

    result = ConversionResult(
        input_path=input_path,
        output_path=output_path,
        command=cmd,
        engine=engine,
        filter_chain=plan.description,
        source=info,
        resampler=resolved_resampler,
    )
    if dry_run:
        return result

    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(cmd)

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise FFmpegError(f"ffmpeg reported success but produced no output: {output_path}")
    return result
