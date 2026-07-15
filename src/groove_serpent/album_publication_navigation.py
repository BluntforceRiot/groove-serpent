"""Exact album navigation artifacts for one verified publication tree.

The JSON artifact is authoritative at integer-sample precision.  Its CUE
companion is deliberately labelled approximate because CUE indexes use a
75-frames-per-second timebase.  Both describe already planned audio files;
neither grants approval or changes audio.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from .album import AlbumProject
from .errors import ExportError
from .models import Project


ALBUM_PUBLICATION_CHAPTERS_SCHEMA = "groove-serpent.album-publication-chapters/1"
ALBUM_PUBLICATION_CHAPTERS_NAME = "album.chapters.json"
ALBUM_PUBLICATION_CUE_NAME = "album.cue"

_BASIS_PROFILES = frozenset({"archival-source", "restored-side", "corrected-lossless"})
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_SIDES = 64
_MAX_TRACKS = 99
_MAX_TEXT = 512
_WINDOWS_DEVICE_STEMS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def _bounded_text(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_TEXT
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ExportError(f"{label} must be bounded, trimmed printable text.")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ExportError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _positive_integer(value: int, label: str, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ExportError(f"{label} is outside its supported range.")
    return value


def _sample(value: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
        raise ExportError(f"{label} must be a bounded non-negative sample coordinate.")
    return value


def _portable_relative_path(value: str, label: str) -> str:
    text = _bounded_text(value, label)
    if (
        "\\" in text
        or text.startswith("/")
        or "//" in text
        or ":" in text
        or any(character in '<>"|?*' for character in text)
        or unicodedata.normalize("NFC", text) != text
    ):
        raise ExportError(f"{label} must be one portable relative path.")
    parts = text.split("/")
    if any(
        part in {"", ".", ".."}
        or len(part) > 255
        or part.endswith((" ", "."))
        or part.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_STEMS
        for part in parts
    ):
        raise ExportError(f"{label} must remain inside the publication directory.")
    if PurePosixPath(text).as_posix() != text:
        raise ExportError(f"{label} is not a canonical relative path.")
    return text


@dataclass(frozen=True, slots=True)
class NavigationTrack:
    """One track's exact source, side-timeline, and referenced-file geometry."""

    album_track_number: int
    local_track_number: int
    title: str
    artist: str
    file_path: str
    file_sha256: str
    file_sample_count: int
    source_start_sample: int
    source_end_sample: int
    side_output_start_sample: int
    side_output_end_sample: int
    file_output_start_sample: int
    file_output_end_sample: int

    def validate(self) -> None:
        _positive_integer(
            self.album_track_number,
            "Album track number",
            maximum=_MAX_TRACKS,
        )
        _positive_integer(
            self.local_track_number,
            "Local track number",
            maximum=_MAX_TRACKS,
        )
        _bounded_text(self.title, "Track title")
        _bounded_text(self.artist, "Track artist")
        _portable_relative_path(self.file_path, "Navigation audio path")
        _digest(self.file_sha256, "Navigation audio SHA-256")
        _positive_integer(
            self.file_sample_count,
            "Navigation audio sample count",
            maximum=(1 << 63) - 1,
        )
        coordinates = (
            (self.source_start_sample, "Source track start"),
            (self.source_end_sample, "Source track end"),
            (self.side_output_start_sample, "Side output start"),
            (self.side_output_end_sample, "Side output end"),
            (self.file_output_start_sample, "File output start"),
            (self.file_output_end_sample, "File output end"),
        )
        for value, label in coordinates:
            _sample(value, label)
        if self.source_end_sample <= self.source_start_sample:
            raise ExportError("Navigation source track range must be non-empty.")
        if self.side_output_end_sample <= self.side_output_start_sample:
            raise ExportError("Navigation side output range must be non-empty.")
        if self.file_output_end_sample <= self.file_output_start_sample:
            raise ExportError("Navigation file output range must be non-empty.")
        if self.file_output_end_sample > self.file_sample_count:
            raise ExportError("Navigation track extends past its referenced audio file.")
        if (
            self.side_output_end_sample - self.side_output_start_sample
            != self.file_output_end_sample - self.file_output_start_sample
        ):
            raise ExportError("Navigation side and file ranges have different lengths.")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "album_track_number": self.album_track_number,
            "local_track_number": self.local_track_number,
            "title": self.title,
            "artist": self.artist,
            "file": {
                "path": self.file_path,
                "sha256": self.file_sha256,
                "sample_count": self.file_sample_count,
                "start_sample": self.file_output_start_sample,
                "end_sample_exclusive": self.file_output_end_sample,
            },
            "source_start_sample": self.source_start_sample,
            "source_end_sample_exclusive": self.source_end_sample,
            "side_output_start_sample": self.side_output_start_sample,
            "side_output_end_sample_exclusive": self.side_output_end_sample,
        }


