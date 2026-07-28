"""Preset loading and validation.

A preset is a named bundle of default values (mode, factor, reverb settings).
CLI flags always win over whatever the preset supplies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .ffmpeg_runner import ENGINES, REVERB_ROOMS, FxSpec, ReverbSettings

MODES = ("slow", "speed", "reverb", "slowed_reverb")

DEFAULT_PRESET_FILE = Path(__file__).with_name("presets.yaml")
# Presets saved from the interface live next to the other user settings, not
# inside the package - that one gets replaced on every reinstall.
USER_PRESET_FILE = Path.home() / ".audiofx-presets.yaml"
ASSETS_DIR = Path(__file__).parent / "assets"

_ALLOWED_KEYS = {
    "description",
    "mode",
    "factor",
    "pitch_shift",
    "preserve_pitch",
    "engine",
    "bass",
    "treble",
    "stereo_width",
    "normalize",
    "reverb",
}
_ALLOWED_REVERB_KEYS = {
    "room",
    "decay",
    "predelay",
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
    bass: float = 0.0
    treble: float = 0.0
    stereo_width: float = 1.0
    normalize: bool = False
    reverb: ReverbSettings | None = None

    def to_spec(self) -> FxSpec:
        return FxSpec(
            tempo=self.factor,
            pitch_semitones=self.pitch_shift,
            preserve_pitch=self.preserve_pitch,
            reverb=self.reverb if self.mode in ("reverb", "slowed_reverb") else None,
            engine=self.engine,
            bass_gain=self.bass,
            treble_gain=self.treble,
            stereo_width=self.stereo_width,
            normalize=self.normalize,
        )

    def summary(self) -> str:
        bits = [f"mode={self.mode}", f"factor={self.factor:g}"]
        if self.pitch_shift is not None:
            bits.append(f"pitch={self.pitch_shift:+g}st")
        if self.preserve_pitch:
            bits.append("preserve-pitch")
        if self.engine != "auto":
            bits.append(f"engine={self.engine}")
        if self.bass:
            bits.append(f"bass={self.bass:+g}dB")
        if self.treble:
            bits.append(f"treble={self.treble:+g}dB")
        if self.stereo_width != 1.0:
            bits.append(f"width={self.stereo_width:g}")
        if self.normalize:
            bits.append("normalize")
        if self.reverb is not None:
            if self.reverb.ir_file is not None:
                bits.append(f"reverb=ir:{Path(self.reverb.ir_file).name}")
            else:
                bits.append(f"reverb={self.reverb.decay:g}s mix {self.reverb.mix:g}")
        return "  ".join(bits)


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

    room = data.get("room", "medium")
    if room not in REVERB_ROOMS:
        raise PresetError(
            f"preset '{preset}': reverb.room must be one of {tuple(REVERB_ROOMS)}"
        )

    try:
        reverb = ReverbSettings(
            decay=_as_float(data.get("decay"), "decay", preset, REVERB_ROOMS[room]),
            predelay=_as_float(data.get("predelay"), "predelay", preset, 0.0),
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

    preset = Preset(
        name=name,
        description=str(data.get("description", "")),
        mode=mode,
        factor=factor,
        pitch_shift=pitch,
        preserve_pitch=bool(data.get("preserve_pitch", False)),
        engine=engine,
        bass=_as_float(data.get("bass"), "bass", name, 0.0),
        treble=_as_float(data.get("treble"), "treble", name, 0.0),
        stereo_width=_as_float(data.get("stereo_width"), "stereo_width", name, 1.0),
        normalize=bool(data.get("normalize", False)),
        reverb=reverb,
    )
    try:
        preset.to_spec().validate()
    except ValueError as exc:
        raise PresetError(f"preset '{name}': {exc}") from exc
    return preset


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


# --------------------------------------------------------------------------
# presets the user saves from the interface
# --------------------------------------------------------------------------


def load_user_presets(path: Path | str | None = None) -> dict[str, Preset]:
    """Read the user's own presets. A missing file just means "none yet"."""
    path = Path(path) if path else USER_PRESET_FILE
    if not path.is_file():
        return {}
    return load_presets(path)


