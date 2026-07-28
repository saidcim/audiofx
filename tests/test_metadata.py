from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from audiofx import ffmpeg_runner as fr
from audiofx.metadata import copy_metadata, read_cover
from conftest import ffmpeg_required

mutagen = pytest.importorskip("mutagen")

COVER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8cfc00000030101003c3b3d2d0000000049454e44ae426082"
)


def _encode(source: Path, target: Path, **metadata: str) -> Path:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    for key, value in metadata.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(target))
    subprocess.run(cmd, check=True, capture_output=True)
    return target


@pytest.fixture()
def tagged_mp3(sample_wav: Path, tmp_path: Path) -> Path:
    path = _encode(
        sample_wav,
        tmp_path / "tagged.mp3",
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
    )
    from mutagen.id3 import APIC, ID3

    tags = ID3(path)
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="cover", data=COVER_PNG))
    tags.save()
    return path


@ffmpeg_required
def test_read_cover_finds_embedded_picture(tagged_mp3: Path):
    cover = read_cover(tagged_mp3)
    assert cover is not None
    assert cover.mime == "image/png"
    assert cover.data == COVER_PNG


@ffmpeg_required
def test_copy_metadata_same_format(tagged_mp3: Path, tmp_path: Path):
    out = tmp_path / "slowed.mp3"
    fr.convert_file(tagged_mp3, out, fr.FxSpec(tempo=0.85))

    assert copy_metadata(tagged_mp3, out) == []

    result = mutagen.File(out, easy=True)
    assert result["title"] == ["Test Track"]
    assert result["artist"] == ["Test Artist"]
    cover = read_cover(out)
    assert cover is not None and cover.data == COVER_PNG


@ffmpeg_required
def test_copy_metadata_across_containers(tagged_mp3: Path, tmp_path: Path):
    out = tmp_path / "slowed.flac"
    fr.convert_file(tagged_mp3, out, fr.FxSpec(tempo=0.85))

    assert copy_metadata(tagged_mp3, out) == []

    result = mutagen.File(out)
    assert result["title"] == ["Test Track"]
    assert result.pictures and result.pictures[0].data == COVER_PNG


@ffmpeg_required
def test_copy_metadata_without_tags_is_noop(sample_wav: Path, tmp_path: Path):
    out = tmp_path / "plain.wav"
    fr.convert_file(sample_wav, out, fr.FxSpec(tempo=0.9))
    assert copy_metadata(sample_wav, out) == []


def test_copy_metadata_never_raises_on_garbage(tmp_path: Path):
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"
    src.write_text("not audio", encoding="utf-8")
    dst.write_text("not audio either", encoding="utf-8")
    warnings = copy_metadata(src, dst)
    assert warnings and isinstance(warnings[0], str)