@dataclass(frozen=True, slots=True)
class NavigationSide:
    """One ordered side in the exact navigation timeline."""

    order: int
    label: str
    source_sample_rate: int
    output_sample_rate: int
    timeline_origin: str
    tracks: tuple[NavigationTrack, ...]

    def validate(self) -> None:
        _positive_integer(self.order, "Navigation side order", maximum=_MAX_SIDES)
        _bounded_text(self.label, "Navigation side label")
        _positive_integer(
            self.source_sample_rate,
            "Navigation source sample rate",
            maximum=768_000,
        )
        _positive_integer(
            self.output_sample_rate,
            "Navigation output sample rate",
            maximum=768_000,
        )
        if self.timeline_origin not in {
            "full-capture",
            "project-music-range",
            "corrected-music-range",
        }:
            raise ExportError("Navigation timeline origin is unsupported.")
        if not self.tracks or len(self.tracks) > _MAX_TRACKS:
            raise ExportError("Navigation side has an unsupported track count.")
        previous_end: int | None = None
        for expected_local, track in enumerate(self.tracks, start=1):
            track.validate()
            if track.local_track_number != expected_local:
                raise ExportError("Navigation local track numbers must be contiguous.")
            if previous_end is not None and track.side_output_start_sample != previous_end:
                raise ExportError("Navigation tracks must be adjacent on the side timeline.")
            previous_end = track.side_output_end_sample

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "order": self.order,
            "label": self.label,
            "timeline_origin": self.timeline_origin,
            "source_sample_rate": self.source_sample_rate,
            "output_sample_rate": self.output_sample_rate,
            "output_start_sample": self.tracks[0].side_output_start_sample,
            "output_end_sample_exclusive": self.tracks[-1].side_output_end_sample,
            "tracks": [track.to_dict() for track in self.tracks],
        }


