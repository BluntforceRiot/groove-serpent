from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import GrooveSerpentError
from .validation import strict_finite_number

_DURATION_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d+))?$")
_LEADING_NUMBER_RE = re.compile(r"^\s*(?:[A-Za-z]?\d+[.)-]?|\d+)\s+")


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


def _seed_from_mapping(item: dict[str, Any], index: int) -> TrackSeed:
    title = str(item.get("title") or item.get("name") or f"Track {index:02d}").strip()
    return TrackSeed(
        title=title,
        duration_seconds=parse_duration(item.get("duration") or item.get("length")),
        artist=str(item.get("artist", "")).strip(),
        side=str(item.get("side", "")).strip(),
    )


def _parse_json(path: Path) -> Tracklist:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GrooveSerpentError(f"Invalid JSON track list: {exc}") from exc

    if isinstance(payload, list):
        track_items = payload
        metadata: dict[str, str] = {}
    elif isinstance(payload, dict):
        track_items = payload.get("tracks", [])
        if not isinstance(track_items, list):
            raise GrooveSerpentError(
                "The 'tracks' field in a JSON track list must be an array."
            )
        metadata = {
            key: str(payload[key]).strip()
            for key in ("artist", "album", "album_artist", "year", "genre", "side")
            if payload.get(key) not in (None, "")
        }
    else:
        raise GrooveSerpentError("A JSON track list must be an array or an object with 'tracks'.")

    tracks: list[TrackSeed] = []
    for index, item in enumerate(track_items, start=1):
        if isinstance(item, str):
            tracks.append(TrackSeed(title=item.strip() or f"Track {index:02d}"))
        elif isinstance(item, dict):
            tracks.append(_seed_from_mapping(item, index))
        else:
            raise GrooveSerpentError(f"Track {index} must be a string or object.")
    if not tracks:
        raise GrooveSerpentError("The track list is empty.")
    return Tracklist(tracks=tracks, metadata=metadata)


def _parse_text(path: Path) -> Tracklist:
    tracks: list[TrackSeed] = []
    metadata: dict[str, str] = {}
    metadata_keys = {"artist", "album", "album_artist", "year", "genre", "side"}

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if ":" in line:
            key, value = line.split(":", 1)
            normalized_key = key.strip().lower().replace(" ", "_")
            if normalized_key in metadata_keys and value.strip():
                metadata[normalized_key] = value.strip()
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
    return _parse_json(path) if path.suffix.lower() == ".json" else _parse_text(path)