def combined_presets(user_file: Path | str | None = None) -> dict[str, Preset]:
    """The shipped presets plus the user's; theirs win on a name clash."""
    presets = load_presets()
    try:
        presets.update(load_user_presets(user_file))
    except PresetError:
        # a hand-edited user file must not take the whole program down
        pass
    return presets


def preset_to_dict(preset: Preset) -> dict[str, Any]:
    """The YAML form of a preset, leaving out everything left at its default."""
    data: dict[str, Any] = {"mode": preset.mode}
    if preset.description:
        data["description"] = preset.description
    if preset.mode != "reverb":
        data["factor"] = round(preset.factor, 3)
    if preset.pitch_shift:
        data["pitch_shift"] = round(preset.pitch_shift, 2)
    if preset.preserve_pitch:
        data["preserve_pitch"] = True
    if preset.engine != "auto":
        data["engine"] = preset.engine
    if preset.bass:
        data["bass"] = round(preset.bass, 2)
    if preset.treble:
        data["treble"] = round(preset.treble, 2)
    if preset.stereo_width != 1.0:
        data["stereo_width"] = round(preset.stereo_width, 2)
    if preset.normalize:
        data["normalize"] = True
    if preset.reverb is not None and preset.mode in ("reverb", "slowed_reverb"):
        reverb: dict[str, Any] = {
            "decay": round(preset.reverb.decay, 2),
            "mix": round(preset.reverb.mix, 3),
            "highpass": round(preset.reverb.highpass, 1),
            "lowpass": round(preset.reverb.lowpass, 1),
        }
        if preset.reverb.predelay:
            reverb["predelay"] = round(preset.reverb.predelay, 1)
        if preset.reverb.ir_file is not None:
            reverb["ir_file"] = str(preset.reverb.ir_file)
        data["reverb"] = reverb
    return data


def valid_preset_name(name: str) -> str:
    """Normalise a name typed by the user, or explain why it cannot be used."""
    cleaned = name.strip()
    if not cleaned:
        raise PresetError("Give the preset a name.")
    if len(cleaned) > 40:
        raise PresetError("Preset names are limited to 40 characters.")
    if any(character in cleaned for character in ":#{}[]\n\r\t"):
        raise PresetError("Preset names cannot contain : # { } [ ] or line breaks.")
    if cleaned in load_presets():
        raise PresetError(f"'{cleaned}' is a built-in preset; pick another name.")
    return cleaned


def save_user_preset(preset: Preset, path: Path | str | None = None) -> Path:
    """Add or replace one preset in the user's file."""
    path = Path(path) if path else USER_PRESET_FILE
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, Mapping):
                existing = dict(loaded.get("presets", loaded))
        except yaml.YAMLError as exc:
            raise PresetError(f"Could not read {path}: {exc}") from exc

    existing[preset.name] = preset_to_dict(preset)
    path.write_text(
        yaml.safe_dump({"presets": existing}, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def delete_user_preset(name: str, path: Path | str | None = None) -> bool:
    """Remove one preset; returns False when it was not there."""
    path = Path(path) if path else USER_PRESET_FILE
    if not path.is_file():
        return False
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, Mapping):
        return False
    entries = dict(loaded.get("presets", loaded))
    if name not in entries:
        return False
    del entries[name]
    path.write_text(
        yaml.safe_dump({"presets": entries}, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    return True


def get_preset(name: str, path: Path | str | None = None) -> Preset:
    presets = combined_presets() if path is None else load_presets(path)
    try:
        return presets[name]
    except KeyError:
        available = ", ".join(sorted(presets)) or "(none)"
        raise PresetError(
            f"There is no preset named '{name}'. Available presets: {available}"
        ) from None