def _validated_sides(values: Iterable[NavigationSide]) -> tuple[NavigationSide, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ExportError("Navigation sides must be a bounded collection.")
    sides = tuple(values)
    if not sides or len(sides) > _MAX_SIDES:
        raise ExportError("Navigation has an unsupported side count.")
    if any(not isinstance(side, NavigationSide) for side in sides):
        raise ExportError("Navigation sides must use the NavigationSide model.")
    ordered = tuple(sorted(sides, key=lambda side: side.order))
    if [side.order for side in ordered] != list(range(1, len(ordered) + 1)):
        raise ExportError("Navigation side orders must be contiguous.")
    labels = [unicodedata.normalize("NFC", side.label).casefold() for side in ordered]
    if len(labels) != len(set(labels)):
        raise ExportError("Navigation side labels must be unique.")
    expected_album_number = 1
    file_identities: dict[str, tuple[str, str]] = {}
    for side in ordered:
        side.validate()
        for track in side.tracks:
            if track.album_track_number != expected_album_number:
                raise ExportError("Navigation album track numbers must be contiguous.")
            expected_album_number += 1
            key = unicodedata.normalize("NFC", track.file_path).casefold()
            identity = (track.file_path, track.file_sha256)
            previous = file_identities.setdefault(key, identity)
            if previous != identity:
                raise ExportError("Navigation audio paths collide under portable Unicode matching.")
    if expected_album_number - 1 > _MAX_TRACKS:
        raise ExportError("CUE navigation is limited to 99 tracks; split the release into volumes.")
    return ordered


def build_album_chapters(
    *,
    plan_sha256: str,
    album_sha256: str,
    basis_profile: str,
    metadata: dict[str, str],
    sides: Iterable[NavigationSide],
) -> dict[str, Any]:
    """Build the exact sample-coordinate navigation payload."""

    _digest(plan_sha256, "Publication plan SHA-256")
    _digest(album_sha256, "Album project SHA-256")
    if basis_profile not in _BASIS_PROFILES:
        raise ExportError("Navigation basis profile is unsupported.")
    if not isinstance(metadata, dict) or len(metadata) > 64:
        raise ExportError("Navigation album metadata must be a bounded object.")
    normalized_metadata: dict[str, str] = {}
    for key, value in sorted(metadata.items()):
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ExportError("Navigation metadata contains an invalid field name.")
        if not isinstance(value, str) or len(value) > _MAX_TEXT or "\x00" in value:
            raise ExportError("Navigation metadata contains an invalid field value.")
        normalized_metadata[key] = value
    ordered = _validated_sides(sides)
    return {
        "schema": ALBUM_PUBLICATION_CHAPTERS_SCHEMA,
        "plan_sha256": plan_sha256,
        "album_project_sha256": album_sha256,
        "basis_profile": basis_profile,
        "metadata": normalized_metadata,
        "precision": "exact integer sample positions",
        "cue_companion": {
            "path": ALBUM_PUBLICATION_CUE_NAME,
            "timebase_frames_per_second": 75,
            "precision": "approximate rounded navigation indexes",
        },
        "total_tracks": sum(len(side.tracks) for side in ordered),
        "sides": [side.to_dict() for side in ordered],
    }


def _inventory_integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= (1 << 63) - 1:
        raise ExportError(f"{label} is not one bounded integer.")
    return value


def _inventory_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ExportError(f"{label} must be text.")
    return value


def _inventory_verification(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("verification")
    if not isinstance(value, dict):
        raise ExportError("Navigation audio inventory has no verification object.")
    return value


def _inventory_index(
    inventory: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, int | None], Mapping[str, Any]]:
    index: dict[tuple[str, str, int | None], Mapping[str, Any]] = {}
    count = 0
    for item in inventory:
        count += 1
        if count > 100_000 or not isinstance(item, Mapping):
            raise ExportError("Publication inventory is not a bounded object collection.")
        profile = item.get("profile")
        side_label = item.get("side_label")
        source_object_id = item.get("source_object_id")
        local_number = item.get("local_track_number")
        if profile not in _BASIS_PROFILES:
            continue
        inventory_identity = (
            source_object_id
            if profile == "archival-source" and isinstance(source_object_id, str)
            else side_label
        )
        if (
            not isinstance(profile, str)
            or not isinstance(inventory_identity, str)
            or (local_number is not None and type(local_number) is not int)
        ):
            continue
        key = (profile, inventory_identity, local_number)
        if key in index:
            raise ExportError("Publication inventory repeats a navigation audio identity.")
        index[key] = item
    return index


def _item_file_identity(item: Mapping[str, Any]) -> tuple[str, str]:
    path = _portable_relative_path(
        _inventory_text(item.get("path"), "Navigation inventory path"),
        "Navigation inventory path",
    )
    sha256 = _digest(
        _inventory_text(item.get("sha256"), "Navigation inventory SHA-256"),
        "Navigation inventory SHA-256",
    )
    return path, sha256


def navigation_sides_from_publication(
    *,
    album: AlbumProject,
    projects_by_label: Mapping[str, Project],
    selected_profiles: Iterable[str],
    inventory: Iterable[Mapping[str, Any]],
    archival_source_bindings: Mapping[str, str] | None = None,
) -> tuple[str, tuple[NavigationSide, ...]]:
    """Bind exact navigation geometry to an already verified audio inventory."""

    album.validate()
    selected = set(selected_profiles)
    if "corrected-lossless" in selected:
        basis = "corrected-lossless"
        origin = "corrected-music-range"
    elif "restored-side" in selected:
        basis = "restored-side"
        origin = "project-music-range"
    elif "archival-source" in selected:
        basis = "archival-source"
        origin = "full-capture"
    else:
        raise ExportError("Publication has no audio profile suitable for navigation.")
    if basis == "archival-source" and archival_source_bindings is not None:
        expected_labels = {side.label for side in album.sides}
        if set(archival_source_bindings) != expected_labels or any(
            not isinstance(object_id, str) or not object_id
            for object_id in archival_source_bindings.values()
        ):
            raise ExportError("Archival navigation side bindings are incomplete or invalid.")
    indexed = _inventory_index(inventory)
    sides: list[NavigationSide] = []
    album_number = 0
    for album_side in sorted(album.sides, key=lambda side: side.order):
        project = projects_by_label.get(album_side.label)
        if not isinstance(project, Project):
            raise ExportError(
                f"Navigation is missing the verified Side {album_side.label} project."
            )
        project.validate()
        music_start = project.tracks[0].start_sample
        shared_artist = (
            album.metadata.get("artist")
            or album.metadata.get("album_artist")
            or project.metadata.get("artist")
            or "Unknown Artist"
        )
        tracks: list[NavigationTrack] = []
        if basis == "corrected-lossless":
            for local_number, source_track in enumerate(project.tracks, start=1):
                album_number += 1
                item = indexed.get((basis, album_side.label, local_number))
                if item is None:
                    raise ExportError("Corrected navigation track is missing from inventory.")
                if item.get("role") != "corrected-track":
                    raise ExportError("Corrected navigation inventory has an invalid role.")
                if (
                    _inventory_integer(
                        item.get("album_track_number"),
                        "Navigation album track number",
                        minimum=1,
                    )
                    != album_number
                ):
                    raise ExportError("Corrected navigation album numbering is inconsistent.")
                side_start = _inventory_integer(
                    item.get("corrected_start_sample"),
                    "Corrected navigation start",
                )
                side_end = _inventory_integer(
                    item.get("corrected_end_sample"),
                    "Corrected navigation end",
                    minimum=1,
                )
                if (
                    _inventory_integer(
                        item.get("source_start_sample"),
                        "Corrected navigation source start",
                    )
                    != source_track.start_sample
                    or _inventory_integer(
                        item.get("source_end_sample"),
                        "Corrected navigation source end",
                        minimum=1,
                    )
                    != source_track.end_sample
                ):
                    raise ExportError(
                        "Corrected navigation source geometry differs from the project."
                    )
                verification = _inventory_verification(item)
                file_count = _inventory_integer(
                    verification.get("exact_sample_count"),
                    "Corrected navigation file length",
                    minimum=1,
                )
                path, sha256 = _item_file_identity(item)
                tracks.append(
                    NavigationTrack(
                        album_number,
                        local_number,
                        source_track.title,
                        album.metadata.get("artist") or source_track.artist or shared_artist,
                        path,
                        sha256,
                        file_count,
                        source_track.start_sample,
                        source_track.end_sample,
                        side_start,
                        side_end,
                        0,
                        file_count,
                    )
                )
        else:
            inventory_identity = album_side.label
            if basis == "archival-source" and archival_source_bindings is not None:
                inventory_identity = archival_source_bindings[album_side.label]
            item = indexed.get((basis, inventory_identity, None))
            if item is None:
                raise ExportError("Continuous navigation side is missing from inventory.")
            expected_role = (
                "music-range-side" if basis == "restored-side" else "full-capture-source"
            )
            if item.get("role") != expected_role:
                raise ExportError("Continuous navigation inventory has an invalid role.")
            path, sha256 = _item_file_identity(item)
            if basis == "restored-side":
                verification = _inventory_verification(item)
                file_count = _inventory_integer(
                    verification.get("exact_sample_count"),
                    "Restored navigation file length",
                    minimum=1,
                )
                expected_music_count = project.tracks[-1].end_sample - music_start
                if file_count != expected_music_count:
                    raise ExportError(
                        "Restored navigation file does not end at the reviewed music end."
                    )
                coordinate_offset = music_start
            else:
                source_sample_count = project.source.sample_count
                if type(source_sample_count) is not int or source_sample_count < 1:
                    raise ExportError(
                        "Archival navigation requires an exact positive source sample count."
                    )
                file_count = source_sample_count
                coordinate_offset = 0
            for local_number, source_track in enumerate(project.tracks, start=1):
                album_number += 1
                file_start = source_track.start_sample - coordinate_offset
                file_end = source_track.end_sample - coordinate_offset
                tracks.append(
                    NavigationTrack(
                        album_number,
                        local_number,
                        source_track.title,
                        album.metadata.get("artist") or source_track.artist or shared_artist,
                        path,
                        sha256,
                        file_count,
                        source_track.start_sample,
                        source_track.end_sample,
                        file_start,
                        file_end,
                        file_start,
                        file_end,
                    )
                )
        sides.append(
            NavigationSide(
                album_side.order,
                album_side.label,
                project.source.sample_rate,
                project.source.sample_rate,
                origin,
                tuple(tracks),
            )
        )
    return basis, _validated_sides(sides)


def _cue_quote(value: str) -> str:
    printable = " ".join(str(value).replace("\x00", " ").splitlines())
    printable = " ".join(printable.split())
    return f'"{printable.replace(chr(34), chr(39) * 2)}"'


def _cue_time(sample: int, sample_rate: int) -> str:
    frames = (sample * 75 + sample_rate // 2) // sample_rate
    minutes, remainder = divmod(frames, 75 * 60)
    seconds, cue_frames = divmod(remainder, 75)
    return f"{minutes:02d}:{seconds:02d}:{cue_frames:02d}"


def render_album_cue(
    *,
    metadata: dict[str, str],
    sides: Iterable[NavigationSide],
) -> str:
    """Render an approximate CUE companion, including multi-file track batches."""

    ordered = _validated_sides(sides)
    album_artist = metadata.get("album_artist") or metadata.get("artist", "")
    album_title = metadata.get("album") or metadata.get("title", "")
    lines = [
        'REM GENERATED_BY "Groove Serpent"',
        'REM INDEX_PRECISION "75 fps approximate; album.chapters.json is exact"',
        f"PERFORMER {_cue_quote(album_artist)}",
        f"TITLE {_cue_quote(album_title)}",
    ]
    current_file: tuple[str, str] | None = None
    for side in ordered:
        lines.append(f"REM SIDE {_cue_quote(side.label)}")
        for track in side.tracks:
            file_identity = (track.file_path, track.file_sha256)
            if file_identity != current_file:
                lines.append(f"FILE {_cue_quote(track.file_path)} WAVE")
                current_file = file_identity
            lines.extend(
                [
                    f"  TRACK {track.album_track_number:02d} AUDIO",
                    f"    TITLE {_cue_quote(track.title)}",
                    f"    PERFORMER {_cue_quote(track.artist)}",
                    (
                        "    INDEX 01 "
                        f"{_cue_time(track.file_output_start_sample, side.output_sample_rate)}"
                    ),
                ]
            )
    return "\n".join(lines) + "\n"


__all__ = [
    "ALBUM_PUBLICATION_CHAPTERS_NAME",
    "ALBUM_PUBLICATION_CHAPTERS_SCHEMA",
    "ALBUM_PUBLICATION_CUE_NAME",
    "NavigationSide",
    "NavigationTrack",
    "build_album_chapters",
    "navigation_sides_from_publication",
    "render_album_cue",
]
