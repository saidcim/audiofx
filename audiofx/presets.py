"""Preset loading and validation.

A preset is a named bundle of default values (mode, factor, reverb settings).
CLI flags always win over whatever the preset supplies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .ffmpeg_runner import ENGINES, REVERB_SIZES, FxSpec, ReverbSettings

MODES = ("slow", "speed", "reverb", "slowed_reverb")

DEFAULT_PRESET_FILE = Path(__file__).with_name("presets.yaml")
ASSETS_DIR = Path(__file__).parent / "assets"

_ALLOWED_KEYS = {
    "description",
    "mode",
    "factor",
    "pitch_shift",
    "preserve_pitch",
    "engine",
    "reverb",
}
_ALLOWED_REVERB_KEYS = {
    "size",
    "delays",
    "decays",
    "mix",
    "highpass",
    "lowpass",
    "ir_file",
}


class PresetError(ValueError):
    """The preset file, or one preset in it, is invalid."""


@dataclass(frozen=True)
class Preset:
    name: str
    description: str = ""
    mode: str = "slowed_reverb"
    factor: float = 1.0
    pitch_shift: float | None = None
    preserve_pitch: bool = False
    engine: str = "auto"
    reverb: ReverbSettings | None = None

    def to_spec(self) -> FxSpec:
        return FxSpec(
            tempo=self.factor,
            pitch_semitones=self.pitch_shift,
            preserve_pitch=self.preserve_pitch,
            reverb=self.reverb if self.mode in ("reverb", "slowed_reverb") else None,
            engine=self.engine,
        )

    def summary(self) -> str:
        bits = [f"mode={self.mode}", f"factor={self.factor:g}"]
        if self.pitch_shift is not None:
            bits.append(f"pitch={self.pitch_shift:+g}st")
        if self.preserve_pitch:
            bits.append("preserve-pitch")
        if self.engine != "auto":
            bits.append(f"engine={self.engine}")
        if self.reverb is not None:
            if self.reverb.ir_file is not None:
                bits.append(f"reverb=ir:{Path(self.reverb.ir_file).name}")
            else:
                bits.append(f"reverb=mix {self.reverb.mix:g}")
        return "  ".join(bits)


def _as_float_list(value: Any, field: str, preset: str) -> tuple[float, ...]:
    if isinstance(value, (int, float)):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise PresetError(
            f"preset '{preset}': reverb.{field} must be a number or a list of numbers"
        )
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise PresetError(f"preset '{preset}': reverb.{field} must be numeric") from exc


def _as_float(value: Any, field: str, preset: str, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PresetError(f"preset '{preset}': reverb.{field} must be numeric") from exc


def _parse_reverb(data: Any, preset: str, base_dir: Path) -> ReverbSettings:
    if not isinstance(data, Mapping):
        raise PresetError(f"preset '{preset}': reverb must be a mapping")

    unknown = set(data) - _ALLOWED_REVERB_KEYS
    if unknown:
        raise PresetError(
            f"preset '{preset}': unknown reverb key: {', '.join(sorted(unknown))}"
        )

    ir_file = data.get("ir_file")
    ir_path: Path | None = None
    if ir_file:
        ir_path = Path(ir_file)
        if not ir_path.is_absolute():
            # relative paths resolve against the package (assets/ir/...)
            ir_path = (base_dir / ir_path).resolve()
        if not ir_path.is_file():
            raise PresetError(f"preset '{preset}': IR file not found: {ir_path}")

    size = data.get("size", "medium")
    if size not in REVERB_SIZES:
        raise PresetError(
            f"preset '{preset}': reverb.size must be one of {tuple(REVERB_SIZES)}"
        )
    default_delays, default_decays = REVERB_SIZES[size]

    delays = (
        _as_float_list(data["delays"], "delays", preset)
        if data.get("delays") is not None
        else default_delays
    )
    decays = (
        _as_float_list(data["decays"], "decays", preset)
        if data.get("decays") is not None
        else default_decays
    )

    try:
        reverb = ReverbSettings(
            delays=delays,
            decays=decays,
            mix=_as_float(data.get("mix"), "mix", preset, 0.35),
            highpass=_as_float(data.get("highpass"), "highpass", preset, 200.0),
            lowpass=_as_float(data.get("lowpass"), "lowpass", preset, 7000.0),
            ir_file=ir_path,
        )
        reverb.validate()
    except (TypeError, ValueError) as exc:
        raise PresetError(f"preset '{preset}': {exc}") from exc
    return reverb


def parse_preset(name: str, data: Any, base_dir: Path | None = None) -> Preset:
    """Build a Preset out of one YAML entry."""
    base_dir = base_dir or DEFAULT_PRESET_FILE.parent
    if not isinstance(data, Mapping):
        raise PresetError(f"preset '{name}' must be a mapping")

    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        raise PresetError(f"preset '{name}': unknown key: {', '.join(sorted(unknown))}")

    mode = str(data.get("mode", "slowed_reverb"))
    if mode not in MODES:
        raise PresetError(f"preset '{name}': mode must be one of {MODES}")

    engine = str(data.get("engine", "auto"))
    if engine not in ENGINES:
        raise PresetError(f"preset '{name}': engine must be one of {ENGINES}")

    try:
        factor = float(data.get("factor", 1.0))
    except (TypeError, ValueError) as exc:
        raise PresetError(f"preset '{name}': factor must be numeric") from exc
    if factor <= 0:
        raise PresetError(f"preset '{name}': factor must be positive")

    pitch = data.get("pitch_shift")
    if pitch is not None:
        try:
            pitch = float(pitch)
        except (TypeError, ValueError) as exc:
            raise PresetError(f"preset '{name}': pitch_shift must be numeric") from exc

    reverb = None
    if data.get("reverb") is not None:
        reverb = _parse_reverb(data["reverb"], name, base_dir)
    elif mode in ("reverb", "slowed_reverb"):
        reverb = ReverbSettings()

    return Preset(
        name=name,
        description=str(data.get("description", "")),
        mode=mode,
        factor=factor,
        pitch_shift=pitch,
        preserve_pitch=bool(data.get("preserve_pitch", False)),
        engine=engine,
        reverb=reverb,
    )


def load_presets(path: Path | str | None = None) -> dict[str, Preset]:
    """Read presets.yaml (or a user supplied file) into Preset objects."""
    path = Path(path) if path else DEFAULT_PRESET_FILE
    if not path.is_file():
        raise PresetError(f"Preset file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PresetError(f"Could not parse preset file ({path}): {exc}") from exc

    if not isinstance(raw, Mapping):
        raise PresetError(f"Preset file must be a mapping: {path}")

    entries = raw.get("presets", raw)
    if not isinstance(entries, Mapping):
        raise PresetError(f"The 'presets' key must be a mapping: {path}")

    base_dir = path.parent
    return {
        str(name): parse_preset(str(name), data, base_dir) for name, data in entries.items()
    }


def get_preset(name: str, path: Path | str | None = None) -> Preset:
    presets = load_presets(path)
    try:
        return presets[name]
    except KeyError:
        available = ", ".join(sorted(presets)) or "(none)"
        raise PresetError(
            f"There is no preset named '{name}'. Available presets: {available}"
        ) from None
