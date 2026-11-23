from __future__ import annotations

import json
import math
import os
import re
import stat
from io import StringIO
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import GrooveSerpentError
from .models import MAX_METADATA_VALUE_LENGTH, MAX_TRACKS, MAX_TRACK_TEXT_LENGTH
from .validation import strict_finite_number

_DURATION_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d+))?$")
_LEADING_NUMBER_RE = re.compile(r"^\s*(?:[A-Za-z]?\d+[.)-]?|\d+)\s+")
MAX_TRACKLIST_BYTES = 8 * 1024 * 1024
MAX_TRACKLIST_LINE_BYTES = 16 * 1024
_METADATA_KEYS = {"artist", "album", "album_artist", "year", "genre", "side"}


@dataclass(slots=True)
class TrackSeed:
    title: str
    duration_seconds: float | None = None
    artist: str = ""
    side: str = ""


@dataclass(slots=True)
class Tracklist:
    tracks: list[TrackSeed]
    metadata: dict[str, str]


def parse_duration(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if type(value) in (int, float):
        try:
            result = strict_finite_number(value, "Track duration")
        except GrooveSerpentError as exc:
            raise GrooveSerpentError("Track duration must be finite.") from exc
        return result if result > 0 else None
    text = str(value).strip()
    try:
        numeric = float(text)
        if not math.isfinite(numeric):
            raise GrooveSerpentError("Track duration must be finite.")
        return numeric if numeric > 0 else None
    except ValueError:
        pass
    match = _DURATION_RE.match(text)
    if not match:
        raise GrooveSerpentError(f"Invalid duration '{text}'. Use seconds or M:SS / H:MM:SS.")
    hours_or_minutes, minutes_or_seconds, seconds, fraction = match.groups()
    second_value = int(seconds)
    if second_value >= 60:
        raise GrooveSerpentError(
            f"Invalid duration '{text}'. Seconds must be between 00 and 59."
        )
    if hours_or_minutes is None:
        minutes = int(minutes_or_seconds)
        hours = 0
    else:
        hours = int(hours_or_minutes)
        minutes = int(minutes_or_seconds)
        if minutes >= 60:
            raise GrooveSerpentError(
                f"Invalid duration '{text}'. Minutes must be between 00 and 59."
            )
    result = hours * 3600 + minutes * 60 + second_value
    if fraction:
        result += float(f"0.{fraction}")
    return float(result)


def _bounded_field(value: Any, label: str, *, limit: int = MAX_TRACK_TEXT_LENGTH) -> str:
    if isinstance(value, (dict, list)):
        raise GrooveSerpentError(f"{label} must be text or a number.")
    text = str(value).strip()
    if len(text) > limit:
        raise GrooveSerpentError(f"{label} exceeds {limit} characters.")
    return text


def _seed_from_mapping(item: dict[str, Any], index: int) -> TrackSeed:
    title = _bounded_field(
        item.get("title") or item.get("name") or f"Track {index:02d}",
        "Track title",
    )
    duration_value = item.get("duration") or item.get("length")
    duration_text = (
        _bounded_field(duration_value, "Track duration")
        if duration_value is not None
        else None
    )
    return TrackSeed(
        title=title,
        duration_seconds=parse_duration(duration_text),
        artist=_bounded_field(item.get("artist", ""), "Track artist"),
        side=_bounded_field(item.get("side", ""), "Track side"),
    )


def _json_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _json_tracks(text: str, index: int, decoder: json.JSONDecoder) -> tuple[list[TrackSeed], int]:
    if index >= len(text) or text[index] != "[":
        raise GrooveSerpentError("The 'tracks' field in a JSON track list must be an array.")
    tracks: list[TrackSeed] = []
    index += 1
    after_comma = False
    while True:
        index = _json_space(text, index)
        if index < len(text) and text[index] == "]":
            if after_comma:
                raise json.JSONDecodeError("trailing comma", text, index)
            return tracks, index + 1
        if len(tracks) >= MAX_TRACKS:
            raise GrooveSerpentError(f"Track list cannot contain more than {MAX_TRACKS} tracks.")
        item, index = decoder.raw_decode(text, index)
        track_number = len(tracks) + 1
        if isinstance(item, str):
            title = _bounded_field(item, "Track title")
            tracks.append(TrackSeed(title=title or f"Track {track_number:02d}"))
        elif isinstance(item, dict):
            tracks.append(_seed_from_mapping(item, track_number))
        else:
            raise GrooveSerpentError(f"Track {track_number} must be a string or object.")
        index = _json_space(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            after_comma = True
            continue
        if index < len(text) and text[index] == "]":
            return tracks, index + 1
        raise json.JSONDecodeError("expected ',' or ']'", text, index)


def _parse_json(text: str) -> Tracklist:
    decoder = json.JSONDecoder()
    index = _json_space(text, 0)
    metadata: dict[str, str] = {}
    try:
        if index < len(text) and text[index] == "[":
            tracks, index = _json_tracks(text, index, decoder)
        elif index < len(text) and text[index] == "{":
            tracks = []
            index += 1
            after_comma = False
            seen_keys: set[str] = set()
            while True:
                index = _json_space(text, index)
                if index < len(text) and text[index] == "}":
                    if after_comma:
                        raise json.JSONDecodeError("trailing comma", text, index)
                    index += 1
                    break
                key, index = decoder.raw_decode(text, index)
                if not isinstance(key, str):
                    raise json.JSONDecodeError("expected string key", text, index)
                if key in seen_keys:
                    raise GrooveSerpentError("Duplicate JSON track list key.")
                seen_keys.add(key)
                index = _json_space(text, index)
                if index >= len(text) or text[index] != ":":
                    raise json.JSONDecodeError("expected ':'", text, index)
                index = _json_space(text, index + 1)
                if key == "tracks":
                    tracks, index = _json_tracks(text, index, decoder)
                else:
                    value, index = decoder.raw_decode(text, index)
                    if key in _METADATA_KEYS and value not in (None, ""):
                        metadata[key] = _bounded_field(value, key, limit=MAX_METADATA_VALUE_LENGTH)
                index = _json_space(text, index)
                if index < len(text) and text[index] == ",":
                    index += 1
                    after_comma = True
                    continue
                if index < len(text) and text[index] == "}":
                    index += 1
                    break
                raise json.JSONDecodeError("expected ',' or '}'", text, index)
        else:
            raise GrooveSerpentError(
                "A JSON track list must be an array or an object with 'tracks'."
            )
        if _json_space(text, index) != len(text):
            raise json.JSONDecodeError("extra data", text, index)
    except json.JSONDecodeError as exc:
        raise GrooveSerpentError(f"Invalid JSON track list: {exc}") from exc
    if not tracks:
        raise GrooveSerpentError("The track list is empty.")
    return Tracklist(tracks=tracks, metadata=metadata)


def _parse_text(text: str) -> Tracklist:
    tracks: list[TrackSeed] = []
    metadata: dict[str, str] = {}
    for raw_line in StringIO(text):
        if len(raw_line.encode("utf-8")) > MAX_TRACKLIST_LINE_BYTES:
            raise GrooveSerpentError("Track list line exceeds its byte limit.")
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if ":" in line:
            key, value = line.split(":", 1)
            normalized_key = key.strip().lower().replace(" ", "_")
            if normalized_key in _METADATA_KEYS and value.strip():
                metadata[normalized_key] = _bounded_field(
                    value, normalized_key, limit=MAX_METADATA_VALUE_LENGTH
                )
                continue

        fields = [field.strip() for field in re.split(r"\t|\s*\|\s*", line)]
        fields = [field for field in fields if field]
        if not fields:
            continue

        duration: float | None = None
        if len(fields) > 1:
            try:
                duration = parse_duration(fields[-1])
                fields = fields[:-1]
            except GrooveSerpentError:
                duration = None

        if len(fields) > 1 and re.fullmatch(r"[A-Za-z]?\d+[.)-]?", fields[0]):
            fields = fields[1:]
        title = " | ".join(fields)
        title = _LEADING_NUMBER_RE.sub("", title).lstrip("| ").strip()
        title = _bounded_field(title, "Track title")
        if len(tracks) >= MAX_TRACKS:
            raise GrooveSerpentError(f"Track list cannot contain more than {MAX_TRACKS} tracks.")
        tracks.append(
            TrackSeed(
                title=title or f"Track {len(tracks) + 1:02d}",
                duration_seconds=duration,
            )
        )

    if not tracks:
        raise GrooveSerpentError("The track list is empty.")
    return Tracklist(tracks=tracks, metadata=metadata)


def load_tracklist(path: Path) -> Tracklist:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise GrooveSerpentError(f"Track list does not exist: {path}")
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise GrooveSerpentError("Track list must be a regular file.")
        if before.st_size > MAX_TRACKLIST_BYTES:
            raise GrooveSerpentError("Track list exceeds its byte limit.")
        raw = source.read(MAX_TRACKLIST_BYTES + 1)
        after = os.fstat(source.fileno())
    if len(raw) > MAX_TRACKLIST_BYTES:
        raise GrooveSerpentError("Track list exceeds its byte limit.")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise GrooveSerpentError("Track list changed while it was read.")
    try:
        text = raw.decode("utf-8" if path.suffix.lower() == ".json" else "utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GrooveSerpentError("Track list must be UTF-8 text.") from exc
    return _parse_json(text) if path.suffix.lower() == ".json" else _parse_text(text)
