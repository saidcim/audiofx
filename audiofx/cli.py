"""audiofx command line interface."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .ffmpeg_runner import (
    AUDIO_EXTENSIONS,
    ENGINES,
    RESAMPLERS,
    REVERB_ROOMS,
    ConversionResult,
    FFmpegError,
    FxSpec,
    ReverbSettings,
    convert_file,
    ensure_tools,
)
from .metadata import copy_metadata
from .presets import MODES, PresetError, combined_presets, get_preset, load_presets

MODE_DEFAULT_FACTOR = {
    "slow": 0.85,
    "speed": 1.25,
    "reverb": 1.0,
    "slowed_reverb": 0.85,
}


class UsageError(Exception):
    """User error - printed as a message instead of a traceback."""


# --------------------------------------------------------------------------
# spec resolution
# --------------------------------------------------------------------------


def resolve_spec(args: argparse.Namespace) -> tuple[FxSpec, str, str | None]:
    """Merge preset values with CLI flags. Returns (spec, mode, preset_name)."""
    preset = get_preset(args.preset, args.presets_file) if args.preset else None

    mode = args.mode or (preset.mode if preset else None)
    if mode is None:
        raise UsageError("Pass --mode or --preset.")
    if mode not in MODES:
        raise UsageError(f"Invalid mode: {mode}")

    if args.factor is not None:
        factor = args.factor
    elif preset is not None and preset.factor != 1.0:
        factor = preset.factor
    else:
        factor = MODE_DEFAULT_FACTOR[mode]

    if mode == "reverb":
        if args.factor is not None and args.factor != 1.0:
            raise UsageError("--factor does not apply to reverb mode (tempo is untouched).")
        factor = 1.0
    elif mode in ("slow", "slowed_reverb") and factor >= 1.0:
        raise UsageError(f"'{mode}' mode needs a factor below 1.0 (got {factor:g}).")
    elif mode == "speed" and factor <= 1.0:
        raise UsageError(f"'speed' mode needs a factor above 1.0 (got {factor:g}).")

    pitch = args.pitch_shift
    if pitch is None and preset is not None:
        pitch = preset.pitch_shift

    preserve_pitch = args.preserve_pitch or bool(preset and preset.preserve_pitch)

    engine = args.engine
    if engine == "auto" and preset is not None and preset.engine != "auto":
        engine = preset.engine

    reverb: ReverbSettings | None = None
    if mode in ("reverb", "slowed_reverb"):
        reverb = (preset.reverb if preset and preset.reverb else None) or ReverbSettings()
        if args.reverb_room:
            reverb = replace(reverb, decay=REVERB_ROOMS[args.reverb_room], ir_file=None)
        if args.reverb_decay is not None:
            reverb = replace(reverb, decay=args.reverb_decay, ir_file=None)
        if args.reverb_predelay is not None:
            reverb = replace(reverb, predelay=args.reverb_predelay)
        if args.reverb_mix is not None:
            reverb = replace(reverb, mix=args.reverb_mix)
        if args.reverb_bass_cut is not None:
            reverb = replace(reverb, highpass=args.reverb_bass_cut)
        if args.reverb_damping is not None:
            reverb = replace(reverb, lowpass=args.reverb_damping)
        if args.ir_file:
            ir_path = Path(args.ir_file)
            if not ir_path.is_file():
                raise UsageError(f"IR file not found: {ir_path}")
            reverb = replace(reverb, ir_file=ir_path)

    spec = FxSpec(
        tempo=factor,
        pitch_semitones=pitch,
        preserve_pitch=preserve_pitch,
        reverb=reverb,
        engine=engine,
        bass_gain=args.bass if args.bass is not None else (preset.bass if preset else 0.0),
        treble_gain=(
            args.treble if args.treble is not None else (preset.treble if preset else 0.0)
        ),
        stereo_width=(
            args.stereo_width
            if args.stereo_width is not None
            else (preset.stereo_width if preset else 1.0)
        ),
        normalize=args.normalize or bool(preset and preset.normalize),
    )
    try:
        spec.validate()
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    return spec, mode, (preset.name if preset else None)


def output_name(
    source: Path, mode: str, preset: str | None, extension: str | None = None
) -> str:
    """`{original_name}_{mode}[_{preset}].{ext}`"""
    suffix = extension or source.suffix.lstrip(".")
    parts = [source.stem, mode]
    if preset:
        parts.append(preset)
    return f"{'_'.join(parts)}.{suffix.lstrip('.')}"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def _report(result: ConversionResult, args: argparse.Namespace) -> None:
    if args.quiet:
        return
    print(f"    engine={result.engine}  filters: {result.filter_chain}")
    if args.dry_run:
        print(f"    command: {' '.join(result.command)}")


def _convert_one(
    source: Path,
    target: Path,
    spec: FxSpec,
    args: argparse.Namespace,
) -> ConversionResult:
    result = convert_file(
        source,
        target,
        spec,
        overwrite=args.overwrite,
        bitrate=args.bitrate,
        resampler=args.resampler,
        dry_run=args.dry_run,
    )
    if not args.dry_run and not args.no_metadata:
        for warning in copy_metadata(source, target):
            if not args.quiet:
                print(f"    warning: {warning}")
    return result


def cmd_convert(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if not source.is_file():
        raise UsageError(f"Input file not found: {source}")

    spec, mode, preset_name = resolve_spec(args)
    out_dir = Path(args.output) if args.output else source.parent
    target = out_dir / output_name(source, mode, preset_name, args.format)

    if not args.quiet:
        print(f"[1/1] {source.name} -> {target.name}")
    result = _convert_one(source, target, spec, args)
    _report(result, args)
    if not args.quiet:
        print("Done." if not args.dry_run else "Dry run: nothing was written.")
    return 0


def collect_inputs(root: Path, extensions: tuple[str, ...], recursive: bool) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    return [
        path
        for path in sorted(root.glob(pattern))
        if path.is_file() and path.suffix.lower() in wanted
    ]


def cmd_batch(args: argparse.Namespace) -> int:
    root = Path(args.input_dir)
    if not root.is_dir():
        raise UsageError(f"Folder not found: {root}")

    spec, mode, preset_name = resolve_spec(args)
    out_root = Path(args.output) if args.output else root.parent / f"{root.name}_audiofx"
    out_root = out_root.resolve()

    files = [
        path
        for path in collect_inputs(root, tuple(args.ext), args.recursive)
        # never re-process what we just wrote into the output folder
        if out_root not in path.resolve().parents
    ]
    if not files:
        raise UsageError(
            f"No audio files to process in {root} (looking for: {', '.join(args.ext)})"
        )

    total = len(files)
    failures = 0
    for index, source in enumerate(files, start=1):
        relative = source.relative_to(root).parent
        target = out_root / relative / output_name(source, mode, preset_name, args.format)
        if not args.quiet:
            print(f"[{index}/{total}] {source.relative_to(root)} -> {target.name}")
        try:
            result = _convert_one(source, target, spec, args)
        except FFmpegError as exc:
            failures += 1
            print(f"    ERROR: {exc}", file=sys.stderr)
            continue
        _report(result, args)

    if not args.quiet:
        done = total - failures
        print(f"Finished: {done}/{total} files processed -> {out_root}")
    return 1 if failures else 0


def cmd_presets(args: argparse.Namespace) -> int:
    presets = load_presets(args.presets_file) if args.presets_file else combined_presets()

    if args.presets_command == "list":
        if not presets:
            print("No presets defined.")
            return 0
        width = max(len(name) for name in presets)
        for name, preset in sorted(presets.items()):
            print(f"{name.ljust(width)}  {preset.description}")
        return 0

    preset = presets.get(args.name)
    if preset is None:
        raise UsageError(
            f"There is no preset named '{args.name}'. Available: {', '.join(sorted(presets))}"
        )

    print(f"{preset.name}")
    if preset.description:
        print(f"  description   : {preset.description}")
    print(f"  mode          : {preset.mode}")
    print(f"  factor        : {preset.factor:g}")
    if preset.pitch_shift is not None:
        print(f"  pitch shift   : {preset.pitch_shift:+g} semitones")
    print(f"  preserve pitch: {'yes' if preset.preserve_pitch else 'no'}")
    print(f"  engine        : {preset.engine}")
    for label, value in (
        ("bass", f"{preset.bass:+g} dB" if preset.bass else None),
        ("treble", f"{preset.treble:+g} dB" if preset.treble else None),
        ("stereo width", f"{preset.stereo_width:g}" if preset.stereo_width != 1.0 else None),
        ("normalize", "yes" if preset.normalize else None),
    ):
        if value is not None:
            print(f"  {label:<14}: {value}")

    if preset.reverb is not None:
        reverb = preset.reverb
        if reverb.ir_file:
            print(f"  reverb        : IR convolution -> {reverb.ir_file}")
        else:
            print(
                f"  reverb        : {reverb.decay:g} s decay"
                f"{f', {reverb.predelay:g} ms pre-delay' if reverb.predelay else ''}"
            )
        print(
            f"  reverb mix    : {reverb.mix:g}"
            f"  (bass cut {reverb.highpass:g} Hz, damping {reverb.lowpass:g} Hz)"
        )
    else:
        print("  reverb        : none")

    from .ffmpeg_runner import plan_filters

    plan = plan_filters(preset.to_spec(), 44100, "classic")
    print(f"  filters       : {plan.description}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .ffmpeg_runner import available_filters, has_soxr

    ffmpeg, ffprobe = ensure_tools()
    print(f"ffmpeg  : {ffmpeg}")
    print(f"ffprobe : {ffprobe}")
    filters = available_filters(ffmpeg)
    for name in ("atempo", "asetrate", "aresample", "aecho", "afir", "amix", "rubberband"):
        mark = "+" if name in filters else "-"
        print(f"  [{mark}] {name}")
    soxr = has_soxr(ffmpeg)
    print(f"  [{'+' if soxr else '-'}] libsoxr (high quality resampling)")
    if "rubberband" not in filters:
        print("Note: no rubberband; --preserve-pitch falls back to atempo (slightly lower quality).")
    if not soxr:
        print("Note: no libsoxr; ffmpeg's built-in swr resampler will be used.")

    from .downloader import SpotdlNotFoundError, find_spotdl

    try:
        print(f"spotdl  : {' '.join(find_spotdl())}")
    except SpotdlNotFoundError:
        print("spotdl  : not found (the optional download box in the GUI stays disabled)")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import launch

    return launch(songs_dir=args.songs, output_dir=args.output)


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def _add_fx_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--output", help="output folder")
    parser.add_argument("--mode", choices=MODES, help="effect to apply")
    parser.add_argument("--factor", type=float, help="tempo multiplier (e.g. 0.85 / 1.25)")
    parser.add_argument(
        "--pitch-shift",
        type=float,
        metavar="SEMITONES",
        help="shift the pitch by N semitones (e.g. -2, +3)",
    )
    parser.add_argument("--preset", help="preset name from presets.yaml")
    parser.add_argument(
        "--preserve-pitch",
        action="store_true",
        help="keep the pitch while the tempo changes (atempo/rubberband)",
    )
    parser.add_argument(
        "--engine",
        choices=ENGINES,
        default="auto",
        help="time/pitch engine (default: auto)",
    )
    parser.add_argument(
        "--reverb-room",
        "--reverb-size",
        dest="reverb_room",
        choices=tuple(REVERB_ROOMS),
        help="room size: %(choices)s (each one is a decay time)",
    )
    parser.add_argument(
        "--reverb-decay",
        type=float,
        metavar="SECONDS",
        help="seconds for the reverb tail to fall 60 dB (overrides --reverb-room)",
    )
    parser.add_argument(
        "--reverb-predelay",
        type=float,
        metavar="MS",
        help="silence before the tail starts; a longer gap reads as a bigger room",
    )
    parser.add_argument(
        "--reverb-mix",
        type=float,
        metavar="0-1",
        help="how much reverb is mixed in (default 0.35)",
    )
    parser.add_argument(
        "--reverb-bass-cut",
        type=float,
        metavar="HZ",
        help="remove the reverb below this frequency; stops the boomy, "
        "bass-boosted sound (default 200, 0 disables)",
    )
    parser.add_argument(
        "--reverb-damping",
        type=float,
        metavar="HZ",
        help="damp the reverb above this frequency (default 7000, 0 disables)",
    )
    parser.add_argument("--ir-file", help="wav impulse response for convolution reverb")
    parser.add_argument(
        "--bass", type=float, metavar="DB", help="low shelf in dB (e.g. +6, -3)"
    )
    parser.add_argument(
        "--treble", type=float, metavar="DB", help="high shelf in dB (e.g. -6)"
    )
    parser.add_argument(
        "--stereo-width",
        type=float,
        metavar="N",
        help="1.0 leaves the stereo image alone, 1.5 widens it, 0 collapses to mono",
    )
    parser.add_argument(
        "--normalize",
        action="store_true",
        help="bring the loudness to about -14 LUFS",
    )
    parser.add_argument("--format", help="output extension (e.g. mp3, wav, flac)")
    parser.add_argument("--bitrate", help="output bitrate (e.g. 192k)")
    parser.add_argument(
        "--resampler",
        choices=RESAMPLERS,
        default="auto",
        help="resampler; auto uses soxr when available (cleaner)",
    )
    parser.add_argument(
        "--no-overwrite",
        dest="overwrite",
        action="store_false",
        help="do not overwrite existing output files",
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="skip copying tags and cover art",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the ffmpeg command without running it",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--presets-file", help="alternative preset yaml file", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audiofx",
        description="Create slowed / sped up / reverb versions of audio files with ffmpeg.",
    )
    parser.add_argument("--version", action="version", version=f"audiofx {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert = subparsers.add_parser("convert", help="convert a single file")
    convert.add_argument("input", help="input audio file")
    _add_fx_arguments(convert)
    convert.set_defaults(func=cmd_convert)

    batch = subparsers.add_parser("batch", help="convert every file in a folder")
    batch.add_argument("input_dir", help="input folder")
    batch.add_argument("--recursive", action="store_true", help="include subfolders")
    batch.add_argument(
        "--ext",
        nargs="+",
        default=list(AUDIO_EXTENSIONS),
        help="extensions to process (default: %(default)s)",
    )
    _add_fx_arguments(batch)
    batch.set_defaults(func=cmd_batch)

    presets = subparsers.add_parser("presets", help="list or show presets")
    presets.add_argument("--presets-file", help="alternative preset yaml file", default=None)
    presets_sub = presets.add_subparsers(dest="presets_command", required=True)
    presets_sub.add_parser("list", help="list all presets")
    show = presets_sub.add_parser("show", help="show one preset in detail")
    show.add_argument("name", help="preset name")
    presets.set_defaults(func=cmd_presets)

    check = subparsers.add_parser("check", help="verify the ffmpeg installation")
    check.set_defaults(func=cmd_check)

    gui = subparsers.add_parser("gui", help="open the graphical interface")
    gui.add_argument("--songs", help="songs folder")
    gui.add_argument("-o", "--output", help="output folder")
    gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (UsageError, PresetError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except FFmpegError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
