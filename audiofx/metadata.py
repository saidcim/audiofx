"""Tag/cover art copying with mutagen.

ffmpeg's `-map_metadata` already carries most tags across, but it drops or
mangles them when the container changes (mp3 -> flac, cover art, unusual
frames). This module re-applies the common fields plus the cover picture and
never raises: metadata is a nice-to-have, a failure here must not kill the
conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

COMMON_KEYS = (
    "title",
    "artist",
    "albumartist",
    "album",
    "date",
    "genre",
    "tracknumber",
    "discnumber",
    "composer",
    "organization",
)


@dataclass
class Cover:
    data: bytes
    mime: str = "image/jpeg"
    description: str = "cover"


def _load_mutagen():
    try:
        import mutagen  # noqa: F401
    except ImportError:  # pragma: no cover - depends on environment
        return None
    return mutagen


def read_cover(path: Path | str) -> Cover | None:
    """Best effort extraction of embedded cover art."""
    mutagen = _load_mutagen()
    if mutagen is None:
        return None

    try:
        audio = mutagen.File(str(path))
    except Exception:
        return None
    if audio is None:
        return None

    tags = getattr(audio, "tags", None)

    # ID3 (mp3, wav, aiff)
    try:
        if tags is not None and hasattr(tags, "getall"):
            frames = tags.getall("APIC")
            if frames:
                frame = frames[0]
                return Cover(frame.data, getattr(frame, "mime", "image/jpeg") or "image/jpeg")
    except Exception:
        pass

    # FLAC
    pictures = getattr(audio, "pictures", None)
    if pictures:
        picture = pictures[0]
        return Cover(picture.data, picture.mime or "image/jpeg")

    # MP4 / M4A
    try:
        covers = tags["covr"] if tags is not None and "covr" in tags else None
        if covers:
            cover = covers[0]
            fmt = getattr(cover, "imageformat", None)
            mime = "image/png" if fmt == 14 else "image/jpeg"
            return Cover(bytes(cover), mime)
    except Exception:
        pass

    return None


def _write_cover(audio, cover: Cover, suffix: str) -> str | None:
    """Attach `cover` to an already opened mutagen file. Returns a warning."""
    mutagen = _load_mutagen()
    if mutagen is None:
        return "mutagen is not installed"

    try:
        if suffix in (".mp3", ".wav", ".aiff", ".aif"):
            from mutagen.id3 import APIC, ID3

            tags = audio.tags
            if tags is None:
                audio.add_tags()
                tags = audio.tags
            if not isinstance(tags, ID3) and not hasattr(tags, "add"):
                return "cover art could not be attached to this file"
            tags.delall("APIC")
            tags.add(
                APIC(
                    encoding=3,
                    mime=cover.mime,
                    type=3,  # front cover
                    desc=cover.description,
                    data=cover.data,
                )
            )
        elif suffix == ".flac":
            from mutagen.flac import Picture

            picture = Picture()
            picture.data = cover.data
            picture.type = 3
            picture.mime = cover.mime
            audio.clear_pictures()
            audio.add_picture(picture)
        elif suffix in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4Cover

            fmt = MP4Cover.FORMAT_PNG if cover.mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(cover.data, imageformat=fmt)]
        else:
            return f"cover art is not supported for {suffix}"
    except Exception as exc:
        return f"could not write cover art: {exc}"
    return None


def copy_metadata(
    source: Path | str, target: Path | str, *, include_cover: bool = True
) -> list[str]:
    """Copy common tags (and optionally the cover) from source to target.

    Returns a list of human readable warnings; an empty list means success.
    """
    mutagen = _load_mutagen()
    if mutagen is None:
        return ["mutagen is not installed, tags were not copied"]

    source = Path(source)
    target = Path(target)
    warnings: list[str] = []

    try:
        src = mutagen.File(str(source), easy=True)
    except Exception as exc:
        return [f"could not read source tags: {exc}"]
    if src is None:
        return [f"reading tags is not supported for {source.suffix}"]

    src_tags = dict(src.tags or {})
    cover = read_cover(source) if include_cover else None

    if not src_tags and cover is None:
        return []

    try:
        dst = mutagen.File(str(target), easy=True)
    except Exception as exc:
        return [f"could not open target file: {exc}"]
    if dst is None:
        return [f"writing tags is not supported for {target.suffix}"]

    if dst.tags is None:
        try:
            dst.add_tags()
        except Exception as exc:
            return [f"could not add tags to the target file: {exc}"]

    for key in COMMON_KEYS:
        if key not in src_tags:
            continue
        try:
            dst[key] = src_tags[key]
        except Exception:
            warnings.append(f"could not copy the '{key}' tag")

    try:
        dst.save()
    except Exception as exc:
        return warnings + [f"could not save tags: {exc}"]

    if cover is not None:
        try:
            raw = mutagen.File(str(target))
        except Exception as exc:
            warnings.append(f"could not open the file for cover art: {exc}")
            return warnings
        if raw is None:
            warnings.append("could not open the file for cover art")
            return warnings
        problem = _write_cover(raw, cover, target.suffix.lower())
        if problem:
            warnings.append(problem)
        else:
            try:
                raw.save()
            except Exception as exc:
                warnings.append(f"could not save cover art: {exc}")

    return warnings
